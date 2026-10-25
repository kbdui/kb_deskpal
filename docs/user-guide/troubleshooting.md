# 常见问题与排错

先记录操作入口、任务 ID/固定错误码、实际生效的非秘密配置和当前状态。不要通过删除数据库、恢复标记或锁文件试图修复状态。

## 安装和启动

| 现象 | 处理 |
| --- | --- |
| `No module named deskpal` | 激活安装依赖时使用的 Python 环境，在仓库根目录执行 `python -m pip install -r requirements.txt` 或 GUI 分组 |
| GUI 依赖未安装 | 安装 `requirements-gui.txt`；只有 CLI 不需要 Qt |
| 提示另一个实例占用 / `busy` | 完整退出同一数据目录的 GUI/CLI，留意托盘和 Closing；模型缓存也可能被其他实例共享使用 |
| `config_missing/invalid_config` | 使用 `python -m deskpal.gui --setup --config 路径`；修复损坏文件前核对设置重置及恢复副本 |
| 改配置无效 / `environment_override` | 核对启动路径、进程环境和配置旁 `.env`；在覆盖来源处修改并重启 |
| `credential_missing` | 确认 `api_key_env` 与实际变量名一致，或在向导保存系统凭据 |
| `credential_store_unavailable` | 改用环境变量，不把密钥写进 TOML |

```powershell
python -m pip check
python -m deskpal --maintenance setup_status --config config.toml
python -m deskpal --maintenance diagnostics --config config.toml
```

## 模型与任务

| 现象 | 原因与处理 |
| --- | --- |
| 对自然语言只返回演示文字 | `offline` 是固定规则演示，连接兼容 Responses 的服务才能使用真实模型 |
| 云模式停在 `NeedsReview` | 检查请求和关联来源是否 `local_only`；有 key 或接受提议不构成用途许可 |
| `Waiting`，文件没写入 | 尚缺授权，核对原计划后按普通/高风险类型确认 |
| 普通授权仍无法移动/删除 | Level 2 需要独立高风险授权 |
| 路径被拒绝 | 检查是否在工作区内、是否含链接/Junction、大小是否超限；不要扩大根目录来绕过核对 |
| API/协议错误或超时 | 检查模型名、API 根地址、Responses 兼容性及服务状态；避免反复提交有潜在副作用的任务 |
| 语义检索退回词法 | 确认激活 `deskpal_env` 或安装了 embedding 分组，且实际配置为 `fastembed`、缓存路径指向现有完整模型；当前工作区为 `data/models/bge-small-zh-v1.5`。完全退出应用后运行 `cache_verify`；聊天模型不负责本地向量缓存 |
| revision/hash 冲突 | 重新读取最新记录/原计划，再明确提交，不复用旧值 |
| `QueueFull` / 管理操作 Busy | 等后台任务结束，保留输入，读取当前状态后主动重试 |

工具失败、取消和表达失败都不能推断外部效果已撤销。`Uncertain` 和部分执行应检查账本与实际文件；参阅[任务与授权](tasks-and-permissions.md)。

## 维护错误

| 固定错误码 | 下一步 |
| --- | --- |
| `optional_dependency_missing` | 先检查是否误用 Anaconda base/其他环境；本工作区使用 `conda activate deskpal_env`，新环境再手工安装 embedding 分组 |
| `cache_missing/cache_invalid` | 先核对配置文件位置、相对路径和环境覆盖，当前模型在 `data/models/bge-small-zh-v1.5`；确实缺失或损坏时保留现场，选择新空目录并明确允许下载 |
| `cache_unmanaged` | 可离线验证；换空目录准备托管缓存 |
| `download_not_allowed/confirmation_required` | 阅读操作内容后提供对应的明确确认 |
| `archive_source_uninitialized` | 正常启动一次应用初始化工作库，再退出备份 |
| `archive_destination_exists` | 选择新的输出文件或目标目录 |
| `archive_schema_unsupported/archive_limit_exceeded` | 保留原数据，使用支持该 schema/容量的版本 |
| `restore_pending/restore_resume_required` | 使用原归档继续未完成恢复 |
| `restore_conflict/archive_invalid` | 保留现场，检查归档，选择新的目标目录 |
| `io_error/archive_io_error` | 检查本地权限、空间和文件占用，再明确重试 |

## 界面与退出

桌宠无法点击时检查鼠标穿透，在主窗口设置或托盘关闭穿透。桌宠状态未知时先检查后端任务查询；禁用再启用可重试素材加载。

通知默认关闭且可能被 Windows 抑制，开启后不补发历史；到窗口查看权威状态。关闭主窗口后仍运行时，检查是否启用了关窗留托盘。

`Closing` 表示同步调用、Core 或图片 worker 尚未退出。取消是协作式的，等待调用返回，查看诊断并重试关闭；关闭超时会保留在用资源和实例锁。不要重复启动同目录实例。

隐私清理 Pending 时使用“重试清理”或 `--privacy retry`；不要通过刷新重新展示旧正文。反馈问题时可提供固定 code/message/next_steps 和最小复现，不附带私人配置、归档或数据库。
