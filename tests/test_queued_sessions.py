"""Queued session adapter computes and verifies only private Go stages."""

import json
from pathlib import Path

import pytest
from test_session_stream import config_sessions, fake_engine, source


def test_stage_and_read_only_verification(tmp_path):
    from backtest_runtime.queued_sessions import stage, verify

    config, sessions = config_sessions()
    reader = source(tmp_path, sessions)
    binary, sha = fake_engine(tmp_path)
    output = tmp_path / "stage"
    output.mkdir()
    receipt = stage(
        reader,
        config,
        executable=binary,
        executable_sha256=sha,
        output_root=output,
        wall_seconds=10,
    )
    assert verify(reader, config, output_root=output) == receipt
    result = Path(receipt["result_path"])
    payload = json.loads(result.read_text())
    payload["final_cash"] = "1"
    result.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        verify(reader, config, output_root=output)


@pytest.mark.parametrize(
    "mutation",
    ["cursor", "metadata", "prefix", "count", "report", "result", "traversal"],
)
def test_resealed_journal_and_report_changes_reject(tmp_path, mutation):
    from backtest_runtime.queued_sessions import stage, verify
    from backtest_runtime.session_stream_io import canonical
    from backtest_runtime.session_stream_journal import digest

    config, sessions = config_sessions()
    reader = source(tmp_path, sessions)
    binary, sha = fake_engine(tmp_path)
    output = tmp_path / "stage"
    output.mkdir()
    stage(
        reader,
        config,
        executable=binary,
        executable_sha256=sha,
        output_root=output,
        wall_seconds=10,
    )
    path = output / "stream/journal.json"
    journal = json.loads(path.read_text())
    entry = journal["acknowledged"][0]
    if mutation == "cursor":
        entry["next_cursor"]["row"] = 999
    elif mutation == "metadata":
        entry["row_metadata"]["event_id"] = 999
    elif mutation == "prefix":
        entry["prefix_sha256"] = "0" * 64
    elif mutation == "count":
        journal["acknowledged"].pop()
    elif mutation == "traversal":
        entry["publication_id"] = "../outside.json"
    else:
        target = entry if mutation == "report" else journal["result"]
        field = "report_sha256" if mutation == "report" else "sha256"
        publication = output / "stream" / target["publication_id"]
        value = json.loads(publication.read_text())
        if mutation == "report":
            value["balance"]["cash"] = "1"
        else:
            value["final_cash"] = "1"
        data = canonical(value) + b"\n"
        publication.write_bytes(data)
        target[field] = digest(data)
    journal.pop("integrity_sha256")
    journal["integrity_sha256"] = digest(canonical(journal))
    path.write_bytes(canonical(journal) + b"\n")
    with pytest.raises(ValueError):
        verify(reader, config, output_root=output)


def test_staged_output_quota_precedes_publication(tmp_path):
    from backtest_runtime.queued_sessions import StagedJournal
    from backtest_runtime.session_stream_io import StreamLimits

    journal = StagedJournal(tmp_path / "journal", {}, StreamLimits(), False)
    try:
        journal.publication_bytes = 248 << 20
        with pytest.raises(ValueError, match="256 MiB"):
            journal._reserve({"synthetic": True})
        assert not list((tmp_path / "journal").glob("report-*"))
    finally:
        journal.close()


def test_stateless_adapter_cli_plan_stage_and_verify(tmp_path):
    import subprocess
    import sys

    from test_rust_engine_jobs import _put

    config, sessions = config_sessions()
    reader = source(tmp_path, sessions)
    binary, sha = fake_engine(tmp_path)
    artifacts = tmp_path / "artifacts"
    manifest = {
        "schema_version": "partition_manifest.v1",
        "row_schema": "event_envelope.v1",
        "order": "event_time_ns,event_id",
        "duplicate_policy": "reject_event_id",
        "global_order": "strict",
        "partitions": [p.to_dict() for p in reader.manifest.partitions],
    }
    request = {
        "schema_version": 7,
        "idempotency_key": "synthetic-adapter",
        "backend": "nautilus.partition_session_replay",
        "evidence_tier": "diagnostic",
        "inputs": {
            "engine_config_ref": _put(artifacts, json.dumps(config).encode()),
            "partition_manifest_ref": _put(artifacts, json.dumps(manifest).encode()),
        },
        "config": {"executable_sha256": sha},
        "execution": {},
        "budgets": {"wall_seconds": 10, "memory_mb": 1024},
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    output = tmp_path / "stage"
    output.mkdir()
    receipts = {}
    for operation in ("plan", "stage", "verify"):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "backtest_runtime.queued_session_adapter",
                operation,
                "--request",
                str(path),
                "--artifact-root",
                str(artifacts),
                "--result-root",
                str(tmp_path),
                "--partition-root",
                str(reader.input_root),
                "--engine-executable",
                str(binary),
                "--staging-path",
                str(output),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        receipts[operation] = json.loads(result.stdout)
    assert receipts["plan"]["checks"]["engine_request_semantics"] == "not_checked"
    assert receipts["stage"] == receipts["verify"]
    assert not list(tmp_path.rglob("*.sqlite"))
