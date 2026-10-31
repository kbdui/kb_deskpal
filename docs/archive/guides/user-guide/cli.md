> 历史存档：保留当时的设计、失败与验收边界；不代表当前实现或新的执行授权。当前入口见 [文档导航](../../../README.md)。

# 命令行使用

在项目环境与仓库根目录运行。安装后也可使用 `deskpal` 命令。与 GUI 共用同一数据目录时，必须先完整退出 GUI。

```powershell
python -m deskpal --help
python -m deskpal --config config.toml --list-tools --json
python -m deskpal --maintenance diagnostics --help
```

## 交互、单次请求和会话

```powershell
python -m deskpal --config config.toml
python -m deskpal --config config.toml --request "time" --json
python -m deskpal --config config.toml --conversation interactive
```

普通交互的每次输入独立；`--conversation interactive` 为当前终端交互新建独立会话并使用有界上下文。生产服务现保存其目录与聊天，关闭只释放近期缓存，不删除存档；下次 CLI 启动仍新建会话，已有存档可从 GUI 专项列表查看。CLI 会话默认限制云用途，明确允许本次交互输入出云时加 `--conversation-cloud`；来源和类别许可仍生效。

普通 `--request` 按配置 provider 运行。CLI 没有普通 `--offline` 参数，离线演示需使用 `provider = "offline"` 的配置。GUI 的 `--offline` 仅属于 GUI 启动入口。

## 确定性调用工具

`--tool` 跳过自然语言规划的选择过程，但仍通过 Core、参数校验、Tool Manager 和权限检查：

```powershell
python -m deskpal --tool timer_create --arguments '{"duration_seconds":60,"label":"休息"}' --authorize timer_create --json
python -m deskpal --request "write another.txt 示例内容" --authorize write_text --json
```

预授权会允许匹配计划执行，适合你已理解操作的情形。首次尝试建议不带授权，先审阅待确认计划。`--authorize` 可重复传入；高风险工具用 `--authorize-high-risk`。

PowerShell 示例用单引号包裹 JSON。若旧版 Windows PowerShell 传参丢失双引号，可使用 PowerShell 7，或通过 GUI 操作，不能通过放宽 JSON 格式解决。

## 管理命令速查

| 类别 | 入口和常用动作 | 详细说明 |
| --- | --- | --- |
| 任务 | `--workflow tasks/task/submit/confirm/cancel/resume/review-uncertain` | [任务与授权](../../../user-guide/tasks-and-permissions.md) |
| 目标 | `--workflow goals/goal-create/milestone-create/bind` | [目标与提议](../../../user-guide/goals-and-proposals.md) |
| 提议 | `--workflow proposals/proposal/accept-proposal/ignore-proposal` | [目标与提议](../../../user-guide/goals-and-proposals.md) |
| 记忆 | `--memory remember/list/profile/accept/reject/correct/forget/export/rebuild-index` | [记忆与隐私](../../../user-guide/memory-and-privacy.md) |
| 隐私 | `--privacy status/forget/retry` | [记忆与隐私](../../../user-guide/memory-and-privacy.md) |
| 人格 | `--companion show/configure/preference/cue/reset/rollback` | [桌面与陪伴](../../../user-guide/desktop.md) |
| 互动 | `--interaction show/cloud_permissions/identity/preference/confirm_candidate/reject_candidate/clear_cue/clear_constraints` | 本地互动管理，写入带 `expected_revision` |
| 维护 | `--maintenance setup_status/diagnostics/cache_status/backup_create/...` | [数据维护](../../../user-guide/maintenance.md) |

人格设置例子，先查询当前 `revision`，替换 JSON 中的 `REV` 后执行：

```powershell
python -m deskpal --companion show
python -m deskpal --companion configure --companion-parameters '{"expected_revision":REV,"enabled":true,"emotions_enabled":true,"preset":"warm"}'
```

互动写入使用 `--interaction-parameters`，人格写入使用 `--companion-parameters`，维护使用 `--maintenance-parameters`。这些参数对象的 revision 来自不同管理入口，不能混用。JSON 中的 revision 是整数；配置维护首次文件 revision 则为字符串 `"missing"`。

## 输出和退出码

普通任务 `--json` 输出包含任务状态、回复、工具结果、警告、所需授权等字段。单次任务退出码：`Completed=0`，`Waiting/NeedsReview=2`，其他非成功结果（如 `Failed/Uncertain/Cancelled`）为 `1`。管理命令返回自身回执，不能一概按任务状态解释。

维护入口总返回 JSON：成功 `0`、运行失败 `1`、参数解析错误 `2`。维护与普通任务/授权参数不能混用。

需要导出记忆时可将 `--memory export --history` 的标准输出保存到你管理的安全文件中；导出可能包含私人内容，不会随原记忆遗忘自动删除。
