# 首次配置、模型缓存与四库维护

2026-10-23。以下是本轮新增实现的操作约定，**尚未试运行/验收**。本轮开发没有执行这些命令。产品版本仍为 `0.5.0rc1`；安装依赖仍参考[依赖维护](../../guides/dependencies.md)，旧功能参考 [V0.5 操作说明](../v0.5/operations.md)。

## 配置向导

```powershell
python -m deskpal.gui
python -m deskpal.gui --setup --config E:/Deskpal/config.toml
```

未指定路径时优先使用当前目录已存在的 `config.toml`；否则 Windows 使用 `%LOCALAPPDATA%/Deskpal/config.toml`，其他平台使用 `$XDG_CONFIG_HOME/deskpal/config.toml` 或 `~/.config/deskpal/config.toml`。首次缺文件进入向导，显式 `--setup` 可进入维护。普通 CLI 使用同一已存在默认文件；没有默认文件时保留原离线默认行为，不自动保存配置。

主窗口“配置与数据维护”会先完整关闭应用和桌宠资源，再打开维护。若后台同步调用尚未结束，仍等待其协作退出；不能同时启动第二个后端维护同一目录。配置错误造成 StartupFailed 时，主窗口保留维护入口与固定指引。

选择 offline 为确定性演示，选择 openai 为兼容 Responses 服务。填写实际支持的模型与 HTTPS API 根地址；本机回环服务可使用 HTTP。保存不会联网探测模型，不代表远端凭据/协议可用。工作区、数据目录、缓存目录会检查互含与链接；默认缓存允许在数据目录下。

文件值和当前生效值分别显示。优先级仍为进程环境 > 配置目录 `.env` > TOML > 默认；环境覆盖时改 TOML 不会覆盖环境，应在来源处更改后重新读取。`--offline` 仅覆盖本次运行及维护启动校验，不修改文件的聊天 provider。配置保存使用文件 revision，冲突后刷新并重新编辑。有未保存编辑时禁用启动；刷新按钮会放弃未保存编辑，保存过程中输入控件锁定。

密钥可沿用原环境变量/`.env`，或在 Windows 密码框输入新密钥保存到当前用户的系统凭据库。环境 key 优先；TOML 只写引用，维护回执不返回密钥。密码框提交后清空，清空输入框本身不删除已存凭据。旧引用不会自动删除，可在 Windows 凭据管理器按 `Deskpal/LLM/…` 识别；不要在不确定引用用途时删除它。系统凭据不可用时改用环境变量，没有明文降级保存。

普通保存保留其他设置。损坏文件可勾选明确确认修复：**重建本轮支持的设置，其他旧文件设置重置**，旁边保留 `<配置名>.recovery-<随机值>` 原文副本。原文可能含曾误填的秘密，需独立保管。正常保存也保留原文副本；它们不进入四库备份，不应提交 Git。

## 语义检索模型

维护页只管理固定 `BAAI/bge-small-zh-v1.5`，用于记忆语义检索；聊天仍由选择的聊天 provider 提供。缓存操作使用已保存路径，先保存路径再准备模型。

1. 查看缓存状态。`ready/previously_verified` 只表示先前验证的托管元数据；不代表本次加载成功。
2. 缺依赖时按提示在项目环境手动安装 `requirements-embedding.txt`，维护不执行 pip。
3. 选择空/不存在的独立缓存目录，明确允许本次下载，再“准备模型”。后台先下载到 stage、离线推理并写清单，完成后发布。
4. “离线校验”执行资产 hash 与固定公共文本的 CPU 推理；`verified_offline` 才是本次验证。旧非托管缓存也可验证。
5. 清理前先读取 revision 并确认，只删除清单归属、内容未变的自有资产。任意旧目录、链接、未知文件或被修改文件会拒绝删除；不能借此清空别的模型目录。

缓存有共享/独占 OS 租约，另一个实例正在使用时维护会 Busy。清理中权限/磁盘故障保留 manifest，修正文件占用后可读取状态并用相同 revision 明确重试；不要手工改 manifest 绕过检查。成功后空根目录和同级持久锁文件可保留，不是未清理的模型资产。

准备/验证中的同步 SDK 无强制中止接口。关窗会请求取消，当前 SDK 阶段结束后再清理并释放；窗口在此期间保留，不将请求停止冒充已经退出。若资产发生故障，优先选择新空目录重新准备。

## 备份与恢复

维护页“创建四库备份”需确认包含敏感内容，并选择尚不存在的输出文件（建议 `*.deskpal-backup.zip`）。先正常启动一次初始化工作库并退出，才有可备份数据。其他 GUI/CLI 正在使用目录时拒绝备份；不要绕过实例锁直接复制运行中的单个 `.sqlite3` 文件。

归档含 workflow、memory、scheduling、companion 四库及 manifest；历史没有 companion 时明确记录缺失。包括记忆/任务文本、删除工作单、来源身份、调用预算等私人数据，**未加密、未签名**。工作区原文件、TOML/`.env`、系统凭据、模型缓存、日志不包含在内。完整迁移需分别保管工作区与设置，并重新配置凭据/模型。哈希只检测完整性，恢复前选择自己信任的归档。

恢复流程：

1. 选择归档并“检查归档”，然后选择父目录并输入尚不存在的新目标名；也可手输完整路径。备份文件与现用数据目录、目标目录须独立，目标父目录需已存在。
2. 确认数据会回退，旧备份可能重新带回后来已遗忘的数据，再恢复。旧目录保留，不覆盖现用数据库。
3. 完成后选择“使用恢复目录”，只把路径填入配置页；检查并明确保存，然后启动。设置若仍被环境覆盖，应先修正环境来源。
4. 启动按原恢复规则复核旧任务。恢复不授予工具权限、不重做 Uncertain 步骤、不补发云请求，也不表示历史隐私删除已经完成。外部文件与工具副作用不随数据库回滚。

中途失败会留 `.restore-in-progress.json`/stage；该目录不能启动。选择**原归档**和“已有未完成目录（继续恢复）”，明确勾选续办；后台校验每个已发布库，只有原 hash/清单一致才继续。配置已指向该未完成目录时也可续办。当前旧目录损坏或不存在时，仍能向另一新目录恢复。

遇到内容变更/未知文件冲突，保留现场，选另一个新目标恢复；不要删标记以假装完成。标记损坏或丢失没有自动强制接管入口。单库超过 512 MiB、总库超过 1 GiB 或不受支持的 schema 会拒绝；这是当前有界实现范围。

## 无 GUI 维护命令

```powershell
python -m deskpal --maintenance setup_status --config E:/Deskpal/config.toml
python -m deskpal --maintenance diagnostics --config E:/Deskpal/config.toml
python -m deskpal --maintenance cache_status --config E:/Deskpal/config.toml
python -m deskpal --maintenance cache_verify --config E:/Deskpal/config.toml
python -m deskpal --maintenance cache_prepare --config E:/Deskpal/config.toml --maintenance-parameters '{"allow_download":true}'
python -m deskpal --maintenance backup_create --config E:/Deskpal/config.toml --maintenance-parameters '{"destination":"E:/DeskpalArchives/first.deskpal-backup.zip","acknowledge_sensitive_data":true}'
python -m deskpal --maintenance backup_inspect --maintenance-parameters '{"archive":"E:/DeskpalArchives/first.deskpal-backup.zip"}'
python -m deskpal --maintenance backup_restore --config E:/Deskpal/config.toml --maintenance-parameters '{"archive":"E:/DeskpalArchives/first.deskpal-backup.zip","target_dir":"E:/Deskpal/restored-data","acknowledge_data_rollback":true}'
```

恢复续办额外传 `"resume":true`。这些命令不启动 Core、不执行工具、不依赖 Qt；回执总为 JSON，成功退出 0、失败退出 1，参数错误由 argparse 返回 2。普通任务/授权参数不能混入维护命令。

配置写入用 `setup_save`，参数为 `values` 对象与刚读到的 `expected_revision`（首次 `"missing"`）。修复改为 `setup_repair` 并加 `"confirm":true`。清理缓存用 `cache_clear`，传缓存回执的 `expected_revision` 与 `"confirm":true`；两种 revision 不可互换。CLI 不接受 JSON 中的 `credential_secret`；保存新密钥可在交互终端加 `--store-api-key`，通过隐藏输入传递，不得将密钥写进命令行或脚本。

失败回执含固定 code/message/next_steps。Busy 先退出实例；修订冲突重新读取；缺依赖手动安装；恢复 Pending 按原归档续办；权限/空间故障检查本地环境后明确重试。排错时只分享固定回执和必要非秘密路径，不公开原文配置恢复副本或数据库归档。
