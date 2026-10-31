"""Static preflight must bind bytes, bound child I/O, and leave storage untouched."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest
from test_rust_engine_jobs import _request

from backtest_runtime import cli
from backtest_runtime.agent_access import AccessError, checked_plan
from backtest_runtime.contracts import BacktestJobRequest


def validator(root: Path, body: str = "") -> Path:
    binary = root / "validator"
    binary.write_text(
        f"#!{sys.executable}\n"
        + """import sys,json
if sys.argv[1:] == ['capabilities']:
    print(json.dumps({'schema_version':'engine_capabilities.v1','request_schema':'engine_request.v1','validator_version':1}))
    sys.exit(0)
"""
        + (
            body
            or """import hashlib,json,sys
assert sys.argv[1:] == ['validate', '-']
assert set(__import__('os').environ) <= {'PATH', 'LANG', 'LC_ALL', 'TZ'}
data = sys.stdin.buffer.read()
print(json.dumps({'schema_version':'engine_validation.v1','request_schema':'engine_request.v1','validator_version':1,'valid':True,'input_sha256':hashlib.sha256(data).hexdigest()}))
"""
        )
    )
    binary.chmod(0o755)
    return binary


def planned(root: Path, binary: Path):
    mapping = _request(root / "artifacts", binary)
    return BacktestJobRequest.from_mapping(mapping)


def test_v5_plan_binds_deterministic_receipt_without_storage(tmp_path):
    binary = validator(tmp_path)
    request = planned(tmp_path, binary)
    plan = checked_plan(request, tmp_path / "artifacts", binary)
    assert plan["checks"]["engine_request_semantics"] == "passed"
    assert plan["engine_validation"] == {
        "schema_version": "engine_validation.v1",
        "request_schema": "engine_request.v1",
        "validator_version": 1,
        "valid": True,
        "input_sha256": request.inputs["engine_request_ref"].rsplit("/", 1)[-1],
    }
    assert checked_plan(request, tmp_path / "artifacts", binary) == plan
    assert sorted(p.name for p in tmp_path.iterdir()) == ["artifacts", "validator"]


@pytest.mark.parametrize(
    "body,code",
    [
        ("import sys; sys.exit(2)", "ENGINE_REQUEST_INVALID"),
        ("import sys; sys.exit(1)", "ENGINE_VALIDATION_UNAVAILABLE"),
        ('print("{}")', "ENGINE_VALIDATION_UNAVAILABLE"),
        ('print("not json")', "ENGINE_VALIDATION_UNAVAILABLE"),
        ('import sys; print("bad"); sys.exit(2)', "ENGINE_VALIDATION_UNAVAILABLE"),
    ],
)
def test_rejections_and_bad_receipts_are_distinct(tmp_path, body, code):
    binary = validator(tmp_path, body)
    with pytest.raises(AccessError) as failure:
        checked_plan(planned(tmp_path, binary), tmp_path / "artifacts", binary)
    assert failure.value.code == code


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", "engine_validation.v9"),
        ("request_schema", "engine_request.v2"),
        ("validator_version", 2),
        ("validator_version", True),
        ("valid", 1),
        ("valid", False),
        ("input_sha256", "f" * 64),
        ("extra", 1),
    ],
)
def test_receipt_schema_is_strict(tmp_path, field, value):
    body = """import hashlib,json,sys
r={'schema_version':'engine_validation.v1','request_schema':'engine_request.v1','validator_version':1,'valid':True,'input_sha256':hashlib.sha256(sys.stdin.buffer.read()).hexdigest()}
"""
    body += f"r[{field!r}]={value!r}\nprint(json.dumps(r))\n"
    binary = validator(tmp_path, body)
    with pytest.raises(AccessError) as failure:
        checked_plan(planned(tmp_path, binary), tmp_path / "artifacts", binary)
    assert failure.value.code == "ENGINE_VALIDATION_UNAVAILABLE"


def test_bound_submit_rejects_before_database_creation(tmp_path, capsys):
    binary = validator(tmp_path, "import sys; sys.exit(2)")
    request = planned(tmp_path, binary)
    manifest = tmp_path / "job.json"
    manifest.write_text(json.dumps(request.to_mapping()))
    assert (
        cli.main(
            [
                "--error-format",
                "json",
                "--database",
                str(tmp_path / "db/jobs.sqlite"),
                "--artifact-root",
                str(tmp_path / "artifacts"),
                "--result-root",
                str(tmp_path / "results"),
                "--engine-executable",
                str(binary),
                "submit",
                str(manifest),
                "--expected-plan-sha256",
                "0" * 64,
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().err)["code"] == "ENGINE_REQUEST_INVALID"
    assert not (tmp_path / "db").exists()
    assert not (tmp_path / "results").exists()


@pytest.mark.parametrize(
    "body",
    [
        "import time; time.sleep(30)",
        "import os,time; os.close(0); os.close(1); os.close(2); time.sleep(30)",
        'import os;\nwhile True: os.write(1,b"x"*4096)',
        'import os;\nwhile True: os.write(2,b"x"*4096)',
    ],
)
def test_deadline_and_stream_caps_kill_and_reap(tmp_path, monkeypatch, body):
    from backtest_runtime import engine_validation

    binary = validator(tmp_path, body)
    request = planned(tmp_path, binary)
    monkeypatch.setattr(engine_validation, "TIMEOUT_SECONDS", 0.15)
    children = []
    launch = engine_validation.subprocess.Popen

    def capture(*args, **kwargs):
        child = launch(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(engine_validation.subprocess, "Popen", capture)
    start = time.monotonic()
    with pytest.raises(AccessError) as failure:
        checked_plan(request, tmp_path / "artifacts", binary)
    assert failure.value.code == "ENGINE_VALIDATION_UNAVAILABLE"
    assert time.monotonic() - start < 2
    assert children[-1].returncode == -signal.SIGKILL
    with pytest.raises(ChildProcessError):
        os.waitpid(children[-1].pid, os.WNOHANG)


def test_input_and_executable_path_replacement_cannot_change_validation(
    tmp_path, monkeypatch
):
    from backtest_runtime import engine_validation

    binary = validator(tmp_path)
    request = planned(tmp_path, binary)
    digest = request.inputs["engine_request_ref"].rsplit("/", 1)[-1]
    input_path = tmp_path / "artifacts/sha256" / digest
    original = engine_validation._invoke

    def replace(binary_fd, data, **kwargs):
        replacement = tmp_path / "replacement"
        replacement.write_text("#!/bin/sh\nexit 2\n")
        replacement.chmod(0o755)
        replacement.replace(binary)
        other = tmp_path / "other-input"
        other.write_bytes(b"{}")
        other.replace(input_path)
        return original(binary_fd, data, **kwargs)

    monkeypatch.setattr(engine_validation, "_invoke", replace)
    plan = checked_plan(request, tmp_path / "artifacts", binary)
    assert plan["engine_validation"]["input_sha256"] == digest
    assert input_path.read_bytes() == b"{}"


def test_changed_input_is_rejected_before_child_launch(tmp_path):
    binary = validator(tmp_path)
    request = planned(tmp_path, binary)
    digest = request.inputs["engine_request_ref"].rsplit("/", 1)[-1]
    (tmp_path / "artifacts/sha256" / digest).write_bytes(b"{}")
    with pytest.raises(AccessError) as failure:
        checked_plan(request, tmp_path / "artifacts", binary)
    assert failure.value.code == "INTEGRITY_FAILURE"


def test_oversize_input_rejected_with_bounded_read(tmp_path):
    from test_rust_engine_jobs import _put

    binary = validator(tmp_path)
    mapping = _request(tmp_path / "artifacts", binary)
    mapping["inputs"]["engine_request_ref"] = _put(
        tmp_path / "artifacts", b" " * (1024 * 1024 + 1)
    )
    with pytest.raises(AccessError) as failure:
        checked_plan(
            BacktestJobRequest.from_mapping(mapping), tmp_path / "artifacts", binary
        )
    assert failure.value.code == "ENGINE_REQUEST_INVALID"


def test_exact_input_limit_and_stream_limit_are_accepted(tmp_path):
    from test_rust_engine_jobs import _put

    body = """import hashlib,json,sys
r={'schema_version':'engine_validation.v1','request_schema':'engine_request.v1','validator_version':1,'valid':True,'input_sha256':hashlib.sha256(sys.stdin.buffer.read()).hexdigest()}
s=json.dumps(r)
sys.stdout.write(s + ' ' * (8192-len(s)))
sys.stderr.write('x' * 8192)
"""
    binary = validator(tmp_path, body)
    mapping = _request(tmp_path / "artifacts", binary)
    mapping["inputs"]["engine_request_ref"] = _put(
        tmp_path / "artifacts", b" " * (1024 * 1024)
    )
    plan = checked_plan(
        BacktestJobRequest.from_mapping(mapping), tmp_path / "artifacts", binary
    )
    assert plan["checks"]["engine_request_semantics"] == "passed"


def test_receipt_duplicate_fields_are_rejected(tmp_path):
    binary = validator(tmp_path, 'print(\'{"valid":true,"valid":false}\')')
    with pytest.raises(AccessError) as failure:
        checked_plan(planned(tmp_path, binary), tmp_path / "artifacts", binary)
    assert failure.value.code == "ENGINE_VALIDATION_UNAVAILABLE"


@pytest.mark.parametrize(
    "mutation", [None, "capability", "duplicate", "chronology", "crossed", "decimal"]
)
def test_real_engine_static_validation(tmp_path, mutation):
    from test_rust_engine_jobs import _put

    binary = os.environ.get("BACKTEST_ENGINE_TEST_EXECUTABLE")
    if binary is None:
        pytest.skip("real Rust validator runs with the pinned integration binary")
    binary = Path(binary)
    mapping = _request(tmp_path / "artifacts", binary)
    digest = mapping["inputs"]["engine_request_ref"].rsplit("/", 1)[-1]
    payload = json.loads((tmp_path / "artifacts/sha256" / digest).read_bytes())
    if mutation == "capability":
        payload["required_capabilities"] = ["unknown"]
    elif mutation == "duplicate":
        payload["orders"][1]["id"] = payload["orders"][0]["id"]
    elif mutation == "chronology":
        payload["quotes"][1]["ts_ns"] = 1
    elif mutation == "crossed":
        payload["quotes"][0]["bid"] = "11.00"
    elif mutation == "decimal":
        payload["initial_cash"] = "999999999999999999999999999999999999"
    mapping["inputs"]["engine_request_ref"] = _put(
        tmp_path / "artifacts", json.dumps(payload).encode()
    )
    request = BacktestJobRequest.from_mapping(mapping)
    if mutation is None:
        assert (
            checked_plan(request, tmp_path / "artifacts", binary)["checks"][
                "engine_request_semantics"
            ]
            == "passed"
        )
    else:
        with pytest.raises(AccessError) as failure:
            checked_plan(request, tmp_path / "artifacts", binary)
        assert failure.value.code == "ENGINE_REQUEST_INVALID"
    assert not (tmp_path / "jobs.sqlite").exists()


def test_unadvertised_validator_exiting_two_is_unavailable(tmp_path):
    binary = validator(tmp_path, "import sys; sys.exit(2)")
    # An unrelated executable has no validator advertisement and must not be
    # mistaken for an engine rejecting request semantics.
    binary.write_text(f"#!{sys.executable}\nimport sys; sys.exit(2)\n")
    with pytest.raises(AccessError) as failure:
        checked_plan(planned(tmp_path, binary), tmp_path / "artifacts", binary)
    assert failure.value.code == "ENGINE_VALIDATION_UNAVAILABLE"


@pytest.mark.parametrize("version", [True, 0, 2, "1", None])
def test_unsupported_capability_version_cannot_validate(tmp_path, version):
    binary = validator(tmp_path)
    source = binary.read_text()
    header = f"""import sys,json
if sys.argv[1:] == ['capabilities']:
    print(json.dumps({{'schema_version':'engine_capabilities.v1','request_schema':'engine_request.v1','validator_version':{version!r}}}))
    sys.exit(0)
"""
    binary.write_text(
        source.split("\n", 1)[0] + "\n" + header + source.split("\n", 1)[1]
    )
    with pytest.raises(AccessError) as failure:
        checked_plan(planned(tmp_path, binary), tmp_path / "artifacts", binary)
    assert failure.value.code == "ENGINE_VALIDATION_UNAVAILABLE"


def test_capabilities_and_validation_share_one_deadline(tmp_path, monkeypatch):
    from backtest_runtime import engine_validation

    binary = validator(tmp_path)
    source = binary.read_text()
    # Each operation individually fits, but their combined cost exceeds 0.3s.
    source = (
        source.split("\n", 1)[0]
        + "\nimport time; time.sleep(0.2)\n"
        + source.split("\n", 1)[1]
    )
    binary.write_text(source)
    monkeypatch.setattr(engine_validation, "TIMEOUT_SECONDS", 0.3)
    with pytest.raises(AccessError) as failure:
        checked_plan(planned(tmp_path, binary), tmp_path / "artifacts", binary)
    assert failure.value.code == "ENGINE_VALIDATION_UNAVAILABLE"
