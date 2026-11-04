# 会话进程通道与重放恢复

[English](session-stream.md)

`backtest_runtime.session_stream.run_partition_sessions` 通过一个持续运行的 Rust
引擎进程，处理已验证的分区输入。这是需要显式调用的 Python API，要求 Linux 和支持
`session-stream` 的引擎版本。现有 v1-v6 排队任务、指纹、资源预算、Go 控制接口与网关
保持原有行为，其 CI 使用的引擎固定版本也不变。

每行输入的 payload 恰好包含两个字段。

```json
{"schema_version":"engine_session_payload.v1","session":{"session_date":"2025-01-02","open_ts_ns":1735781400000000000,"valuation_ts_ns":1735781401000000000,"quotes":[],"orders":[]}}
```

示例仅说明封装结构。行情和委托必须符合引擎 `MarketSession` 契约，公开示例应使用
合成数据。外层 `event_time_ns` 必须等于该会话的估值时间。事件 ID、事件时间和可用
时间保留在日志元数据中。引擎只收到 session，因此外层可用时间不会自动成为策略的
信息可见性规则。

`config` 包含 `engine_request.v2` 的全部必需头部字段，不包含 `sessions`。
字段为 `schema_version`、`currency`、`price_basis`、`initial_cash`、`fee_bps`、
`enforce_t1`、`instruments`、`required_capabilities`。运行前会通过严格的规范化 JSON
复制配置。输入和市场语义仍由引擎定义。

```python
from pathlib import Path
from backtest_runtime.partition_inputs import PartitionManifest, VerifiedPartitionReader
from backtest_runtime.session_stream import StreamLimits, run_partition_sessions

# manifest_mapping 和 config 由操作者提供，示例使用公开合成输入。
manifest = PartitionManifest.from_dict(manifest_mapping)
reader = VerifiedPartitionReader(
    manifest, input_root=Path("/data/synthetic/inputs"),
    task_root=Path("/data/synthetic/spool"),
)
receipt = run_partition_sessions(
    reader, config,
    executable=Path("/releases/engine/quant-backtest-engine"),
    executable_sha256=operator_verified_binary_sha256,
    output_root=Path("/data/synthetic/session-run"),
    limits=StreamLimits(wall_seconds=300, max_sessions=4096),
    resume=False,
)
# 成功返回后，回执才指向已完成核验的最终结果。
print(receipt["result_path"], receipt["result_sha256"])
```

调用方须先创建输入目录和分区暂存目录，并提供自己独占的绝对输出路径。输入、暂存
和结果都应放在源码目录之外，也不要使用生产或共享权威数据路径。输出根目录不得
与任务数据库或维护 `current`、`latest` 的发布流程共用。

客户端发送规范化 start 帧，收到 ready 后，逐个发送会话。每次收到报告后才读取下一
行已验证输入。发送 finish 后关闭 stdin，再接受最终成功。头部 SHA-256 绑定 start
帧去除换行后的原始 JSON 字节。前缀链对上一段原始 32 字节哈希和当前 session 或
finish 帧去除换行后的原始字节共同计算 SHA-256。

`SessionStreamClient` 也可以在上下文管理器中直接调用 `start`、`session`、`finish`。
它返回传输层报告和结果。需要金融核验与持久化发布时，应使用
`run_partition_sessions`。

会话报告和 `report-*.json` 文件都是暂定记录。运行器在当前输入仍可读取时，核对
委托、成交、估值价格和时间约束，只累计输出记录。结束后使用已有结果校验器，
独立核对完整的现金、持仓、费用、净值及 T+1 台账。引擎最终结果必须与累计报告
组成的结果完全一致，包括十进制字符串。通过这些检查后，才发布 `result-*.json`
并将日志标记为完成。

## 资源限制与进程生命周期

默认限制为 4096 个会话、300 秒墙钟时间、每帧 64 MiB（含换行）、累计 8 KiB stderr
和 8 MiB 日志。可以降低帧上限，不能超过协议最大值。严格 UTF-8 JSON 解析拒绝
重复键、非有限数值、多余字段、错误的序号或哈希、截断和额外 stdout。分区读取器
另行执行分区、行数和单行大小限制。

输入保留范围是一份已验证分区的磁盘暂存内容，以及当前完整会话。接口不支持把
一个会话拆成流式片段。输出记录、引擎历史、汇总结果和日志随会话数增长，不能
据此宣称金融账户状态占用恒定内存。最终结果超过帧上限时，即使各份报告未超限，
运行也会失败。

非阻塞 selectors 在写入 stdin 时并行读取 stdout 和 stderr。等待管道时，最长每
50 毫秒检查一次截止时间和取消信号。交付输入行时也会检查。分区验证、文件系统
fsync 和 Python 结果校验是同步操作，不承诺在 50 毫秒内中断。取消回调返回 `True`
时终止运行。

可执行文件必须是普通文件。运行器通过持有的描述符校验 SHA-256，再通过
`/proc/self/fd` 执行。引擎 inode 和 runtime 发布源码都应不可变，运行期间不要原地
修改。独立启动的小型 Python 辅助进程设置 Linux 父进程死亡 SIGKILL，并核对预期
父进程 PID 后才 exec，避免在嵌入式宿主中使用 `preexec_fn`。运行器在回收引擎组长之前终止其拥有的进程组，并回收子进程。成功完成后的
上下文清理不会再对已回收的进程组编号发送信号。异常离开上下文时也执行清理。宿主突然死亡会杀死引擎。可信引擎不应
脱离进程组派生子进程，也不应改动死亡信号设置。本接口不提供可执行文件沙箱。

## 日志与重启

输出根目录持有独占、非阻塞 `flock`。`resume=False` 遇到已有日志或发布记录会
拒绝继续，避免覆盖。`resume=True` 要求日志已存在。日志绑定 manifest 摘要、规范化
配置、可执行文件字节、相关 runtime 模块源码和通道限制。任意绑定改变都需要使用
另一个输出目录。

每条确认记录保存下一行 `InputCursor`、序号、输入前缀哈希、报告摘要、确定性的
发布标识和外层时间元数据。日志有大小上限，进行严格解析，并带有内容完整性摘要。
摘要用于检测内容损坏，不证明来源真实性，也不能防止有写权限的人修改后重新
计算摘要。恢复仍会将报告、前缀和游标与实际引擎重放结果比较。

发布文件由运行绑定、序号和内容摘要确定。先 fsync 文件，通过不会覆盖已有文件的
链接操作发布，再 fsync 目录。之后才原子替换并持久化确认日志。已有同名文件必须
逐字节一致，才能去重。已确认文件缺失或损坏会拒绝恢复。若发布后、日志确认前
崩溃，恢复时重新执行并去重相同内容。中断写入可能留下 `.pending-*` 文件，它们
不代表已确认记录或结果。

每次重启都创建新引擎，从 `reader.iter_rows()` 的第零行开始重放，包括全部已确认
会话。每份已保存报告、前缀和游标均须与重放一致。单独的输入游标不能恢复现金、
持仓、委托、成交或费用状态。本功能不保存金融状态快照，不通过 pickle 或内存
缓存恢复账户。发布目录支持本地重放的幂等去重，不向外部回调或其他存储承诺
恰好执行一次。只有最终结果通过验证且日志完成，才表示运行成功。

真实引擎测试使用 `BACKTEST_ENGINE_STREAM_TEST_EXECUTABLE` 指向新版本二进制，
执行 `tests/test_session_stream.py`。完整测试仍应让原有
`BACKTEST_ENGINE_TEST_EXECUTABLE` 使用原来固定的引擎版本。验证记录应同时列出
两个仓库的源码修订和通道二进制的 SHA-256。
