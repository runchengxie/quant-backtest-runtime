# 仓库职责与迁移

[English page](ownership.md)

从研究仓库拆出运行时后，通用任务操作的使用说明、示例和运维脚本统一在本仓库维护。

| 内容 | 维护位置 |
| --- | --- |
| 任务请求、SQLite 状态、worker、取消和恢复 | `quant-backtest-runtime` |
| 通用 CLI 示例、合成数据检查、发布和回滚 | 本仓库的 `scripts/`、`examples/` 和 `docs/` |
| 策略输入准备、研究客户端、来源记录与晋升 | `quant-research` |
| 旧研究任务库的只读适配与切换前检查 | `quant-research` |
| 回测算法、执行模拟和标准结果包 | `quant-platform` |

研究侧的 `scripts/dev/check_legacy_backtest_jobs.py` 专门检查旧 `ExperimentRegistry` 是否还有未结束的任务，随历史适配保留在研究仓库。旧任务全部迁移或退役且无人使用 `--legacy` 后，可由研究仓库移除这一适配。

平台仓库中与运行时拆分有关的架构设计和实施计划保留为迁移历史，当前运行方法以本仓库文档为准。跨仓调用方通过链接引用说明，避免维护多份任务协议和部署步骤。

基础 runtime 需要 Python 解释器，无需第三方 Python 包。使用旧 quant-platform 后端时，需要显式安装 `legacy` 可选安装组。模型训练由调用方安排。

[Runtime 的语言方向](runtime-language.zh-CN.md)说明后续如何评估 Go，以及迁移前需要通过哪些兼容检查。
