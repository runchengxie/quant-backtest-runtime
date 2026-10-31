# Go runtime 使用指南

[English page](go-runtime.md)

Go 版 `backtest-job` CLI 已负责 SQLite 状态、worker 进程、调度和最终结果发布。v5、v6 任务直接调用独立安装的 Rust 引擎，无需 Python。目前尚未切换生产数据库和定时任务。

## 构建与验证

使用 Go 1.27.3 和仓库中的模块锁定文件，构建产物放在仓库之外。

```sh
go build -o /data/runtime-build/backtest-job ./cmd/backtest-job
go vet ./...
go test -race ./...
```

集成测试通过 `BACKTEST_GO_EXECUTABLE` 指定 Go CLI 的绝对路径，通过 `BACKTEST_ENGINE_TEST_EXECUTABLE` 指定引擎的绝对路径。使用锁定的 legacy Python 开发环境，可以同时验证原生任务和兼容适配器。

```sh
BACKTEST_GO_EXECUTABLE=/data/runtime-build/backtest-job \
BACKTEST_ENGINE_TEST_EXECUTABLE=/releases/engine/quant-backtest-engine \
uv run --locked --extra legacy pytest -q tests/test_go_runtime.py
```

CI 还会用固定源码版本构建的真实引擎运行 Go 协议测试。这些检查只覆盖相应样例，不能据此认定所有历史任务和部署都已兼容。

需要暂存不可变的本地发行目录时，可使用 `scripts/release.py stage COMMIT --implementation go`，同时按运维说明传入 `--repo` 和 `--production` 路径。提交必须已经进入 `origin/main`。工具构建 `bin/backtest-job`，通过 `version` 记录源码版本，构建成功后才写入就绪标记。暂存不会切换活动版本。工具默认仍使用 Python，兼容已有部署。

## 提交原生任务

通过 `--engine-executable` 传入已安装引擎的绝对路径，任务中的 `config.executable_sha256` 固定程序字节。runtime 打开程序、核对摘要，再通过保留的文件描述符执行。因此，替换路径不会换掉本次选中的 inode。部署时保持程序文件不可变，通过原子替换更新版本。

```sh
/data/runtime-build/backtest-job \
  --database /data/runtime/jobs.sqlite \
  --artifact-root /data/runtime/artifacts \
  --result-root /data/runtime/results \
  --engine-executable /releases/engine/quant-backtest-engine \
  submit /data/runtime/job-v6.json
```

普通 `submit` 会立即启动独立 worker 并返回任务 ID，保留已有的立即提交流程。这种模式没有跨任务的统一并发上限。`status`、`inspect`、`lookup`、`result`、`cancel` 和 `recover` 使用命令指定的数据库与产物目录。

## 使用常驻队列

为队列提交使用独立数据库。每个任务通过 `submit --queue` 入队，再启动一个设有明确并发上限的调度器。

```sh
/data/runtime-build/backtest-job \
  --database /data/runtime/queue.sqlite \
  --artifact-root /data/runtime/artifacts \
  --result-root /data/runtime/results \
  --engine-executable /releases/engine/quant-backtest-engine \
  submit --queue /data/runtime/job-v6.json

/data/runtime-build/backtest-job \
  --database /data/runtime/queue.sqlite \
  --artifact-root /data/runtime/artifacts \
  --result-root /data/runtime/results \
  --engine-executable /releases/engine/quant-backtest-engine \
  serve --concurrency 4
```

调度器重启后会发现数据库中已经入队的任务。worker 完成回收后才释放并发名额。验证时可用 `serve --concurrency 4 --once` 处理完队列后退出。一个数据库只运行一个调度器，调度期间的所有提交都使用队列模式，不要混入立即启动的任务。

## 可选的 Python 兼容适配器

v1 至 v4 请求以及需要旧版契约校验的操作，需要在 Python 环境中安装 `quant-backtest-runtime[legacy]`，再通过 `--python-executable` 传入解释器的绝对路径。仓库开发环境可用 `uv sync --locked --extra legacy --group dev` 安装锁定依赖。

Go 通过指定解释器调用无状态适配器。Python 沿用现有金融契约进行规范化，完成计算并写入暂存产物。SQLite 状态变更、结果校验和最终发布由 Go 负责。适配器不会打开任务数据库，也不会发布最终结果目录。原有 Python CLI 继续保留，供兼容使用和回滚。

## 切换与回滚

`budgets.memory_mb` 限制引擎或旧版计算子进程的虚拟地址空间，Go 任务管理进程的内存没有计入这项上限。整项任务的内存约束尚未证明与旧 Python worker 等价。部署如果要求严格限制整项任务的内存，需要先验证委派的 cgroup 等机制，再切换。队列并发上限另行生效。

生产切换前，先停止 Python 的新任务提交，等待 worker 和调度器停止，再备份数据库及相关产物。先用独立的合成任务数据库验证 Go，记录 runtime 版本、引擎版本与程序摘要、使用的 Python 环境和验证结果，再把生产数据库交给 Go 独占管理。

Python 和 Go 的任务管理代码不能同时操作同一数据库。回滚时，先停止 Go 的新提交与调度，等待现有 worker 完成或明确取消它们，确认相关进程全部退出后，再恢复 Python 管理。保留备份和结果清单。本次改动没有迁移生产数据，也没有修改定时任务。

Linux 内存限制、超时、日志上限、父进程死亡信号和进程身份检查用于管理受信任的工作负载，无法为不受信任的策略或程序提供沙箱隔离。职责划分和后续评估见[语言方向记录](runtime-language.zh-CN.md)。
