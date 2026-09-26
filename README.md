# quant-backtest-runtime

独立部署的本地回测任务运行时。负责请求校验、SQLite 任务状态、worker 进程、资源限制、输入校验、结果发布和命令行操作。

`quant-platform` 提供回测后端、执行模拟和结果协议。`quant-research` 负责策略研究、输入准备、研究来源记录和晋升决策。运行时支持通用任务编排，不包含策略代码或行情接入。

## 安装与开发

需要 Linux、Python 3.12 或 3.13，以及 `uv`。进程识别和回收依赖 Linux `/proc` 与 pidfd，其他操作系统不支持提交任务。

```bash
uv sync --locked --group dev --python 3.13
uv run --locked backtest-job --help
```

依赖版本记录在 `uv.lock`，其中 `quant-platform` 固定到具体 Git 提交。开发和 CI 都使用锁定依赖。

## 跑通一个合成示例

下面的脚本生成虚构标的的数据，提交任务，等待完成，再校验并读取结果。数据库和产物放在临时目录，脚本退出后清理。

```bash
uv run --locked python scripts/smoke_backtest.py
```

需要保留请求和结果时，可以分步操作：

```bash
example_root=$(mktemp -d /tmp/backtest-example.XXXXXX)
uv run --locked python scripts/create_example.py --output "$example_root"
uv run --locked backtest-job \
  --database "$example_root/jobs.sqlite" \
  --artifact-root "$example_root/artifacts" \
  --result-root "$example_root/results" submit "$example_root/request.json"
```

保存输出中的 `job_id`。沿用相同的三个目录参数，把最后的子命令换成 `status <job-id>`、`cancel <job-id>`、`result <job-id>` 或 `recover`。`submit` 返回后，worker 在独立进程中运行，结果需要等状态变为 `SUCCEEDED` 后读取。

[完整请求示例](examples/request-v2.json)展示 v2 字段结构。使用生成脚本时，输入哈希和生产者提交号会按实际文件更新。

## 文档

- [请求、任务状态和结果校验](docs/jobs.md)
- [发布、回滚和故障排查](docs/operations.md)
- [开发检查与测试范围](docs/development.md)
- [仓库职责与迁移记录](docs/ownership.md)

生产调用使用稳定发布目录下的 `current/.venv/bin/backtest-job`。数据库、输入、结果、日志和凭证都放在发布目录之外。
