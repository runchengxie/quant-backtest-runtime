"""Read-only dependency preflight for the optional Python compatibility stack."""

from importlib.util import find_spec


class LegacyDependencyUnavailable(RuntimeError):
    """The operator must install the legacy extra before selecting this path."""

    code = "LEGACY_DEPENDENCY_UNAVAILABLE"


def require_legacy() -> None:
    """Check package availability without importing pandas or backend code.

    This is not a backend compatibility or request semantics validation.
    """
    if any(
        find_spec(name) is None
        for name in ("portfolio_backtester", "pandas", "research_contracts", "pyarrow")
    ):
        raise LegacyDependencyUnavailable(
            "Install quant-backtest-runtime[legacy] from the reviewed locked source "
            "(uv sync --locked --extra legacy) for v1-v4 jobs, legacy results, "
            "or quant_run_manifest_ref support"
        )


def require_request_dependencies(version: int, manifest_ref: str | None) -> None:
    if version in {1, 2, 3, 4} or manifest_ref is not None:
        require_legacy()
