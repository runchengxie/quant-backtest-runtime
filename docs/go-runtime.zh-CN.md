# Go runtime 使用指南

[English page](go-runtime.md)

Go 版 `backtest-job` CLI 已负责 SQLite 状态、worker 进程、调度和最终结果发布。v5、v6 任务直接调用独立安装的 Rust 引擎，无需 Python。实际使用的实现由部署配置决定。
显式 [v7 分区会话模式](queued-sessions.zh-CN.md) 使用无状态 Python 适配器，沿用 Go 生命周期控制。

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

## 检查待发布版本

使用 Python 3.12 或更新版本运行 `scripts/smoke_native.py`。脚本只用标准库，无需安装 Python 运行时包。输入使用已审查引擎版本中的合成样例，输出放在源码之外的新目录。

```sh
python3 /releases/runtime/COMMIT/scripts/smoke_native.py \
  --executable /releases/runtime/COMMIT/bin/backtest-job \
  --expected-runtime-revision COMMIT \
  --engine-executable /releases/engine/quant-backtest-engine \
  --engine-request /releases/engine/fixtures/sessions.json \
  --root /data/runtime-checks/unique-check
```

将 `COMMIT` 换成已合并的完整提交号。脚本先核对程序版本，再把带有预检哈希的任务放进队列。它检查重复提交和回执查询，取消第二个排队任务，然后运行调度器并读取已校验的结果。输出目录保留 `receipt.json` 和合成任务数据，便于排查。回执记录程序摘要与结果哈希。目录只要已经存在，脚本就会拒绝使用，包括空目录。检查通过只证明这份样例能跑通，样例的金融计算预期仍需另行核对。

## 限制调度服务的资源

主机如果支持 systemd 用户服务，并开放了内存和 CPU 控制器，可以用临时服务限制调度器及其子进程。先用合成任务验证，再确定实际任务的配置。

```sh
systemd-run --user --wait --pipe --collect \
  --unit=runtime-native-check-UNIQUE \
  -p MemoryMax=2G -p MemorySwapMax=0 -p CPUQuota=200% \
  python3 /releases/runtime/COMMIT/scripts/smoke_native.py \
  --executable /releases/runtime/COMMIT/bin/backtest-job \
  --expected-runtime-revision COMMIT \
  --engine-executable /releases/engine/quant-backtest-engine \
  --engine-request /releases/engine/fixtures/sessions.json \
  --root /data/runtime-checks/unique-cgroup-check
```

这里的数值用于合成检查。生产配置应按实际任务的内存峰值设置，并给调度器留出空间。核对服务所在 cgroup 的 `memory.max`、`memory.swap.max` 和 `cpu.max`，确认设置生效。还应在独立探测任务中让子进程申请超额内存，检查系统是否将它终止。探测数据要与实际任务分开。

长期运行时，将 `backtest-job ... serve --concurrency N` 作为服务命令。服务之外只提交排队任务。调度器启动的 worker 会继承服务的 cgroup，其他进程直接提交并启动的任务不会自动归入其中。服务上限约束整个队列的进程树，每个任务另有计算子进程的虚拟地址空间上限。内存不足可能中断多个任务，重启后应检查残留进程并运行 `recover`。需要逐任务限制时，可使用下面的委派模式。

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

## 限制每个排队任务的内存

为 systemd 服务设置 `Delegate=memory` 和 `DelegateSubgroup=supervisor`，再运行 `serve --cgroup-root auto`。调度器需要从委派的 `supervisor` 子组启动。也可以通过 `--cgroup-root` 传入一个绝对路径，指向已经委派且没有进程的 cgroup v2 父组。普通目录或缺少委派权限时，程序返回 `RESOURCE_LIMIT_UNAVAILABLE`，不会改用更弱的限制。这个选项只供 `serve` 使用。

调度器按任务库建立独立目录，再为每个任务创建新组。Linux 从启动时就把 worker 放进组里，覆盖 Go 初始化和后续子进程。任务的 `budgets.memory_mb` 按 MiB 设置 `memory.max`，禁用交换空间，并启用整组内存超限终止。worker 与计算子进程共用这份内存预算，调度器留在组外。原有计算子进程的虚拟地址空间限制也继续生效。两种限制的计量方式不同，不能直接当作与旧 Python 限制等价。

系统因内存不足终止任务组后，调度器将活跃任务记为 `FAILED`，错误码为 `JOB_MEMORY_EXCEEDED`，然后处理后续任务。已经请求取消时优先记录取消。程序将内存峰值和超限计数写入 `.logs/<任务号>.resources.json`，先同步保存快照，再删除任务组，并在回执中记录清理结果。回执保存失败会停止调度。任务组尚未删除时，计数会保留供后续恢复。金融结果清单沿用原契约。

调度器重启后，会继续等待仍在运行的已领取任务，并保留它们占用的并发名额。worker 如果在启动后、领取前中断，程序会保守地将任务记为失败并清理任务组。已完成或废弃的任务组会终止残留进程，再删除目录。已经提交的成功状态会保留。这些机制面向可信代码，拥有委派目录写权限的进程仍能修改控制设置。

仓库提供 [TOML 配置样例](https://github.com/runchengxie/quant-backtest-runtime/blob/main/configs/runtime.example.toml)和[用户服务模板](https://github.com/runchengxie/quant-backtest-runtime/blob/main/configs/quant-backtest-runtime.service)。实际路径保存在私有部署配置中。运行 `scripts/serve_config.py --config /configured/runtime.toml --dry-run` 可以查看启动命令，不会打开任务库。去掉 `--dry-run` 后，标准库启动脚本会将自身替换为 Go 进程。研究客户端也可以读取同一份 `[runtime]` 配置。设置 `queue=true`，固定引擎与可选的 Python 解释器路径，再按实际资源设置 `[scheduler].concurrency`。安装用户服务并重新加载后，先用独立合成任务库验证，再接入实际任务。

内存控制行为见 [Linux cgroup v2 文档](https://docs.kernel.org/admin-guide/cgroup-v2.html)。委派和子组设置见 [systemd 资源控制文档](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html)。

## 可选的 Python 兼容适配器

v1 至 v4 请求以及需要旧版契约校验的操作，需要在 Python 环境中安装 `quant-backtest-runtime[legacy]`，再通过 `--python-executable` 传入解释器的绝对路径。仓库开发环境可用 `uv sync --locked --extra legacy --group dev` 安装锁定依赖。

Go 通过指定解释器调用无状态适配器。Python 沿用现有金融契约进行规范化，完成计算并写入暂存产物。SQLite 状态变更、结果校验和最终发布由 Go 负责。适配器不会打开任务数据库，也不会发布最终结果目录。原有 Python CLI 继续保留，供兼容使用和回滚。

## 切换与回滚

`budgets.memory_mb` 限制引擎或旧版计算子进程的虚拟地址空间，Go 任务管理进程的内存没有计入这项上限。整项任务的内存约束尚未证明与旧 Python worker 等价。部署如果要求严格限制整项任务的内存，需要启用并验证逐任务委派模式，再切换。队列并发上限另行生效。

生产切换前，先停止 Python 的新任务提交，等待 worker 和调度器停止，再备份数据库及相关产物。先用独立的合成任务数据库验证 Go，记录 runtime 版本、引擎版本与程序摘要、使用的 Python 环境和验证结果，再把生产数据库交给 Go 独占管理。

Python 和 Go 的任务管理代码不能同时操作同一数据库。回滚时，先停止 Go 的新提交与调度，等待现有 worker 完成或明确取消它们，确认相关进程全部退出后，再恢复 Python 管理。保留备份和结果清单。本次改动没有迁移生产数据，也没有修改定时任务。

Linux 内存限制、超时、日志上限、父进程死亡信号和进程身份检查用于管理受信任的工作负载，无法为不受信任的策略或程序提供沙箱隔离。职责划分和后续评估见[语言方向记录](runtime-language.zh-CN.md)。
