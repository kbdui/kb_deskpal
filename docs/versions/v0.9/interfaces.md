# 产品化维护接口

2026-10-23。实现已整合，集中验证进度与证据见[测试记录](test-report.md)。此接口增量不改变既有 Task/Plan、权限、记忆、人格和七字段 presentation 契约。

## 调用与生命周期

`deskpal.maintenance.service.MaintenanceService(config_path=None, *, cancelled=None, provider_override=None).execute(operation, parameters) -> dict` 为不依赖 Qt 的同步维护入口。调用者负责后台调度；它不创建生产 ApplicationService、Core 或云模型。`provider_override` 仅允许本次 `offline`，供 GUI `--offline` 的启动前置校验使用，不写回文件。异常转交 `safe_error` 后返回固定 `{code, message, next_steps}`，不使用任意异常正文。

| operation | parameters |
| --- | --- |
| `setup_status` | `{}` |
| `setup_save` | `values`, `expected_revision`，可选瞬时 `credential_secret` |
| `setup_repair` | 同上，另须 `confirm: true` |
| `cache_status` / `cache_verify` | `{}` |
| `cache_prepare` | `allow_download: true` |
| `cache_clear` | `expected_revision`, `confirm: true` |
| `backup_create` | `destination`, `acknowledge_sensitive_data: true` |
| `backup_inspect` | `archive` |
| `backup_restore` | `archive`, `target_dir`, `acknowledge_data_rollback: true`，可选 `resume: true` |
| `diagnostics` | `{}` |

拒绝未知操作和参数、非有限 JSON、过大输入。服务参数最多 128 KiB，结果最多 4 MiB、深度 16；GUI 通道另收紧为请求 64 KiB、结果 256 KiB，并保持最多一个未完成请求和一个结果。界面依 request_id/generation 拒绝过期回执，关闭后不处理成功迟到结果。

GUI 启动器串行拥有 DesktopHost 或 MaintenanceWindow。Host.finished 发生在后端 Closed 和桌宠资源完成释放之后；维护 finished 则在其线程实际结束之后。同步 SDK 调用不强杀，关窗请求设置取消标志并保持事件循环至后台结束。维护不接入既有运行命令队列执行模型或工具。

主应用 `startup` 失败与维护窗口使用相同固定故障分类：同目录实例竞争返回 `code:"busy"` 及 `next_steps`，不再将 `InstanceLockError` 等异常类名作为启动回执 code。既有运行中命令的分类契约保持原样；固定启动 code 不影响 `StartupFailed` 状态、所有权或完整退出等待。

## 配置和凭据

`SetupService(Path).inspect/save/repair` 返回安全字段 `values/revision/config_exists/config_valid/credential_present/credential_source/sources/issues`。维护 facade 增加权威 `can_start` 与经过校验的 `effective`；它们只表示本地前置条件，保存和读取不验证云端账号或远端模型。环境覆盖是提示，不自动阻止合法有效配置启动；未完成恢复标记则阻止启动。`configuration_reset/local_recovery_copy` 标识保存行为。

`values` 仅含 provider/model/base_url/api_key_env/credential_ref/timeout_seconds/expression_output_format/workspace_dir/data_dir/embedding_provider/model_cache_dir。文件修订为 SHA-256，缺文件为 `missing`。写入先验证、规范路径、按顺序取得配置 OS 租约和旧/新/有效数据目录实例锁，再复核修订并发布；旧文件保存原文恢复副本。普通保存保留未知设置/注释，无法证明语义保留时拒绝；明确修复重建受支持设置。数据与工作区不能互含；缓存可位于 data 子目录，不能覆盖 data 或与工作区重叠。

`WindowsCredentialStore` 通过 WinCred 读取/写入当前用户保护的 generic credential。引用限 `Deskpal/LLM/<32位小写十六进制>`，UTF-16 内容最多 2560 bytes，拒绝控制字符；TOML 和回执不保存密钥。环境/.env 的所选 key 优先，只有 openai 且环境无 key 时才读引用。显式传 `load_config(..., environ=...)` 或 offline override 不读系统凭据。新引用先写库，配置发布失败尽力删新引用，旧引用不自动删除；清理失败不回显秘密。

`llm.url_policy.validate_base_url` 为 provider 与向导共用的纯校验：HTTPS 或回环 HTTP，禁止 userinfo/query/fragment、无效端口等，不做 DNS 或网络请求。

## 模型资产

`ModelCacheManager(Path).status/verify/prepare/clear` 仅管理 `BAAI/bge-small-zh-v1.5` 与 FastEmbed 0.9.0 CPU。状态含 `status/revision/managed/files/bytes/model/purpose/install_command/verification/dependency_available`。`ready + previously_verified` 只表示托管元数据记录曾成功验证；`verified_offline` 表示本次 hash 及固定公共文本 CPU 推理成功。非托管旧缓存可离线校验，但不取得任意删除资格。

准备先持独占 cache lease，下载到同父目录新 stage，离线推理和完整清单通过后发布至空/不存在的目标。manifest 为 `.deskpal-model-cache.json`，记录资产路径、目录、大小和 SHA-256；文件/目录数量与字节受限。清理必须带 manifest 的 revision，拒绝链接、未知目录/文件、剩余资产 hash/大小不符；仅允许 manifest 声明且已被先前清理删除的条目缺失。资产和子目录先删，manifest 最后删，成功保留空 root；因此部分失败可用相同修订明确重试。

`CacheLease(cache_dir, exclusive=...)` 在缓存同级持久锁文件上使用 OS 共享/独占锁；不删除锁文件。Windows 使用 `LockFileEx/UnlockFileEx` 的立即失败共享/独占字节区间锁，Unix 使用 `flock`，不用 CRT 的读锁别名冒充跨进程共享。FastEmbedProvider 从初始化到 close 持共享锁，构造失败释放；同进程共享读者引用计数避免提前解锁。获取/释放失败仍回收 fd/registry。维护昂贵/写操作还持已配置 data_dir 的 InstanceLock，跨数据目录共享 cache 亦受缓存锁保护。

## 备份与恢复

`create_backup(data_dir, destination, *, acknowledge_sensitive_data=False)`、`inspect_backup(archive)`、`restore_backup(archive, target_dir, *, acknowledge_data_rollback=False, resume=False, current_data_dir=None, fault_hook=None)`。`fault_hook` 仅为固定阶段名故障注入缝隙，不接收数据库内容。

备份/检查返回 `{format_version:1, archive_path, archive_sha256, manifest}`；恢复另返回 `status:"restored"/target_dir/startup_review_required:true/privacy_deletion_completed:false`。manifest 固定记录 workflow/memory/scheduling/companion 四个库的存在、bytes/sha256/schema_version，以及 scope/exclusions；前 3 库必需，历史缺 companion 可显式记 absent，已有损坏 companion 不可跳过。

源应用完全关闭后获取 data_dir InstanceLock，SQLite online backup 收集各库及 WAL 的已提交数据。由于没有生产写者，四份快照处于同一应用静止点；不承诺与绕开实例锁的外部写入者协调。只读检查 quick_check 与已知 schema，不通过构造 store 偷做迁移。归档无覆盖发布，ZIP 限 manifest+最多四库，单库 512 MiB、总库 1 GiB、manifest 16 KiB；拒绝 ZIP64、未知附件、重复/大小写冲突、路径穿越、链接和超额内容。哈希不提供来源认证。

恢复完整校验后创建新目标，持目标锁写 `.restore-in-progress.json`，在 `.restore-stage` 暂存并逐库无覆盖发布；标记记录归档 hash、manifest 和已发布列表。失败保留标记；续办要求原归档和一致清单，未知文件、已发布库丢失或内容变化均拒绝。仅对严格对应的中断发布双硬链接进行专门恢复，其他硬链接不接受。不覆盖现用目录，不递归删除恢复目标。

当前目录与目标分离时，只检查其所有权；当前目录不存在则不创建它，损坏或 Pending 仍允许恢复。当前目录等于目标时仅允许显式 resume，目标只获取一次锁。`assert_restore_ready(data_dir)` 在生产工厂取得实例锁后、构造任何数据库前拒绝 `.restore-*` 项。完整校验结束最后清标记；采用新 data_dir 需另行保存配置。正常启动继续沿用原迁移、NeedsReview/Uncertain、遗忘 Pending 和授权清空规则。
