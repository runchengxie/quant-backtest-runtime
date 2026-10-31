"""Verify independent-engine records and balances without simulating trades."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

_FIELDS = {
    "schema_version",
    "backend",
    "backend_version",
    "capabilities",
    "currency",
    "cash",
    "positions_value",
    "equity",
    "commission",
    "orders",
    "fills",
    "positions",
}
_CAPABILITIES = {
    "quote_market_orders",
    "multi_asset",
    "final_balances",
    "proportional_fees",
}
_ROWS = {
    "orders": {"id", "symbol", "status", "filled_quantity"},
    "fills": {"order_id", "symbol", "side", "ts_ns", "quantity", "price", "commission"},
    "positions": {"symbol", "quantity", "mark_price", "market_value"},
}
_STATUSES = {
    "INITIALIZED",
    "SUBMITTED",
    "ACCEPTED",
    "PARTIALLY_FILLED",
    "PENDING_CANCEL",
    "PENDING_UPDATE",
    "CANCELED",
    "FILLED",
    "DENIED",
    "REJECTED",
    "EXPIRED",
    "NOT_SUBMITTED",
}


def _decimal(value: Any, *, positive: bool = False, integer: bool = False) -> Decimal:
    if (
        not isinstance(value, str)
        or len(value) > 28
        or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,8})?", value)
    ):
        raise ValueError("engine financial values must be bounded decimal strings")
    number = Decimal(value)
    if (
        number > Decimal("1e18")
        or (positive and number == 0)
        or (integer and number != number.to_integral_value())
    ):
        raise ValueError("engine decimal is outside supported bounds")
    return number


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", value):
        raise ValueError("invalid engine identifier")
    return value


def _records(payload: dict[str, Any], name: str) -> list[dict[str, Any]]:
    rows = payload[name]
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or set(row) != _ROWS[name] for row in rows
    ):
        raise ValueError(f"invalid engine {name} record fields")
    return rows


def _orders(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    orders = {}
    for row in rows:
        identity = _identifier(row["id"])
        _identifier(row["symbol"])
        status = row["status"]
        if not isinstance(status, str) or status not in _STATUSES or identity in orders:
            raise ValueError("invalid engine order identity or status")
        filled = _decimal(row["filled_quantity"], integer=True)
        if status in {"DENIED", "REJECTED", "NOT_SUBMITTED"} and filled != 0:
            raise ValueError("unexecuted engine order has filled quantity")
        orders[identity] = row
    return orders


def _fills(rows: list[dict[str, Any]], orders: dict[str, dict[str, Any]]) -> Decimal:
    quantities: dict[str, Decimal] = {}
    commission = Decimal(0)
    for row in rows:
        identity = _identifier(row["order_id"])
        if identity not in orders or row["symbol"] != orders[identity]["symbol"]:
            raise ValueError("engine fill references an unknown order or symbol")
        if (
            row["side"] not in ("buy", "sell")
            or type(row["ts_ns"]) is not int
            or not 0 < row["ts_ns"] <= 4_102_444_800_000_000_000
        ):
            raise ValueError("invalid engine fill side or time")
        quantity = _decimal(row["quantity"], positive=True, integer=True)
        _decimal(row["price"], positive=True)
        commission += _decimal(row["commission"])
        quantities[identity] = quantities.get(identity, Decimal(0)) + quantity
    for identity, order in orders.items():
        if quantities.get(identity, Decimal(0)) != _decimal(order["filled_quantity"]):
            raise ValueError("engine fill quantities do not reconcile with orders")
    return commission


def _positions(rows: list[dict[str, Any]]) -> Decimal:
    symbols = set()
    total = Decimal(0)
    for row in rows:
        symbol = _identifier(row["symbol"])
        if symbol in symbols:
            raise ValueError("duplicate engine position symbol")
        symbols.add(symbol)
        quantity = _decimal(row["quantity"], positive=True, integer=True)
        mark = _decimal(row["mark_price"], positive=True)
        value = _decimal(row["market_value"])
        if quantity * mark != value:
            raise ValueError("engine position mark does not reconcile")
        total += value
    return total


def validate_engine_result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise ValueError("invalid engine result fields")
    identity = (
        payload["schema_version"],
        payload["backend"],
        payload["backend_version"],
        payload["currency"],
    )
    if identity != ("engine_result.v1", "nautilus", "0.66.0", "USD"):
        raise ValueError("unsupported engine result identity")
    capabilities = payload["capabilities"]
    if (
        not isinstance(capabilities, list)
        or any(not isinstance(c, str) for c in capabilities)
        or len(capabilities) != len(_CAPABILITIES)
        or set(capabilities) != _CAPABILITIES
    ):
        raise ValueError("unsupported engine result capabilities")
    cash, value, equity, commission = (
        _decimal(payload[name])
        for name in ("cash", "positions_value", "equity", "commission")
    )
    if cash + value != equity or _positions(_records(payload, "positions")) != value:
        raise ValueError("engine equity or position values do not reconcile")
    orders = _orders(_records(payload, "orders"))
    if _fills(_records(payload, "fills"), orders) != commission:
        raise ValueError("engine commission does not reconcile")
    return payload


def engine_summary(payload: dict[str, Any]) -> dict[str, str]:
    return {
        name: payload[name]
        for name in ("cash", "positions_value", "equity", "commission")
    }
