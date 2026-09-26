# quant-backtest-runtime

`quant-backtest-runtime` 接收已提交的回测任务，负责校验请求、管理任务状态、运行 worker，并发布结果。这里的 runtime 指回测任务执行环境，不运行 LLM Agent。

本项目属于 Quant Research 项目系列。同系列仓库各自独立维护，通过明确的接口协作：`quant-platform` 提供回测能力，私有 `quant-research` 管理策略研究和证据，本项目负责任务执行。

## 跑通合成示例

需要 Linux、Python 3.12 或 3.13，以及 `uv`：

```bash
uv sync --locked --group dev --python 3.13
uv run --locked python scripts/smoke_backtest.py
```

示例使用虚构数据，在临时目录中提交任务并检查结果。

## 文档

- [任务请求与结果](docs/jobs.md)：了解请求格式、任务状态和结果校验
- [开发指南](docs/development.md)：安装选项、完整检查和测试范围
- [运维指南](docs/operations.md)：部署、回滚和故障处理
- [职责说明](docs/ownership.md)：了解本项目与上游、下游的分工
