# quant-backtest-runtime

`quant-backtest-runtime` 接收已提交的回测任务，校验请求，使用 SQLite 管理任务状态，运行 worker，并校验和发布结果。它不运行 LLM Agent。

本仓库属于 Quant Research 项目系列：[`quant-platform`](https://github.com/runchengxie/quant-platform) 提供可复用的回测能力；研究侧负责策略、输入来源和研究晋升；本仓库负责通用任务执行。详细分工见[仓库职责](ownership.md)。

## 跑通合成示例

需要 Linux、Python 3.12 或 3.13，以及 `uv`。在仓库根目录执行：

```bash
uv sync --locked --group dev --python 3.13
uv run --locked python scripts/smoke_backtest.py
```

示例只使用虚构数据，在临时目录中提交任务并检查结果，不需要真实研究数据。

## 阅读顺序

1. [回测任务与结果](jobs.md)：请求版本、输入引用、任务状态和结果校验。
2. [开发与检查](development.md)：本地环境、质量检查和测试范围。
3. [发布与故障排查](operations.md)：发布目录、回滚与常见错误。
4. [仓库职责](ownership.md)：与研究侧及平台侧的接口边界。
