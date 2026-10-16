# V0.5 候选版接口

代码版本 0.5.0rc1。领域模块保持无 Qt 依赖；新增边界位于 application.desktop 与 gui。CLI 同步 API 继续可用。

## 应用层

ApplicationService.enqueue_confirmation 在校验原 hash、工具契约、工作区、来源/记忆后返回 Task，不等待执行；支持持久 command_id 幂等。旧 confirm_task 为该接口 + wait 包装。首次确认与 dispatcher.submit 遵循一致锁顺序，重复 command 不添加授权或再次执行。

review_task 在 workflow 同事务返回 task/plan/attempts；跨记忆与调度仅携来源 token，不声称跨库原子快照。begin_shutdown 幂等停止接收/领取与提议轮询、请求协作取消；close 完成 join 与资源释放，False 时保锁保连接。

DesktopFacade.execute(operation, parameters) 返回 JSON 安全的深复制字典，带 session_id、memory_epoch。它是同步后台接口，GUI 主线程不能调用。列表统一 items/offset/has_more；task 为 task/plan/attempts；submit/confirm 为 accepted/task_id/status/revision 回执。

| 类别 | operation |
| --- | --- |
| 请求/恢复 | submit、confirm、resume、cancel、review_uncertain |
| Task 查询 | tasks、task |
| Memory | memories、memory（remember/accept/reject/correct/forget），retry_cleanup |
| Goal | goals、goal、create_goal、update_goal、goal_status |
| Milestone | milestones、create_milestone、update_milestone、milestone_status |
| Binding | bindings、bind、update_binding |
| Proposal | proposals、proposal、accept_proposal、ignore_proposal |
| 运维 | settings、diagnostics |
| 通知元数据 | notification_feed、notification_validate |

写命令使用明确 command_id，实体修改保持 expected_revision/CAS。Memory command 幂等为有界会话记录，仅存指纹与安全元数据，重复调用不回传已遗忘正文；容量满明确拒绝。准确字段以 DesktopFacade 与其实际测试为准，不扩展后端既有权限语义。

Task 列表只查询 ID/状态/版本/时间等摘要，不拉 result 正文；Memory 真 SQL 分页并携同锁 epoch。分页上限 100，不用全量 list_items 后切片。

notification_feed 使用 after_sequence/until_sequence/limit：无 after 建立最新基线，后续每页最多扫描 100 条元数据变更，固定上界跨页；无关行也推进 next_sequence。它是应用层当前状态投影，不是直接暴露 audit_outbox 作为可重放 GUI 订阅。notification_validate 最多 20 个 ID，重查当前状态和 Proposal TTL/来源；不返回私人正文。

## Qt 桥接与宿主

ApplicationBridge 公开 start、send、shutdown、state/ privacy_epoch / pending_count，以及 state_changed(str)、reply(object)、privacy_changed(int)。send 返回 request_id，回复包含 request_id/operation/generation/privacy_epoch/ok 与 data 或分类 error。异常不直接 str(exc) 回显私人参数。

命令、查询、控制分三条后台通道；只有既有 dispatcher 执行 Core。接受请求最多 32 个，参数≤128KiB、单回复≤4MiB；Qt 投递使用有界缓冲与合并的 queued 唤醒，在主线程分批排出，关闭期间才启用短时 timer 等待线程退出。结果预算 8MiB、含拒绝回执条数上限 96。过载可返回 QueueFull，交付缓冲饱和时 send 抛 BufferError，由 UI 明确处理。

forget/correct/accept/reject 增加界面隐私 epoch，丢弃旧查询与排队回调；视图仍核验 generation/epoch。遗忘已提交和受理前拒绝分开；GUI 缓存与授权选择一起失效。任务快照风险来自持久 PlanSnapshot.tool_specs，不假定恢复结果总有 authorization_requests。

任务操作发出时立即失效任务/对话详情，mutation 成功时使所有实体查询失效。分页刷新后选中 DTO 变化则重新审核，选择消失则清空详情与操作资格；旧 Waiting 回复不能恢复确认按钮。表单显式空值会清除上一实体的字段。通知开关/隐私变化独立递增查询 generation，避免桥接复用旧回执。

DesktopHost 拥有后台直到 Closed。Initializing/Ready/CleanupBlocked/StartupFailed/Closing/Closed 均显式展示；Closed 只在资源关闭且所有桥接线程停止后，由 Qt 主线程发布，随后才允许结束事件循环。初始化未结束时的退出仍回收迟到的 Application。

MainWindow 提供 navigate(kind, entity_id)，只重新查询与导航；close_requested 交宿主判定关窗留托盘或 shutdown。所有 Widgets 更新留主线程；页面数据有界，模型/工具文本只按纯文本显示。

主页面以滚动视口承载长表单，避免隐藏页的 minimumSizeHint 把整个窗口撑出屏幕。首次显示、DPR/屏幕/字体或可用工作区变化时合并一次几何调整，使用真实 frame 装饰限制可见范围，不增加空闲轮询。临时屏幕约束与用户正常尺寸分开记录；Qt 已成功恢复的保存位置不重复叠加标题栏偏移。

GuiPreferences 使用 QSettings，仅存 UI 偏好；NotificationController 使用增量游标、4096 条 metadata ledger、最多20项 quiet 待核验队列和请求 token。TrayController 发恢复/退出/消息点击信号，不调用 Core、不赋权、不自行释放服务。
