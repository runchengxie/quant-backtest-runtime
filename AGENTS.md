# quant-backtest-runtime 工作规则

本仓库维护通用回测任务协议、SQLite 状态、worker、资源控制、产物校验、CLI 和发布工具。策略、研究来源记录和晋升规则属于 `quant-research`。可复用的回测算法、执行模拟和结果协议属于 `quant-platform`。

## 开发与交付

- 先检查工作树、分支、远端和已有 worktree，保留其他任务的改动。
- 拉取远端后，从 `origin/main` 创建独立分支和 worktree。临时 worktree 放在 `/home/richard/code/.worktrees/`。
- 使用锁定依赖，在任务 worktree 完成修改和验证。检查命令见 [开发文档](docs/development.md)。提交前必须通过 Ruff、ty、pytest 覆盖率检查和合成数据 CLI 检查。
- 提交并推送任务分支，通过面向 `main` 的 PR 交付。审查和必需检查完成后合并，不跳过 hooks、不直推 `main`、不强推争抢合并。
- 确认 PR 已合并且工作树没有唯一未保存内容后，清理本任务分支和 worktree。主检出干净时才 fast-forward 同步。
- 跨仓修改分别开 PR，先合并提供接口或文档的仓库，再更新调用方。

## 运行与发布

- 只部署已经进入 `origin/main` 的提交，发布目录使用完整提交号。
- 数据库、输入、结果、日志和凭证放在发布目录之外。
- 切换前先执行 dry run 和合成数据 CLI 检查，保留可回滚版本。
- 定时任务引用稳定的 `current` 路径，不引用开发 worktree。
- 公开示例只使用合成数据，不复制私有研究参数、策略实现或真实数据。
