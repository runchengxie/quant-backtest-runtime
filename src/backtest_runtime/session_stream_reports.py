"""Bind provisional current-session output; reconcile the accumulated result at EOF."""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Any

from .engine_protocol import _decimal, _fills, _orders, _records
from .engine_sessions_protocol import (
    _days,
    _holdings,
    _rejections,
    bind_session_request,
    validate_session_result,
)
from .session_stream_io import fields

CAPABILITIES = [
    "session_ledger",
    "configured_t_plus_one",
    "buy_lots",
    "quote_trade_permissions",
    "quote_liquidity",
]


def identity(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "engine_result.v2",
        "backend": "nautilus",
        "backend_version": "0.66.0",
        **{
            key: config[key]
            for key in ("currency", "initial_cash", "price_basis", "enforce_t1")
        },
        "valuation_timezone": "Asia/Shanghai" if config["currency"] == "CNY" else "UTC",
        "capabilities": CAPABILITIES.copy(),
    }


class Reports:
    """Keep only result records. Quotes and session intents are released after add."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.payload = {
            **identity(config),
            "orders": [],
            "fills": [],
            "rejections": [],
            "daily_ledger": [],
            "daily_positions": [],
        }
        self.count = 0

    def add(self, report: Any, session: dict[str, Any]) -> dict[str, Any]:
        report = fields(
            report,
            {
                "session_index",
                "session_date",
                "valuation_ts_ns",
                "balance",
                "positions",
                "orders",
                "fills",
                "rejections",
            },
        )
        if (
            type(report["session_index"]) is not int
            or report["session_index"] != self.count
        ):
            raise ValueError("session report index mismatch")
        for key in ("session_date", "valuation_ts_ns"):
            if (
                type(report[key]) is not type(session[key])
                or report[key] != session[key]
            ):
                raise ValueError("session report valuation mismatch")
        day = report["balance"]
        if not isinstance(day, dict) or any(
            day.get(key) != report[key] for key in ("session_date", "valuation_ts_ns")
        ):
            raise ValueError("session balance/report valuation mismatch")
        view = {
            **identity(self.config),
            "daily_ledger": [day],
            "daily_positions": report["positions"],
            **{key: report[key] for key in ("orders", "fills", "rejections")},
        }
        with localcontext() as context:
            context.prec = 64
            days = _days(view)
            _holdings(view, days)
            orders = _orders(_records(view, "orders"))
            if any(
                order["status"]
                not in {
                    "FILLED",
                    "CANCELED",
                    "DENIED",
                    "REJECTED",
                    "NOT_SUBMITTED",
                    "EXPIRED",
                }
                for order in orders.values()
            ):
                raise ValueError("session report contains nonterminal orders")
            if _fills(_records(view, "fills"), orders) != _decimal(day["commission"]):
                raise ValueError("session report commission mismatch")
            _rejections(view, orders)
            bind_session_request(view, {**self.config, "sessions": [session]})
        self.payload["daily_ledger"].append(day)
        self.payload["daily_positions"].extend(report["positions"])
        for key in ("orders", "fills", "rejections"):
            self.payload[key].extend(report[key])
        self.count += 1
        return report

    def finish(self, result: Any) -> dict[str, Any]:
        if not self.count:
            raise ValueError("at least one session required")
        last = self.payload["daily_ledger"][-1]
        with localcontext() as context:
            context.prec = 64
            commission = sum(
                (_decimal(fill["commission"]) for fill in self.payload["fills"]),
                Decimal(0),
            )
            expected = {
                **self.payload,
                "final_cash": last["cash"],
                "final_nav": last["nav"],
                "total_commission": format(commission.normalize(), "f"),
            }
            validate_session_result(expected)
            validate_session_result(result)
        if expected != result:
            raise ValueError("engine final result differs from provisional reports")
        return result
