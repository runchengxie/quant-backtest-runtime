"""One-shot local subprocess worker for durable backtest jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .jobs import BacktestJobRequest, BacktestJobService
from .store import JobStore, RegistryConflict

RESULT_SCHEMA = "ticknet.backtest_job_result.v1"


class JobCancelled(Exception):
    """Raised when the supervisor requests cooperative cancellation."""


class JobTimedOut(Exception):
    """Raised when the configured wall clock budget expires."""


def _apply_memory_limit(memory_mb: int) -> None:
    try:
        import resource

        limit = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ImportError, AttributeError, OSError, ValueError) as error:
        message = "RESOURCE_LIMIT_UNAVAILABLE: memory budget cannot be enforced"
        raise RuntimeError(message) from error


def _run_native_backend(
    request: BacktestJobRequest, service: BacktestJobService
) -> Any:
    if request.schema_version == 4:
        return _run_trade_accounting_backend(request, service)
    if request.schema_version == 3:
        return _run_sequenced_backend(request, service)
    import pandas as pd
    from portfolio_backtester import PositionBacktestConfig
    from portfolio_backtester.backends import (
        NativePositionReplayBackend,
        NativePositionReplayRequest,
    )
    from portfolio_backtester.execution_sim import ExecutionSimConfig

    inputs = {
        name: pd.read_parquet(service.resolve_artifact(ref))
        for name, ref in request.inputs.items()
        if ref is not None
    }
    if request.schema_version == 2:
        _validate_input_clock_dates(inputs, request.research_clock or {})
    config = PositionBacktestConfig(**request.config)
    execution_values: dict[str, Any] = dict(request.execution["ledger_config"])
    if isinstance(execution_values.get("liquidity_cols"), list):
        execution_values = {
            **execution_values,
            "liquidity_cols": tuple(execution_values["liquidity_cols"]),
        }
    backend_request = NativePositionReplayRequest(
        positions=inputs["positions_ref"],
        pricing=inputs["pricing_ref"],
        periods=inputs["periods_ref"],
        intraday_bars=inputs.get("intraday_bars_ref"),
        config=config,
        intraday_execution_assumption=request.execution[
            "intraday_execution_assumption"
        ],
        allow_stale_execution_price=request.execution["allow_stale_execution_price"],
        ledger=request.execution["ledger"],
        ledger_config=ExecutionSimConfig(**execution_values),
    )
    result = NativePositionReplayBackend().run(backend_request)
    result.validate()
    return result


def _run_trade_accounting_backend(
    request: BacktestJobRequest, service: BacktestJobService
) -> Any:
    import pandas as pd
    from portfolio_backtester.backends.trade_accounting import (
        TradeCostConfig,
        compute_trade_accounting_frame,
    )

    reference = request.inputs["accounting_ref"]
    if reference is None:
        raise ValueError("accounting_ref must be an artifact reference")
    frame = pd.read_parquet(service.resolve_artifact(reference))
    accounting = compute_trade_accounting_frame(
        frame, TradeCostConfig(**request.config)
    )
    return pd.DataFrame(
        [
            {
                "turnover": accounting.turnover,
                "commission": accounting.commission,
                "stamp_tax": accounting.stamp_tax,
                "slippage": accounting.slippage,
                "total_cost": accounting.total_cost,
            }
        ]
    )


def _run_sequenced_backend(
    request: BacktestJobRequest, service: BacktestJobService
) -> Any:
    import pandas as pd
    from portfolio_backtester.backends import (
        SequencedExecutionBackend,
        SequencedExecutionRequest,
    )
    from portfolio_backtester.execution_sim import ExecutionSimConfig

    inputs = request.inputs

    def resolve(name: str) -> Path:
        reference = inputs[name]
        if reference is None:
            raise ValueError(f"{name} must be an artifact reference")
        return service.resolve_artifact(reference)

    positions = pd.read_parquet(resolve("positions_ref"))
    pricing = pd.read_parquet(resolve("pricing_ref"))
    clocks_path = resolve("decision_clocks_ref")
    if clocks_path.stat().st_size > 1024 * 1024:
        raise ValueError("decision clocks artifact exceeds 1 MiB")
    clocks = json.loads(clocks_path.read_text(encoding="utf-8"))
    if not isinstance(clocks, dict) or not clocks:
        raise ValueError("decision clocks must be a nonempty JSON object")
    ledger_values = dict(request.execution["ledger_config"])
    ledger_values["liquidity_cols"] = tuple(ledger_values["liquidity_cols"])
    result = SequencedExecutionBackend().run(
        SequencedExecutionRequest(
            positions=positions,
            pricing=pricing,
            decision_clocks=clocks,
            config=ExecutionSimConfig(**ledger_values),
            **request.config,
        )
    )
    result.validate()
    return result


def _manifest_for_result(
    result: Any, *, job_id: str, request_sha256: str
) -> dict[str, Any]:
    from portfolio_backtester.backends import to_json_compatible

    result.validate()
    inventory = []
    for name, frame in sorted(result.frames().items()):
        filename = f"{name}.parquet"
        inventory.append({"path": filename, "rows": int(frame.shape[0])})
    return {
        "schema_version": RESULT_SCHEMA,
        "job_id": job_id,
        "request_sha256": request_sha256,
        "backend": result.backend_name,
        "capabilities": result.capabilities.to_mapping(),
        "summary": to_json_compatible(result.summary),
        "metadata": to_json_compatible(result.metadata),
        "inventory": inventory,
    }


def _frame_dates(frame: Any, column: str) -> list[date]:
    if frame.empty:
        return []
    if column not in frame:
        raise ValueError(f"execution ledger is missing {column}")
    dates = []
    for value in frame[column]:
        value_text = str(value)
        try:
            dates.append(date.fromisoformat(value_text))
        except ValueError as error:
            raise ValueError(
                f"invalid execution ledger {column}: {value_text}"
            ) from error
    return dates


def _validate_input_clock_dates(inputs: dict[str, Any], clock: dict[str, Any]) -> None:
    """A v2 Job represents one decision and its bounded execution window."""
    cutoff = datetime.fromisoformat(clock["information_cutoff_at"]).date()
    earliest = datetime.fromisoformat(clock["earliest_order_at"]).date()
    execution_end = datetime.fromisoformat(clock["execution_window_end_at"]).date()
    valuation = datetime.fromisoformat(clock["valuation_at"]).date()
    for frame_name in ("positions_ref", "periods_ref"):
        frame = inputs[frame_name]
        if any(day != cutoff for day in _frame_dates(frame, "rebalance_date")):
            raise ValueError(
                f"{frame_name} rebalance dates must match the decision cutoff date"
            )
        if any(
            not earliest <= day <= execution_end
            for day in _frame_dates(frame, "entry_date")
        ):
            raise ValueError(
                f"{frame_name} entry dates fall outside the research clock window"
            )
    if any(day > valuation for day in _frame_dates(inputs["periods_ref"], "exit_date")):
        raise ValueError("period exit dates fall after research clock valuation")
    if any(
        day > valuation for day in _frame_dates(inputs["pricing_ref"], "trade_date")
    ):
        raise ValueError("pricing contains future dates after research clock valuation")
    bars = inputs.get("intraday_bars_ref")
    if bars is not None and any(
        day > execution_end for day in _frame_dates(bars, "trade_date")
    ):
        raise ValueError("intraday bars fall after the research clock execution window")


def _validate_ledger_clock_dates(result: Any, clock: dict[str, Any]) -> None:
    """Check date coverage; native fill timestamps do not encode session time."""
    ledger = result.unified_ledger
    if ledger is None:
        raise ValueError("Execution-aware bundle requires a full execution ledger.")
    start = datetime.fromisoformat(clock["execution_window_start_at"]).date()
    end = datetime.fromisoformat(clock["execution_window_end_at"]).date()
    valuation = datetime.fromisoformat(clock["valuation_at"]).date()
    for frame, column in ((ledger.orders, "entry_date"), (ledger.fills, "trade_date")):
        if any(not start <= day <= end for day in _frame_dates(frame, column)):
            raise ValueError(
                "execution ledger dates fall outside the research clock window"
            )
    if any(
        not start <= day <= valuation
        for day in _frame_dates(ledger.daily_nav, "trade_date")
    ):
        raise ValueError("daily NAV dates fall outside the research clock window")


def _write_result(
    result: Any,
    *,
    job_id: str,
    request: BacktestJobRequest,
    result_root: Path,
) -> tuple[str, str]:
    result_root.mkdir(parents=True, exist_ok=True)
    final_dir = result_root / job_id
    if final_dir.exists():
        raise FileExistsError(f"result directory already exists: {job_id}")
    tmp_dir = Path(tempfile.mkdtemp(prefix=f".{job_id}.tmp-", dir=result_root))
    try:
        if request.schema_version == 4:
            path = tmp_dir / "accounting.parquet"
            result.to_parquet(path, index=False)
            manifest = {
                "schema_version": "quant.trade_accounting_result.v1",
                "job_id": job_id,
                "request_sha256": request.request_sha256,
                "backend": request.backend,
                "summary": {
                    name: float(result.iloc[0][name]) for name in result.columns
                },
                "inventory": [
                    {
                        "path": "accounting.parquet",
                        "rows": 1,
                        "sha256": _sha256_file(path),
                        "size_bytes": path.stat().st_size,
                    }
                ],
            }
        elif request.schema_version == 2:
            from portfolio_backtester.backends import (
                write_execution_aware_result_bundle,
            )

            _validate_ledger_clock_dates(result, request.research_clock or {})
            input_refs = [
                {"artifact_id": name, "sha256": ref.rsplit("/", 1)[-1]}
                for name, ref in sorted(request.inputs.items())
                if ref is not None
            ]
            write_execution_aware_result_bundle(
                tmp_dir / "bundle",
                result=result,
                run_id=job_id,
                research_clock=request.research_clock or {},
                producer=request.producer or {},
                configuration_sha256=request.request_sha256,
                input_refs=input_refs,
            )
            manifest = {
                "schema_version": "quant.backtest_job_result.v2",
                "job_id": job_id,
                "request_sha256": request.request_sha256,
                "bundle_manifest_path": "bundle/manifest.json",
                "bundle_manifest_sha256": _sha256_file(
                    tmp_dir / "bundle" / "manifest.json"
                ),
            }
        else:
            manifest = _manifest_for_result(
                result, job_id=job_id, request_sha256=request.request_sha256
            )
            for name, frame in result.frames().items():
                frame.to_parquet(tmp_dir / f"{name}.parquet", index=False)
            inventory = []
            for item in manifest["inventory"]:
                path = tmp_dir / item["path"]
                inventory.append(
                    {
                        **item,
                        "sha256": _sha256_file(path),
                        "size_bytes": path.stat().st_size,
                    }
                )
            manifest["inventory"] = inventory
        manifest_path = tmp_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                manifest, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
            )
            + "\n",
            encoding="utf-8",
        )
        manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        tmp_dir.replace(final_dir)
        return f"backtest-job://{job_id}", manifest_sha256
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def execute_job(
    job_id: str,
    *,
    registry_path: str | Path,
    artifact_root: str | Path,
    result_root: str | Path,
) -> int:
    previous_umask = os.umask(0o077)
    try:
        registry = JobStore(registry_path)
    except BaseException:
        os.umask(previous_umask)
        raise
    service = BacktestJobService(
        registry, artifact_root=artifact_root, result_root=result_root
    )
    try:
        row = registry.get_backtest_job(job_id)
        if row is None:
            return 2
        try:
            mapping = json.loads(row["request_json"])
            request = BacktestJobRequest.from_mapping(mapping)
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            service.fail_submitted(
                job_id,
                code="INVALID_PERSISTED_REQUEST",
                message=str(error),
            )
            return 1
        if request.request_sha256 != row["request_sha256"]:
            service.fail_submitted(
                job_id,
                code="REQUEST_HASH_MISMATCH",
                message="Persisted request JSON does not match its recorded fingerprint.",
            )
            return 1
        lease_seconds = request.budgets["wall_seconds"] + 30
        if (
            service.claim(job_id, worker_pid=os.getpid(), lease_seconds=lease_seconds)
            is None
        ):
            return 0
        return _execute_claimed_job(
            job_id,
            request=request,
            service=service,
            result_root=Path(result_root).expanduser().resolve(),
        )
    finally:
        registry.close()
        os.umask(previous_umask)


def _execute_claimed_job(
    job_id: str,
    *,
    request: BacktestJobRequest,
    service: BacktestJobService,
    result_root: Path,
) -> int:
    def _cancel(_signum: int, _frame: Any) -> None:
        raise JobCancelled

    def _timeout(_signum: int, _frame: Any) -> None:
        raise JobTimedOut

    previous_int = signal.signal(signal.SIGINT, _cancel)
    previous_term = signal.signal(signal.SIGTERM, _cancel)
    previous_alarm = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(request.budgets["wall_seconds"])
    try:
        _apply_memory_limit(request.budgets["memory_mb"])
        result = _run_native_backend(request, service)
        return _publish_job_result(
            result,
            job_id=job_id,
            request=request,
            service=service,
            result_root=result_root,
        )
    except JobCancelled:
        return _handle_cancel(job_id, service=service, result_root=result_root)
    except JobTimedOut:
        return _handle_timeout(job_id, service=service, result_root=result_root)
    except Exception as error:  # noqa: BLE001 - terminal Job state records backend failures
        return _handle_failure(job_id, error, service=service, result_root=result_root)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGALRM, previous_alarm)


def _publish_job_result(
    result: Any,
    *,
    job_id: str,
    request: BacktestJobRequest,
    service: BacktestJobService,
    result_root: Path,
) -> int:
    result_ref, result_sha256 = _write_result(
        result,
        job_id=job_id,
        request=request,
        result_root=result_root,
    )
    try:
        service.complete(job_id, result_ref=result_ref, result_sha256=result_sha256)
    except RegistryConflict:
        current = service.get_status(job_id)
        if current.status == "CANCELLED" or current.cancel_requested_at is not None:
            shutil.rmtree(result_root / job_id, ignore_errors=True)
            if current.status == "RUNNING":
                service.acknowledge_cancel(job_id)
            return 130
        if current.status != "SUCCEEDED":
            shutil.rmtree(result_root / job_id, ignore_errors=True)
        raise
    return 0


def _handle_cancel(
    job_id: str, *, service: BacktestJobService, result_root: Path
) -> int:
    try:
        status = service.acknowledge_cancel(job_id)
    except RegistryConflict:
        status = service.get_status(job_id)
    if status.status == "SUCCEEDED":
        return 0
    shutil.rmtree(result_root / job_id, ignore_errors=True)
    return 130 if status.status == "CANCELLED" else 1


def _handle_timeout(
    job_id: str, *, service: BacktestJobService, result_root: Path
) -> int:
    try:
        status = service.fail(
            job_id, code="JOB_TIMEOUT", message="Wall clock budget exceeded."
        )
    except RegistryConflict:
        status = service.get_status(job_id)
    if status.status == "SUCCEEDED":
        return 0
    shutil.rmtree(result_root / job_id, ignore_errors=True)
    return 124 if status.status == "FAILED" else 1


def _handle_failure(
    job_id: str,
    error: Exception,
    *,
    service: BacktestJobService,
    result_root: Path,
) -> int:
    current = service.get_status(job_id)
    if current.status in {"FAILED", "CANCELLED", "SUCCEEDED"}:
        if current.status != "SUCCEEDED":
            shutil.rmtree(result_root / job_id, ignore_errors=True)
        return 1
    service.fail(job_id, code=_error_code(error), message=str(error)[:2000])
    shutil.rmtree(result_root / job_id, ignore_errors=True)
    return 1


def _error_code(error: Exception) -> str:
    if isinstance(error, FileNotFoundError):
        return "INPUT_ARTIFACT_MISSING"
    if isinstance(error, ValueError) and "CAPABILITY_MISMATCH" in str(error):
        return "CAPABILITY_MISMATCH"
    if isinstance(error, RuntimeError) and str(error).startswith(
        "RESOURCE_LIMIT_UNAVAILABLE"
    ):
        return "RESOURCE_LIMIT_UNAVAILABLE"
    if isinstance(error, ValueError):
        return "BACKTEST_REJECTED"
    return "BACKEND_EXECUTION_FAILED"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one durable backtest job")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--result-root", required=True)
    args = parser.parse_args(argv)
    return execute_job(
        args.job_id,
        registry_path=args.registry,
        artifact_root=args.artifact_root,
        result_root=args.result_root,
    )


if __name__ == "__main__":
    sys.exit(main())
