# Session process channel implementation plan

The authorized design adds an opt-in, Linux, standard-library Python API over the
engine session stream. Existing queued jobs and Go controls remain unchanged.
A held, SHA-256 verified regular executable is launched through a small fresh
Python helper that installs parent-death SIGKILL before exec. Selectors supervise
bounded NDJSON pipes with deadline/cancellation checks. Each session is submitted
and acknowledged before the next verified partition row is delivered.

Reports are provisional. Bind each current report to its session input while its
quotes remain available, accumulate output records only, then independently
reconcile the complete result and compare it exactly with the engine finish.
Recovery binds input/config/binary/runtime identities in an integrity-digested
journal and always replays every input into a fresh engine. Durable publications
use deterministic content paths and exact-byte deduplication under an exclusive
operator output-root lock. This is replay recovery, not a financial snapshot.

- [x] Write failing process/framing and two-session runner/replay tests.
- [x] Implement strict framing, bounded selector supervision and helper process.
- [x] Implement report binding and final reconciliation using existing validators.
- [x] Implement atomic publication, journal integrity and full-prefix replay.
- [x] Exercise malformed protocol, failure/cancel/deadline, corruption and replay.
- [x] Document English/Chinese usage and boundaries; run locked repository gates.
- [x] Record exact provider/runtime revisions and binary SHA; commit for integration.
