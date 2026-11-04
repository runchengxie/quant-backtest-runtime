# quant-backtest-runtime

[English](README.md) · [中文文档](https://runchengxie.github.io/quant-backtest-runtime/index.zh-CN/)

提交回测、查看进度，再取回经过校验的结果。
这个 runtime 用 SQLite 保存任务状态，通过独立 worker 进程执行任务，
支持排队和取消，并在保存结果前检查其完整性，方便后续读取。

当直接运行引擎已经不够，需要管理多个任务或保留结果时，可以使用它。
Go 命令行负责管理任务，单独安装的
[quant-backtest-engine](https://github.com/runchengxie/quant-backtest-engine)负责模拟交易。
策略与研究判断由调用方负责。
[quant-platform](https://github.com/runchengxie/quant-platform)提供旧任务可选的 Python 后端。
详见[仓库职责](docs/ownership.zh-CN.md)。

## 运行一个合成任务

需要 Linux 源码仓库、Go 1.27.3，以及运行检查脚本的 Python 3.12 或更新版本。
脚本只用 Python 标准库。原生引擎任务本身不需要安装 Python。
请先按引擎的[版本安装说明](https://runchengxie.github.io/quant-backtest-engine/releases.zh-CN/)
安装经过审查的可执行文件。文档中的 alpha.2 支持当前 v5 任务所需的静态校验。

在本仓库根目录运行，先替换下面三个路径。编译和结果目录都放在源码之外，
每次运行的 `check_root` 都必须是尚不存在的新目录。

```sh
runtime_build=/absolute/external/runtime-build
engine=/absolute/path/to/quant-backtest-engine
check_root=/absolute/external/new-synthetic-check
mkdir -p "$runtime_build"
revision=$(git rev-parse HEAD)
go build -trimpath \
  -ldflags "-X github.com/runchengxie/quant-backtest-runtime/internal/app.SourceRevision=$revision" \
  -o "$runtime_build/backtest-job" ./cmd/backtest-job
python3 scripts/smoke_native.py \
  --executable "$runtime_build/backtest-job" \
  --expected-runtime-revision "$revision" \
  --engine-executable "$engine" \
  --engine-request examples/engine-request-v2.json \
  --root "$check_root"
```

脚本生成合成输入并将任务加入队列，检查重复提交，取消第二个任务，
然后执行队列并校验保存的结果。
成功时打印 JSON，其中 `status` 为 `"SUCCEEDED"`、`cancel_status` 为 `"CANCELLED"`，
`summary.equity` 为 `"10099"`。数据库、输入、结果和 `receipt.json`
留在 `check_root` 下供检查，不需要私有数据或生产数据库。

## 当前范围

原生 v5 任务返回最终账户快照，v6 任务返回逐日账本，两者都调用外部 Rust 引擎，
结果仍属于诊断证据。支持哪些交易规则取决于引擎。结果发布成功并不表示每张订单都已成交，
也不表示策略已满足生产要求。原始分区传输 v7 仍处于设计提案阶段，尚未启用。

旧 v1 至 v4 任务通过显式配置的 Python 兼容适配器运行。
这条路径见[安装方式](docs/development.zh-CN.md#安装方式)和
[Python 示例](docs/index.zh-CN.md)。
Go 与 Python 任务管理程序不能同时管理同一个数据库。
生产切换前要停止原来的管理程序并备份状态，详见[切换与回滚](docs/go-runtime.zh-CN.md)。

## 接下来

- [Go 使用指南](docs/go-runtime.zh-CN.md)。提交任务、运行常驻队列、设置资源限制
- [任务与结果](docs/jobs.zh-CN.md)。请求、状态与结果校验
- [会话进程通道](docs/session-stream.zh-CN.md)。显式启用的 Python 流式调用与重放恢复
- [开发指南](docs/development.zh-CN.md)。依赖与必需检查
- [运维指南](docs/operations.zh-CN.md)。发布、回滚与故障排查
