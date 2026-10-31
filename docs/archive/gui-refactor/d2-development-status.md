> 历史存档：保留当时的设计、失败与验收边界；不代表当前实现或新的执行授权。当前入口见 [文档导航](../../README.md)。

# D2 任务审核、提醒与工具目录开发交付

日期：2026-10-25。当前是源码开发和静态阅读审查，未运行 pytest、mypy、GUI、截图、模型或网络测试。AST 解析只核对 Python 语法，不证明业务行为通过。

## 任务与确认

`ApplicationService` 和 `DesktopFacade` 提供 `task_review(task_id)`、`task_updates(task_ids, known_revisions=None)` 和 `command_status(command_id)`。原 `task` raw 查询保留兼容，新界面消费结构化 review。

review 包含 task_id/task_revision/status/title/conversation_id、plan_hash/workspace、全计划 steps，以及 required_permissions、missing_permissions、granted_in_current_process。三个权限对象的键均为 ordinary_tools/high_risk_tools。每步保留 call_id、tool_name、中文 display_name、index、可信 permission_level/permission_label、contract_version、完整 arguments 和递归 fields；字段中文名由可信映射提供，未知字段及嵌套值完整回退，不丢弃。风险和权限来自持久 ToolSpec，不能由模型正文或界面参数指定。针对只提供 ID 的 timer_cancel/calendar_update/calendar_delete，target_summary 从 Scheduling 公共快照读取当前名称和时间，明确标注“当前对象名称”、covered_by_plan_hash=false；原 ID 仍完整保留，不把当前名称冒充计划哈希冻结参数。

available_actions 由当前 Task、取消、attempt、来源校验及进程队列状态计算；NeedsReview 没有 plan 且没有 attempt、来源及隐私仍有效时，提供 resume 以恢复尚未完成的原规划；有完整原计划则直接 confirm。正常 Completed 的只读卡片展示真实 attempt_summary，不挂“已有执行记录不能重放”的警告，执行防重放仍在确认入口严格检查。confirmation_state 为 none/accepted，confirmation 含 queued/active、当前进程授权和 revision。顶层 confirmation_revision 与 confirmation.revision 同值，是本次进程的单调队列控制版本；同 task_revision 的迟到 none 回复不能覆盖更新的 accepted。读取时在公开 snapshot_control 边界取得队列及工作库快照，锁外校验来源，返回前再次复核 Task/控制版本；变化时有界重读一次，否则要求刷新。历史查看使用当前全局隐私门禁，旧执行 admission 失效只阻止执行；已 scrub 的任务只返回去正文元信息。

DesktopFacade `confirm` 必须带 task_id、plan_hash、expected_revision、command_id，ordinary_tools/high_risk_tools 是明确列表。Application 同步 CLI 包装保留 expected_revision 可省略的兼容形式。新受理先复核 hash、当前工具契约/工作区/来源，拒绝缺少任一所需权限、未知额外工具、错误权限级别、已有 attempt、取消和非审核状态。重复 command_id 先核对原 payload，之后直接返回历史 Task，即使当前版本已经变化；异 payload/operation 拒绝。历史 receipt 永远不恢复进程授权。已排队或正在确认的任务拒绝第二个新确认命令。

受理仅将原计划交给唯一 dispatcher，不等待工具结束。NeedsReview 的未执行完整原计划可直接确认，无需 GUI 先发 resume；Core pending 的恢复由该 worker 在实际领取时完成，因此其他任务运行期间也能受理确认。恢复没有重新规划，不重放任何已有 attempt。工作库提交失败会撤销本次内存队列元数据。

task_updates 最多 100 个互异 ID，只返回有界状态、版本、取消和确认信息。tasks 分页仍仅投影有界摘要，额外在同库边界补 title（请求前120字符，清理后固定名称）和 conversation_id，不读取 result 正文或返回 request 列。command_status 同时查询 workflow.command 与 conversation_receipt：Task 回执返回 workflow namespace 和当前 task_id/status/revision；会话或 reminder_ack 回执返回 conversation namespace、operation、conversation_id/result_identity；不存在时 known=false。原 submit 的两个配套回执可共用键，其余跨命名空间混用拒绝，不将会话 result_identity 当成 Task。

## 独立本地提醒

`ReminderInbox` 使用 schema3 的 desktop_reminder_cursor/inbox/resource_link。`ReminderPump` 是 Application 拥有、启动、停止和 join 的独立轻量后台消费者，与主动建议、窗口可见性和系统通知开关无关；不调用模型或工具、不重建提醒。关闭超时保留资源，pump 结束后才关闭调度与数据库。

每个 timer/calendar 流首次启用取当前事件尾部作为 baseline，避免洪泛旧事件；之后 inbox 与 cursor 同工作库事务提交，事件身份由 source/store_id/generation/event_id 得到稳定 SHA-256 ID。来源身份、世代或历史锚点不匹配则 blocked，不猜测续接。退出或休眠期间错过的到期项作为 missed 留在收件箱。due/missed 可浏览，取消/修订后抑制为 suppressed，隐私清理为 redacted 墓碑；后两者清空 label，不能从调度库复活。

资源到 task/conversation 的关联只来自 ToolManager 已实际执行且成功、由主机规范化的 timer_create/calendar_create 结果。资源创建与到期回调竞争时，迟到关联会幂等补写原会话的提醒条目；若任务已 scrub，则将先到的未关联 inbox 绑定墓碑、清 label，不再显示。时间线 ID 为 reminder:<inbox-id>，同一任务可创建多条提醒，不受消息的 task/kind 唯一约束影响。

| 操作 | 参数与返回 |
| --- | --- |
| reminder_feed | cursor=None、limit=50（1..100）、unread=False；返回 items/next_cursor/has_more/unread_count/streams |
| reminder_detail | id；返回 item 或 item=null、unavailable=true，不重新创建资源 |
| reminder_ack | id、command_id；幂等设置 acknowledged_at，只标已读，不操作调度来源 |

item 含 id/source/source_id/event_id/store_id/generation/occurrence_revision/due_at/recorded_at/label/conversation_id/task_id/acknowledged_at/delivery_state/status，status 等于 delivery_state。每次读在调度公共 source_snapshot 校验后，在工作库隐私边界重新读取记录和 Task，防止清理竞争泄漏旧 label；抑制所选记录后重新计算未读计数。分页只取 limit+1 条，不为列表物化所有提醒。

系统通知复用 audit feed：到期投影同事务写 desktop_reminder_due，data.id 为 inbox ID；notification_feed/notification_validate 增加 kind=reminder。安全通知只含 kind/entity_id/revision=occurrence_revision/status=due；投递前再核对当前来源、未已读和 120 秒 TTL，不含 label。missed 保留在收件箱，不补发过期系统通知。独立进程通知是否实际展示与恰好一次投递仍不承诺。

`schedule_items(source='all',offset=0,limit=50,status='all')` 是当前资源列表，独立于只含到期项的 reminder_feed；安排页和 Goal 来源选择用它查看未来日程/未到期计时器。查询调用 Scheduling 公共 `page_items`，SQLite 内 LIMIT+1 分页，不执行工具、不推进提醒、不消费事件。source 为 all/timer/calendar；status 为 all、timer 的 pending/fired/cancelled、calendar 的 scheduled/reminded（仅表示 reminder_fired，不声称参会完成）。item 带 source/id/source_id/title/label/due_at/start_at/end_at/reminder_at/timezone/status/duration_seconds/created_at/updated_at/occurrence_revision，不适用字段为 null；顶层 offset/has_more/store_id/generation。关联已清理 Task 的资源保留身份与时间，名称替换为固定清理提示、label 清空并 redacted=true。

## 工具目录与素材路由

目录来自实际 `ToolManager.list_tools()`，不以中文映射伪造已安装工具。当前生产装配有 19 个可信工具；动态主机注册照实显示。tools_catalog 支持 q/category/origin/cursor/limit（默认50、上限100）/known_catalog_revision；tool_detail(id,catalog_revision=None) 返回完整 schema 与字段说明；plugins_catalog 聚合实际 origin/plugin_id。revision 是实际契约与可用性投影的 hash，分页 cursor 绑定版本和过滤条件。capabilities 明确 read_only=true、installation=false、enable_disable=false。

截图目录仅检查 mss 模块是否可发现；缺失时 unavailable，依赖存在或未确定时 check_at_execution 并说明桌面会话、显示器和权限尚需执行检查。查询不加载 MSS、不枚举显示器、不截图、不联网。

素材 JSON 操作：appearance_snapshot() 返回 profiles 列表；asset_import_prepare(path,slot,generation)、appearance_apply(slot,token,expected_revision,generation,display_mode='contain',crop=None)、appearance_reset(slot,expected_revision)、asset_import_discard(token)、asset_collect(limit=32) 是现有 AssetService 的薄委托，不接收额外 command_id。`app.asset_preview(**params)->AssetPreview` 带 _ready 门禁，仅供专用图片通道使用，不注册为 DesktopFacade JSON 操作。

## D5 最终静态整合补充

本次按实际源码复核 application/service、desktop、dispatcher、task_review、reminders、tool_catalog，Scheduling 公共端口与 SQL 分页、Core pending 授权查询、ToolManager 成功结果观察，以及 D1 的提交绑定/默认会话保护；未修改 GUI 所有权文件。

- 修补确认恢复的两处重复来源验证：NeedsReview 在事务内改用 `tasks.resume`；`dispatcher.confirm → tasks.requeue` 仅在 Application 已于事务外验证来源时传内部 `_external_validated=True`。直接 requeue/dispatcher 调用默认仍验证外部来源。事务内保留隐私、依赖、版本、取消、attempt 和容量检查，领取/执行/Started 前仍重验，避免 workflow→scheduling 与调度审计 scheduling→workflow 的锁序反转。
- `conversation_task_location(id, task_id)` 新增在 Repository/Application/DesktopFacade 的薄查询。按稳定任务卡主键索引验证会话归属、当前隐私门禁和未清理状态，只返回 identity、元数据及 `before=task_card.sequence+2`；随后 `conversation_timeline(id,before=...)` 可读取包含原用户/卡片/助手和前文的有界页。异会话、已 redacted 卡片或 scrubbed Task 拒绝定位，不扫描全部历史，也不返回正文。
- 复核 Completed 的正常 attempt 摘要与重放警告区分、旧确认 receipt 在 CAS 前辨认、进程确认 revision、唯一 worker 的重启恢复、提醒先到期后关联/清理墓碑/同 task 多提醒/未读重算、120 秒通用通知、未来安排公共 SQL LIMIT 分页、真实 19 工具与只读依赖发现、图片 bytes 专用通道；这些仍是源码证据，不是行为验收。
- 同步明确冲突的旧测试调用方：V0.5/V0.8 Desktop 确认增加 expected_revision；V0.4/V0.8/Humanization 中空授权继续排队的期望改为明确拒绝，并保留原计划/hash/无 attempt/无文件效果的断言；CLI 缺授权/错授权类别现在明确失败；Humanization 迁移的两处升级结果改为 schema3，冻结旧 fixture 与 persona schema 不变。

已为重启确认补充“外部 validator 不在 workflow 事务内执行”的断言；持久会话用例新增超出最近 50 项的旧任务定位、跨会话拒绝、当前隐私 epoch 重准入和清理拒绝。21 个相关 Python 源码/测试文件只执行 AST 语法解析，退出码为 0；所修改受 Git 跟踪文件的 `git diff --check` 退出码为 0，仅提示既有 LF/CRLF 转换。未导入产品模块、未执行测试。

## 未执行验证与限制

新增 [D2 集成用例](../../../tests/integration/application/test_task_review.py)，覆盖全计划/权限、非阻塞重复确认、重启直接确认、其他任务运行时排队、历史 receipt 不恢复授权、跨命名空间、历史查看与 scrub、目录只读与未知字段、短计时关联竞争、同 task 多提醒、清理竞争、源变更抑制、首次 baseline/退出 missed/世代阻断和 pump 生命周期。均未执行。

T1 授权后建议先运行：

```powershell
python -m pytest tests/integration/test_gui_refactor_d2.py tests/integration/test_persistent_conversations.py tests/integration/test_humanization_migration.py
python -m pytest tests/integration/test_v04_application.py tests/integration/test_v05_desktop.py tests/integration/test_v08_companion_integration.py tests/integration/test_humanization_application.py tests/test_v04_cli.py
```

上述命令本轮均未执行；新接口有意要求完整授权和明确 CAS，不能放宽实际权限检查以适配旧期望。真实 Qt 并发、系统通知、Windows 休眠/唤醒和截图环境仍需独立验证。审核 DTO 保留完整参数；若显式调高计划/结果容量，或深层嵌套参数在结构化字段中重复膨胀，仍可能触及 bridge 的 4 MiB 回复预算，此时明确读取失败而不是截断后允许授权，容量边界尚未实测。
