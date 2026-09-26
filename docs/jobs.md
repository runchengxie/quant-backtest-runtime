# 回测任务与结果

## 请求协议

运行时接受两种请求，后端均为 `native.position_replay`。

| 请求版本 | 用途 | 必要条件 | 结果格式 |
| --- | --- | --- | --- |
| v1 | 诊断回放 | `evidence_tier=diagnostic` | `ticknet.backtest_job_result.v1` |
| v2 | 带执行账本的回放 | `evidence_tier=execution_aware`、完整研究时钟、生产者身份、开启账本 | `quant.backtest_job_result.v2` |

v2 同时要求 `execution.ledger=true` 和 `execution.ledger_config.enabled=true`。完整字段见 [v2 请求示例](../examples/request-v2.json)，校验实现见 [contracts.py](../src/backtest_runtime/contracts.py)。不支持的字段会被拒绝，避免拼写错误被默默忽略。

请求文件必须是普通 JSON 文件，大小不超过 64 KiB。`idempotency_key` 为 1 至 128 个安全 ASCII 字符。同一键和同一请求重复提交返回已有任务，同一键绑定不同内容时会报冲突。修改参数后重跑应使用新键。

输入采用 `artifact://sha256/<digest>`，对应文件位于 `<artifact-root>/sha256/<digest>`。`positions_ref`、`pricing_ref` 和 `periods_ref` 必填，`intraday_bars_ref` 可选。worker 读取 Parquet 前会校验文件 SHA-256。调用方负责把输入写入该目录并保持不可变。

`budgets.wall_seconds` 取值为 1 至 86400，`budgets.memory_mb` 取值为 256 至 65536。内存限制约束进程虚拟地址空间，导入依赖也会占用预算。无法设置资源限制时任务失败。

## 研究时钟

v2 由调用方提供完整的 `research.clock.v1`。运行时校验时间顺序，并检查订单日期、成交日期是否落在执行窗口内，净值日期是否落在执行开始日至估值日之间。

原生回放的成交时间只有日期精度。研究时钟不能证明具体的日内成交时刻，调用方提供的窗口必须覆盖完整回放期间。

## 状态与恢复

```mermaid
stateDiagram-v2
    [*] --> SUBMITTED
    SUBMITTED --> RUNNING: worker 领取
    SUBMITTED --> CANCELLED: 领取前取消
    SUBMITTED --> FAILED: 启动失败或启动租约过期
    RUNNING --> SUCCEEDED: 结果发布并完成校验
    RUNNING --> CANCELLED: worker 确认取消
    RUNNING --> FAILED: 超时、异常或租约过期
```

运行中取消先记录取消请求，再向确认属于该任务的 worker 发信号。已经完成的任务不会因为重复取消而改变状态。取消请求写入后，成功状态更新不能覆盖它。

worker 领取任务后，租约截止时间为领取时间加 `wall_seconds` 再加 30 秒。CLI 启动 worker 时另设 60 秒的启动租约。`recover` 处理过期租约，`submit` 和 `status` 也会触发恢复。没有常驻调度器，长期无人查询的环境应定时执行 `recover`。

回收运行中的过期任务时，会先核对 `/proc` 中的命令和任务 ID，再通过 pidfd 终止并确认进程退出。读取 `/proc` 失败或无法完成终止时保留状态，供后续恢复和排查。确认进程已经退出，或 PID 对应其他进程时，会结束过期任务并清理未发布结果，保留无关进程。

## 结果与来源关系

```mermaid
flowchart LR
    A[请求 JSON 与输入文件哈希] --> B[请求指纹与 SQLite 任务记录]
    B --> C[worker 调用平台后端]
    C --> D[临时结果目录]
    D --> E[按任务 ID 发布结果目录]
    E --> F[数据库保存结果清单哈希]
    F --> G[result 校验清单与所有文件]
```

v1 结果包含五张诊断表。v2 结果包含平台的 `portfolio_backtester.backtest_result.v1` 标准结果包。`result` 会校验任务身份、请求指纹、清单哈希和结果文件哈希，再返回元数据。发现文件被修改时会报错。

回测任务成功表示结果已完成发布。研究晋升还需要调用方关联独立的根 `ResearchRunManifest`，并完成研究侧的来源核对和审批。
