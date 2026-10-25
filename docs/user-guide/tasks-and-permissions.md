# 任务与授权

Deskpal 先生成完整计划，再执行工具。模型提出的计划不等于已获授权。查看任务时应核对工具名、每步参数、工作区、风险、来源用途和计划 hash。

## 状态怎样理解

| 状态 | 含义及处理 |
| --- | --- |
| `Created` / 排队 | 已创建，等待规划或执行 |
| `Planning` | 正在生成计划 |
| `Waiting` | 原计划缺少必要授权，等待用户核对确认 |
| `Running` | 正在执行，取消只能阻止后续边界继续 |
| `Completed` | 当前任务可靠完成，不代表关联目标自动完成 |
| `Failed` | 任务失败；可能已有部分外部效果，先查步骤结果 |
| `Cancelled` | 已按取消规则结束；已发生的效果不会自动撤销 |
| `NeedsReview` | 来源变化、重启或部分执行等原因需要人工复核 |
| `Uncertain` | 有已开始步骤却无可靠结果，先检查真实文件或外部效果 |

界面可能显示“待处理”等展示状态；持久任务与提交队列分别记录状态。查看详情和步骤账本才能判断执行进度。

## 三类工具权限

| 等级 | 典型工具 | 授权方式 |
| --- | --- | --- |
| Level 0 自动 | `echo`、`time`、读取、文档搜索等 | 在路径、参数和用途检查通过后执行 |
| Level 1 普通确认 | 写文件、复制文档、修改日程/计时、截图 | 普通工具授权 |
| Level 2 高风险 | 移动、删除文档 | 独立高风险授权 |

具体工具及参数用 `python -m deskpal --list-tools --json` 查询。文件操作限于配置工作区，拒绝符号链接/Junction；删除不递归。截图需要可选依赖并明确授权，PNG 不会自动上传。

全计划先检查权限，不足时进入 `Waiting`，本次计划零工具执行。当前授权按准确计划中的工具名生效；普通授权不能放行 Level 2。修改参数、来源或工具契约后必须重新审核。重启会清空授权。

## GUI 确认流程

1. 在任务页选择 `Waiting` 或可重新确认的 `NeedsReview` 任务。
2. 查看完整原计划、工作区、每步参数、风险与 hash。
3. 仅勾选愿意授权的普通/高风险工具并明确确认。
4. 返回任务详情检查状态及步骤结果。

接受提议、启用人格、点击通知或桌宠均不授予工具权限。运行中部分管理操作会返回 Busy，等任务结束后再明确重试。

## CLI 确认示例

先确保 GUI 已退出、配置 provider 为离线演示：

```powershell
python -m deskpal --config config.toml --request "write note.txt 你好" --json
python -m deskpal --config config.toml --workflow task --id TASK_ID
python -m deskpal --config config.toml --workflow confirm --id TASK_ID --plan-hash HASH --authorize write_text
```

第一次返回 `Waiting`，第二步读取原任务的真实 hash，第三步确认同一任务。`HASH` 是 64 位小写 SHA-256。高风险工具使用 `--authorize-high-risk 工具名`。交互模式支持 `/confirm TASK_ID`、`/confirm-high-risk TASK_ID` 和 `/cancel TASK_ID`。

## 取消与重启恢复

取消记录 `cancel_requested`，正在执行的同步工具/模型调用要返回后才能继续处理。已开始的文件操作可能成功，不会自动回滚。

重启后排队、规划、等待确认的未结束任务先转入复核；没有任何 attempt 的任务才有恢复或重新确认的路径。部分成功的任务检查账本后，另建剩余意图；`Started` 无可靠结果的步骤为 `Uncertain`，人工复核也不授予重跑原步骤资格。

```powershell
python -m deskpal --workflow tasks --limit 100
python -m deskpal --workflow task --id TASK_ID
python -m deskpal --workflow cancel --id TASK_ID
python -m deskpal --workflow review-uncertain --id TASK_ID --verdict occurred --evidence "已人工检查目标文件，记录实际结果"
```

`verdict` 按实际证据选择 `occurred/not_occurred/unknown`。不要直接复制示例中的结论；需要重做时先判断重复操作风险，再创建新任务。
