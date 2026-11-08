"""Fixed Go-only MinuteSession adapter. No SQLite writes or checkpoint publication."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path

from .session_stream_io import canonical, parse
from .session_stream_journal import read_file

PROVIDER = "7a618bddd8fdeee5dbe37fe5b54a9698e0de5355"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def source_identity(runtime_source):
    import quant_platform

    distribution = importlib.metadata.distribution("quant-platform")
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    if direct.get("vcs_info", {}).get("commit_id") != PROVIDER:
        raise ValueError("install the exact locked Platform provider revision")
    identities = {"runtime_revision": runtime_source, "provider_revision": PROVIDER}
    for label, root in (
        ("platform", Path(quant_platform.__file__).parent),
        ("runtime", Path(__file__).parent),
    ):
        identities[label] = {
            str(p.relative_to(root)): digest(p.read_bytes())
            for p in sorted(root.rglob("*.py"))
        }
    identities["libraries"] = {
        name: importlib.metadata.version(name)
        for name in ("pandas", "numpy", "pyarrow")
    }
    return identities


def prepare(mapping, root, runtime_source):
    from .financial_sessions import create

    sha = mapping["inputs"]["minute_input_ref"].removeprefix("artifact://sha256/")
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("invalid input identity")
    raw = read_file(root / "sha256" / sha, 1 << 20)
    if digest(raw) != sha:
        raise ValueError("input artifact digest mismatch")
    data = parse(raw)
    session = create(data)
    binding = digest(
        canonical(
            dict(
                input_sha256=sha,
                config=mapping["config"],
                source=source_identity(runtime_source),
            )
        )
    )
    return data, session, binding


def preflight(mapping, root, runtime_source):
    _, _, binding = prepare(mapping, root, runtime_source)
    result = dict(
        schema="quant.backtest_plan.v1",
        request=mapping,
        backend=mapping["backend"],
        financial_binding=binding,
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


def step(mapping, args):
    from .financial_sessions import create, finish

    data, session, binding = prepare(mapping, args.artifact_root, args.runtime_source)
    if binding != args.binding:
        raise ValueError("financial source/config binding changed")
    start = 0
    if args.checkpoint is not None:
        raw = read_file(args.checkpoint, 64 << 20)
        if digest(raw) != args.checkpoint_sha256:
            raise ValueError("admitted checkpoint digest mismatch")
        prior = parse(raw)
        if (
            prior["schema"] != "runtime-minute-checkpoint.v1"
            or prior["binding"] != binding
        ):
            raise ValueError("checkpoint identity mismatch")
        session = create(data, prior["checkpoint"])
        start = session.cursor
        if start <= 0:
            raise ValueError("resume requires committed progress")
    if session.step():
        checkpoint = session.checkpoint()
        value = dict(
            schema="runtime-minute-checkpoint.v1",
            job_id=args.job_id,
            token=args.token,
            binding=binding,
            cursor=session.cursor,
            checkpoint=checkpoint,
        )
        raw = canonical(value)
        (args.staging_path / "candidate.json").write_bytes(raw)
        return dict(
            done=False,
            cursor=session.cursor,
            binding=binding,
            sha256=digest(raw),
            start_cursor=start,
        )
    raw = canonical(finish(session))
    (args.staging_path / "financial.json").write_bytes(raw)
    return dict(
        done=True,
        cursor=session.cursor,
        binding=binding,
        sha256=digest(raw),
        start_cursor=start,
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
        mapping = parse(read_file(args.request, 65536))
        result = (
            preflight(mapping, args.artifact_root, args.runtime_source)
            if args.operation == "plan"
            else step(mapping, args)
        )
        print(json.dumps(result, sort_keys=True, allow_nan=False))
    except (ImportError, importlib.metadata.PackageNotFoundError) as error:
        print(
            json.dumps(
                dict(
                    code="LEGACY_DEPENDENCY_UNAVAILABLE",
                    message=f"install locked legacy extra: {error}",
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
