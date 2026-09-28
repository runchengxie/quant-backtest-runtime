# 开发与检查

## 本地检查

在独立任务 worktree 根目录执行：

```bash
uv sync --locked --group dev --python 3.13
uv run --locked ruff check src scripts tests
uv run --locked ruff format --check src scripts tests
uv run --locked ty check src scripts
uv run --locked coverage erase
uv run --locked coverage run -m pytest -q
uv run --locked coverage combine
uv run --locked coverage report
uv run --locked python scripts/smoke_backtest.py
uv run --locked pip-audit --local
uv run --locked --group docs mkdocs build --strict
git diff --check
```

CI 在 Linux 的 Python 3.12 和 3.13 环境运行上述静态检查、测试和 CLI 示例。依赖安全审计在 Python 3.13 环境执行。

文档站点由 `.github/workflows/docs.yml` 构建并发布到 GitHub Pages。站点构建使用严格模式，发现失效的内部链接时会失败。

Ruff 检查常见错误、导入顺序、易错写法、过时语法和复杂度，单个函数的圈复杂度上限为 10。ty 检查运行时代码与维护脚本。JSON 动态字段仍有 `Any`，其内容约束由请求校验和测试共同保证。

## 覆盖率与测试边界

coverage 同时统计语句和分支，并启用 Python 子进程采集。独立 worker 测试的数据通过 `coverage combine` 合并。CLI 为子进程限制环境变量，部分间接启动的 worker 不会继承采集配置，因此还保留直接启动 worker 的集成测试。

当前总覆盖率门槛为 75%，按语句和分支合并计算。提高门槛应以补充行为测试为前提，不能用排除代码或删除断言来达标。操作系统强制杀死的进程可能无法写出覆盖率数据。

测试覆盖请求指纹和幂等性、状态更新竞争、取消、超时、启动失败、过期恢复、结果篡改，以及 v1 和 v2 的实际子进程回放。`scripts/smoke_backtest.py` 另外验证安装后的 CLI、独立 worker 和示例数据是否能完整协作。

## 依赖审计边界

`pip-audit --local` 审计已安装环境中的已知漏洞，发现漏洞会使 CI 失败。`quant-backtest-runtime`、`quant-platform` 和 `research-contracts` 通过本地源码或 Git 安装，目前没有可供 PyPI 漏洞数据库匹配的发行记录，工具会列出跳过原因。这些包需要结合固定提交、代码审查和各自测试维护，安全审计通过不代表它们已经完成漏洞扫描。

## 模块职责

| 模块 | 职责 |
| --- | --- |
| `contracts.py` | 请求字段、输入引用、研究时钟与指纹校验 |
| `jobs.py` | 任务服务、取消、租约恢复与输入解析 |
| `store.py` | SQLite 持久化和带前置状态条件的更新 |
| `worker.py` | 资源限制、后端调用和结果发布 |
| `results.py` | v1 与 v2 结果完整性校验 |
| `cli.py` | 命令解析和 worker 启动 |

`jobs.BacktestJobRequest` 保留导入兼容性，新代码可直接从 `contracts` 导入。提交 PR 时说明行为变化、验证命令和结果，跨仓修改附上关联 PR。
