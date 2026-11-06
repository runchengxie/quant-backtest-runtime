"""Held executable and process-group ownership survive exits and host death."""

import hashlib
import json
import os
from pathlib import Path

import pytest
from test_session_stream import config_sessions, fake_engine, run, source


def _active_process(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


@pytest.mark.parametrize("state, expected", [("R", True), ("Z", False)])
def test_process_poll_classifies_kernel_state(monkeypatch, state, expected):
    monkeypatch.setattr(Path, "read_text", lambda self: f"123 (engine) {state} 1 2")
    assert _active_process(123) is expected


@pytest.mark.parametrize("error", [FileNotFoundError(), ProcessLookupError()])
def test_process_poll_treats_disappearing_process_as_inactive(monkeypatch, error):
    def disappeared(self):
        raise error

    monkeypatch.setattr(Path, "read_text", disappeared)
    assert _active_process(123) is False


@pytest.mark.parametrize("error", [PermissionError(), OSError("unexpected I/O error")])
def test_process_poll_does_not_hide_unexpected_errors(monkeypatch, error):
    def failed(self):
        raise error

    monkeypatch.setattr(Path, "read_text", failed)
    with pytest.raises(type(error)) as caught:
        _active_process(123)
    assert caught.value is error


def test_parent_sigkill_kills_engine_and_resume_replays_all_prefix(tmp_path):
    import signal
    import subprocess
    import sys
    import time

    binary, digest = fake_engine(tmp_path)
    marker = tmp_path / "acknowledged"
    pidfile = tmp_path / "engine.pid"
    driver = tmp_path / "driver.py"
    driver.write_text("""import sys,time,json
from pathlib import Path
from test_session_stream import config_sessions,source
from backtest_runtime.session_stream import run_partition_sessions
from backtest_runtime.session_stream_io import PipeProcess
from backtest_runtime.session_stream_journal import ReplayJournal
root=Path(sys.argv[1])
launch=PipeProcess._launch
def launched(self,*args):
    launch(self,*args)
    (root/"engine.pid").write_text(str(self.child.pid))
PipeProcess._launch=launched
ack=ReplayJournal.acknowledge
def acknowledged(self,*args,**kwargs):
    ack(self,*args,**kwargs)
    (root/"acknowledged").write_text("1")
    time.sleep(30)
ReplayJournal.acknowledge=acknowledged
config,sessions=config_sessions()
run_partition_sessions(source(root,sessions),config,executable=root/"engine",executable_sha256=sys.argv[2],output_root=root/"output")
""")
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(Path(__file__).parent), str(Path(__file__).parents[1] / "src")]
        ),
    }
    child = subprocess.Popen(
        [sys.executable, str(driver), str(tmp_path), digest], env=env
    )
    try:
        deadline = time.monotonic() + 10
        while (
            not marker.exists() and child.poll() is None and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert marker.exists()
        engine_pid = int(pidfile.read_text())
        assert Path(f"/proc/{engine_pid}").exists()
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=5)
        deadline = time.monotonic() + 5

        while _active_process(engine_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _active_process(engine_pid)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
    journal = json.loads((tmp_path / "output/journal.json").read_bytes())
    assert len(journal["acknowledged"]) == 1
    from backtest_runtime.session_stream import run_partition_sessions

    config, sessions = config_sessions()
    inputs = source(tmp_path, sessions)
    seen = []
    original = inputs.iter_rows

    def tracked(*, cursor=None):
        assert cursor is None
        for row, next_cursor in original():
            seen.append(row["event_id"])
            yield row, next_cursor

    inputs.iter_rows = tracked
    receipt = run_partition_sessions(
        inputs,
        config,
        executable=binary,
        executable_sha256=digest,
        output_root=tmp_path / "output",
        resume=True,
    )
    assert seen == [1, 2]
    assert receipt["session_count"] == 2
    assert len(list((tmp_path / "output").glob("report-*.json"))) == 2


def test_held_binary_survives_atomic_path_replacement(tmp_path, monkeypatch):
    import backtest_runtime.session_stream_io as transport
    from backtest_runtime.session_stream import run_partition_sessions

    binary, digest = fake_engine(tmp_path)
    replacement = tmp_path / "replacement"
    replacement.write_text(
        "#!/usr/bin/python3\nraise RuntimeError('path was reopened')\n"
    )
    replacement.chmod(0o700)
    original = transport.subprocess.Popen

    def replace_path(*args, **kwargs):
        os.replace(replacement, binary)
        return original(*args, **kwargs)

    monkeypatch.setattr(transport.subprocess, "Popen", replace_path)
    config, sessions = config_sessions()
    receipt = run_partition_sessions(
        source(tmp_path, sessions),
        config,
        executable=binary,
        executable_sha256=digest,
        output_root=tmp_path / "output",
    )
    assert receipt["session_count"] == 2


def test_finished_client_does_not_signal_reaped_process_group_on_later_close(
    tmp_path, monkeypatch
):
    import backtest_runtime.session_stream_io as transport
    from backtest_runtime.session_stream import SessionStreamClient

    config, sessions = config_sessions()
    binary, digest = fake_engine(tmp_path)
    original = transport.os.killpg
    signals = []

    def tracked(group, sig):
        signals.append(group)
        original(group, sig)

    monkeypatch.setattr(transport.os, "killpg", tracked)
    with SessionStreamClient(binary, digest) as client:
        client.start(config)
        client.session(sessions[0])
        client.finish()
        assert client.process.child.returncode == 0
        before_close = signals.copy()
    assert signals == before_close


def test_helper_imports_from_explicit_package_root_not_host_cwd(tmp_path, monkeypatch):
    spoof = tmp_path / "backtest_runtime"
    spoof.mkdir()
    (spoof / "__init__.py").write_text("")
    (spoof / "session_stream_process.py").write_text(
        "raise RuntimeError('host cwd module shadowed helper')\n"
    )
    monkeypatch.chdir(tmp_path)
    assert run(tmp_path)["session_count"] == 2


def test_finish_kills_descendants_before_reaping_the_group_leader(tmp_path):
    import time

    from backtest_runtime.session_stream import SessionStreamClient

    binary, _ = fake_engine(tmp_path)
    pidfile = tmp_path / "descendant.pid"
    binary.write_text(
        binary.read_text().replace(
            "ledger=[]; seq=0",
            """forked = os.fork()
if forked == 0:
    os.close(0);os.close(1);os.close(2)
    time.sleep(30)
    os._exit(0)
open("""
            + repr(str(pidfile))
            + ""","w").write(str(forked))
ledger=[]; seq=0""",
        )
    )
    config, sessions = config_sessions()
    with SessionStreamClient(
        binary, hashlib.sha256(binary.read_bytes()).hexdigest()
    ) as client:
        client.start(config)
        client.session(sessions[0])
        client.finish()
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 5

        while _active_process(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _active_process(pid)
