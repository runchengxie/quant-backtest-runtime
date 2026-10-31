"""Agent-facing contracts must expose effects and support safe recovery."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

import pytest
from test_execution_aware import _request_v2

from backtest_runtime import cli
from backtest_runtime.contracts import BacktestJobRequest
from backtest_runtime.jobs import BacktestJobService
from backtest_runtime.store import JobStore


def args(root):
    return [
        "--database",
        str(root / "db" / "jobs.sqlite"),
        "--artifact-root",
        str(root / "artifacts"),
        "--result-root",
        str(root / "results"),
    ]


def request_file(root):
    path = root / "request.json"
    path.write_text(json.dumps(_request_v2(root / "artifacts")))
    return path


def call(capsys, argv):
    assert cli.main(argv) == 0
    return json.loads(capsys.readouterr().out)


def test_discovery_without_storage_paths(tmp_path, capsys):
    result = call(capsys, ["capabilities"])
    assert result["schema"] == "quant.agent_capabilities.v1"
    operations = {row["id"]: row for row in result["operations"]}
    assert operations["inspect"]["side_effects"] == []
    assert "recovery" in operations["status"]["side_effects"]
    assert result["request_versions"] == [1, 2, 3, 4, 5, 6]
    assert not list(tmp_path.iterdir())


def test_plan_does_not_create_runtime_storage_and_binds_content(tmp_path, capsys):
    manifest = request_file(tmp_path)
    plan = call(capsys, [*args(tmp_path), "plan", str(manifest)])
    assert plan["schema"] == "quant.backtest_plan.v1"
    assert (
        plan["request_sha256"]
        == BacktestJobRequest.from_mapping(
            json.loads(manifest.read_text())
        ).request_sha256
    )
    assert plan["budgets"]["wall_seconds"] > 0
    assert plan["checks"]["input_hashes"] == "passed"
    assert plan["checks"]["historical_pit"] == "not_checked"
    assert plan["checks"]["parquet_rows"] == "not_checked"
    assert not (tmp_path / "db").exists()
    assert not (tmp_path / "results").exists()
    assert (
        call(capsys, [*args(tmp_path), "plan", str(manifest)])["plan_sha256"]
        == plan["plan_sha256"]
    )


def test_bound_submission_rejects_plan_change_before_storage(tmp_path, capsys):
    manifest = request_file(tmp_path)
    plan = call(capsys, [*args(tmp_path), "plan", str(manifest)])
    payload = json.loads(manifest.read_text())
    payload["budgets"]["wall_seconds"] += 1
    manifest.write_text(json.dumps(payload))
    assert (
        cli.main(
            [
                "--error-format",
                "json",
                *args(tmp_path),
                "submit",
                str(manifest),
                "--expected-plan-sha256",
                plan["plan_sha256"],
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().err)["code"] == "PLAN_MISMATCH"
    assert not (tmp_path / "db").exists()


def test_plan_detects_modified_artifact(tmp_path, capsys):
    manifest = request_file(tmp_path)
    ref = json.loads(manifest.read_text())["inputs"]["positions_ref"]
    (tmp_path / "artifacts" / "sha256" / ref.rsplit("/", 1)[-1]).write_bytes(b"corrupt")
    assert (
        cli.main(["--error-format", "json", *args(tmp_path), "plan", str(manifest)])
        == 1
    )
    assert json.loads(capsys.readouterr().err)["code"] == "INTEGRITY_FAILURE"
    assert not (tmp_path / "db").exists()


def test_pure_reads_leave_expired_job_and_output_untouched(tmp_path, capsys):
    request = BacktestJobRequest.from_mapping(_request_v2(tmp_path / "artifacts"))
    store = JobStore(tmp_path / "db" / "jobs.sqlite")
    service = BacktestJobService(
        store, artifact_root=tmp_path / "artifacts", result_root=tmp_path / "results"
    )
    receipt = service.submit(request)
    store.set_backtest_job_logs(
        receipt.job_id, stdout_path="unused", stderr_path="unused"
    )
    store._connection.execute("UPDATE backtest_jobs SET lease_expires_at=0")
    store._connection.commit()
    original = asdict(service.get_status(receipt.job_id))
    store.close()
    output = tmp_path / "results" / receipt.job_id
    output.mkdir(parents=True)
    (output / "sentinel").write_text("retain")
    assert call(capsys, [*args(tmp_path), "inspect", receipt.job_id]) == original
    found = call(
        capsys,
        [*args(tmp_path), "lookup", "--idempotency-key", request.idempotency_key],
    )
    assert found["job_id"] == receipt.job_id
    assert (output / "sentinel").read_text() == "retain"
    assert call(capsys, [*args(tmp_path), "recover"]) == {"recovered": 1}


def test_read_only_store_reads_live_wal_without_migration(tmp_path, capsys):
    path = tmp_path / "db" / "jobs.sqlite"
    store = JobStore(path)
    store._connection.execute("PRAGMA journal_mode=WAL")
    request = BacktestJobRequest.from_mapping(_request_v2(tmp_path / "artifacts"))
    store.insert_backtest_job("test-job", request)
    try:
        assert (
            call(capsys, [*args(tmp_path), "inspect", "test-job"])["status"]
            == "SUBMITTED"
        )
        with JobStore(path, read_only=True)._connection as connection:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                connection.execute("UPDATE backtest_jobs SET status='FAILED'")
    finally:
        store.close()


@pytest.mark.parametrize(
    "command", [["inspect", "absent"], ["lookup", "--idempotency-key", "absent"]]
)
def test_missing_database_is_not_created(tmp_path, capsys, command):
    assert cli.main(["--error-format", "json", *args(tmp_path), *command]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "DATABASE_UNAVAILABLE"
    assert not (tmp_path / "db").exists()


def test_json_parser_failure(capsys):
    assert cli.main(["--error-format", "json", "not-a-command"]) == 2
    assert json.loads(capsys.readouterr().err)["code"] == "INVALID_ARGUMENTS"


def test_bound_lifecycle_recovers_lost_receipt_and_checks_result(
    tmp_path, capsys, monkeypatch
):
    manifest = request_file(tmp_path)
    plan = call(capsys, [*args(tmp_path), "plan", str(manifest)])
    processes = []
    launch = cli.subprocess.Popen

    def start(*a, **kw):
        process = launch(*a, **kw)
        processes.append(process)
        return process

    monkeypatch.setattr(cli.subprocess, "Popen", start)
    call(
        capsys,
        [
            *args(tmp_path),
            "submit",
            str(manifest),
            "--expected-plan-sha256",
            plan["plan_sha256"],
        ],
    )
    key = json.loads(manifest.read_text())["idempotency_key"]
    recovered = call(capsys, [*args(tmp_path), "lookup", "--idempotency-key", key])
    assert processes[0].wait(timeout=45) == 0
    replay = call(
        capsys,
        [
            *args(tmp_path),
            "submit",
            str(manifest),
            "--expected-plan-sha256",
            plan["plan_sha256"],
        ],
    )
    assert replay["job_id"] == recovered["job_id"]
    assert replay["created"] is False
    assert len(processes) == 1
    assert (
        call(capsys, [*args(tmp_path), "result", recovered["job_id"]])["schema_version"]
        == "quant.backtest_job_result.v2"
    )
    payload = json.loads(manifest.read_text())
    payload["budgets"]["wall_seconds"] += 1
    manifest.write_text(json.dumps(payload))
    assert (
        cli.main(["--error-format", "json", *args(tmp_path), "submit", str(manifest)])
        == 1
    )
    assert json.loads(capsys.readouterr().err)["code"] == "IDEMPOTENCY_CONFLICT"


def test_missing_job_differs_from_missing_database(tmp_path, capsys):
    JobStore(tmp_path / "db" / "jobs.sqlite").close()
    assert (
        cli.main(["--error-format", "json", *args(tmp_path), "inspect", "absent"]) == 1
    )
    assert json.loads(capsys.readouterr().err)["code"] == "JOB_NOT_FOUND"


def test_plan_digest_is_independent_of_storage_paths(tmp_path, capsys):
    import shutil

    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    manifest = request_file(left)
    first = call(capsys, [*args(left), "plan", str(manifest)])
    shutil.copytree(left, right)
    second = call(capsys, [*args(right), "plan", str(right / "request.json")])
    assert first["plan_sha256"] == second["plan_sha256"]


def test_bound_submit_uses_the_request_that_was_planned(tmp_path, capsys, monkeypatch):
    manifest = request_file(tmp_path)
    original_payload = json.loads(manifest.read_text())
    original_sha = BacktestJobRequest.from_mapping(original_payload).request_sha256
    plan = call(capsys, [*args(tmp_path), "plan", str(manifest)])
    checked = cli.checked_plan

    def replace_after_validation(*a, **kw):
        result = checked(*a, **kw)
        changed = json.loads(manifest.read_text())
        changed["budgets"]["wall_seconds"] += 1
        manifest.write_text(json.dumps(changed))
        return result

    monkeypatch.setattr(cli, "checked_plan", replace_after_validation)
    monkeypatch.setattr(cli, "_start_worker", lambda *a: None)
    call(
        capsys,
        [
            *args(tmp_path),
            "submit",
            str(manifest),
            "--expected-plan-sha256",
            plan["plan_sha256"],
        ],
    )
    store = JobStore(tmp_path / "db" / "jobs.sqlite", read_only=True)
    try:
        assert (
            store.get_backtest_job_by_key(original_payload["idempotency_key"])[
                "request_sha256"
            ]
            == original_sha
        )
    finally:
        store.close()


def test_nonobject_request_is_invalid_request(tmp_path, capsys):
    manifest = tmp_path / "request.json"
    manifest.write_text("[]")
    assert (
        cli.main(["--error-format", "json", *args(tmp_path), "plan", str(manifest)])
        == 1
    )
    assert json.loads(capsys.readouterr().err)["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize(
    "version,request_sha256,plan_sha256",
    [
        (
            1,
            "dffcbd5a73f2aa4fa7b764990a56c18d9ee7f25aca481ffcd7b4dd8525065bec",
            "1ae968d8705a46ccd9b3bb6cb926c3542b0acaed229a8d6e6dc9adbcbec9f886",
        ),
        (
            2,
            "f3d4c6fb938045fe9b0822741560ee4f16c96b7e2ecd2ebc80bdcbfb77992a7a",
            "eca475b74bc35578f2ce37e8f866cf9bd191106657b8dfd1dbd236c858b18b5f",
        ),
        (
            3,
            "04cf530016ec3182bedbdb1eca28286c11b1af26f9aa3a15a6a6c463815d7bb6",
            "2d5e7185266790ae7dc6fec34295c640e2e8d09f0c06e049f03036f885377f06",
        ),
        (
            4,
            "dd01ebd5afcfaaf287454011032c304001c439d0b5a1883da08102786f9c9b28",
            "b0af0382d11f0b7736035448c0a1c8f43921e92e8c94fe1d4f00f5ada95e54e9",
        ),
    ],
)
def test_legacy_plan_and_request_golden_identities(
    tmp_path, version, request_sha256, plan_sha256
):
    # Captured from d2f20b4 before v5 validation was added, using locked synthetic
    # fixtures. A new v5 receipt must never change an earlier protocol's identity.
    from test_sequenced_jobs import _request_v3
    from test_trade_accounting_jobs import _request_v4

    from backtest_runtime.agent_access import checked_plan

    factory = {1: _request_v2, 2: _request_v2, 3: _request_v3, 4: _request_v4}[version]
    mapping = factory(tmp_path)
    if version == 1:
        mapping["schema_version"] = 1
        mapping.pop("producer")
        mapping.pop("research_clock")
        mapping["evidence_tier"] = "diagnostic"
    request = BacktestJobRequest.from_mapping(mapping)
    assert request.request_sha256 == request_sha256
    assert checked_plan(request, tmp_path, None)["plan_sha256"] == plan_sha256
