# 数据维护

GUI 从“设置 → 数据与高级”进入独立维护阶段；会先等待应用、桌宠、小窗和工作线程完全退出。CLI 的 `--maintenance` 不构造生产 Core，也不需要 Qt。

## 本地数据放在哪里

默认数据目录下有 `workflow.sqlite3`、`memory.sqlite3`、`scheduling.sqlite3`、`companion.sqlite3`、托管静态图片 `assets/` 和 `execution.jsonl`。工具文件在工作区；GUI 偏好通过 QSettings 保存；配置、`.env`、凭据和模型缓存各有独立位置。

备份前先正常启动一次应用再退出，初始化工作库。同一数据目录正被 GUI/CLI 使用时会拒绝维护，不要绕过实例锁复制运行中的单个数据库。

## 准备语义检索模型

模型固定为 `BAAI/bge-small-zh-v1.5`，用途是本地中文记忆检索。聊天 provider 和检索模型是两套独立设置。

### 当前工作区：使用已准备的模型

已验证的托管模型现位于 `data/models/bge-small-zh-v1.5`，本地 `config.toml` 已启用 `fastembed` 并引用该目录。原 `data/models/fastembed` 旧缓存保留，当前配置使用新目录。无需重新下载；先完全退出使用同一数据目录的 GUI/CLI，再在仓库根目录检查：

```powershell
conda activate deskpal_env
python -m deskpal --maintenance cache_status --config config.toml
python -m deskpal --maintenance cache_verify --config config.toml
```

`cache_verify` 会重新检查资产 hash 并执行实际 CPU 推理，成功回执为 `ready/verified_offline`。它不启动 Core，不调用聊天服务。当前路径已通过迁移后验证：512 维归一化向量、实际 CPU session、网络尝试 0，详见[真实链路与应用引用记录](../versions/v0.9/managed-embedding-live-report.md)。

### 新机器或尚无模型的配置

1. 在项目环境安装 `python -m pip install -r requirements-embedding.txt`。
2. 在维护页保存一个独立、空或不存在的缓存目录，再查看缓存状态。
3. 明确允许本次下载后选择“准备模型”；后台下载、校验、固定文本 CPU 推理后发布缓存。已有托管模型时使用“离线校验”，不要向非空目录重复准备。
4. 需要复查时执行“离线校验”。`verified_offline` 表示本次校验；`ready/previously_verified` 仅表示已有托管记录。
5. 将配置中的 `memory.embedding_provider` 设为 `fastembed` 后重新启动。应用仅使用本地缓存，缺模型/依赖时警告并回退词法检索，不静默下载。

```powershell
python -m deskpal --maintenance cache_status --config config.toml
python -m deskpal --maintenance cache_prepare --config config.toml --maintenance-parameters '{"allow_download":true}'
python -m deskpal --maintenance cache_verify --config config.toml
```

清理需要当前缓存 revision 和明确确认，仅删除清单归属且内容未变的资产。旧非托管缓存可验证，不能直接按托管缓存清理；未知文件、链接或内容变化会导致拒绝。缓存使用中会 Busy。准备/验证中的同步 SDK 需等待当前阶段返回后才能释放资源。

当前维护入口已补齐[真实 BGE 下载、托管登记与离线 CPU 推理验证](../versions/v0.9/managed-embedding-live-report.md)：约 95 MB 缓存、512 维有效归一化向量、离线网络尝试为零。物理桌面点击和显示仍单独验收；模型设置及历史 adapter 记录见[Embedding 指南](../guides/embedding-setup.md)。

## 创建四库备份

模型资产不包含在四库备份中，也不随 Git 克隆分发。换机器或恢复备份后，仍需准备模型并核对实际配置路径；不要只复制 ONNX 文件而遗漏 tokenizer、配置和托管清单。

维护页选择“创建四库备份”，确认含敏感内容，选择尚不存在的文件，建议后缀 `.deskpal-backup.zip`。CLI 示例中的父目录需已存在，路径按你的环境替换：

```powershell
python -m deskpal --maintenance backup_create --config config.toml --maintenance-parameters '{"destination":"E:/DeskpalArchives/first.deskpal-backup.zip","acknowledge_sensitive_data":true}'
```

| 备份包含 | 需要另行保存 |
| --- | --- |
| workflow、memory、scheduling、companion 四库、当前头像/桌宠托管 PNG 和 manifest；旧数据无 companion 时明确记录缺失 | 工作区原文件、TOML、`.env`、系统凭据、模型缓存、JSONL 日志、GUI 偏好 |

本轮源码使用格式 2，图片与数据库在同一静止点保存，恢复先校验全部引用再允许启动；仍可读取旧格式 1 四库归档，旧归档没有图片时回退内置默认。跨机器恢复需要重新配置密钥。本轮格式演进待集中测试，旧格式历史测试结论不覆盖它。

归档未加密、未签名，包含私人记忆、任务、来源和删除工作单。完整性 hash 不能证明归档来源可信，恢复时只选择可信归档。

## 恢复到新目录

1. 选择备份并检查归档。
2. 选择已存在的父目录，输入尚不存在的新目标目录；不要覆盖现用数据目录。
3. 确认回退影响，尤其是旧备份可能重新引入后来遗忘的内容。
4. 恢复成功后选“使用恢复目录”，检查配置并明确保存，再启动。

```powershell
python -m deskpal --maintenance backup_inspect --maintenance-parameters '{"archive":"E:/DeskpalArchives/first.deskpal-backup.zip"}'
python -m deskpal --maintenance backup_restore --config config.toml --maintenance-parameters '{"archive":"E:/DeskpalArchives/first.deskpal-backup.zip","target_dir":"E:/Deskpal/restored-data","acknowledge_data_rollback":true}'
```

恢复不授予工具权限、不自动重放任务、不补发云请求，外部文件或工具效果也不随数据库回滚。启动后按[任务恢复规则](tasks-and-permissions.md)逐项复核。单库超过 512 MiB、总库超过 1 GiB 或 schema 不受支持时拒绝。

中途失败会留下 `.restore-in-progress.json`/stage，生产入口会阻止启动该目录。选择原归档和原未完成目录，明确继续恢复；CLI 在原参数中增加 `"resume":true`。不要删除标记伪装成功。内容冲突时保留现场，另选新目标恢复。

配置写入、缓存清理等完整参数见[维护操作契约](../versions/v0.9/operations.md)。常见固定错误码见[排错指南](troubleshooting.md)。
