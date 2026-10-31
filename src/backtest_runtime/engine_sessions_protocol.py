"""Strict session-ledger reconciliation, independent of matching/portfolio decisions."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any
from zoneinfo import ZoneInfo

from .engine_protocol import (
    _decimal,
    _fills,
    _identifier,
    _orders,
    _positions,
    _records,
)

_FIELDS = {
    "schema_version",
    "backend",
    "backend_version",
    "currency",
    "price_basis",
    "valuation_timezone",
    "enforce_t1",
    "initial_cash",
    "final_cash",
    "final_nav",
    "total_commission",
    "capabilities",
    "orders",
    "fills",
    "rejections",
    "daily_ledger",
    "daily_positions",
}
_CAPABILITIES = {
    "session_ledger",
    "configured_t_plus_one",
    "buy_lots",
    "quote_trade_permissions",
    "quote_liquidity",
}
_DAY = {
    "session_date",
    "valuation_ts_ns",
    "cash",
    "positions_value",
    "nav",
    "commission",
}
_HOLDING = {"session_date", "symbol", "quantity", "mark_price", "market_value"}
_REJECTION = {"order_id", "reason", "event_type"}


def _rows(value: Any, keys: set[str], label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(
        not isinstance(row, dict) or set(row) != keys for row in value
    ):
        raise ValueError(f"invalid session {label} records")
    return value


def _date(ts: int, zone: str) -> str:
    return (
        datetime.fromtimestamp(ts // 1_000_000_000, UTC)
        .astimezone(ZoneInfo(zone))
        .date()
        .isoformat()
    )


def _identity(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise ValueError("invalid session result fields")
    identity = (
        payload["schema_version"],
        payload["backend"],
        payload["backend_version"],
        payload["price_basis"],
    )
    if identity != ("engine_result.v2", "nautilus", "0.66.0", "raw"):
        raise ValueError("unsupported session result identity")
    currency = payload["currency"]
    if not isinstance(currency, str) or currency not in {"CNY", "USD"}:
        raise ValueError("unsupported session currency")
    if (
        payload["valuation_timezone"]
        != ("Asia/Shanghai" if currency == "CNY" else "UTC")
        or type(payload["enforce_t1"]) is not bool
    ):
        raise ValueError("invalid session timezone or T+1 flag")
    capabilities = payload["capabilities"]
    if (
        not isinstance(capabilities, list)
        or any(not isinstance(c, str) for c in capabilities)
        or len(capabilities) != len(_CAPABILITIES)
        or set(capabilities) != _CAPABILITIES
    ):
        raise ValueError("unsupported session capabilities")
    return payload


def _days(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _rows(payload["daily_ledger"], _DAY, "balance")
    if not rows:
        raise ValueError("session balances must not be empty")
    previous_date, previous_ts = "", 0
    for row in rows:
        label, ts = row["session_date"], row["valuation_ts_ns"]
        if (
            not isinstance(label, str)
            or date.fromisoformat(label).isoformat() != label
            or label <= previous_date
        ):
            raise ValueError("session dates must be unique and increasing")
        if (
            type(ts) is not int
            or not previous_ts < ts <= 4_102_444_800_000_000_000
            or _date(ts, payload["valuation_timezone"]) != label
        ):
            raise ValueError("invalid session valuation time")
        cash, value, nav = (
            _decimal(row[name]) for name in ("cash", "positions_value", "nav")
        )
        if cash + value != nav:
            raise ValueError("session NAV does not reconcile")
        _decimal(row["commission"])
        previous_date, previous_ts = label, ts
    return rows


def _holdings(
    payload: dict[str, Any], days: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    labels = {day["session_date"] for day in days}
    for row in _rows(payload["daily_positions"], _HOLDING, "holding"):
        if (
            not isinstance(row["session_date"], str)
            or row["session_date"] not in labels
        ):
            raise ValueError("holding references an unknown session")
        _identifier(row["symbol"])
        by_date[row["session_date"]].append(
            {name: row[name] for name in _HOLDING - {"session_date"}}
        )
    for day in days:
        if _positions(by_date[day["session_date"]]) != _decimal(day["positions_value"]):
            raise ValueError("session position values do not reconcile")
    return by_date


def _rejections(payload: dict[str, Any], orders: dict[str, dict[str, Any]]) -> None:
    seen = set()
    for row in _rows(payload["rejections"], _REJECTION, "rejection"):
        identity = _identifier(row["order_id"])
        if (
            identity not in orders
            or identity in seen
            or row["event_type"] not in ("denied", "rejected")
        ):
            raise ValueError("invalid session rejection identity")
        if (
            orders[identity]["status"] != row["event_type"].upper()
            or not isinstance(row["reason"], str)
            or not 0 < len(row["reason"]) <= 4096
        ):
            raise ValueError("session rejection does not match order")
        seen.add(identity)
    expected = {
        identity
        for identity, order in orders.items()
        if order["status"] in {"DENIED", "REJECTED"}
    }
    if seen != expected:
        raise ValueError("session rejection evidence is incomplete")


def _group_fills(
    payload: dict[str, Any], days: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    boundaries = {
        day["session_date"]: (
            days[i - 1]["valuation_ts_ns"] if i else 0,
            day["valuation_ts_ns"],
        )
        for i, day in enumerate(days)
    }
    for fill in payload["fills"]:
        label = _date(fill["ts_ns"], payload["valuation_timezone"])
        if label not in boundaries:
            raise ValueError("fill references an unknown session")
        start, end = boundaries[label]
        if not start < fill["ts_ns"] <= end:
            raise ValueError("fill is outside session valuation boundaries")
        by_date[label].append(fill)
    return by_date


def _t1(fills: list[dict[str, Any]], held: dict[str, Decimal], enabled: bool) -> None:
    if not enabled:
        return
    sold: dict[str, Decimal] = defaultdict(Decimal)
    for fill in fills:
        if fill["side"] == "sell":
            sold[fill["symbol"]] += _decimal(fill["quantity"])
    if any(
        quantity > held.get(symbol, Decimal(0)) for symbol, quantity in sold.items()
    ):
        raise ValueError("session sells violate declared T+1 eligibility")


def _flows(
    payload: dict[str, Any],
    days: list[dict[str, Any]],
    holdings: dict[str, list[dict[str, Any]]],
) -> None:
    cash = _decimal(payload["initial_cash"], positive=True)
    quantities: dict[str, Decimal] = defaultdict(Decimal)
    fills = _group_fills(payload, days)
    for day in days:
        cost = Decimal(0)
        _t1(fills[day["session_date"]], quantities, payload["enforce_t1"])
        for fill in fills[day["session_date"]]:
            quantity, price, fee = (
                _decimal(fill[name]) for name in ("quantity", "price", "commission")
            )
            notional = (quantity * price).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_EVEN
            )
            cash += notional if fill["side"] == "sell" else -notional
            cash -= fee
            cost += fee
            quantities[fill["symbol"]] += (
                quantity if fill["side"] == "buy" else -quantity
            )
        actual = {
            row["symbol"]: _decimal(row["quantity"])
            for row in holdings[day["session_date"]]
        }
        expected = {symbol: qty for symbol, qty in quantities.items() if qty != 0}
        if (
            actual != expected
            or cash != _decimal(day["cash"])
            or cost != _decimal(day["commission"])
        ):
            raise ValueError("session fill/holding/cash/fee flows do not reconcile")


def validate_session_result(payload: Any) -> dict[str, Any]:
    payload = _identity(payload)
    with localcontext() as context:
        context.prec = 64
        days = _days(payload)
        holdings = _holdings(payload, days)
        orders = _orders(_records(payload, "orders"))
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
            raise ValueError("session contains nonterminal orders")
        commission = _fills(_records(payload, "fills"), orders)
        if commission != _decimal(payload["total_commission"]):
            raise ValueError("session total commission does not reconcile")
        _rejections(payload, orders)
        _flows(payload, days, holdings)
        if _decimal(payload["final_cash"]) != _decimal(days[-1]["cash"]) or _decimal(
            payload["final_nav"]
        ) != _decimal(days[-1]["nav"]):
            raise ValueError("session final summary does not reconcile")
    return payload


def session_summary(payload: dict[str, Any]) -> dict[str, str]:
    return {
        "cash": payload["final_cash"],
        "positions_value": payload["daily_ledger"][-1]["positions_value"],
        "equity": payload["final_nav"],
        "commission": payload["total_commission"],
    }


def bind_session_request(result: dict[str, Any], request: Any) -> None:
    if (
        not isinstance(request, dict)
        or request.get("schema_version") != "engine_request.v2"
    ):
        raise ValueError("v6 requires an engine_request.v2 artifact")
    for key in ("currency", "initial_cash", "price_basis", "enforce_t1"):
        if result[key] != request.get(key):
            raise ValueError(f"session result/request {key} mismatch")
    sessions = request.get("sessions", [])
    expected = [(s["session_date"], s["valuation_ts_ns"]) for s in sessions]
    actual = [(s["session_date"], s["valuation_ts_ns"]) for s in result["daily_ledger"]]
    if expected != actual:
        raise ValueError("session result/request valuation coverage mismatch")
    _bind_marks(result, sessions)
    intents = {o["id"]: (o, session) for session in sessions for o in session["orders"]}
    if set(intents) != {o["id"] for o in result["orders"]}:
        raise ValueError("session result/request order coverage mismatch")
    for order in result["orders"]:
        intent, _ = intents[order["id"]]
        filled, requested = (
            _decimal(order["filled_quantity"]),
            _decimal(intent["quantity"]),
        )
        if (
            order["symbol"] != intent["symbol"]
            or filled > requested
            or (order["status"] == "FILLED" and filled != requested)
        ):
            raise ValueError("session order does not match request intent")
    for fill in result["fills"]:
        intent, session = intents[fill["order_id"]]
        _bind_fill(fill, intent, session)


def _bind_fill(
    fill: dict[str, Any], intent: dict[str, Any], session: dict[str, Any]
) -> None:
    if fill["symbol"] != intent["symbol"] or fill["side"] != intent["side"]:
        raise ValueError("session fill/request identity mismatch")
    if (
        not session["open_ts_ns"] <= fill["ts_ns"] <= session["valuation_ts_ns"]
        or fill["ts_ns"] <= intent["information_ts_ns"]
    ):
        raise ValueError("session fill/request clock mismatch")


def _bind_marks(result: dict[str, Any], sessions: list[dict[str, Any]]) -> None:
    marks = {
        (session["session_date"], quote["symbol"]): quote
        for session in sessions
        for quote in session["quotes"]
        if quote["ts_ns"] == session["valuation_ts_ns"]
    }
    with localcontext() as context:
        context.prec = 64
        for holding in result["daily_positions"]:
            quote = marks.get((holding["session_date"], holding["symbol"]))
            if (
                quote is None
                or _decimal(holding["mark_price"])
                != (_decimal(quote["bid"]) + _decimal(quote["ask"])) / 2
            ):
                raise ValueError("session valuation mark does not match request quote")
