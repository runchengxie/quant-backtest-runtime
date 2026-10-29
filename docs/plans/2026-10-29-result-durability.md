# Result publication durability

Approved scope: persist staged payloads and the publication namespace before SQLite may record `SUCCEEDED`; preserve request/result schemas, lease fencing, integrity checks and recovery.

The existing writer closed files and renamed its staging directory without persistence barriers. The implementation uses a small POSIX filesystem helper: sync root ancestry, sync every staged file and directories bottom-up, rename, sync the publication parent, then call the existing conditional completion. Unsupported directory syncing and all I/O errors propagate; an orphan after rename remains recoverable through the existing lease-expiry path. Filesystem barriers are not a universal hardware guarantee.

Implementation and validation plan:

1. Add behavioral fault tests and verify failure against the existing writer.
2. Add persistence barriers without changing the completion/state contract.
3. Preserve and run stale-worker, tampering, rename and state-commit recovery tests; check nested payload ordering and sync failures.
4. Run locked Ruff, ty, coverage, synthetic CLI, dependency audit, and documentation checks before committing.

The first test run produced four intended failures: the ordering assertion and the file, staging-directory, and publication-parent sync fault cases. The pre-existing rename fault case passed. Final validation is recorded in the task delivery report.

Validation on Python 3.13.14 against source base `2a2dc0a` and locked platform revision `15ec62b66e894395d694049eaaace931de4a1265`:

- Ruff lint and format, ty, and `git diff --check`: passed.
- Full pytest suite under coverage: 56 passed; combined statement/branch coverage 80% (threshold 75%).
- Synthetic CLI: `SUCCEEDED`, with result retrieval validated.
- Strict MkDocs build and locale navigation check: passed; generated site kept outside source.
- Dependency audit: failed on the existing locked transitive dependency `msgpack==1.2.2`, reported as CVE-2026-114613 (fix version 1.2.3). This bounded change does not modify dependencies. The three local/Git project packages were unavailable for PyPI vulnerability auditing.

The audit limitation must be resolved before delivery is treated as having all development checks passing. No production state or inputs were used.
