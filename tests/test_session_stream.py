"""The opt-in stream uses bounded pipes and replay, never a restored account pickle."""

import hashlib
import json
import os
from pathlib import Path

import pytest
from test_engine_sessions import _session_request
from test_partition_inputs import reader


def config_sessions():
    request = _session_request()
    first = request.pop("sessions")[0]
    first["orders"] = []
    second = json.loads(json.dumps(first))
    second["session_date"] = "2025-01-03"
    second["open_ts_ns"] += 86400000000000
    second["valuation_ts_ns"] += 86400000000000
    for quote in second["quotes"]:
        quote["ts_ns"] += 86400000000000
    return request, [first, second]


def source(tmp_path, sessions):
    return reader(
        tmp_path,
        groups=[
            [
                {
                    "event_id": i + 1,
                    "event_time_ns": session["valuation_ts_ns"],
                    "available_time_ns": session["valuation_ts_ns"] + 1,
                    "payload": {
                        "schema_version": "engine_session_payload.v1",
                        "session": session,
                    },
                }
                for i, session in enumerate(sessions)
            ]
        ],
    )[0]


def fake_engine(tmp_path, mode="valid"):
    binary = tmp_path / "engine"
    binary.write_text(
        """#!/usr/bin/python3
import sys,json,hashlib,time,os
MODE = """
        + repr(mode)
        + """
def send(value):
    print(json.dumps(value,separators=(",",":")),flush=True)
if MODE == "hang":
    time.sleep(30)
raw=sys.stdin.buffer.readline().rstrip(b"\\n")
start=json.loads(raw); config=start["config"]
prefix=hashlib.sha256(raw).digest()
if MODE == "stderr":
    sys.stderr.write("x" * 100000);sys.stderr.flush()
if MODE == "duplicate":
    print('{"schema_version":"engine_session_stream_result.v1","type":"ready","sequence":0,"sequence":0,"header_sha256":"'+prefix.hex()+'","max_frame_bytes":67108864}',flush=True)
else:
    send(dict(schema_version="engine_session_stream_result.v1",type="ready",sequence=0,header_sha256=("0"*64 if MODE=="hash" else prefix.hex()),max_frame_bytes=67108864))
ledger=[]; seq=0
for raw in sys.stdin.buffer:
    raw=raw.rstrip(b"\\n"); data=json.loads(raw);seq+=1
    prefix=hashlib.sha256(prefix+raw).digest()
    if data["type"]=="finish":
        assert not sys.stdin.buffer.read()
        result=dict(schema_version="engine_result.v2",backend="nautilus",backend_version="0.66.0",currency=config["currency"],price_basis=config["price_basis"],valuation_timezone="Asia/Shanghai",enforce_t1=config["enforce_t1"],initial_cash=config["initial_cash"],final_cash=config["initial_cash"],final_nav=config["initial_cash"],total_commission="0",capabilities=["session_ledger","configured_t_plus_one","buy_lots","quote_trade_permissions","quote_liquidity"],orders=[],fills=[],rejections=[],daily_ledger=ledger,daily_positions=[])
        send(dict(schema_version="engine_session_stream_result.v1",type="finished",sequence=seq,prefix_sha256=prefix.hex(),result=result))
        if MODE=="extra": print("{}",flush=True)
        break
    session=data["session"]
    day=dict(session_date=session["session_date"],valuation_ts_ns=session["valuation_ts_ns"],cash=config["initial_cash"],positions_value="0",nav=config["initial_cash"],commission="0")
    ledger.append(day)
    report=dict(session_index=seq-1,session_date=session["session_date"],valuation_ts_ns=session["valuation_ts_ns"],balance=day,positions=[],orders=[],fills=[],rejections=[])
    if MODE=="crash": sys.exit(3)
    if MODE=="oversize": print("x"*10000,flush=True)
    elif MODE=="truncated": sys.stdout.write("{");sys.stdout.flush();sys.exit(0)
    else: send(dict(schema_version="engine_session_stream_result.v1",type="session",sequence=(seq+1 if MODE=="sequence" else seq),prefix_sha256=prefix.hex(),report=report))
"""
    )
    binary.chmod(0o700)
    return binary, hashlib.sha256(binary.read_bytes()).hexdigest()


def run(tmp_path, mode="valid", **kwargs):
    from backtest_runtime.session_stream import run_partition_sessions

    config, sessions = config_sessions()
    binary, digest = fake_engine(tmp_path, mode)
    return run_partition_sessions(
        source(tmp_path, sessions),
        config,
        executable=binary,
        executable_sha256=digest,
        output_root=tmp_path / "output",
        **kwargs,
    )


def test_stream_two_sessions_and_idempotent_full_replay(tmp_path):
    from backtest_runtime.session_stream import run_partition_sessions

    receipt = run(tmp_path)
    result = json.loads(Path(receipt["result_path"]).read_bytes())
    assert len(result["daily_ledger"]) == 2
    assert result["final_cash"] == "10000"
    assert len(list((tmp_path / "output").glob("report-*.json"))) == 2
    config, sessions = config_sessions()
    binary, digest = fake_engine(tmp_path)
    repeated = run_partition_sessions(
        source(tmp_path, sessions),
        config,
        executable=binary,
        executable_sha256=digest,
        output_root=tmp_path / "output",
        resume=True,
    )
    assert repeated == receipt
    assert len(list((tmp_path / "output").glob("report-*.json"))) == 2


@pytest.mark.parametrize(
    "mode",
    [
        "hash",
        "duplicate",
        "sequence",
        "extra",
        "crash",
        "truncated",
        "oversize",
        "stderr",
    ],
)
def test_invalid_child_never_publishes_final(tmp_path, mode):
    from backtest_runtime.session_stream import StreamLimits

    with pytest.raises((ValueError, RuntimeError)):
        run(tmp_path, mode, limits=StreamLimits(max_frame_bytes=4096))
    assert not list((tmp_path / "output").glob("result-*.json"))


def test_hung_child_deadline_and_cancellation(tmp_path):
    from backtest_runtime.session_stream import StreamLimits

    with pytest.raises(TimeoutError):
        run(tmp_path, "hang", limits=StreamLimits(wall_seconds=0.15))
    other = tmp_path / "cancelled"
    other.mkdir()
    with pytest.raises(RuntimeError, match="cancel"):
        run(other, cancelled=lambda: True)


def test_resume_refuses_changed_config_and_corrupt_publication(tmp_path):
    from backtest_runtime.session_stream import run_partition_sessions

    run(tmp_path)
    config, sessions = config_sessions()
    config["initial_cash"] = "20000"
    binary, digest = fake_engine(tmp_path)
    with pytest.raises(ValueError, match="binding"):
        run_partition_sessions(
            source(tmp_path, sessions),
            config,
            executable=binary,
            executable_sha256=digest,
            output_root=tmp_path / "output",
            resume=True,
        )
    report = next((tmp_path / "output").glob("report-*.json"))
    report.write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        run(tmp_path, resume=True)


def test_real_stream_matches_one_shot_exactly(tmp_path):
    import subprocess

    from backtest_runtime.session_stream import run_partition_sessions

    binary = os.environ.get("BACKTEST_ENGINE_STREAM_TEST_EXECUTABLE")
    if binary is None:
        pytest.skip("new opt-in stream provider executable required")
    config, sessions = config_sessions()
    request = tmp_path / "request.json"
    request.write_text(json.dumps({**config, "sessions": sessions}))
    expected = json.loads(subprocess.check_output([binary, "run", str(request)]))
    receipt = run_partition_sessions(
        source(tmp_path, sessions),
        config,
        executable=Path(binary),
        executable_sha256=hashlib.sha256(Path(binary).read_bytes()).hexdigest(),
        output_root=tmp_path / "output",
    )
    assert json.loads(Path(receipt["result_path"]).read_bytes()) == expected


def test_provisional_report_is_available_before_finish(tmp_path):
    from backtest_runtime.session_stream import SessionStreamClient

    config, sessions = config_sessions()
    binary, digest = fake_engine(tmp_path)
    with SessionStreamClient(binary, digest) as client:
        client.start(config)
        report = client.session(sessions[0])
        assert report["session_index"] == 0
        assert report["balance"]["cash"] == config["initial_cash"]
        assert client.process.child.poll() is None
        assert client.session(sessions[1])["session_index"] == 1
        assert len(client.finish()["daily_ledger"]) == 2
        with pytest.raises(ValueError, match="not active"):
            client.finish()


def test_publish_before_journal_ack_replays_and_deduplicates(tmp_path, monkeypatch):
    from backtest_runtime.session_stream_journal import ReplayJournal

    original = ReplayJournal._write

    def fail_ack(self, data):
        if len(data["acknowledged"]) == 1:
            raise OSError("injected fsync failure")
        original(self, data)

    with monkeypatch.context() as patch:
        patch.setattr(ReplayJournal, "_write", fail_ack)
        with pytest.raises(OSError, match="injected"):
            run(tmp_path)
    assert len(list((tmp_path / "output").glob("report-*.json"))) == 1
    assert (
        json.loads((tmp_path / "output/journal.json").read_bytes())["acknowledged"]
        == []
    )
    receipt = run(tmp_path, resume=True)
    assert receipt["session_count"] == 2
    assert len(list((tmp_path / "output").glob("report-*.json"))) == 2


def test_existing_root_and_exclusive_lock_reject_concurrent_writer(tmp_path):
    from backtest_runtime.session_stream import StreamLimits
    from backtest_runtime.session_stream_journal import ReplayJournal

    root = tmp_path / "locked"
    journal = ReplayJournal(root, {"test": "binding"}, StreamLimits(), False)
    try:
        with pytest.raises(BlockingIOError):
            ReplayJournal(root, {"test": "binding"}, StreamLimits(), True)
    finally:
        journal.close()
    run(tmp_path)
    with pytest.raises(ValueError, match="resume"):
        run(tmp_path)


@pytest.mark.parametrize(
    "mutation", ["digest", "version", "cursor", "missing", "source", "binary"]
)
def test_resume_rejects_modified_bindings_and_acknowledged_history(
    tmp_path, monkeypatch, mutation
):
    import backtest_runtime.session_stream as stream

    run(tmp_path)
    path = tmp_path / "output/journal.json"
    data = json.loads(path.read_bytes())
    if mutation == "missing":
        next((tmp_path / "output").glob("report-*.json")).unlink()
    elif mutation == "source":
        monkeypatch.setattr(stream, "_source_identity", lambda: "0" * 64)
    elif mutation == "binary":
        binary, _ = fake_engine(tmp_path)
        binary.write_bytes(binary.read_bytes() + b"# changed\n")
    else:
        if mutation == "version":
            data["schema_version"] = "session_stream_journal.v999"
        elif mutation == "cursor":
            data["acknowledged"][0]["next_cursor"]["row"] = 0
        else:
            data["complete"] = False
        if mutation != "digest":
            from backtest_runtime.session_stream_io import canonical

            data.pop("integrity_sha256")
            data["integrity_sha256"] = hashlib.sha256(canonical(data)).hexdigest()
        path.write_text(json.dumps(data))
    config, sessions = config_sessions()
    binary = tmp_path / "engine"
    with pytest.raises((ValueError, OSError)):
        stream.run_partition_sessions(
            source(tmp_path, sessions),
            config,
            executable=binary,
            executable_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            output_root=tmp_path / "output",
            resume=True,
        )


def test_corrupt_partition_rejected_without_session_publication(tmp_path):
    from backtest_runtime.session_stream import run_partition_sessions

    config, sessions = config_sessions()
    inputs = source(tmp_path, sessions)
    (tmp_path / "inputs/0.ndjson").write_text("corrupt")
    binary, digest = fake_engine(tmp_path)
    with pytest.raises(ValueError):
        run_partition_sessions(
            inputs,
            config,
            executable=binary,
            executable_sha256=digest,
            output_root=tmp_path / "output",
        )
    assert not list((tmp_path / "output").glob("report-*.json"))


def test_real_stream_with_buy_and_later_sell_matches_one_shot(tmp_path):
    import subprocess

    from backtest_runtime.session_stream import run_partition_sessions

    binary = os.environ.get("BACKTEST_ENGINE_STREAM_TEST_EXECUTABLE")
    if binary is None:
        pytest.skip("new opt-in stream provider executable required")
    config, sessions = config_sessions()
    sessions[0]["orders"] = _session_request()["sessions"][0]["orders"]
    sessions[0]["orders"].append(
        {
            **sessions[0]["orders"][0],
            "id": "same-day-sell",
            "side": "sell",
            "information_ts_ns": sessions[0]["open_ts_ns"],
        }
    )
    sessions[1]["orders"] = [
        {
            **sessions[0]["orders"][0],
            "id": "sell",
            "side": "sell",
            "information_ts_ns": sessions[1]["open_ts_ns"] - 1,
        }
    ]
    request = tmp_path / "request.json"
    request.write_text(json.dumps({**config, "sessions": sessions}))
    expected = json.loads(subprocess.check_output([binary, "run", str(request)]))
    assert len(expected["fills"]) == 2
    assert len(expected["rejections"]) == 1
    receipt = run_partition_sessions(
        source(tmp_path, sessions),
        config,
        executable=Path(binary),
        executable_sha256=hashlib.sha256(Path(binary).read_bytes()).hexdigest(),
        output_root=tmp_path / "output",
    )
    assert json.loads(Path(receipt["result_path"]).read_bytes()) == expected


@pytest.mark.parametrize(
    "response",
    [
        b"{}\n",
        b"\xff\n",
        b'{"x":NaN}\n',
        b'{"x":1e999}\n',
        b"\n",
        b'{"schema_version":"engine_session_stream_result.v1","type":"ready","sequence":false,"header_sha256":"HASH","max_frame_bytes":67108864}\n',
        b'{"schema_version":"wrong","type":"ready","sequence":0,"header_sha256":"HASH","max_frame_bytes":67108864}\n',
    ],
)
def test_strict_ready_frames_fail_closed(tmp_path, response):
    from backtest_runtime.session_stream import SessionStreamClient

    config, _ = config_sessions()
    binary, _ = fake_engine(tmp_path)
    content = binary.read_text()
    begin = content.index('if MODE == "duplicate":')
    end = content.index("ledger=[]", begin)
    content = (
        content[:begin]
        + "sys.stdout.buffer.write("
        + repr(response)
        + ".replace(b'HASH',prefix.hex().encode()));sys.stdout.flush()\n"
        + content[end:]
    )
    binary.write_text(content)
    with (
        pytest.raises(ValueError),
        SessionStreamClient(
            binary, hashlib.sha256(binary.read_bytes()).hexdigest()
        ) as client,
    ):
        client.start(config)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c.update(sessions=[]),
        lambda c: c.update(enforce_t1=1),
        lambda c: c.update(initial_cash=float("nan")),
        lambda c: c["instruments"][0].update(price_precision=True),
        lambda c: c.update(required_capabilities="session_ledger"),
    ],
)
def test_strict_config_rejected_before_publication(tmp_path, mutation):
    from backtest_runtime.session_stream import run_partition_sessions

    config, sessions = config_sessions()
    mutation(config)
    binary, digest = fake_engine(tmp_path)
    with pytest.raises(ValueError):
        run_partition_sessions(
            source(tmp_path, sessions),
            config,
            executable=binary,
            executable_sha256=digest,
            output_root=tmp_path / "output",
        )
    assert not (tmp_path / "output/journal.json").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda report: report.update(session_index=True),
        lambda report: report.update(session_date="2025-01-09"),
        lambda report: report["balance"].update(cash="999", nav="999"),
        lambda report: report.update(unexpected="field"),
        lambda report: report.update(
            positions=[
                {
                    "session_date": "2025-01-02",
                    "symbol": "AAA",
                    "quantity": "1",
                    "mark_price": "999",
                    "market_value": "999",
                }
            ]
        ),
    ],
)
def test_report_and_final_reconciliation_reject_fabricated_outputs(mutation):
    from backtest_runtime.session_stream_reports import Reports

    config, sessions = config_sessions()
    session = sessions[0]
    day = {
        "session_date": session["session_date"],
        "valuation_ts_ns": session["valuation_ts_ns"],
        "cash": "10000",
        "positions_value": "0",
        "nav": "10000",
        "commission": "0",
    }
    report = {
        "session_index": 0,
        "session_date": session["session_date"],
        "valuation_ts_ns": session["valuation_ts_ns"],
        "balance": day,
        "positions": [],
        "orders": [],
        "fills": [],
        "rejections": [],
    }
    mutation(report)
    reports = Reports(config)
    with pytest.raises(ValueError):
        reports.add(report, session)
        final = {
            **reports.payload,
            "final_cash": day["cash"],
            "final_nav": day["nav"],
            "total_commission": "0",
        }
        reports.finish(final)


def bound_report():
    from test_engine_sessions import _result

    result = _result()
    config = {
        "schema_version": "engine_request.v2",
        "currency": "USD",
        "price_basis": "raw",
        "initial_cash": "10",
        "fee_bps": "10",
        "enforce_t1": True,
        "instruments": [{"symbol": "AAA", "price_precision": 2, "buy_lot": 1}],
        "required_capabilities": ["session_ledger"],
    }
    day = result["daily_ledger"][0]
    session = {
        "session_date": day["session_date"],
        "open_ts_ns": day["valuation_ts_ns"] - 1000000000,
        "valuation_ts_ns": day["valuation_ts_ns"],
        "quotes": [
            {"symbol": "AAA", "ts_ns": day["valuation_ts_ns"], "bid": "2", "ask": "2"}
        ],
        "orders": [
            {
                "id": "buy",
                "symbol": "AAA",
                "quantity": "1",
                "side": "buy",
                "information_ts_ns": day["valuation_ts_ns"] - 1000000001,
            }
        ],
    }
    report = {
        "session_index": 0,
        "session_date": day["session_date"],
        "valuation_ts_ns": day["valuation_ts_ns"],
        "balance": day,
        "positions": [result["daily_positions"][0]],
        "orders": result["orders"],
        "fills": result["fills"],
        "rejections": [],
    }
    return config, session, report


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s, r: s["orders"][0].update(quantity="0"),
        lambda s, r: s["orders"][0].update(information_ts_ns=r["fills"][0]["ts_ns"]),
        lambda s, r: s.update(open_ts_ns=r["fills"][0]["ts_ns"] + 1),
        lambda s, r: r["fills"][0].update(side="sell"),
        lambda s, r: r["orders"][0].update(status="ACCEPTED"),
        lambda s, r: s["quotes"][0].update(bid="999", ask="999"),
        lambda s, r: r.update(
            rejections=[{"order_id": "buy", "event_type": "denied", "reason": "fake"}]
        ),
    ],
)
def test_current_report_binds_requested_quantity_marks_and_fill_clocks(mutation):
    from backtest_runtime.session_stream_reports import Reports

    config, session, report = bound_report()
    mutation(session, report)
    with pytest.raises(ValueError):
        Reports(config).add(report, session)


def test_current_report_binding_accepts_valid_quote_and_execution():
    from backtest_runtime.session_stream_reports import Reports

    config, session, report = bound_report()
    assert Reports(config).add(report, session) == report


@pytest.mark.parametrize(
    "what", ["payload", "envelope", "session_count", "input_frame", "journal_size"]
)
def test_input_contract_and_policy_limits_fail_closed(tmp_path, what):
    from backtest_runtime.session_stream import StreamLimits, run_partition_sessions

    config, sessions = config_sessions()
    inputs = source(tmp_path, sessions)
    limits = StreamLimits()
    if what in ("payload", "envelope"):
        original = inputs.iter_rows

        def bad_rows():
            for row, cursor in original():
                if what == "payload":
                    row["payload"]["unexpected"] = True
                else:
                    row["event_time_ns"] += 1
                yield row, cursor

        inputs.iter_rows = bad_rows
    elif what == "session_count":
        limits = StreamLimits(max_sessions=1)
    elif what == "input_frame":
        limits = StreamLimits(max_frame_bytes=350)
    else:
        limits = StreamLimits(max_journal_bytes=10)
    binary, digest = fake_engine(tmp_path)
    with pytest.raises(ValueError):
        run_partition_sessions(
            inputs,
            config,
            executable=binary,
            executable_sha256=digest,
            output_root=tmp_path / "output",
            limits=limits,
        )
    assert not list((tmp_path / "output").glob("result-*.json"))
