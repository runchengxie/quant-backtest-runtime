"""Fixed policy construction and serialization; Platform owns all financial work."""

from __future__ import annotations

import json
import math
from datetime import date
from typing import Any

import pandas as pd
from quant_platform.backtest.impact import LinearImpact
from quant_platform.backtest.minute import (
    CapitalFlow,
    CorporateAction,
    MinuteExecutionConfig,
    ValuationEvent,
)
from quant_platform.backtest.minute_session import MinuteSession
from quant_platform.backtest.quantity_locks import QuantityLock


class TargetWeightsPolicy:
    """target-weights.v1: wait for pending reports/orders, otherwise use input weights."""

    def __init__(self, rows=None):
        self.rows = (
            [] if rows is None else json.loads(json.dumps(rows, allow_nan=False))
        )

    def __call__(self, state, proposed):
        self.rows.append(
            dict(
                time=state.time.isoformat(),
                cash=state.cash,
                holdings=dict(state.holdings),
                pending=bool(state.pending_orders),
            )
        )
        return None if state.pending_orders else proposed

    def checkpoint(self):
        return self.rows.copy()


def _frame(rows, clocks):
    if not isinstance(rows, list) or not 0 < len(rows) <= 4096:
        raise ValueError("1..4096 input rows required")
    frame = pd.DataFrame(rows)
    for clock in clocks:
        values = [pd.Timestamp(value) for value in frame[clock]]
        if any(value.tzinfo is None for value in values):
            raise ValueError("timezone aware input clocks required")
        frame[clock] = pd.to_datetime(values, utc=True).tz_convert("Asia/Shanghai")
    return frame


def _quantity_lock(lock: dict[str, Any]):
    value: dict[str, Any] = {**lock, "symbols": tuple(lock["symbols"])}
    return QuantityLock(**value)


def create(data: dict[str, Any], checkpoint=None):
    keys = {
        "schema",
        "timezone",
        "targets",
        "bars",
        "config",
        "report_delay_seconds",
        "impact",
        "quantity_locks",
        "corporate_actions",
        "valuation_events",
        "capital_flows",
    }
    if set(data) != keys or data["schema"] != "runtime-minute-input.v1":
        raise ValueError("unsupported minute input schema/fields")
    if data["timezone"] != "Asia/Shanghai":
        raise ValueError("unsupported market calendar")
    targets = _frame(data["targets"], ("decision_time", "entry_time"))
    bars = _frame(data["bars"], ("time", "interval_start"))
    config = MinuteExecutionConfig(**data["config"])
    tapes = {
        name: tuple(cls(**event) for event in data[name])
        for name, cls in (
            ("corporate_actions", CorporateAction),
            ("valuation_events", ValuationEvent),
            ("capital_flows", CapitalFlow),
        )
    }
    locks = tuple(_quantity_lock(lock) for lock in data["quantity_locks"])
    impact = LinearImpact(**data["impact"]) if data["impact"] is not None else None
    policy = TargetWeightsPolicy(checkpoint["policy"] if checkpoint else None)
    if checkpoint is None:
        return MinuteSession(
            targets,
            bars,
            config,
            policy=policy,
            **tapes,
            quantity_locks=locks,
            impact=impact,
            report_delay_seconds=data["report_delay_seconds"],
        )
    # Owner restore checks sources and every external financial tape, including empties.
    restored = MinuteSession.restore(
        targets, bars, config, checkpoint, policy=policy, **tapes
    )
    expected_impact = data["impact"]
    actual_impact = checkpoint["impact"]
    if (expected_impact is None) != (actual_impact is None):
        raise ValueError("impact intent mismatch")
    if impact is not None and any(
        impact.checkpoint()[key] != actual_impact[key]
        for key in ("transient", "permanent", "decay_seconds")
    ):
        raise ValueError("impact intent mismatch")
    if (
        restored.quantity_locks != locks
        or restored.delay != data["report_delay_seconds"]
    ):
        raise ValueError("lock/delay intent mismatch")
    return restored


def _json(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None  # Unknown valuation remains explicit JSON null.
    if hasattr(value, "item"):
        return _json(value.item())
    return value


def finish(session):
    while session.step():
        pass
    checkpoint = session.checkpoint()
    result = session.run()
    tables = {}
    for name in (
        "daily",
        "orders",
        "fills",
        "holdings",
        "valuation_ledger",
        "capital_flow_ledger",
    ):
        frame = getattr(result, name)
        tables[name] = dict(
            columns=list(frame.columns), rows=_json(frame.to_dict("records"))
        )
    return dict(
        schema="runtime-minute-result.v1",
        checkpoint=checkpoint,
        tables=tables,
        summary=_json(result.summary),
    )
