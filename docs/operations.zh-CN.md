# 发布与故障排查

[English page](operations.md)

## 目录约定

下面以 `/srv/quant-backtest-runtime` 为例。`releases/<sha>` 保存源码和虚拟环境，`current` 指向当前版本，`previous` 保存回滚目标。数据独立保存在 `data/jobs.sqlite`、`data/artifacts` 和 `data/results`，worker 日志位于 `data/results/.logs`。

运行时只接受自己的 SQLite 任务库。旧研究仓库的任务库结构不同，不能直接作为运行时数据库使用。

## 发布

下面的命令安装 Python 版本。发布 Go 版本时，使用 `stage COMMIT --implementation go`，按 [Go 使用指南](go-runtime.zh-CN.md)检查，再将调用路径改为 `current/bin/backtest-job`。Go 发布目录不会生成 `current/.venv/bin/backtest-job`，也不会自动安装旧协议适配器。切换实现前要先清点调用方。

PR 合并后，拉取远端并记录完整提交号。

```bash
git fetch origin
release_sha=$(git rev-parse origin/main)
production_root=/srv/quant-backtest-runtime
python scripts/release.py --repo . --production "$production_root" --dry-run stage "$release_sha"
python scripts/release.py --repo . --production "$production_root" stage "$release_sha"
```

`stage` 只接受已经进入 `origin/main` 历史的提交。它将源码放入固定版本目录，再安装锁定的运行依赖，成功后写入 `.release-ready`。现有同名目录不会被覆盖。安装失败可能留下没有 `.release-ready` 的目录，`switch` 会拒绝它，再次 `stage` 也不会覆盖。清理或重试前应保留并检查该失败目录。

使用待发布版本自身的脚本和可执行文件跑通合成数据，再切换。

```bash
"$production_root/releases/$release_sha/.venv/bin/python" \
  "$production_root/releases/$release_sha/scripts/smoke_backtest.py" \
  --executable "$production_root/releases/$release_sha/.venv/bin/backtest-job"
python scripts/release.py --repo . --production "$production_root" --dry-run switch "$release_sha"
python scripts/release.py --repo . --production "$production_root" switch "$release_sha"
```

生产调用使用 `current/.venv/bin/backtest-job` 并显式传入三个数据路径，无需设置 `PYTHONPATH`。运行中的 worker 继续使用启动时的解释器。清理旧版本前应确认没有进程仍在使用它。

回滚前可以先预演。

```bash
python scripts/release.py --repo . --production "$production_root" --dry-run rollback
python scripts/release.py --repo . --production "$production_root" rollback
```

`previous` 为符号链接时回滚到上一版本。首次安装没有上一版本时，回滚会移除 `current` 指针，数据目录保持原样。

回滚会使用上一版本的完整路径，也支持共享发布代次中的目录。工具先检查该目录的就绪标记和对应实现的可执行文件，通过后才修改指针。需要保留回滚能力时，应保留原发布代次及其 Python 环境。共享代次的指针由部署方统一协调。

## 故障排查

| 现象或错误码 | 检查方法 |
| --- | --- |
| `WORKER_START_FAILED` | 查看任务错误信息，检查解释器、文件权限和日志目录 |
| `WORKER_START_EXPIRED` | worker 启动后未能领取任务，检查 `.logs` 中的标准错误日志 |
| `JOB_TIMEOUT` | 检查输入规模和时间预算，以新幂等键提交调整后的请求 |
| `RESOURCE_LIMIT_UNAVAILABLE` | 检查 Linux 资源限制支持和运行权限 |
| `INPUT_ARTIFACT_MISSING` | 核对输入目录与请求中的哈希引用 |
| `BACKTEST_REJECTED` | 检查参数、输入数据和研究时钟范围 |
| `WORKER_LEASE_EXPIRED` | 检查进程是否异常退出，再确认恢复后的终态 |
| `RUNNING` 且已请求取消 | 查询状态，必要时执行 `recover`，核对进程权限和 `/proc` 可见性 |
| 结果哈希不匹配 | 保留损坏文件与日志排查来源，重新生成输入或任务，不手工改写结果清单 |

备份时使用 SQLite 支持的一致性备份方式，并同时保存输入与结果目录。备份和清理应由部署方安排，本仓库没有常驻备份服务。

## 发布故障恢复

结果目录发布与 SQLite 成功状态提交是两个边界。目录已重命名不等于结果成功，必须完成状态提交。租约恢复会保守处理未完成的任务，不会直接将该目录认定为成功结果。

重复恢复会重试已过期失败任务的输出清理，包括之前清理被中断的情况。无关目录和进程不在清理范围内。

## 离线备份与恢复

先暂停提交和调度，再等待 worker 退出，或明确取消任务并确认进程已退出。
使用恢复副本时不要重启原实例，两个运行时不能同时拥有同一份任务状态。
此流程保留排队任务和已完成结果，不会恢复运行中的引擎账户。

在发布目录之外创建空的备份目录。使用 Python 的 `sqlite3.Connection.backup`
或 SQLite 的 `.backup` 将数据库写为 `jobs.sqlite`，不要只复制 SQLite 主文件，
因为已提交的数据可能仍在 WAL 中。在所有写入方保持停止的期间，复制完整的
artifact 和 result 目录（含日志），以及另行保存的请求、计划和提交回执。
v7 还需复制分区目录并保留相对路径。保留权限；拒绝符号链接或明确记录链接，
不要默默跟随链接复制。

记录每个备份文件的相对路径、字节数和 SHA-256，并记录运行时源码版本、
可执行文件哈希、提供方版本与启动参数。清单放在被清点的目录之外。
复制到新的空恢复目录后，核对清单，并对恢复的 SQLite 运行
`PRAGMA integrity_check`。自行生成的清单能检测传输损坏，不独立证明来源。
验收完成前保留原始数据和备份。

重新开放提交前，向同一个兼容运行时明确传入恢复后的数据库、artifact、result
和分区路径。保留原先固定的引擎文件字节及所需 Python 适配器环境。通过
`result` 读取每个成功任务，验证清单和产物哈希，并比对任务 ID、状态、
请求指纹与保存的回执。再次提交未改动的请求，确认幂等键仍指向原任务。
通过这些检查后才启动调度器处理 `SUBMITTED` 任务。重复 `recover` 不应
改动已完成任务。备份前的 `RUNNING` 记录需要先确认旧进程消失，再单独处理
过期租约；恢复过程不能还原引擎内存。

`tests/test_go_runtime.py::test_offline_backup_restores_results_queue_and_idempotency`
使用真实 Go CLI 和固定 Rust 引擎验证这一边界：移走原目录，恢复 SQLite、
不可变输入、结果和回执，读取已完成结果，继续排队任务，并验证幂等性和重复恢复。
测试使用合成 v6 任务，不代表部署存储、外部回执保留、v7 分区迁移或备份调度已获验证。
