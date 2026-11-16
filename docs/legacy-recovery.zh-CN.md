# 旧版本恢复环境

[English](legacy-recovery.md)

历史恢复环境必须保持不可变。v9 是新增接口，不升级 v8 Platform checkpoint、
v1–v4 结果读取器、v7 租约过期终止语义或原生 Nautilus 校验器。禁止将旧 checkpoint
重新绑定到新 provider 或源码。

在私有 SOURCE 清单中保留完整 Runtime 提交、源码归档和 wheel、Go 工具链与二进制
SHA-256、Python 解释器字节与 ABI、Platform/core/SDK 的 VCS 身份和 wheel、全部
安装依赖的文件哈希与版本、锁文件哈希、使用的 Rust 提交/二进制、输入与合成夹具哈希
以及完整验证命令。版本号、提交声明或单个旧 provider wheel 都不足以构成恢复环境。
清单不能包含凭证，须保留 provider 校验所需的安装来源。清单和测试状态应遵循项目
数据目录约定，不放入源码，也不使用生产状态执行测试。

v8 保留基线为 Runtime `01ed4c34f955ef6cd39d13aa073b6d61b610d321`、0.1.0、
Go 1.27.3、Platform/research-contracts `7a618bddd8fdeee5dbe37fe5b54a9698e0de5355`。
恢复以当时的原始 `uv.lock` 为准。Runtime 0.2.0 保留独立 legacy 解析；新 Engine
环境不能替代历史完整环境。未修改的 v8 四边界测试须分别在保留的旧 wheel/二进制及
新 Runtime 的固定 legacy provider 环境运行。独立测试数据库防止旧调度器接触 v9。

| 协议 | 所有者与恢复要求 |
| --- | --- |
| v1–v4 | 固定 Platform、历史读取器和不可变 golden 字节 |
| v5–v6 | 精确 Rust 二进制及严格原生结果身份校验 |
| v7 | 精确 stream provider；租约过期仍终止，不隐式恢复 |
| v8 | Platform minute-session.v2 与原始 Runtime 完整环境 |
| v9 | Engine 公共会话 API、精确 owner checkpoint 字节与新环境 |

淘汰任何环境前，必须用清单记录的二进制、解释器和依赖证明合成恢复及完整账本一致。
即使当前消费者已迁移，也要保留历史清单和标识。合成验收不意味着生产切换。
