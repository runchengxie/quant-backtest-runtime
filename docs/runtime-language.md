# Runtime language direction

[中文页面](runtime-language.zh-CN.md)

Go implements CLI lifecycle management, SQLite state, persistent single-machine scheduling, process supervision and result publication. Rust remains the independently released backtest engine. Python remains the optional compatibility adapter for existing financial computation and contracts. Production cutover has not been performed.

## Current ownership

| Component | Owner |
| --- | --- |
| Job claims, leases, cancellation and recovery | Go runtime |
| Queue scheduling and explicit concurrency limit | Go runtime |
| Final artifact verification and publication | Go runtime |
| v5/v6 native execution | Independent Rust engine, supervised by Go |
| Legacy financial computation and normalization | Optional stateless Python adapter |
| Strategies, research provenance and promotion rules | Research repository |

Go's native v5/v6 path runs without Python. Legacy operations use an explicitly configured interpreter with `quant-backtest-runtime[legacy]` installed. The adapter writes staged artifacts and returns computation results; it does not manage SQLite or publish final directories. The existing Python CLI remains available for compatibility and rollback, with exclusive ownership of any database assigned to it.

Ordinary `submit` immediately starts a detached worker. Queue scheduling uses `submit --queue` and `serve --concurrency N`; use a dedicated queue database and one scheduler. Do not mix these modes. The limit covers scheduler-managed workers, and a slot is held through process reaping. See [Go runtime usage](go-runtime.md) for commands and deployment steps.

## Process and protocol boundaries

The runtime opens and hashes executable bytes, then executes through the held descriptor. A reexec helper applies Linux memory limits immediately before execution. Parent-death signals, explicit process-group termination, bounded output and reaping cover child lifecycle. Worker cancellation and recovery establish identity through pidfds and exact worker arguments. These controls manage trusted workloads and do not constitute an untrusted-code sandbox.

Go verifies engine results and financial evidence without implementing order matching or portfolio construction. Cross-language fixtures check canonical request bytes and SHA-256 identity. Keep decimal monetary values exact. Existing request and result schemas remain the compatibility contract; fixture coverage alone does not prove every historical workload is supported.

## Deployment status and remaining evidence

The implementation is available for validation on separate synthetic databases. No production database or schedule has been switched. Before cutover, quiesce Python workers and submissions, back up state and artifacts, and record the tested runtime, engine and adapter revisions. Go must become the sole lifecycle owner. Rollback requires quiescing Go and confirming no active workers remain before restoring Python ownership.

Lifecycle tests exercise queue persistence, bounded concurrency, cancellation, process cleanup and publication integrity. Continue to check deployment-specific historical fixtures and failure recovery before assigning production ownership. Keep the Python deployment available until those checks pass.

No controlled Python-versus-Go benchmark establishes a reduction in total backtest time. Record scheduler overhead separately from engine execution, alongside peak memory and cancellation latency. Multiple-machine scheduling remains separate work requiring shared artifact storage, a distributed claim protocol and protection against expired workers publishing results.
