# Queued partition session implementation plan

Authorized bounded design: Go-only job v7 selects `nautilus.partition_session_replay`,
with `engine_config_ref` and `partition_manifest_ref` immutable artifact inputs,
`config.executable_sha256`, empty execution and existing budgets. Operator CLI
`--partition-root` supplies an absolute approved input root. Requests cannot select
paths or executables. Python remains a stateless compute/verification subprocess.

Go owns SQLite, claims, cancellation, wall deadlines, memory controls, staging and
final publication. The existing supervisor executes the Python adapter, which uses
the held-inode session client and verified reader. Its private journal/report/result
files live only within the Go stage. Go verifies all staged inventory before final
rename and success CAS. Result reads recheck inventory and independently reconcile
reports against verified inputs through the existing financial validator.

Preflight reads at most 1 MiB each for config and partition manifest, validates
metadata and root, and performs start/ready negotiation without any sessions. It
has a 10-second and 256-MiB child policy. It does not scan partition bodies or prove
session financial semantics. Execution/verification reads every partition under the
job deadline. Limits are 4096 sessions/partitions, 256 MiB per partition, 16 GiB
aggregate declared bytes, 1 MiB per row, 64 MiB per report/result frame, 8 MiB journal.
Retained output is capped at 256 MiB including an 8-MiB journal reserve.
Ready proves decoding and protocol identity, with config financial semantics unchecked.
Outputs and financial account history grow with sessions. This is bounded policy,
not constant memory. Legacy v1-v6 normalization, fingerprints and behavior remain.

Alternatives considered: modifying v6 would change its accepted input contract.
A Go implementation of financial reconciliation would duplicate the existing owner.
The separate v7 adapter reuses established contracts and keeps lifecycle authority.

Queued restarts do not restore accounts. A restarted scheduler preserves live workers;
expired workers terminate and become failed/cancelled through existing lease recovery.
Standalone session replay remains a separate explicitly invoked API.

- [x] Pin v7 request validation/fingerprints and reject extra/path fields.
- [x] Implement bounded stateless preflight, staging and read-only verification.
- [x] Integrate Go CLI/worker/result inventory with existing lifecycle controls.
- [x] Test real engine, invalid metadata/payload, tamper, timeout/cancel/death/restart.
- [x] Document English/Chinese usage and limitations, run all repository gates.
