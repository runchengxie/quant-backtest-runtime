# Runtime language direction

[中文页面](runtime-language.zh-CN.md)

Keep the Python runtime in service. When development starts on a persistent scheduler, evaluate a Go prototype first. The Rust backtest engine stays independently released and communicates with the runtime through versioned JSON.

This record guides future implementation. This repository currently has no Go scheduler or Python-versus-Go benchmark results.

## Current implementation

`backtest-job submit` persists a job and directly starts an independent Python worker. The worker claims the job, invokes its backend and publishes results. SQLite stores state and leases. Cancellation and recovery check process identity.

v5/v6 jobs use a separately installed Rust engine. The runtime checks the executable digest and launches the executable through a held descriptor. It terminates and reaps the child on timeout. Linux parent-death signalling handles unexpected worker death. The base installation requires Python but no third-party Python packages. The `legacy` extra provides the older backends.

A persistent queue and a global concurrency limit remain to be implemented. Submitting many jobs can start many workers. Each job already has memory and wall-time limits. Global scheduling and resource budgets require explicit development.

## Why evaluate Go next

Goroutines can coordinate waiting jobs, while channels or semaphores bound active work. A context carries cancellation and deadlines. The prototype should establish whether maintainers find this implementation easier to understand and diagnose than extending Python.

Rust also fits runtime development, particularly components that require precise resource control. Go adds another maintained language and cross-language protocol checks. No measurements currently show that either choice materially reduces total backtest time.

| Approach | Useful investment | Remaining work |
| --- | --- | --- |
| Extend Python | Add single-machine scheduling using existing tests | Queueing, concurrency limits and recovery |
| Prototype Go scheduling | Compare persistent-service implementation and maintenance | Process control and protocol compatibility |
| Build a Rust runtime | Use one language for system components | State migration and recovery tests |

The Go documentation specifies that `exec.CommandContext` cancels by calling `Kill` on its associated process by default. Descendant cleanup needs an explicit design, and `Wait` releases command resources. Preserve executable digest and process identity checks. See [os/exec](https://pkg.go.dev/os/exec) and [Go concurrency](https://go.dev/doc/effective_go#concurrency).

## Prototype scope

Use a separate synthetic job database and result directory. Initially, a Go scheduler can invoke the existing Python CLI. Python continues to manage job state and publish results. Go decides when to submit another job and queries completion through the job ID.

The worker may still be running when `submit` returns. Release a concurrency slot only after the job reaches a terminal state and its processes have been cleaned up. After a scheduler restart, rediscover submitted jobs and use idempotency keys to handle a lost submission response. Invoke existing `cancel` and `recover` commands for cancellation and expired jobs. Direct Go writes to the same SQLite database require a separate design.

First exercise successful completion, queueing and scheduler restart. Then inject completion/cancellation races, unexpected worker death and interrupted result publication. Use task-owned databases and retain full logs and program revisions.

| Check | Acceptance evidence |
| --- | --- |
| Concurrency limit | Submit more jobs than the configured limit and continuously observe live workers staying within it |
| Restart and idempotency | Kill the scheduler before and after submission responses, then recover one valid result per job |
| Cancellation and reaping | Observe both worker and engine exit, with state consistent with actual processes |
| Protocol compatibility | Preserve existing v1–v6 fingerprints, errors and result checks on shared fixtures |
| Publication and recovery | Interrupted writes leave readers with complete, verified results only |
| Operating cost | Record scheduler overhead separately from engine time, plus peak memory and cancellation latency |

Measure the existing Python implementation first. Fix the machine, inputs and engine executable digest for the comparison. Set resource budgets and acceptable cancellation deadlines before using the results to decide on migration.

## Ownership transfer

First establish cross-language fixtures for JSON normalization, request SHA-256 and idempotency. Preserve decimal monetary strings without converting through binary floating-point values before hashing.

Before Go takes over state management, define the sole SQLite owner and change workers to compute, write staged artifacts and return execution results. Go can then manage claims, leases and final publication. Retain a rollback path and keep each job assigned to one state-management implementation during migration.

Evaluate multiple machines after single-machine scheduling is stable. Remote workers need shared artifact storage and a new claim protocol, including protection against expired workers publishing results.
