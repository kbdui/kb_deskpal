# 存储、隐私与维护

## 四库及其他文件

| 位置 | 保存内容 | 主要代码 |
| --- | --- | --- |
| `workflow.sqlite3` | Task/Submission/Plan/attempt、目标/绑定/提议、依赖、inbox/cursor、删除工作单、outbox、回合/预算/隐私/效果账本 | [workflow/](../../src/deskpal/workflow) |
| `memory.sqlite3` | 记忆断言、来源、revision、用途、审核、索引与画像相关记录 | [memory/repository.py](../../src/deskpal/memory/repository.py) |
| `scheduling.sqlite3` | 日程、计时器、到期事件及来源库身份/世代 | [scheduling/service.py](../../src/deskpal/scheduling/service.py) |
| `companion.sqlite3` | 人格版本、有限模拟情绪与持久互动设置 | [companion/repository.py](../../src/deskpal/companion/repository.py) |
| `execution.jsonl` | 生产元数据审计观察副本 | [application/audit.py](../../src/deskpal/application/audit.py) |
| 配置 / `.env` / Windows 凭据 | 非秘密设置、外置密钥或系统引用 | [config/](../../src/deskpal/config)、[credentials.py](../../src/deskpal/credentials.py) |
| QSettings | GUI 几何、偏好、通知去重 | [gui/preferences.py](../../src/deskpal/gui/preferences.py) |
| 工作区 / 模型缓存 | 工具文件和检索资产，各自策略管理 | [sandbox/](../../src/deskpal/sandbox)、[maintenance/cache.py](../../src/deskpal/maintenance/cache.py) |

SQLite 运行中还可能存在 WAL/SHM 文件。不要把单个主库文件复制当成运行中一致备份。完整聊天档案不在本版范围；有界会话内存与持久任务/上下文工作副本必须区分。

## 事务、版本和锁

WorkflowStore 使用 WAL、foreign keys 和 FULL synchronous；嵌套事务复用连接，内部失败使外层回滚。Proposal 接受与唯一 Task/Submission 创建属于同事务。跨 Memory/Scheduling/Workflow 的预检不是全局事务，因此需要在执行边界重新验证来源。

Goal 的 revision 随写入递增，execution_version 仅随状态/执行条件变化。备注变化不能粗暴失效所有兄弟工作；Milestone/Binding 有独立作用域。修改语义时应先理解依赖版本与失效范围。

`InstanceLock` 在数据库初始化之前获取，以规范 `data_dir` 对应的 OS 文件锁保证单 owner。Windows 用 msvcrt，非 Windows 用 fcntl；异常退出由 OS 释放锁，不通过 PID/过期时间自行抢占。

模型缓存是独立资源：[memory/cache_lock.py](../../src/deskpal/memory/cache_lock.py) 用共享使用租约和独占维护租约，Windows 使用 LockFileEx，Unix 使用 flock。不同 data_dir 共享缓存仍需锁，不能依赖数据目录锁代替。

## BGE 资产与应用引用

当前工作区已将通过实测的托管 BGE 从诊断目录迁入 `data/models/bge-small-zh-v1.5`，本地 `config.toml` 的 `memory.embedding_provider="fastembed"`、`memory.model_cache_dir="data/models/bge-small-zh-v1.5"` 已生效。保留的 `data/models/fastembed` 是旧缓存，不是当前配置的模型来源。代码与 `config.example.toml` 的默认值仍为 `disabled` 和 `data/models/fastembed`；新克隆不会获得忽略的权重、配置或恢复副本。

装配由 [config/__init__.py](../../src/deskpal/config/__init__.py) 解析配置，再由 [application/factory.py](../../src/deskpal/application/factory.py) 在启用 `fastembed` 且没有注入 embedding 时创建 `FastEmbedProvider`，交给 `MemoryRetriever`，随后由 `MemoryCoordinator` 使用。模型路径按配置文件父目录解析。初始化失败提供启动 warning 并使用词法检索；正常运行只加载缓存，不自动下载、不改变聊天 provider 或来源用途许可。provider 从构造到 `close()` 持共享租约，应用负责关闭。

| 对象 | 用途 |
| --- | --- |
| 缓存根目录 `.deskpal-model-cache.json` | 托管资产/目录清单、大小和 SHA-256；文件 SHA-256 作为维护 revision，用于完整性复核及限定清理归属 |
| 运行时 `EmbeddingManifest(model, dimensions, version)` | 模型/维度、资产 digest 和预处理版本，参与索引版本匹配；不能用缓存目录名代替 |

准备流程为独占租约下下载到 stage、实际 CPU 校验、清单登记后发布；复查为资产 hash＋本次 CPU 推理，只有 `verified_offline` 表示本次验证。清单哈希不证明模型发布者签名。搬迁要保留完整缓存树及 manifest，校验源/目标边界，持相应实例/缓存锁，通过配置 CAS 更新引用并复核实际生效路径；不要合并未知旧目录或只搬 ONNX 文件。

当前新路径已通过实际维护校验和 CPU 512 维归一化向量验证，网络 guard 计数 0，迁移前后清单 revision 相同，见[真实链路及应用引用](../versions/v0.9/managed-embedding-live-report.md)。独立诊断脚本产生的合成 root 不会自动修改正式配置；模型移出后不能再对旧 root 执行诊断 `--offline`。应用缓存使用 `python -m deskpal --config config.toml --maintenance cache_verify` 复查，先完整退出同 data_dir 应用；此入口不开私人四库或 Core。

## 遗忘和可靠投递

隐私入口先持久化 epoch/阻断和删除工作单，使旧计划、回调和待派发数据失效，再删除记忆来源闭包、明确派生工作副本及相关缓存。失败保留 Pending，启动先续办；解除阻断必须确认没有未完成删除。

独立 Goal/Calendar 不按相似文字删除；历史日志、备份、导出、外部副本有独立保留边界。不能把“记忆库里已无该行”当作全流程遗忘完成。

生产审计状态与 outbox 同事务。JSONL 投递失败可用同 event ID 重投，允许重复物理行；日志不是执行权威，更不能根据 outbox 重放工具。删除未完成时暂停相应投递和正文展示。

## 独立维护实现

[maintenance/service.py](../../src/deskpal/maintenance/service.py) 的 `MaintenanceService.execute()` 用明确操作白名单与参数集合提供配置、缓存、归档和诊断，不构造生产 Application/Core。异常经 `safe_error()` 转成固定 code/message/next_steps，不返回底层异常中的路径、请求或秘密。

- [setup.py](../../src/deskpal/maintenance/setup.py)：配置文件 CAS、原文保留、损坏文件明确修复、文件值/生效值区分；配置 revision 与数据库 revision 不同。
- [credentials.py](../../src/deskpal/credentials.py)：可选当前用户 Windows 系统凭据，TOML 只保存引用；环境密钥优先，不明文降级。
- [cache.py](../../src/deskpal/maintenance/cache.py)：固定模型显式下载到 stage，hash/离线推理/manifest 后发布；清理只接受自有且未变资产，拒绝链接/未知内容。
- [archives.py](../../src/deskpal/maintenance/archives.py)：协调四库快照、manifest、可信归档检查、新目录恢复及可续办标记。
- [cli.py](../../src/deskpal/maintenance/cli.py)：有界、唯一键、有限 JSON；秘密用交互隐藏输入，不接受命令行 JSON `credential_secret`。

## 归档与恢复屏障

四库备份使用数据目录实例锁和 SQLite backup 形成快照，包含 manifest；历史 companion 缺失可明确表示。工作区、配置、凭据、模型缓存、日志和 GUI 偏好不在归档中。归档未加密未签名，hash 只校验完整性。

恢复只发布到新目标目录。中断留下 `.restore-in-progress.json`/stage，只有相同归档、相同清单/hash 的明确 resume 才续办；`create_application()` 在开库前拒绝未完成目录。不能删除标记绕过屏障或用未知内容覆盖已有目标。

当前单库上限 512 MiB、总库上限 1 GiB，不支持的 schema 拒绝。恢复不会回滚外部效果，也不会恢复会话授权或自动执行旧任务。

修改 schema 时查阅 [workflow/migrations.py](../../src/deskpal/workflow/migrations.py) 及各库初始化实现，补旧数据迁移和中断恢复验证。维护接口完整定义见[V0.9 接口](../versions/v0.9/interfaces.md)。
