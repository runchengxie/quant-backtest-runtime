# 分区会话队列任务（v7）

[English](queued-sessions.md)

Go CLI 提供显式的 v7 模式，把已验证分区会话接入现有任务队列、取消、超时和资源控制。
安装基础 Python runtime 包，以及支持 `session-stream` 的已审阅 Rust 引擎即可。
v7 无需 legacy 扩展。Go 独占 SQLite 状态写入和最终结果发布。
v1-v6 的请求格式与指纹保持原有约定。

## 请求和操作员目录

把去掉 `sessions` 的 `engine_request.v2` 配置和
[`partition_manifest.v1`](partition-inputs-v1.zh-CN.md) 清单保存为不可变 artifact，
路径为 `ARTIFACTS/sha256/<文件实际 SHA-256>`。每行使用
[`engine_session_payload.v1`](session-stream.zh-CN.md) 会话信封。
清单绑定分区字节和行范围。请求只接受以下字段。

```json
{
  "schema_version": 7,
  "idempotency_key": "synthetic-partition-sessions",
  "backend": "nautilus.partition_session_replay",
  "evidence_tier": "diagnostic",
  "inputs": {
    "engine_config_ref": "artifact://sha256/<config-file-sha256>",
    "partition_manifest_ref": "artifact://sha256/<manifest-file-sha256>"
  },
  "config": {"executable_sha256": "<engine-binary-sha256>"},
  "execution": {},
  "budgets": {"wall_seconds": 300, "memory_mb": 4096}
}
```

操作员通过绝对路径指定 `--engine-executable`、`--python-executable` 和
`--partition-root`。请求自身没有目录或命令字段。分区路径必须是相对路径。
持有目录描述符的读取方式拒绝目录穿越、符号链接和特殊文件。
输入、数据库、结果和临时文件均放在源码树及生产共享数据之外。

```sh
backtest-job --database /data/synthetic/jobs.sqlite \
  --artifact-root /data/synthetic/artifacts --result-root /data/synthetic/results \
  --engine-executable /releases/engine/quant-backtest-engine \
  --python-executable /releases/runtime/venv/bin/python \
  --partition-root /data/synthetic/inputs \
  submit /data/synthetic/job-v7.json --queue
```

使用相同的目录和程序路径运行 `serve --concurrency N` 来消费队列。
`plan` 接受相同参数，提交时可用 `--expected-plan-sha256` 绑定计划。
Go 能力发现通过 `queued_session` 声明该模式。
共享 Python 能力发现和请求解析继续支持 v1-v6。

## 校验边界和资源上限

预检在创建数据库前检查请求结构、两个 artifact 的哈希和清单元数据。
它在 10 秒和 256 MiB 子进程限制下执行严格的 start/ready 握手。
ready 证明通道可用及配置可解码。预检明确保留金融和配置语义、分区正文未检查的状态。
计划阶段不提交会话，也不重复扫描大规模输入正文。

worker 在交付前验证整个分区并写入私有暂存文件。每次提交一个会话并接收临时报告。
最终使用现有金融校验器核对账户。单独的校验步骤重新读取输入和报告，核对游标、元数据、
精确前缀哈希与最终结果。Go 独立检查所有暂存文件的清单，执行 fsync 和目录重命名，
再通过 SQLite 条件更新提交成功状态。Python 只写入 Go 提供的私有暂存目录和临时分区缓冲，
整个过程由 Go 控制数据库和最终任务目录。

协议上限为每份配置或清单 1 MiB、4096 个会话和分区、每分区 256 MiB、
声明输入总量 16 GiB、每行 NDJSON 1 MiB、每个通道帧或报告或结果 64 MiB（含换行）、
journal 8 MiB，以及保留的报告、结果、journal 合计 256 MiB。
报告和结果合计最多 248 MiB，为 journal 预留 8 MiB。
输入必须以完整会话为单位满足这些边界。更大的会话需要另行审阅输入协议。

输出历史、账户状态和结果校验随会话数增长。
默认地址空间预算分别约束 Python 适配器及继承限制的引擎。
如需约束 worker 及全部后代进程的合计物理内存，请使用
[每任务委托 cgroup](go-runtime.zh-CN.md)。Go 的任务期限覆盖计算和校验。
读取结果时按任务的期限与内存策略重新核对输入和报告。输入缺失、清单或内容不一致均会失败。
读取结果时只执行校验器。

## 重启和验证证据

调度器重启后识别存活 worker，并继续占用其并发槽位。
worker 死亡或租约过期时，沿用现有的失败或取消终态及清理规则。
队列 v7 使用这些终态规则。需要重新执行时，显式使用新的幂等键提交。
独立的[会话 API](session-stream.zh-CN.md) 继续提供显式的完整前缀重放。
队列成功以 Go 发布完成且数据库提交成功为准。

合成验收测试覆盖真实引擎、无数据库副作用的预检失败、无效 payload、分区哈希与符号链接失败、
取消、超时、worker 硬退出、调度器重启和结果篡改。
设置 `BACKTEST_GO_EXECUTABLE` 与 `BACKTEST_ENGINE_STREAM_TEST_EXECUTABLE` 后运行
`tests/test_go_queued_sessions.py`。记录两个源码提交和实际二进制哈希。
更广泛的分区传输提案继续作为单独设计记录保留。
