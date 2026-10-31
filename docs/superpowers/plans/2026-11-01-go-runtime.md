# Go Runtime Implementation Plan

> For agentic workers, use superpowers:subagent-driven-development or superpowers:executing-plans. Continue through validation and review under the user's standing authorization.

Goal is a Go runtime that owns job state and supervision, with optional stateless Python financial adapters.

Architecture uses separate protocol, SQLite and process packages, composed by a Go CLI. Runtime data remains outside source. Go 1.27.3 and pinned Go modules build Linux binaries.

Spec is [the migration design](../specs/2026-11-01-go-runtime.md).

## Global constraints

Preserve v1–v6 identities, decimal strings and existing result schemas. Python adapters cannot write SQLite or final results. No production cutover or strategy changes. Use synthetic fixtures and task-owned processes only.

## Review focus

Process identity must survive PID reuse checks. Cancellation must win over success. Invalid request structure, input hashes and supported static checks must fail before opening state. Both native and compatibility outputs need full result integrity checks. Queue limits count active worker processes until reaped.

## Tasks

- [x] Implement native protocol validation in `internal/protocol`, starting with request hash and financial tampering tests. Add parity cases against Python normalization. Run `go test ./internal/protocol`.
- [x] Implement `internal/store` using the existing SQLite schema, starting with competing submission, claim and cancel/finish tests. Run its tests with race detection.
- [x] Implement Linux process ownership in `internal/supervisor`, starting with timeout, output overflow, executable replacement and parent-death tests. Verify only owned processes are killed and every child is reaped.
- [x] Compose `internal/app` and `cmd/backtest-job`. Add a stateless Python adapter and tests proving it creates no registry and does not rename into the final result directory. Exercise v1–v6 jobs and fault paths.
- [x] Add bounded queue scheduling and restart tests. Add Go CI, release tooling and bilingual operational guidance. Run all existing Python gates and real alpha.2 integration, then review the complete branch.
- [ ] Commit, push, open a PR, validate its current base, merge after required checks, and remove only this task's branch/worktree. Preserve external validation records.
