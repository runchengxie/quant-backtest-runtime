# quant-backtest-runtime

[English README](README.md)

Go 实现负责管理任务状态和 worker，并持久化发布结果。v5、v6 Rust 引擎任务无需安装 Python。v1 至 v4 的金融计算通过显式配置的无状态 Python 适配器完成。

在 Linux 上使用 Go 1.27.3 构建，将程序放到源码目录之外。

```sh
go build -trimpath -o /absolute/external/runtime/bin/backtest-job ./cmd/backtest-job
```

[Go 使用指南](docs/go-runtime.zh-CN.md)说明立即提交、常驻队列和旧版 Python 适配器的用法。生产切换前需要停止原来的任务管理进程并备份数据库。本次迁移没有切换生产服务或定时任务。

`quant-backtest-runtime` 接收已提交的回测任务，负责校验请求、管理任务状态、运行 worker，并发布结果。这里的 runtime 指回测任务执行环境，不运行 LLM Agent。

本项目属于 Quant Research 项目系列。同系列仓库各自独立维护，通过明确的接口协作：`quant-platform` 提供回测能力，私有 `quant-research` 管理策略研究和证据，本项目负责任务执行。

## Python 兼容示例

需要 Linux 和 `uv`。包声明要求 Python 3.12 或更新版本，CI 验证 3.12、3.13，不覆盖其他 Python 版本：

```bash
uv sync --locked --extra legacy --group dev --python 3.13
uv run --locked --extra legacy python scripts/smoke_backtest.py
```

示例使用虚构数据，在临时目录中提交任务并检查结果。

## 文档

在线文档：[quant-backtest-runtime 文档](https://runchengxie.github.io/quant-backtest-runtime/)。

- [任务请求与结果](docs/jobs.zh-CN.md)：了解请求格式、任务状态和结果校验
- [开发指南](docs/development.zh-CN.md)：安装选项、完整检查和测试范围
- [运维指南](docs/operations.zh-CN.md)：部署、回滚和故障处理
- [职责说明](docs/ownership.zh-CN.md)：了解本项目与上游、下游的分工

## Python 兼容安装方式

基础包没有 Python 运行时依赖，支持 v5/v6 Rust 任务、SQLite 生命周期管理、
引擎结果校验，以及 agent 能力查询和预检。请单独安装经过审查的引擎可执行文件，
通过 `--engine-executable` 传入绝对路径：

```sh
pip install quant-backtest-runtime
# 仓库内安装锁定的开发工具，不安装旧 Python 后端：
uv sync --locked --group dev
```

v1-v4 执行、旧结果读取和 `quant_run_manifest_ref` 校验需要显式安装兼容扩展：

```sh
pip install 'quant-backtest-runtime[legacy]'
uv sync --locked --extra legacy --group dev
```

该扩展将 quant-platform 固定到经过审查的 Git 提交，需要 Git 访问权限。
`dev` 依赖组不会自动启用它。缺少依赖时返回 `LEGACY_DEPENDENCY_UNAVAILABLE`
和安装提示；CLI 的 plan 和 submit 会在创建状态之前拒绝请求。
依赖预检只检查包是否存在，不导入 pandas 或组合回测代码，也不证明后端兼容性或
请求语义正确。v1-v6 请求指纹、计划内容和持久化状态协议保持不变。
v6 计划仍将引擎语义标为 `not_checked`，v5 使用引擎提供的静态校验。

此次改动只拆分安装依赖，没有将 Python 算法迁入 Rust。在调用方和暂不支持的场景
完成迁移前，请保留 legacy 扩展。runtime 继续通过自身引擎协议负责进程监管、
SQLite 状态和结果发布校验，不使用提供方 SDK 监管子进程。
