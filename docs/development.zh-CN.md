# 开发与检查

[English page](development.md)

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
uv run --locked --group docs python project_tools/check_locale_navigation.py
git diff --check
```

CI 在 Linux 的 Python 3.12 和 3.13 环境运行上述静态检查、测试和 CLI 示例。依赖安全审计在 Python 3.13 环境执行。

文档站点由 `.github/workflows/docs.yml` 构建并发布到 GitHub Pages。站点构建使用严格模式，发现失效的内部链接时会失败。

PR 也会构建文档，检查中英文侧栏、HTML 语言和主题切换文案。语言只影响展示，不改变请求字段或结果数据。页面 hook 保留主题方案与图标，对不带 `zh-CN` 后缀的文档路径使用英文。PR 检查不发布站点。

Ruff 检查常见错误、导入顺序、易错写法、过时语法和复杂度，单个函数的圈复杂度上限为 10。ty 检查运行时代码与维护脚本。JSON 动态字段仍有 `Any`，其内容约束由请求校验和测试共同保证。

## 覆盖率与测试边界

coverage 同时统计语句和分支，并启用 Python 子进程采集。独立 worker 测试的数据通过 `coverage combine` 合并。CLI 为子进程限制环境变量，部分间接启动的 worker 不会继承采集配置，因此还保留直接启动 worker 的集成测试。

当前总覆盖率门槛为 75%，按语句和分支合并计算。提高门槛应以补充行为测试为前提，不能用排除代码或删除断言来达标。操作系统强制杀死的进程可能无法写出覆盖率数据。

测试覆盖请求指纹和幂等性、状态更新竞争、取消、超时、启动失败、过期恢复、结果篡改，以及 v1 至 v4 的实际子进程回放，包括序列执行与成交记账结果校验。`scripts/smoke_backtest.py` 另外验证安装后的 CLI、独立 worker 和示例数据是否能完整协作。

## 依赖审计边界

`pip-audit --local` 审计已安装环境中的已知漏洞，发现漏洞会使 CI 失败。`quant-backtest-runtime`、`quant-platform` 和 `research-contracts` 通过本地源码或 Git 安装，目前没有可供 PyPI 漏洞数据库匹配的发行记录，工具会列出跳过原因。这些包需要结合固定提交、代码审查和各自测试维护，安全审计通过不代表它们已经完成漏洞扫描。

## 模块职责

| 模块 | 职责 |
| --- | --- |
| `contracts.py` | 请求字段、输入引用、研究时钟与指纹校验 |
| `jobs.py` | 任务服务、取消、租约恢复与输入解析 |
| `store.py` | SQLite 持久化和带前置状态条件的更新 |
| `worker.py` | 资源限制、后端调用和结果发布 |
| `results.py` | v1 至 v4 结果完整性校验 |
| `cli.py` | 命令解析和 worker 启动 |

`jobs.BacktestJobRequest` 保留导入兼容性，新代码可直接从 `contracts` 导入。提交 PR 时说明行为变化、验证命令和结果，跨仓修改附上关联 PR。

## 可选 Rust 引擎任务（v5）

新增 `nautilus.quote_market_orders` 后端，调用独立的
[quant-backtest-engine](https://github.com/runchengxie/quant-backtest-engine)。
旧 v1–v4 的规范化和请求指纹保持不变，新版本只提供 diagnostic 最终快照，
不能当作 v2 的完整每日执行账本。

由运维通过 `--engine-executable` 指定绝对路径；请求只能通过
`config.executable_sha256` 固定二进制内容，不能提交任意命令。
`inputs.engine_request_ref` 引用标准 SHA-256 目录中的不可变引擎 JSON 请求，
`execution` 为空对象。完整请求示例与命令见本页对应的
[英文说明](development.md#independent-rust-engine-jobs-v5)。

Python worker 继续独占 SQLite 状态和结果发布；Rust 子进程继承地址空间限制，
取消和超时会终止并回收它，父进程被硬杀时使用 Linux parent-death SIGKILL 清理。
这只面向当前单线程 Linux worker，不宣称能安全运行不可信程序。
二进制哈希固定的是构建字节，部署记录仍需保存源提交和工具链。

结果采用独立的 `quant.engine_job_result.v1` 清单，包含 engine_result.json、
大小、哈希和资金摘要；读取时重新核对完整性。任务成功不代表所有订单都成交。
A 股交易规则和日频账本尚未完成新引擎兼容验证，不能自动切换旧任务。

设置 `BACKTEST_ENGINE_TEST_EXECUTABLE` 可运行真实引擎集成测试；专门的 CI
会构建固定的已合并提供方提交并验证，其他测试覆盖超时、取消、父进程硬杀和非法输出。


## v5 静态预检

`plan` 和绑定计划摘要的 `submit` 在打开任务数据库前调用引擎的 `validate -`，
不运行回测，也不创建任务或临时输出。输入最多 1 MiB，读取后校验摘要，再把同一份字节
送入已经校验 SHA-256 的可执行文件。进程通过保持打开的文件描述符启动，环境变量受限，
先通过同一描述符调用 `capabilities`，确认 `validator_version=1`，再校验请求；
两次调用共用 10 秒期限，每次的标准输出和错误输出各限 8 KiB。超限或超时会终止并回收进程。

成功的 v5 计划把 `engine_request_semantics` 标为 `passed`，并把
`engine_validation.v1` 回执（`validator_version=1` 和输入摘要）纳入计划摘要。
语义错误返回 `ENGINE_REQUEST_INVALID`；不兼容的引擎、错误回执或进程故障返回
`ENGINE_VALIDATION_UNAVAILABLE`，不会退回未检查状态。v1–v4 的计划摘要和所有请求指纹
保持兼容。部署必须使用不可原地修改的引擎文件：描述符只能防止路径替换，不能防止
对已打开文件的原地写入。执行阶段仍会重新核验输入、引擎和请求语义。


## 逐日账本任务（v6）

v6 使用 nautilus.session_replay，输入是不可变的 engine_request.v2 JSON。
其余任务字段沿用 v5：固定二进制 SHA-256、空 execution 对象和明确的资源预算。
旧 v1–v5 的规范化与指纹不变，能力发现和无副作用预检增加 v6。

worker 保留相同的子进程控制和唯一发布职责，结果采用 quant.engine_job_result.v2。
发布前校验币种、原始价、时区、T+1 标志、交易日覆盖、订单与成交数量、拒单原因，
并按上游的逐笔货币取整规则重建现金与股数来对账。
结果还与输入资金、币种、订单和时钟绑定，T+1 与前日持仓独立核对。
这些检查只验证结果，不重新决定交易或撮合订单。

合成示例首日现金 8999、净值 10099，同日卖单因 T+1 被拒绝，次日卖出后现金和
净值均为 10197.8。完整逐日 JSON 保存在清单库存中，摘要保留通用资金字段。

逐日结果不自动升级为研究执行证据。公司行为应收款、退市、日成交量参与率和历史
税率仍需专门后端，不能因部分市场约束可用就自动替换旧任务。
CI 固定已合并提供方 3ee74bfa21d743e595ce61d0ac06b97dd588ebd6，同时验证 v5 与 v6。
