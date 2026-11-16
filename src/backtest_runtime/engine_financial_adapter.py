"""Trusted Go-only Engine adapter: computes and writes staging files only."""

import argparse
import base64
import importlib.metadata
import json
import sys
from importlib import import_module
from pathlib import Path

from .engine_financial_protocol import decode_checkpoint, digest, installed_identity
from .engine_financial_sessions import create
from .session_stream_io import canonical, parse
from .session_stream_journal import read_file


def prepare(request, artifact_root, runtime_source=None, runtime_binary=None):
    artifacts = import_module("quant_backtest_core.artifacts")
    read_artifact = artifacts.read_artifact
    product = import_module("quant_backtest_engine.product")
    ArtifactRef, loads_manifest = product.ArtifactRef, product.loads_manifest

    sha = request["inputs"]["product_manifest_ref"].removeprefix("artifact://sha256/")
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("invalid product manifest reference")
    raw = read_file(artifact_root / "sha256" / sha, 1 << 20)
    if digest(raw) != sha:
        raise ValueError("product manifest digest mismatch")
    manifest = loads_manifest(raw.decode())
    if manifest.output_uri.split("/", 1)[0] in {
        "candidate.json",
        "manifest.json",
        "financial.json",
    }:
        raise ValueError("reserved Runtime staging output path")
    if manifest.backend_id != "python.minute":
        raise ValueError("v9 requires python.minute")
    inputs = {
        key: digest(read_artifact(ref, artifact_root)) for key, ref in manifest.inputs
    }
    inputs["calendar"] = digest(read_artifact(manifest.calendar, artifact_root))
    for key, ref in manifest.tapes:
        if isinstance(ref, ArtifactRef):
            inputs["tape:" + key] = digest(read_artifact(ref, artifact_root))
    source = installed_identity(runtime_source, runtime_binary)
    binding = digest(
        canonical(
            dict(
                manifest_sha256=sha,
                inputs=inputs,
                config=request["config"],
                source=source,
            )
        )
    )
    return dict(
        manifest=manifest,
        binding=binding,
        source=source,
        manifest_sha256=sha,
        product_manifest_sha256=digest(
            artifacts.json_bytes(product.manifest_to_json(manifest))
        ),
    )


def preflight(request, artifact_root, runtime_source=None, runtime_binary=None):
    from tempfile import TemporaryDirectory

    prepared = prepare(request, artifact_root, runtime_source, runtime_binary)
    with TemporaryDirectory(prefix="engine-preflight-") as stage:
        create(prepared["manifest"], artifact_root, stage)
    result = dict(
        schema="quant.backtest_plan.v1",
        request=request,
        backend=request["backend"],
        financial_binding=prepared["binding"],
        product_manifest_sha256=prepared["product_manifest_sha256"],
        checks=dict(
            input_hashes="passed",
            financial_constructor="passed",
            historical_pit="not_checked",
            economic_validity="not_checked",
        ),
        effects=["job_insert", "checkpoint_admission", "result_publication"],
    )
    result["plan_sha256"] = digest(canonical(result))
    return result


def step(request, args):
    prepared = prepare(
        request, args.artifact_root, args.runtime_source, args.runtime_binary
    )
    binding = prepared["binding"]
    if binding != args.binding:
        raise ValueError("Engine source/config/input binding changed")
    checkpoint = None
    start = 0
    if args.checkpoint is not None:
        raw = read_file(args.checkpoint, 64 << 20)
        if digest(raw) != args.checkpoint_sha256:
            raise ValueError("admitted checkpoint digest mismatch")
        wrapper = parse(raw)
        checkpoint = decode_checkpoint(wrapper, binding)
        start = wrapper["cursor"]
        if start <= 0:
            raise ValueError("resume requires committed progress")
    session = create(
        prepared["manifest"], args.artifact_root, args.staging_path, checkpoint
    )
    observation = session.step()
    if observation is not None:
        # Keep the successful API-owned bytes; never checkpoint a poisoned session.
        checkpoint = session.checkpoint()
        value = dict(
            schema="runtime-engine-checkpoint.v1",
            job_id=args.job_id,
            attempt=args.token,
            token=args.token,
            binding=binding,
            cursor=observation.cursor,
            checkpoint_base64=base64.b64encode(checkpoint).decode(),
            checkpoint_sha256=digest(checkpoint),
        )
        raw = canonical(value)
        (args.staging_path / "candidate.json").write_bytes(raw)
        return dict(
            done=False,
            cursor=observation.cursor,
            binding=binding,
            sha256=digest(raw),
            start_cursor=start,
        )
    session.finish()
    # Product owner writes its complete result plus original ledger beneath staging.
    product = args.staging_path / prepared["manifest"].output_uri
    raw = product.read_bytes()
    (args.staging_path / "financial.json").write_bytes(raw)
    return dict(
        done=True,
        cursor=session.observe().cursor,
        binding=binding,
        sha256=digest(raw),
        start_cursor=start,
        product_result=prepared["manifest"].output_uri,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "step"))
    for name in (
        "request",
        "artifact-root",
        "result-root",
        "staging-path",
        "checkpoint",
        "runtime-binary",
    ):
        parser.add_argument(
            "--" + name,
            type=Path,
            required=name in {"request", "artifact-root", "result-root"},
        )
    for name in ("runtime-source", "binding", "job-id", "token", "checkpoint-sha256"):
        parser.add_argument("--" + name)
    args = parser.parse_args()
    try:
        request = parse(read_file(args.request, 65536))
        result = (
            preflight(
                request, args.artifact_root, args.runtime_source, args.runtime_binary
            )
            if args.operation == "plan"
            else step(request, args)
        )
        print(json.dumps(result, sort_keys=True, allow_nan=False))
    except (ImportError, importlib.metadata.PackageNotFoundError) as error:
        print(
            json.dumps(
                dict(
                    code="ENGINE_DEPENDENCY_UNAVAILABLE",
                    message=f"install locked engine extra: {error}",
                )
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from error
    except (ValueError, TypeError, KeyError, OSError) as error:
        print(
            json.dumps(dict(code="INVALID_REQUEST", message=str(error))),
            file=sys.stderr,
        )
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
