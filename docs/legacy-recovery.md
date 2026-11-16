# Legacy recovery closures

[中文页面](legacy-recovery.zh-CN.md)

Keep historical recovery environments immutable. v9 is additive: it does not upgrade
v8 Platform checkpoints, v1–v4 result readers, v7 lease-expiry terminal behavior or
native Nautilus validators. No checkpoint may be rebound to a new provider or source.

Retain a private SOURCE inventory with full Runtime revision/source archive and wheel,
Go toolchain and executable SHA-256, Python interpreter bytes and ABI, Platform/core/
SDK VCS identities and wheels, all installed dependency files and versions, lock hash,
Rust revision/executable when used, fixture/input hashes and exact validation commands.
A package version, Git revision string or old provider wheel alone is insufficient.
Keep credentials out of receipts. Retain artifact origins needed by provider verification.
Store receipts and generated job state under the owning project's data contract,
never in the source tree or production state for a test.

The retained v8 Runtime baseline is `01ed4c34f955ef6cd39d13aa073b6d61b610d321`,
Runtime 0.1.0, Go 1.27.3, Platform/research-contracts
`7a618bddd8fdeee5dbe37fe5b54a9698e0de5355`. Its original `uv.lock` remains the
recovery authority. Current Runtime 0.2.0 keeps a separate legacy resolution; a new
Engine environment does not substitute for the historical closure. Dedicated tests
run the unchanged four-boundary v8 suite against the retained old wheel/binary, then
against the new Runtime with its pinned legacy provider. Distinct task-only stores
protect v9 from old schedulers.

| Protocol | Owner and recovery requirement |
| --- | --- |
| v1–v4 | Pinned Platform, historical artifact readers and immutable golden bytes |
| v5–v6 | Exact Rust executable and strict native result identity validation |
| v7 | Exact stream provider; lease expiry remains terminal, no implicit resume |
| v8 | Platform minute-session.v2 and original Runtime closure |
| v9 | Engine public session API, exact owner checkpoint bytes and new closure |

Before retiring any closure, prove synthetic restore and complete ledger equality with
its recorded binary, interpreter and dependencies. Preserve historical receipts and
identifiers even after current consumers move forward. No production cutover follows
from synthetic acceptance.
