# Engine 财务会话（Go v9）

[English](engine-financial-sessions.md)

新建 Engine 分钟任务使用 v9。Runtime 0.2.0 的 `engine` extra 将 core `[minute]`
与 SDK 固定在已合并的 `292a67aebe534769d9be7c9fea72bd94fc20c8f6`。
`legacy` 仍固定 Platform `7a618bddd8fdeee5dbe37fe5b54a9698e0de5355`。
两个 extra 必须安装在独立环境，避免 Engine 的 pandas 限制改变旧环境。

只有 Go CLI 支持 v9，Python CLI 仍支持 v1–v6。Go capabilities 的
`engine_financial_session` 给出接口与隔离要求。操作员配置绝对路径
`--python-executable`；请求不能指定模块、命令或自定义策略实现。v9 忽略配置的
Rust 可执行文件。完整合成请求见 [English 页面](engine-financial-sessions.md)，
产品 manifest 与日历在 `tests/fixtures/engine-financial/`。

将 manifest 原始字节放入 `artifact-root/sha256/<digest>`，输入字段为
`product_manifest_ref`，后端为 `engine.minute_session_resume`，配置包含固定的
`provider_revision`、`checkpoint_format=engine-financial-checkpoint.v1`、
`policy=target-weights.v1` 和 `provider_binding_schema=runtime-engine-binding.v1`。
产品引用路径相对于 artifact root；provider 校验日历、输入与事件带的哈希和能力。

绑定包括 manifest、引用输入/事件带/日历的实际字节、请求配置、core/SDK/依赖文件、
Runtime Python 源码、Python 解释器和 Go 可执行文件字节及声明的源码版本。
core/SDK 必须来自固定 VCS 提交或已审核 wheel，安装文件必须与 RECORD 一致。
从 wheel 安装时须保留原路径的 wheel 供校验。这是本地来源验证，不是签名或运行
不可信代码的沙箱。`plan` 只构建会话，不运行回测；缺少或不兼容 provider 时在创建
数据库之前失败。适配器只计算和写暂存文件，Go 独占数据库、checkpoint 准入及发布。
财务计算、小数字符串与纳秒字符串转换均归 Engine 所有。

## 恢复与隔离

**v9 必须使用独立数据库、结果目录和 worker 环境。** 旧 v8 调度器遇到未知版本
可能将队列记录改为失败；不能让它访问 v9 数据库。新 worker 的筛选无法改变旧二进制。
本次交付不迁移生产数据库，也不切换服务。

外层 `runtime-engine-checkpoint.v1` 包含任务、attempt/token、绑定、游标和内部哈希；
base64 精确保留 `engine-financial-checkpoint.v1` 原始字节。v8 外层与 Platform
checkpoint 仍是独立协议。成功 step 的字节先进入暂存；失败后保留原异常，不向已失败
会话请求新 checkpoint。

持久化顺序为写候选、文件 fsync、不覆盖 rename、目录 fsync、SQLite 准入、确认。
未准入文件只是孤儿文件，不能作为恢复来源。租约过期后旧任务转为 FAILED，新任务用
新幂等键和 `execution.resume_from`，只读取失败且未取消前驱的最后一个兼容准入点，
并获取独立 token。丢失提交确认后重复提交仍返回同一后继。取消、源码/配置/输入变化、
checkpoint 篡改和跨 provider 复用均拒绝恢复；必须使用原始完整环境或从输入重跑。

`quant.engine_financial_job_result.v1` 引用产品结果及完整路径/大小/SHA-256 清单，
包含 owner 原始账本。读取时校验清单和任务/请求身份；发布沿用持久化 rename 和 token
约束的终态 CAS。测试专用 `__worker --financial-fault` 支持四个真实 SIGKILL 边界：
写入前、fsync 后、rename 后、准入后确认前；普通命令拒绝该参数。进程测试比较连续运行
与恢复后的完整产品表格和原始账本。

验证见 [开发文档](development.zh-CN.md)，历史读取与环境保留见
[旧版本恢复](legacy-recovery.zh-CN.md)。

每个 v9 子进程同时使用 `-B` 和全新空目录 `-X pycache_prefix`，退出后删除该目录。
即使环境中存在时间戳/大小均有效的旧字节码，也会执行源码；单独 `-B` 仍会读取缓存。
固定 provider、Runtime 与依赖包中只有字节码、没有源码的模块会被拒绝。进程回归测试
先证明普通 `-B` 会执行被改写但仍有效的缓存，再证明 Go 调用执行原始可审计源码。

从绑定旧 Engine 的部署升级时，为这个版本使用单独的队列数据库、输入目录和结果目录。旧任务使用原来的 Runtime 与 Engine 安装恢复。
