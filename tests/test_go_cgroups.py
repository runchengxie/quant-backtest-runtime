"""Opt-in delegated-kernel checks for whole-job queue memory isolation."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from test_go_runtime import Runtime
from test_rust_engine_jobs import _put, _request


def test_job_oom_does_not_stop_scheduler_or_next_job(tmp_path: Path) -> None:
    cli = os.environ.get("BACKTEST_GO_EXECUTABLE")
    engine = os.environ.get("BACKTEST_ENGINE_TEST_EXECUTABLE")
    delegated = os.environ.get("BACKTEST_CGROUP_ROOT")
    if not cli or not engine or not delegated:
        pytest.skip("requires a delegated cgroup and real Go/Rust executables")
    evidence = tmp_path / "child-cgroup.json"
    wrapper = tmp_path / "engine-wrapper"
    wrapper.write_text(
        "#!/usr/bin/python3\n"
        "import json,os,sys,time\nfrom pathlib import Path\n"
        f"engine={engine!r}\nevidence=Path({str(evidence)!r})\n"
        "if sys.argv[1]=='run' and json.loads(Path(sys.argv[2]).read_text())['initial_cash']=='10000.00':\n"
        " group=Path('/sys/fs/cgroup')/Path('/proc/self/cgroup').read_text().strip().split(':',2)[2].lstrip('/')\n"
        " evidence.write_text(json.dumps({'path':str(group),'max':(group/'memory.max').read_text().strip(),'oom_group':(group/'memory.oom.group').read_text().strip()}))\n"
        " for _ in range(4):\n"
        "  if os.fork()==0:\n"
        "   allocation=bytearray(80*1024*1024)\n"
        "   time.sleep(10)\n   os._exit(0)\n"
        " while True: os.wait()\n"
        "else: os.execv(engine,[engine,*sys.argv[1:]])\n"
    )
    wrapper.chmod(0o700)
    runtime = Runtime(Path(cli), tmp_path, wrapper, python=None)
    mapping = _request(runtime.artifacts, wrapper)
    mapping["budgets"]["memory_mb"] = 256
    first = runtime.call("submit", "--queue", str(runtime.write(mapping)))
    ref = mapping["inputs"]["engine_request_ref"].rsplit("/", 1)[1]
    payload = json.loads((runtime.artifacts / "sha256" / ref).read_text())
    payload["initial_cash"] = "20000.00"
    mapping["idempotency_key"] = "after-oom"
    mapping["inputs"]["engine_request_ref"] = _put(
        runtime.artifacts, json.dumps(payload).encode()
    )
    mapping["budgets"]["memory_mb"] = 512
    second = runtime.call("submit", "--queue", str(runtime.write(mapping, "next")))
    completed = subprocess.run(
        runtime.args(
            "serve", "--cgroup-root", delegated, "--concurrency", "1", "--once"
        ),
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    failed = runtime.call("inspect", first["job_id"])
    assert failed["status"] == "FAILED"
    assert failed["error_code"] == "JOB_MEMORY_EXCEEDED"
    assert not (runtime.results / first["job_id"]).exists()
    good = runtime.call("inspect", second["job_id"])
    assert good["status"] == "SUCCEEDED", good
    assert runtime.call("result", second["job_id"])["summary"]["equity"] == "20019.78"
    observed = json.loads(evidence.read_text())
    assert observed["max"] == str(256 * 1024 * 1024)
    assert observed["oom_group"] == "1"
    assert observed["path"].endswith(first["job_id"])
    for job, expected in ((first, 256), (second, 512)):
        receipt = json.loads(
            (runtime.results / ".logs" / f"{job['job_id']}.resources.json").read_text()
        )
        assert receipt["cgroup"]["memory_max"] == expected * 1024 * 1024
        assert receipt["cgroup"]["memory_peak"] > 0
        assert not receipt["cgroup"]["populated"]
    failed_receipt = json.loads(
        (runtime.results / ".logs" / f"{first['job_id']}.resources.json").read_text()
    )
    assert failed_receipt["cgroup"]["oom_kill"] > 0
    assert not Path(observed["path"]).exists()
    assert (
        hashlib.sha256(wrapper.read_bytes()).hexdigest()
        == mapping["config"]["executable_sha256"]
    )


@pytest.mark.parametrize("action", ["cancel", "restart"])
def test_group_cleanup_after_cancel_or_scheduler_restart(
    tmp_path: Path, action: str
) -> None:
    cli = os.environ.get("BACKTEST_GO_EXECUTABLE")
    engine = os.environ.get("BACKTEST_ENGINE_TEST_EXECUTABLE")
    delegated = os.environ.get("BACKTEST_CGROUP_ROOT")
    if not cli or not engine or not delegated:
        pytest.skip("requires a delegated cgroup and real Go/Rust executables")
    evidence = tmp_path / "group-path"
    wrapper = tmp_path / "slow-engine"
    wrapper.write_text(
        "#!/usr/bin/python3\n"
        "import os,sys,time\nfrom pathlib import Path\n"
        f"engine={engine!r}\n"
        "if sys.argv[1]=='run':\n"
        f" Path({str(evidence)!r}).write_text(Path('/proc/self/cgroup').read_text().strip().split(':',2)[2])\n"
        " time.sleep(2)\n"
        "os.execv(engine,[engine,*sys.argv[1:]])\n"
    )
    wrapper.chmod(0o700)
    runtime = Runtime(Path(cli), tmp_path, wrapper, python=None)
    mapping = _request(runtime.artifacts, wrapper)
    first = runtime.call("submit", "--queue", str(runtime.write(mapping)))
    command = runtime.args("serve", "--cgroup-root", delegated, "--once")
    scheduler = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        deadline = time.monotonic() + 15
        while not evidence.exists():
            assert time.monotonic() < deadline
            time.sleep(0.02)
        group = Path("/sys/fs/cgroup") / evidence.read_text().lstrip("/")
        assert group.name == first["job_id"]
        worker = runtime.call("inspect", first["job_id"])["worker_pid"]
        assert (
            Path(f"/proc/{worker}/cgroup").read_text().strip().endswith(first["job_id"])
        )
        if action == "cancel":
            runtime.call("cancel", first["job_id"])
            out, err = scheduler.communicate(timeout=20)
            assert scheduler.returncode == 0, (out, err)
            expected = "CANCELLED"
        else:
            scheduler.kill()
            scheduler.communicate(timeout=5)
            resumed = subprocess.run(
                command, capture_output=True, timeout=20, check=False
            )
            assert resumed.returncode == 0, resumed.stderr
            expected = "SUCCEEDED"
        assert runtime.call("inspect", first["job_id"])["status"] == expected
        assert not group.exists()
        receipt = runtime.results / ".logs" / f"{first['job_id']}.resources.json"
        assert json.loads(receipt.read_text())["cgroup"]["populated"] is False
    finally:
        if scheduler.poll() is None:
            scheduler.terminate()
            scheduler.communicate(timeout=20)
