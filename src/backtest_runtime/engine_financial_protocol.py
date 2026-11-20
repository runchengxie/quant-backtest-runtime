"""Independent Engine provider identities and exact checkpoint byte transport."""

import base64
import csv
import hashlib
import importlib.metadata
import io
import json
import sys
from importlib import import_module
from importlib.util import find_spec, source_from_cache
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .session_stream_io import canonical

WHEELS = {
    "quant-backtest-core": "190194bbccde400d4240de5bd0007038e073315d266c84f4c0ddd139356ef3da",
    "quant-backtest-engine-client": "46d9632a3ecc29900908fa8ec3bdcbbc92552de47d0339d3171cd5dfdbcaf726",
}


def verify_provider(dist, direct, name):
    commit = direct.get("vcs_info", {}).get("commit_id")
    if commit != PROVIDER:
        url = urlsplit(direct.get("url", ""))
        if (
            url.scheme != "file"
            or digest(Path(unquote(url.path)).read_bytes()) != WHEELS[name]
        ):
            raise ValueError("install the exact merged Engine core/SDK provider")
    for path, checksum, _size in csv.reader(
        io.StringIO(dist.read_text("RECORD") or "")
    ):
        if checksum:
            algorithm, encoded = checksum.split("=", 1)
            raw = Path(dist.locate_file(path)).read_bytes()
            actual = (
                base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
                .decode()
                .rstrip("=")
            )
            if algorithm != "sha256" or actual != encoded:
                raise ValueError(
                    "installed provider file differs from distribution RECORD"
                )


PROVIDER = "292a67aebe534769d9be7c9fea72bd94fc20c8f6"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def require_source_modules():
    for name in (
        "backtest_runtime",
        "quant_backtest_core",
        "quant_backtest_engine",
        "numpy",
        "pandas",
        "dateutil",
        "tzdata",
    ):
        spec = find_spec(name)
        if spec is None:
            raise ModuleNotFoundError(name)
        if spec.origin is None or not spec.origin.endswith(".py"):
            raise ValueError(
                "financial provider packages require audited Python sources"
            )
        for root in spec.submodule_search_locations or ():
            for cached in Path(root).rglob("*.pyc"):
                try:
                    source = Path(source_from_cache(str(cached)))
                except ValueError:
                    source = cached.with_suffix(".py")
                if not source.is_file():
                    raise ValueError(
                        "bytecode-only financial provider module is unsupported"
                    )


def installed_identity(runtime_source, runtime_binary=None):
    require_source_modules()
    sources = {}
    for label, module in (
        ("core", "quant_backtest_core"),
        ("sdk", "quant_backtest_engine"),
    ):
        root = Path(str(import_module(module).__file__)).parent
        sources.update(
            {
                f"{label}/{p.relative_to(root).as_posix()}": digest(p.read_bytes())
                for p in sorted(root.rglob("*.py"))
            }
        )
    if (
        digest(canonical(sources) + b"\n")
        != "328c0a658cc2c8dd2b90d5991005f4a210fd880ca16dc1dec04e2866dd5eaec9"
    ):
        raise ValueError(
            "installed Engine core/SDK sources differ from pinned provider"
        )
    identities = {}
    for name in (
        "quant-backtest-core",
        "quant-backtest-engine-client",
        "numpy",
        "pandas",
        "python-dateutil",
        "tzdata",
    ):
        dist = importlib.metadata.distribution(name)
        files = {
            str(p): digest(Path(str(dist.locate_file(p))).read_bytes())
            for p in dist.files or ()
            if not str(p).endswith(".pyc") and Path(str(dist.locate_file(p))).is_file()
        }
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
        if name in WHEELS:
            verify_provider(dist, direct, name)
        identities[name] = dict(version=dist.version, files=files, origin=direct)
    root = Path(__file__).parent
    return dict(
        schema="runtime-engine-binding.v1",
        provider_revision=PROVIDER,
        runtime_revision=runtime_source,
        runtime_binary_sha256=digest(Path(runtime_binary).read_bytes())
        if runtime_binary
        else None,
        interpreter_sha256=digest(Path(sys.executable).read_bytes()),
        runtime={
            str(p.relative_to(root)): digest(p.read_bytes())
            for p in sorted(root.rglob("*.py"))
        },
        distributions=identities,
    )


def decode_checkpoint(value, binding):
    if value["schema"] != "runtime-engine-checkpoint.v1" or value["binding"] != binding:
        raise ValueError("Engine checkpoint binding/schema mismatch")
    raw = base64.b64decode(value["checkpoint_base64"], validate=True)
    inner = json.loads(raw)
    if (
        digest(raw) != value["checkpoint_sha256"]
        or inner["schema"] != "engine-financial-checkpoint.v1"
        or inner["cursor"] != value["cursor"]
    ):
        raise ValueError("Engine checkpoint bytes/schema mismatch")
    return raw
