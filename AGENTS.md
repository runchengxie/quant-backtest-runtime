# quant-backtest-runtime

This repository owns generic backtest Job request validation, SQLite lifecycle, workers, artifact verification, result publication, CLI, and deployment. Private strategies and research promotion remain in quant-research; reusable execution and bundle contracts remain in quant-platform.

Develop in a dedicated branch and worktree created from origin/main. Use locked dependencies and run pytest, Ruff, and an end-to-end worker check before each PR. Merge to main only after review and required checks. Deploy immutable commit-addressed releases; store databases, artifacts, results, logs, and credentials outside the release tree. Never switch production callers to a development worktree.
