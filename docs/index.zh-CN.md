# quant-backtest-runtime

[English page](index.md)

`quant-backtest-runtime` 接收已提交的回测任务，校验请求，使用 SQLite 管理任务状态，运行 worker，并校验和发布结果。它不运行 LLM Agent。

本仓库属于 Quant Research 项目系列：[`quant-platform`](https://github.com/runchengxie/quant-platform) 提供可复用的回测能力；研究侧负责策略、输入来源和研究晋升（判断研究结果能否进入下一验证或使用阶段）；本仓库负责通用任务执行。详细分工见[仓库职责](ownership.zh-CN.md)。

## 跑通合成示例

需要 Linux 和 `uv`。包声明要求 Python 3.12 或更新版本，CI 验证 3.12、3.13，不覆盖其他 Python 版本。在仓库根目录执行：

```bash
uv sync --locked --extra legacy --group dev --python 3.13
uv run --locked --extra legacy python scripts/smoke_backtest.py
```

示例只使用虚构数据，在临时目录中提交任务并检查结果，不需要真实研究数据。

```json
{"status": "SUCCEEDED", "job_id": "923e452a-c73b-4932-a7d1-52d329da4f9f"}
```

上面是一次成功运行的输出。每次生成的 `job_id` 不同，`SUCCEEDED` 表示任务已完成并通过结果校验。worker 是独立执行任务的进程，负责调用回测能力、写出结果并更新状态。成功任务依次经过 `SUBMITTED`（已提交）、`RUNNING`（执行中）和 `SUCCEEDED`（已完成）。脚本提交任务后持续查询状态，完成后读取结果。临时目录在脚本退出时删除。

## 阅读顺序

1. [回测任务与结果](jobs.md)：请求版本、输入引用、任务状态和结果校验。
2. [开发与检查](development.md)：本地环境、质量检查和测试范围。
3. [发布与故障排查](operations.md)：发布目录、回滚与常见错误。
4. [仓库职责](ownership.md)：与研究侧及平台侧的接口边界。

## 安装方式

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
