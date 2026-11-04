# 已验证的分区输入 v1

[English](partition-inputs-v1.md)

`backtest_runtime.partition_inputs` 提供可选的 Python 输入读取器。每个 NDJSON
分区必须完整通过校验，才能向调用方交付该分区的第一行。读取器不管理任务、策略、撮合、
组合或结果发布，也没有接入现有引擎传输。v1 至 v6 任务、引擎请求、指纹、大小限制和版本固定值均未改变。

## 清单与事件格式

使用 `PartitionManifest.from_dict(mapping, limits=PartitionLimits(...))` 创建不可变清单。
未知字段、策略和版本会被拒绝。调用方负责在解析清单前限制传输大小。构造后修改原始字典，
不会改变清单内容或摘要。

```json
{
  "schema_version": "partition_manifest.v1",
  "row_schema": "event_envelope.v1",
  "order": "event_time_ns,event_id",
  "duplicate_policy": "reject_event_id",
  "global_order": "strict",
  "partitions": [
    {
      "path": "synthetic/0001.ndjson",
      "sha256": "<64 lowercase hexadecimal characters>",
      "bytes": 123,
      "rows": 1,
      "first": [100, 1],
      "last": [100, 1]
    }
  ]
}
```

示例摘要是占位符。`bytes` 包含每行必须具备的末尾换行。分区和清单都不能为空。
`first`、`last` 是首尾行的 `[event_time_ns,event_id]`。路径必须是唯一的相对 POSIX
路径。绝对路径、空路径分量、`.`/`..`、反斜杠、NUL 和任意层级的符号链接均被拒绝。
读取器通过已持有的目录描述符和 `O_NOFOLLOW` 打开普通文件。调用方显式提供现有的绝对
`input_root`，作为获准读取的目录。读取器不会扩展读取范围。

每个 UTF-8 NDJSON 行必须具有以下全部字段，且不能增加其他字段。

```json
{"event_id":1,"event_time_ns":100,"available_time_ns":150,"payload":{"symbol":"SYNTH"}}
```

ID 和时间戳为非负的有符号 64 位整数，不接受布尔值。发生时间 `event_time_ns` 在分区内和
分区间均不能倒退，整数事件 ID 必须严格递增。ID 用作稳定的重放序号，证券代码保存在其他字段。严格递增
可在不保存全量 ID 集合的情况下拒绝重复。读取器不会排序、去重或修复数据。
`available_time_ns >= event_time_ns`，但可在相邻行之间下降。较早发生、较晚报告的成交，
可以排在较晚发生但立即可见的行情之前。读取器按事实发生顺序输出。消费者另行控制策略的信息可见性。
`payload` 必须是 JSON 对象，其金融含义由消费者验证。重复 JSON 键、非有限数值（含指数溢出）、
非法 JSON、空行和缺少末尾换行的截断行均被拒绝。

## 资源与校验边界

`PartitionLimits` 默认允许 4096 个分区、每分区 256 MiB、合计 16 GiB、每分区 100 万行、
合计 1 亿行和每行 1 MiB（包含换行）。限制值必须是正的有界整数。打开输入前检查声明的分区
及总量上限，实际字节数和行数不能超过声明。这些是读取器资源策略，不会改变引擎的 64 MiB 传输上限。

读取器逐行处理，每次最多读取一个受限行。它完整校验顺序、字节数、行数、首尾范围及 SHA-256，
并将原始字节写入调用方显式指定的现有绝对 `task_root` 下的私有临时文件。校验完成后，
只从该临时文件交付数据，因此源文件随后被替换或修改不会影响已经校验的分区。
后续分区失败时，先前分区可能已交付。本 API 不保证整个数据集原子交付。
临时文件在分区结束、失败或迭代器关闭时释放。放弃迭代时应显式 `close()`。
磁盘占用最多为一个声明的分区，逐行迭代内存受行大小限制。批量迭代还保留指定批次的行。
批量大小须为正数且不能超过每分区行数上限。调用方应根据实际资源收紧限制。
输入目录、任务目录和临时文件存储必须由操作者控制。该机制无法隔离同一用户运行的恶意进程。

## 游标与消费者集成

```python
from pathlib import Path
from backtest_runtime.partition_inputs import PartitionManifest, VerifiedPartitionReader

manifest = PartitionManifest.from_dict(manifest_mapping)
reader = VerifiedPartitionReader(
    manifest, input_root=Path("/data/approved-inputs"), task_root=Path("/data/tasks/run-1")
)
for row, next_cursor in reader.iter_rows():
    consume(row)
    save_together(consumer_state(), next_cursor.to_dict())
```

`InputCursor.to_dict()` 只含 `schema_version: partition_input_cursor.v1`、
`manifest_sha256`、`partition`、`row`。下标从零开始，指向下一行。分区末尾归一为下一分区
第零行，最终位置为 `(len(partitions),0)`。解析与读取会拒绝未知字段和版本、类型错误、负值、
越界及不匹配的清单。合法范围内的位置只是定位指令，不能证明此前的金融运算已经执行。
恢复时会重新校验所选完整分区，再跳过之前的行。不会读取此前分区，也不保证此前文件仍然存在。

清单摘要取自规范化 JSON 的 SHA-256。JSON 使用键排序、ASCII 转义和紧凑分隔符，内容包括分区顺序、路径、
摘要、字节数、行数、范围及全部格式和策略字段。调用方的资源限制不参与摘要。
`iter_batches(batch_size,cursor=...)` 只对相同的 `(row,next_cursor)` 序列分组。改变批量
大小不改变事件顺序或游标身份。

输入游标仅记录读取位置。消费者必须将自己的有版本金融状态、配置和源代码版本与游标原子绑定，
验证负载并拒绝不兼容的检查点。重新读取输入本身不能恢复现金、订单、手续费或尚未可见的信息。
未来集成边界见独立的 [v7 传输提案](plans/partition-transport-v7.zh-CN.md)，该提案尚未启用。
