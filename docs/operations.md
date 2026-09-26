# 发布与故障排查

## 目录约定

下面以 `/srv/quant-backtest-runtime` 为例。`releases/<sha>` 保存源码和虚拟环境，`current` 指向当前版本，`previous` 保存回滚目标。数据独立保存在 `data/jobs.sqlite`、`data/artifacts` 和 `data/results`，worker 日志位于 `data/results/.logs`。

运行时只接受自己的 SQLite 任务库。旧研究仓库的任务库结构不同，不能直接作为运行时数据库使用。

## 发布

PR 合并后，拉取远端并记录完整提交号：

```bash
git fetch origin
release_sha=$(git rev-parse origin/main)
production_root=/srv/quant-backtest-runtime
python scripts/release.py --repo . --production "$production_root" --dry-run stage "$release_sha"
python scripts/release.py --repo . --production "$production_root" stage "$release_sha"
```

`stage` 只接受已经进入 `origin/main` 历史的提交。它将源码放入固定版本目录，再安装锁定的运行依赖，成功后写入 `.release-ready`。现有同名目录不会被覆盖。

使用待发布版本自身的脚本和可执行文件跑通合成数据，再切换：

```bash
"$production_root/releases/$release_sha/.venv/bin/python" \
  "$production_root/releases/$release_sha/scripts/smoke_backtest.py" \
  --executable "$production_root/releases/$release_sha/.venv/bin/backtest-job"
python scripts/release.py --repo . --production "$production_root" --dry-run switch "$release_sha"
python scripts/release.py --repo . --production "$production_root" switch "$release_sha"
```

生产调用使用 `current/.venv/bin/backtest-job` 并显式传入三个数据路径，无需设置 `PYTHONPATH`。运行中的 worker 继续使用启动时的解释器。清理旧版本前应确认没有进程仍在使用它。

回滚前可以先预演：

```bash
python scripts/release.py --repo . --production "$production_root" --dry-run rollback
python scripts/release.py --repo . --production "$production_root" rollback
```

有 `previous` 时回滚到上一版本。首次安装没有上一版本时，回滚会移除 `current` 指针，数据目录保持原样。

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
