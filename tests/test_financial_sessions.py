"""Real owner restore must preserve the complete financial payload."""

import copy
import json

import pandas as pd
import pytest


def fixture():
    targets = []
    bars = []
    for day, weight, price in ((5, 1, 10.0), (6, 0, 4.5), (7, 0, 4.5)):
        entry = pd.Timestamp(f"2026-01-{day:02} 09:30", tz="Asia/Shanghai")
        targets.append(
            dict(
                decision_time=(entry - pd.Timedelta(minutes=1)).isoformat(),
                entry_time=entry.isoformat(),
                symbol="A",
                weight=weight,
            )
        )
        for minute in (29, 32, 33):
            time = entry.replace(minute=minute)
            bars.append(
                dict(
                    time=time.isoformat(),
                    interval_start=(time - pd.Timedelta(seconds=30)).isoformat(),
                    symbol="A",
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=1000,
                    amount=price * 1000,
                    up_limit=11,
                    down_limit=1,
                    tradable=True,
                )
            )
    return dict(
        schema="runtime-minute-input.v1",
        timezone="Asia/Shanghai",
        targets=targets,
        bars=bars,
        config=dict(
            initial_cash=10000, participation_rate=0.1, buy_fee_bp=0, sell_fee_bp=0
        ),
        report_delay_seconds=600,
        impact=dict(transient=0.0, permanent=0.0001, decay_seconds=60),
        quantity_locks=[],
        corporate_actions=[
            dict(
                event_id="distribution",
                symbol="A",
                available_date="2026-01-04",
                record_date="2026-01-05",
                ex_date="2026-01-06",
                cash_per_share=1,
                cash_pay_date="2026-01-07",
                stock_per_share=1,
                stock_tradable_date="2026-01-07",
            )
        ],
        valuation_events=[],
        capital_flows=[
            dict(
                event_id="deposit",
                available_time="2026-01-07 09:28+08:00",
                effective_time="2026-01-07 09:29+08:00",
                amount=1000,
                source="synthetic",
            )
        ],
    )


def test_real_restore_every_committed_boundary_complete_payload():
    from backtest_runtime import financial_sessions as f

    data = fixture()
    session = f.create(data)
    checkpoints = []
    while session.step():
        checkpoints.append(
            json.loads(json.dumps(session.checkpoint(), allow_nan=False))
        )
    expected = f.finish(session)
    assert len(expected["tables"]["fills"]["rows"]) == 2
    for checkpoint in checkpoints:
        restored = f.create(data, checkpoint)
        assert restored.cursor == checkpoint["cursor"] > 0
        assert f.finish(restored) == expected


@pytest.mark.parametrize(
    "field", ["schema", "corporate_actions", "capital_flows", "config", "impact"]
)
def test_restore_rejects_changed_intent(field):
    from backtest_runtime import financial_sessions as f

    data = fixture()
    session = f.create(data)
    session.step()
    checkpoint = session.checkpoint()
    changed = copy.deepcopy(data)
    if field == "schema":
        checkpoint["schema"] = "minute-session.v1"
    elif field == "config":
        changed[field]["initial_cash"] = 20000
    elif field == "impact":
        changed[field]["permanent"] = 0.1
    else:
        changed[field] = []
    with pytest.raises(ValueError):
        f.create(changed, checkpoint)


def test_midnight_calendar_and_capital_hand_expectations():
    from backtest_runtime import financial_sessions as f

    data = fixture()
    data["targets"] = [
        dict(
            decision_time="2026-01-05T23:56:00+08:00",
            entry_time="2026-01-05T23:57:00+08:00",
            symbol="A",
            weight=1,
        )
    ]
    template = data["bars"][0]
    data["bars"] = []
    for clock, tradable in (
        ("2026-01-05T23:56:00+08:00", True),
        ("2026-01-05T23:59:00+08:00", True),
        ("2026-01-06T00:01:00+08:00", False),
    ):
        t = pd.Timestamp(clock)
        data["bars"].append(
            {
                **template,
                "time": clock,
                "interval_start": (t - pd.Timedelta(seconds=30)).isoformat(),
                "tradable": tradable,
            }
        )
    action = data["corporate_actions"][0]
    action.update(cash_pay_date="2026-01-06", stock_tradable_date="2026-01-06")
    data["capital_flows"] = [
        dict(
            event_id="deposit",
            available_time="2026-01-05 15:59+00:00",
            effective_time="2026-01-05 16:00+00:00",
            amount=1000,
            source="synthetic",
        )
    ]
    data["valuation_events"] = [
        dict(
            event_id="residual",
            symbol="A",
            effective_time="2026-01-06 00:00+08:00",
            available_time="2026-01-05 23:59+08:00",
            value_per_share=4.5,
            basis="declared",
            source="synthetic",
        )
    ]
    data["report_delay_seconds"] = 0
    data["impact"] = None
    full = f.finish(f.create(data))
    daily = full["tables"]["daily"]["rows"]
    assert [row["nav"] for row in daily] == [10000, 11000]
    assert [row["pnl"] for row in daily] == [0, 0]
    assert [row["cumulative_return"] for row in daily] == [0, 0]
    assert daily[-1]["cash"] == 10100
    assert full["tables"]["valuation_ledger"]["rows"][0]["quantity"] == 200
    partial = f.create(data)
    while partial.step():
        restored = f.create(data, partial.checkpoint())
        assert f.finish(restored) == full


def test_locks_receivables_and_unknown_marks_are_preserved():
    from backtest_runtime import financial_sessions as f

    data = fixture()
    data["impact"] = None
    data["report_delay_seconds"] = 0
    data["quantity_locks"] = [
        dict(
            available_ns=pd.Timestamp("2026-01-06", tz="Asia/Shanghai").value,
            start_ns=pd.Timestamp("2026-01-06", tz="Asia/Shanghai").value,
            end_ns=pd.Timestamp("2026-01-07 09:30", tz="Asia/Shanghai").value,
            symbols=["A"],
            source_binding="synthetic",
        )
    ]
    full = f.finish(f.create(data))
    assert full["tables"]["daily"]["rows"][-1]["cash"] == 9200
    assert len(full["tables"]["fills"]["rows"]) == 2
    assert full["tables"]["capital_flow_ledger"]["rows"][0]["nav_before"] is None
    part = f.create(data)
    while part.step():
        assert f.finish(f.create(data, part.checkpoint())) == full


def test_conflicting_market_calendar_rejected():
    from backtest_runtime import financial_sessions as f

    data = fixture()
    data["timezone"] = "America/New_York"
    with pytest.raises(ValueError, match="calendar"):
        f.create(data)


def test_adapter_stages_only_and_rejects_loaded_source_change(tmp_path, monkeypatch):
    import argparse
    from pathlib import Path

    from test_rust_engine_jobs import _put

    from backtest_runtime import financial_session_adapter as adapter
    from backtest_runtime import financial_sessions

    data = fixture()
    mapping = dict(
        inputs=dict(minute_input_ref=_put(tmp_path, json.dumps(data).encode())),
        config=dict(
            provider_revision=adapter.PROVIDER,
            checkpoint_format="minute-session.v2",
            policy="target-weights.v1",
        ),
        backend="platform.minute_session_resume",
    )
    plan = adapter.preflight(mapping, tmp_path, "synthetic-source")
    stage = tmp_path / "stage"
    stage.mkdir()
    args = argparse.Namespace(
        artifact_root=tmp_path,
        runtime_source="synthetic-source",
        binding=plan["financial_binding"],
        checkpoint=None,
        checkpoint_sha256=None,
        job_id="job",
        token="owner",
        staging_path=stage,
    )
    first = adapter.step(mapping, args)
    assert first["cursor"] == 1 and first["start_cursor"] == 0
    args.checkpoint = stage / "candidate.json"
    args.checkpoint_sha256 = first["sha256"]
    original = Path.read_bytes
    with monkeypatch.context() as patch:
        patch.setattr(
            Path,
            "read_bytes",
            lambda p: (
                original(p)
                + (b"#changed" if p == Path(financial_sessions.__file__) else b"")
            ),
        )
        with pytest.raises(ValueError, match="source/config binding changed"):
            adapter.step(mapping, args)
    receipt = adapter.step(mapping, args)
    assert receipt["start_cursor"] == 1 and receipt["cursor"] == 2
    while not receipt["done"]:
        args.checkpoint_sha256 = receipt["sha256"]
        receipt = adapter.step(mapping, args)
    assert (stage / "financial.json").exists()
    assert not (tmp_path / "jobs.sqlite").exists()


def test_adapter_rejects_wrong_installed_provider(monkeypatch):
    from backtest_runtime import financial_session_adapter as adapter

    class WrongProvider:
        def read_text(self, name):
            return json.dumps({"vcs_info": {"commit_id": "wrong"}})

    monkeypatch.setattr(
        adapter.importlib.metadata, "distribution", lambda name: WrongProvider()
    )
    with pytest.raises(ValueError, match="exact locked Platform"):
        adapter.source_identity("source")
