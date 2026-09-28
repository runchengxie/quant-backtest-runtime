"""Versioned request validation and canonical fingerprints."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from math import isfinite
from typing import Any
from urllib.parse import urlsplit

_DIGEST_REF = re.compile(r"artifact://sha256/([0-9a-f]{64})\Z")
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_INPUT_KEYS = {"positions_ref", "pricing_ref", "periods_ref", "intraday_bars_ref"}
_CONFIG_KEYS = {
    "price_col",
    "entry_price_col",
    "exit_price_col",
    "transaction_cost_bps",
    "trading_days_per_year",
    "long_only",
    "preserve_gross_exposure",
    "exit_price_policy",
    "exit_fallback_policy",
    "tradable_col",
}
_EXECUTION_KEYS = {
    "ledger",
    "ledger_config",
    "allow_stale_execution_price",
    "intraday_execution_assumption",
}
_LEDGER_CONFIG_KEYS = {
    "enabled",
    "portfolio_value",
    "participation_rate",
    "liquidity_cols",
    "liquidity_notional_multiplier",
    "buy_max_days",
    "sell_max_days",
    "zero_fill_abort_days_buy",
    "unfilled_buy_action",
    "unfilled_sell_action",
    "round_lot",
    "enforce_t1",
    "enforce_price_limits",
    "enforce_listing_status",
    "limit_up_col",
    "limit_down_col",
    "listing_status_col",
    "lot_tolerance",
}
_SEQUENCED_CONFIG_KEYS = {
    "price_col",
    "tradable_col",
    "buy_tradable_col",
    "sell_tradable_col",
    "limit_up_col",
    "limit_down_col",
    "listing_status_col",
    "transaction_cost_bps",
    "price_basis",
}
_ACCOUNTING_CONFIG_KEYS = {"commission_rate", "stamp_tax_rate", "slippage_rate"}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _exact_keys(value: Any, expected: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a JSON object")
    missing = expected - set(value)
    extra = set(value) - expected
    if missing or extra:
        details = []
        if missing:
            details.append("missing: " + ", ".join(sorted(missing)))
        if extra:
            details.append("unknown: " + ", ".join(sorted(extra)))
        raise ValueError(f"{label} keys invalid ({'; '.join(details)})")
    return value


def artifact_digest(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST_REF.fullmatch(value):
        parsed = urlsplit(value if isinstance(value, str) else "")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                f"{label} must not contain credentials, query, or fragment"
            )
        raise ValueError(f"{label} must be artifact://sha256/<lowercase-sha256>")
    return value.rsplit("/", 1)[-1]


def _validate_request_identity(mapping: dict[str, Any]) -> tuple[str, str]:
    version = mapping["schema_version"]
    if type(version) is not int or version not in {1, 2, 3, 4}:
        raise ValueError("schema_version must be integer 1, 2, 3 or 4")
    key = mapping["idempotency_key"]
    if not isinstance(key, str) or not _IDEMPOTENCY_KEY.fullmatch(key):
        raise ValueError("idempotency_key must be 1-128 safe ASCII characters")
    backend = {
        3: "native.sequenced_execution",
        4: "native.trade_accounting",
    }.get(version, "native.position_replay")
    if mapping["backend"] != backend:
        raise ValueError(f"backend must be {backend} in v{version}")
    evidence_tier = mapping["evidence_tier"]
    if evidence_tier != ("execution_aware" if version == 2 else "diagnostic"):
        raise ValueError("unsupported evidence_tier")
    return key, evidence_tier


def _validate_inputs(value: Any, *, version: int) -> dict[str, str]:
    if version == 4:
        if not isinstance(value, dict) or set(value) != {"accounting_ref"}:
            raise ValueError("trade accounting inputs must contain accounting_ref")
        return {
            "accounting_ref": f"artifact://sha256/{artifact_digest(value['accounting_ref'], label='accounting_ref')}"
        }
    if version == 3:
        expected = {"positions_ref", "pricing_ref", "decision_clocks_ref"}
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError(
                "sequenced inputs must contain positions_ref, pricing_ref and decision_clocks_ref"
            )
        return {
            name: f"artifact://sha256/{artifact_digest(ref, label=name)}"
            for name, ref in value.items()
        }
    if not isinstance(value, dict) or set(value) not in (
        {"positions_ref", "pricing_ref", "periods_ref"},
        {"positions_ref", "pricing_ref", "periods_ref", "intraday_bars_ref"},
    ):
        raise ValueError(
            "inputs must contain positions_ref, pricing_ref, periods_ref "
            "and optional intraday_bars_ref"
        )
    return {
        name: f"artifact://sha256/{artifact_digest(ref, label=name)}"
        for name, ref in value.items()
    }


def _require_string_fields(
    values: dict[str, Any], names: tuple[str, ...], *, label: str
) -> None:
    for name in names:
        if (
            not isinstance(values[name], str)
            or not values[name].strip()
            or len(values[name]) > 128
        ):
            raise ValueError(f"{label}.{name} must be a non-empty string")


def _require_boolean_fields(
    values: dict[str, Any], names: tuple[str, ...], *, label: str
) -> None:
    for name in names:
        if type(values[name]) is not bool:
            raise ValueError(f"{label}.{name} must be boolean")


def _validate_position_config(config: dict[str, Any]) -> None:
    _require_string_fields(config, ("price_col",), label="config")
    for name in ("entry_price_col", "exit_price_col", "tradable_col"):
        value = config[name]
        if value is not None and (
            not isinstance(value, str) or not value.strip() or len(value) > 128
        ):
            raise ValueError(f"config.{name} must be a non-empty string or null")
    if (
        isinstance(config["transaction_cost_bps"], bool)
        or not isinstance(config["transaction_cost_bps"], (int, float))
        or not isfinite(config["transaction_cost_bps"])
        or config["transaction_cost_bps"] < 0
    ):
        raise ValueError("config.transaction_cost_bps must be a non-negative number")
    if (
        type(config["trading_days_per_year"]) is not int
        or config["trading_days_per_year"] <= 0
    ):
        raise ValueError("config.trading_days_per_year must be a positive integer")
    _require_boolean_fields(
        config, ("long_only", "preserve_gross_exposure"), label="config"
    )
    if not isinstance(config["exit_price_policy"], str) or config[
        "exit_price_policy"
    ] not in {"period", "strict", "ffill", "delay"}:
        raise ValueError("config.exit_price_policy is unsupported")
    if not isinstance(config["exit_fallback_policy"], str) or config[
        "exit_fallback_policy"
    ] not in {"ffill", "none"}:
        raise ValueError("config.exit_fallback_policy is unsupported")


def _validate_ledger_numeric_fields(ledger_config: dict[str, Any]) -> None:
    for name in (
        "portfolio_value",
        "participation_rate",
        "liquidity_notional_multiplier",
    ):
        value = ledger_config[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or value <= 0
        ):
            raise ValueError(
                f"execution.ledger_config.{name} must be a positive finite number"
            )
    for name in ("buy_max_days", "sell_max_days"):
        if type(ledger_config[name]) is not int or ledger_config[name] <= 0:
            raise ValueError(
                f"execution.ledger_config.{name} must be a positive integer"
            )
    for name in ("zero_fill_abort_days_buy", "round_lot"):
        value = ledger_config[name]
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError(
                f"execution.ledger_config.{name} must be a positive integer or null"
            )
    if (
        isinstance(ledger_config["lot_tolerance"], bool)
        or not isinstance(ledger_config["lot_tolerance"], (int, float))
        or not isfinite(ledger_config["lot_tolerance"])
        or ledger_config["lot_tolerance"] < 0
    ):
        raise ValueError(
            "execution.ledger_config.lot_tolerance must be a non-negative finite number"
        )


def _validate_ledger_behavior_fields(ledger_config: dict[str, Any]) -> None:
    if ledger_config["unfilled_buy_action"] != "keep_cash":
        raise ValueError(
            "execution.ledger_config.unfilled_buy_action must be keep_cash"
        )
    if ledger_config["unfilled_sell_action"] != "keep_position":
        raise ValueError(
            "execution.ledger_config.unfilled_sell_action must be keep_position"
        )
    if (
        not isinstance(ledger_config["liquidity_cols"], list)
        or not ledger_config["liquidity_cols"]
        or len(ledger_config["liquidity_cols"]) > 32
        or any(
            not isinstance(col, str) or not col.strip() or len(col) > 128
            for col in ledger_config["liquidity_cols"]
        )
    ):
        raise ValueError(
            "execution.ledger_config.liquidity_cols must be an array of strings"
        )
    for name in ("limit_up_col", "limit_down_col", "listing_status_col"):
        value = ledger_config[name]
        if value is not None and (
            not isinstance(value, str) or not value.strip() or len(value) > 128
        ):
            raise ValueError(
                f"execution.ledger_config.{name} must be a non-empty string or null"
            )


def _validate_ledger_config(ledger_config: dict[str, Any]) -> None:
    _require_boolean_fields(
        ledger_config,
        ("enabled", "enforce_t1", "enforce_price_limits", "enforce_listing_status"),
        label="execution.ledger_config",
    )
    _validate_ledger_numeric_fields(ledger_config)
    _validate_ledger_behavior_fields(ledger_config)


def _validate_configs(mapping: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    config = _exact_keys(mapping["config"], _CONFIG_KEYS, label="config")
    execution = _exact_keys(mapping["execution"], _EXECUTION_KEYS, label="execution")
    ledger_config = _exact_keys(
        execution["ledger_config"], _LEDGER_CONFIG_KEYS, label="execution.ledger_config"
    )
    if (
        type(execution["ledger"]) is not bool
        or type(ledger_config["enabled"]) is not bool
    ):
        raise ValueError(
            "execution.ledger and execution.ledger_config.enabled must be boolean"
        )
    if execution["ledger"] != ledger_config["enabled"]:
        raise ValueError("execution.ledger must match execution.ledger_config.enabled")
    if type(execution["allow_stale_execution_price"]) is not bool:
        raise ValueError("execution.allow_stale_execution_price must be boolean")
    if execution["intraday_execution_assumption"] is not None and not isinstance(
        execution["intraday_execution_assumption"], str
    ):
        raise ValueError(
            "execution.intraday_execution_assumption must be a string or null"
        )
    if execution["intraday_execution_assumption"] not in {
        None,
        "signal_before_session",
        "caller_windowed",
    }:
        raise ValueError("execution.intraday_execution_assumption is unsupported")
    if (
        config["exit_price_policy"] == "ffill"
        and not execution["allow_stale_execution_price"]
    ):
        raise ValueError("ffill exit pricing requires explicit stale-price opt-in")
    if ("intraday_bars_ref" in mapping["inputs"]) != (
        execution["intraday_execution_assumption"] is not None
    ):
        raise ValueError(
            "intraday bars and their execution assumption must be supplied together"
        )
    _validate_position_config(config)
    _validate_ledger_config(ledger_config)
    return dict(config), {
        "ledger": execution["ledger"],
        "ledger_config": dict(ledger_config),
        "allow_stale_execution_price": execution["allow_stale_execution_price"],
        "intraday_execution_assumption": execution["intraday_execution_assumption"],
    }


def _validate_budgets(value: Any) -> dict[str, int]:
    budgets = _exact_keys(value, {"wall_seconds", "memory_mb"}, label="budgets")
    for name, low, high in (("wall_seconds", 1, 86400), ("memory_mb", 256, 65536)):
        number = budgets[name]
        if type(number) is not int or not low <= number <= high:
            raise ValueError(f"budgets.{name} must be an integer from {low} to {high}")
    return dict(budgets)


def _validate_sequenced_config(
    mapping: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    config = _exact_keys(mapping["config"], _SEQUENCED_CONFIG_KEYS, label="config")
    _require_string_fields(config, ("price_col",), label="config")
    for name in _SEQUENCED_CONFIG_KEYS - {"price_col", "transaction_cost_bps"}:
        value = config[name]
        if value is not None and (
            not isinstance(value, str) or not value.strip() or len(value) > 128
        ):
            raise ValueError(f"config.{name} must be a non-empty string or null")
    cost = config["transaction_cost_bps"]
    if (
        isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not isfinite(cost)
        or cost < 0
    ):
        raise ValueError("config.transaction_cost_bps must be non-negative and finite")
    execution = _exact_keys(mapping["execution"], {"ledger_config"}, label="execution")
    ledger = _exact_keys(
        execution["ledger_config"], _LEDGER_CONFIG_KEYS, label="execution.ledger_config"
    )
    _validate_ledger_config(ledger)
    if not ledger["enabled"]:
        raise ValueError("sequenced execution requires an enabled ledger")
    return dict(config), {"ledger_config": dict(ledger)}


def _validate_accounting_config(
    mapping: dict[str, Any],
) -> tuple[dict[str, float], dict[str, Any]]:
    config = _exact_keys(mapping["config"], _ACCOUNTING_CONFIG_KEYS, label="config")
    for name, value in config.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or value < 0
        ):
            raise ValueError(f"config.{name} must be non-negative and finite")
    execution = _exact_keys(mapping["execution"], set(), label="execution")
    return {name: float(value) for name, value in config.items()}, execution


def _validate_backend_config(
    mapping: dict[str, Any], version: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    if version == 3:
        return _validate_sequenced_config(mapping)
    if version == 4:
        return _validate_accounting_config(mapping)
    return _validate_configs(mapping)


@dataclass(frozen=True, slots=True)
class BacktestJobRequest:
    """Validated, versioned immutable request stored in the job ledger."""

    schema_version: int
    idempotency_key: str
    backend: str
    evidence_tier: str
    request_sha256: str
    _request_json: str

    @property
    def inputs(self) -> dict[str, str | None]:
        return json.loads(self._request_json)["inputs"]

    @property
    def config(self) -> dict[str, Any]:
        return json.loads(self._request_json)["config"]

    @property
    def execution(self) -> dict[str, Any]:
        return json.loads(self._request_json)["execution"]

    @property
    def budgets(self) -> dict[str, int]:
        return json.loads(self._request_json)["budgets"]

    @property
    def research_clock(self) -> dict[str, Any] | None:
        return self.to_mapping().get("research_clock")

    @property
    def producer(self) -> dict[str, Any] | None:
        return self.to_mapping().get("producer")

    @classmethod
    def from_mapping(cls, value: Any) -> BacktestJobRequest:
        expected = {
            "schema_version",
            "idempotency_key",
            "inputs",
            "backend",
            "evidence_tier",
            "config",
            "execution",
            "budgets",
        }
        if not isinstance(value, dict):
            raise TypeError("request must be a JSON object")
        version = value.get("schema_version")
        if type(version) is int and version == 2:
            expected |= {"research_clock", "producer"}
        mapping = _exact_keys(value, expected, label="request")
        key, evidence_tier = _validate_request_identity(mapping)
        inputs = _validate_inputs(mapping["inputs"], version=version)
        config, execution = _validate_backend_config(mapping, version)
        budgets = _validate_budgets(mapping["budgets"])
        if version == 2:
            from research_contracts import ProducerIdentity, validate_research_clock

            if not execution["ledger"]:
                raise ValueError("execution-aware jobs require execution.ledger=true")
            clock = mapping["research_clock"]
            if not isinstance(clock, dict):
                raise ValueError("research_clock must be an object")
            validate_research_clock(clock, require_execution=True)
            producer = mapping["producer"]
            if not isinstance(producer, dict):
                raise ValueError("producer must be an object")
            ProducerIdentity.from_mapping(producer)
            if producer.get("backend") != mapping["backend"]:
                raise ValueError("producer.backend must match backend")
        normalized = {
            "schema_version": version,
            "idempotency_key": key,
            "inputs": inputs,
            "backend": mapping["backend"],
            "evidence_tier": evidence_tier,
            "config": config,
            "execution": execution,
            "budgets": budgets,
        }
        if version == 2:
            normalized["research_clock"] = clock
            normalized["producer"] = producer
        request_json = _canonical_json(normalized)
        request_bytes = request_json.encode("utf-8")
        if len(request_bytes) > 64 * 1024:
            raise ValueError("serialized backtest job request must not exceed 64 KiB")
        digest = hashlib.sha256(request_bytes).hexdigest()
        return cls(
            schema_version=version,
            idempotency_key=key,
            backend=mapping["backend"],
            evidence_tier=evidence_tier,
            request_sha256=digest,
            _request_json=request_json,
        )

    def to_mapping(self) -> dict[str, Any]:
        return json.loads(self._request_json)
