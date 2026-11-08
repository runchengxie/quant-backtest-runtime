# 财务检查点会话（Go v8）

[English](financial-sessions.md)

需要恢复 Platform 分钟模拟的财务状态时，显式选择
`platform.minute_session_resume`。既有 v1–v7 任务仍在失败后终止并清理暂存目录；
v7 重放日志不是财务检查点。本功能用于合成数据诊断，不代表券商或交易所故障恢复。

## 输入和支持范围

安装锁定的 `legacy` extra，Platform 版本必须是
`7a618bddd8fdeee5dbe37fe5b54a9698e0de5355`，并通过 `--python-executable`
传入该环境的解释器绝对路径。Go 只启动固定模块；请求不能选择命令、导入路径、
可执行文件、任意策略或反序列化器。

```json
{
  "schema_version": 8,
  "idempotency_key": "synthetic-minute-1",
  "backend": "platform.minute_session_resume",
  "evidence_tier": "diagnostic",
  "inputs": {"minute_input_ref": "artifact://sha256/<input-sha256>"},
  "config": {
    "provider_revision": "7a618bddd8fdeee5dbe37fe5b54a9698e0de5355",
    "checkpoint_format": "minute-session.v2",
    "policy": "target-weights.v1"
  },
  "execution": {},
  "budgets": {"wall_seconds": 120, "memory_mb": 2048}
}
```

输入是一个不超过 1 MiB 的不可变 UTF-8 JSON 文件，必须包含以下字段：

| 字段 | 约定 |
| --- | --- |
| `schema` | `runtime-minute-input.v1` |
| `timezone` | 仅支持 `Asia/Shanghai` 市场日历 |
| `targets` | 1–4096 行：带时区的 `decision_time`、`entry_time`、`symbol`、`weight` |
| `bars` | 1–4096 行：带时区的 `time`/`interval_start`、证券、原始 OHLC、成交量/额、涨跌停价和可交易标志 |
| `config` | `MinuteExecutionConfig` 字段，包括初始现金和明确的买卖费用 |
| `report_delay_seconds` | 有限且非负的报告延迟 |
| `impact` | `null` 或 `LinearImpact` 的 transient/permanent 系数及 decay_seconds；系数单位是每股有符号成交量对应的价格变化 |
| `quantity_locks` | `QuantityLock` 字段；时间为整数 epoch 纳秒，symbols 为数组 |
| `corporate_actions` | `CorporateAction` 字段，使用日历日期 |
| `valuation_events` | `ValuationEvent` 字段，使用带时区的可用/生效时间 |
| `capital_flows` | `CapitalFlow` 字段，使用带时区时间；入金出金与盈亏分开 |

所有事件数组都必须提供，没有事件时传空数组。目标与分钟行情时间先转换为声明的
市场时区，再交给原财务实现；UTC 资金流仍按真实时刻和本地日期处理。估值或收益
无法确定时输出 JSON `null`。固定策略仅在没有待处理订单或报告时使用输入权重，
并保存完整观察历史。不支持学习型策略、任意模型、其他市场日历或未知行动类型。
冲击模型只用于未校准压力测试。撮合、权益、成本与资金流收益计算完全由 Platform 负责。

`plan` 验证输入摘要、已安装提供者版本和财务构造参数，不执行事件，也不创建数据库。
它不证明经济有效性或历史时点可得性。绑定范围包括输入原始字节、完整配置、策略、
实际加载的 Runtime/Platform 源码、依赖版本及 Go 的 `SourceRevision`。
不可变构建必须填写真实提交号；提交时可用 `--expected-plan-sha256` 防止预检变化。

## 显式恢复

原 v8 任务在租约到期并经 `recover` 标为 `FAILED` 后，使用新的幂等键、相同输入和配置，
并设置 `execution: {"resume_from": "<failed-job-id>"}`。恢复流程先确认并终止旧 worker，
再标记终态。取消、成功、没有已接纳进度或源码/配置发生变化的来源均被拒绝。
原任务保持失败，新任务的持久化请求保留来源链接。重复相同幂等键返回原新任务；
不自动重试，也不重新认领旧任务。

Go 在认领任务后生成随机所有权令牌。固定 Python 子进程每次恢复最后一个已接纳的
检查点，推进一个原生事件并写暂存 JSON。Go 验证连续游标与回执，写私有文件、fsync、
无覆盖重命名、fsync 目录，然后仅在任务仍为 RUNNING 且未取消时，将摘要、游标、绑定和
令牌原子接纳到 SQLite。下一步只使用该回执。恢复始终向 `MinuteSession.restore`
传入外部预期事件数组，包括空数组，不从事件零重新计算已提交的财务前缀。

检查点位于 `RESULT_ROOT/.checkpoints/<job>/<token>/<cursor>.json`，独立于最终结果
暂存目录。已重命名但没有数据库接纳记录的文件是孤立文件，不能恢复；数据库接纳后、
确认前死亡则使用已接纳游标。旧令牌和终态任务不能继续接纳或提交成功。取消与结果发布
仍由 Go 负责。此版本保留检查点供检查，不自动回收；不要手工接纳孤立文件或修改数据库。

最终 `quant.minute_job_result.v1` 清单绑定请求、财务文件摘要和大小、最终及恢复起始游标。
`financial.json` 包含全部日表、订单、成交、持仓、估值、资金流表、摘要，以及最终收尾前的
完整原生检查点：账户、应收权益、行动/冲击/策略进度、已送达和待送达报告、锁定与轨迹。
`result` 验证发布完整性。结果发布与检查点接纳相互独立；尝试元数据可以不同，但连续执行
与恢复执行的规范财务载荷必须完全相同。

## 诊断性故障验收

仅供操作者调用的 `__worker JOB --financial-fault PHASE:CURSOR` 在指定游标暂停，
由外部发送 SIGKILL。阶段限于 `before_write`、`after_fsync`、`after_rename`
（数据库接纳前）和 `after_admission`（确认前）。worker 写入
`.checkpoints/<job>/fault.json` 记录 PID、阶段和游标，等待正常截止时间或取消。
公共 submit 不接受该选项，也不能传入代码。它是合成故障探针，不是生产重试策略。

`tests/test_go_financial_sessions.py` 使用真实 Go CLI/Python 子进程覆盖四个死亡边界；
`tests/test_financial_sessions.py` 验证每个原生事件边界，并独立计算本地跨午夜分红送股、
估值与资金流期望值。设置 `BACKTEST_GO_EXECUTABLE` 并使用锁定环境运行。
既有引擎和队列会话测试仍是兼容性必检项。参见[开发文档](development.zh-CN.md)。
