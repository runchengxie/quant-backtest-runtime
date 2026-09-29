"""The locked platform dependency exposes every runtime backend we dispatch to."""

from portfolio_backtester.backends import SequencedExecutionBackend
from portfolio_backtester.backends.trade_accounting import (
    TradeCostConfig,
    compute_trade_accounting_frame,
)


def test_locked_platform_exposes_v3_and_v4_backends() -> None:
    assert SequencedExecutionBackend is not None
    assert TradeCostConfig is not None
    assert compute_trade_accounting_frame is not None
