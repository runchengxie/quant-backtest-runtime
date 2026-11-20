# Engine financial sessions (Go v9)

[中文页面](engine-financial-sessions.zh-CN.md)

Use v9 for new Engine-owned minute jobs. Install Runtime 0.2.0 with the `engine`
extra in a dedicated environment. Core `[minute]` and SDK are pinned to merged
`292a67aebe534769d9be7c9fea72bd94fc20c8f6`. The `legacy` extra retains Platform
`7a618bddd8fdeee5dbe37fe5b54a9698e0de5355`; the extras are mutually exclusive so
Engine's pandas constraint cannot silently replace legacy's locked pandas.

Only the Go CLI supports v9. Python CLI capabilities remain v1–v6. Go capabilities
advertise `engine_financial_session`, including the mandatory queue isolation.
Configure an absolute `--python-executable`; submitted requests cannot select a
module, executable, policy implementation, or shell command. Configured Rust
executables are ignored for v9.

The synthetic product manifest and calendar are under
`tests/fixtures/engine-financial/`. Put the manifest's exact bytes into the existing
`artifact-root/sha256/<digest>` layout and supply this request:

```json
{
  "schema_version": 9,
  "idempotency_key": "synthetic-engine-minute",
  "backend": "engine.minute_session_resume",
  "evidence_tier": "diagnostic",
  "inputs": {"product_manifest_ref": "artifact://sha256/<manifest-digest>"},
  "config": {
    "provider_revision": "292a67aebe534769d9be7c9fea72bd94fc20c8f6",
    "checkpoint_format": "engine-financial-checkpoint.v1",
    "policy": "target-weights.v1",
    "provider_binding_schema": "runtime-engine-binding.v1"
  },
  "execution": {},
  "budgets": {"wall_seconds": 120, "memory_mb": 2048}
}
```

Resolve product artifact paths relative to the configured artifact root. The provider
checks calendar, input and tape hashes and capabilities before construction. Runtime
binds the exact manifest, referenced input/tape/calendar bytes, request config,
installed core/SDK/dependency file hashes, Runtime Python source hashes, Python
interpreter bytes, Go executable bytes and declared source revision. Core/SDK
installation must identify the exact VCS revision or a reviewed wheel digest; installed
provider files must match their RECORD. Retain the original wheel at its installation
location for wheel-origin verification. This is local provenance verification, not a
signature or sandbox for untrusted code.

`plan` constructs a session without running it and rejects missing/incompatible
providers before creating the job database. The trusted adapter writes only staging
files. Go remains the sole database writer, checkpoint admission owner and final
publisher. The Engine alone computes financial ledgers and decimal/string clock
transport. Runtime does not recreate accounting.

## Recovery and isolation

**Use separate database, result root and worker environment for v9.** A retained
v8 scheduler cannot safely inspect a mixed queue: unsupported decoding may terminalize
an unknown row. Do not point it at the v9 database. A new selector cannot retrofit an
old binary. This release makes no production database migration or service cutover.

Go wraps the exact opaque owner bytes in `runtime-engine-checkpoint.v1`, with job,
attempt/token, binding, cursor and inner digest. Base64 preserves the exact
`engine-financial-checkpoint.v1` payload. v8's `runtime-minute-checkpoint.v1` and
Platform checkpoint identities remain separate. Successful step bytes are staged
before durable writing; an exception preserves the original failure and does not call
checkpoint on a poisoned session.

Durability order is candidate write, file fsync, no-replace rename, directory fsync,
SQLite admission, then acknowledgment. Files without admission are orphans, never
resume sources. An expired worker becomes FAILED. A successor with a new idempotency
key and `execution.resume_from` can use only the last admitted compatible checkpoint
of a failed, uncancelled predecessor. It receives a distinct token. Repeating a lost
submit acknowledgment returns the same successor. Cancellation, source/config/input
changes, changed checkpoint bytes and cross-provider reuse fail closed; restore with
the original full closure or restart from inputs.

`quant.engine_financial_job_result.v1` identifies the product result and a complete
path/size/SHA-256 inventory, including original owner ledgers. Result reads verify
inventory and job/request identity. Publication uses the existing durable rename and
token-fenced terminal CAS. The operator-only `__worker --financial-fault` test seam
supports `before_write`, `after_fsync`, `after_rename`, `after_admission`; public
commands reject it. The four-boundary suite sends real SIGKILL and compares full
uninterrupted/restored product tables and original ledgers.

See [development](development.md) for gates and [legacy recovery](legacy-recovery.md)
for retaining historical readers and closures.

Each trusted v9 child uses `-B` and a new, empty `-X pycache_prefix` directory, removed
after the child exits. This forces source execution even when a timestamp/size-valid
stale cache exists in the environment; `-B` alone would still read it. Runtime rejects
bytecode-only modules in the fixed provider/runtime/dependency packages. The process
regression first proves altered valid cached bytecode executes under ordinary `-B`,
then proves Go executes the original audited adapter source instead.

When deploying alongside an older Engine binding, use a separate queue database, input store and result store for this release. Resume existing jobs with their original Runtime and Engine installation.
