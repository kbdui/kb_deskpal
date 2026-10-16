# V0.5 开发计划

日期：2026-10-16。基线：0.4.0；本文件为待开发方案，不表示已开始编码或已交付 GUI。环境见[评估](environment-assessment.md)，版本门槛见[验收边界](acceptance-boundaries.md)。

目标：把现有后端变为可日常操作的 Windows 应用，完成输入、排队、查看原计划、准确确认、执行结果、记忆/目标管理和托盘生命周期。保留 CLI；不改变 Agent Core→Planner→Tool Manager→Sandbox→Tools 与 Memory/LLM 的依赖方向。

## 范围冻结

| 必交内容 | 具体范围 |
| --- | --- |
| 主窗口 | 中文优先；输入与结果、任务状态、错误/忙碌/取消请求可见；有界当前会话展示 |
| 任务/确认/恢复 | 分页、详情、attempt/部分结果、Waiting 原计划、普通/高风险分别授权、NeedsReview 与 Uncertain |
| 记忆 | 来源/用途/状态、候选审核、更正与遗忘；画像仍限已有字段；清理阻塞可见 |
| 目标/提议 | 已有 Goal/Milestone/Binding 操作的界面入口，Proposal 查看/接受/忽略；完成与版本检查仍由后端决定 |
| 设置 | GUI 偏好即时生效；后端有效配置与来源只读展示、配置路径和重启说明；不在本版新增密钥编辑器或后端热重配 |
| 托盘/通知 | 恢复/退出、显式启用的关窗留托盘、本地状态提醒、quiet/去重/降级；静态角色图或无图占位 |
| 安装与诊断 | 可选 GUI extra、独立 GUI 入口、缺依赖提示、源安装/wheel 冒烟、无密钥诊断与操作文档 |

静态图放在主窗口，不要求透明悬浮桌宠窗口。界面显示连续消息不等于模型已具备完整多轮 WorkingContext；每次请求继续遵循当前后端语义，不额外建立脱离遗忘机制的聊天数据库。

开机自启、原生 toast action/AUMID、通知中心、窗口唤醒 IPC、便携可执行目录包为可选后续项；本版不把它们作为必交。Live2D、人格情绪、语音、无人值守自动工具、OS 隔离/强杀同步工具、自动更新和正式安装器留后续阶段。

## 先补应用边界

当前源码审查发现：

| 位置 | 当前行为 | 必要改动 |
| --- | --- | --- |
| application/service.py | submit 同步写库；run/wait/confirm_task 阻塞，close join 线程 | 分离入队回执与执行结果；所有同步方法在后台调用；保留 CLI 同步包装 |
| application/dispatcher.py | 唯一 Core worker；management 在活动任务时拒绝 | 保留唯一执行者，提供明确 Busy，不在 UI 隐式等待/重试授权 |
| application/factory.py | 启动包括锁、迁移、恢复清理、模型加载 | 后台初始化，主线程只维护加载/失败/清理阻塞画面 |
| application/service.py review_task | task/plan/attempts 分次读 | workflow 同事务组合 DTO；跨库只声明独立来源版本 |
| workflow/tasks.py、memory/coordinator.py | Task 默认无界，Memory list 无受控分页 | 增加真正后端分页，禁止全量取出再切片 |
| application/instance.py、CLI | 同 data_dir 单实例，CLI 查询也创建应用 | 冻结 GUI/CLI 互斥：GUI 完整退出后再使用 CLI；第二实例清晰失败，不偷偷换库 |
| dispatcher.stored_result | 恢复结果不重建完整 authorization_requests | 确认页从 PlanSnapshot + tool_specs 展示风险与参数 |
| agent/ports.py 等 | 旧 Protocol 不覆盖 V0.4 应用能力 | 增加应用层窄 Protocol，GUI 不绕到 SQL 或 Core 内部补缺口 |

## 线程、契约与状态

建议新增 application/contracts.py 与 application/queries.py/commands.py（名称在第一阶段冻结）；Qt 代码放 src/deskpal/gui/，按 host、bridge、viewmodels、views、tray、notifications、resources 拆分。Core/LLM/Tool/Memory 不导入 Qt。

```text
Qt 主线程：Widgets / 有界视图模型 / 生命周期状态
    ↕ queued signal + 深复制 DTO
后台应用桥：短命令、分页查询、管理操作；独立控制通道
    ↓ 窄 Application Protocol
唯一 ApplicationService → 持久队列 → 唯一 Core 执行 worker
```

独立控制通道用于 request_cancel 和发起关闭；不能把 wait、阻塞 confirm、长记忆操作、close 串行放在同一个线程，再让取消排在后面。长操作回报进度/完成，不为每个 task 无界创建线程。后台服务的数据库锁争用仍可能延迟控制命令，必须测量并显示 pending，不承诺立即强停。

Qt 要求 widgets 留在主线程，跨线程使用 queued connection；QThread 对象本身的线程归属不能当作 worker 方法已在后台运行的证明。不使用 processEvents 绕过阻塞，不用 BlockingQueuedConnection 等待慢调用。[Qt 线程说明](https://doc.qt.io/qtforpython-6/overviews/qtdoc-threads-qobject.html)

拟新增接口（名称可在接口冻结时调整，非已有 API）：

| 应用契约 | 结果与约束 |
| --- | --- |
| submit_intent(command_id, request, use_policy) | SubmissionReceipt：Accepted/Busy/Rejected + task_id；不等结果 |
| enqueue_confirmation(command_id, task_id, plan_hash, ordinary_tools, high_risk_tools) | 校验原计划后入队回执；CLI confirm_task 可继续包装为入队+wait |
| resume_task / request_cancel | 明确恢复回执或取消请求回执；取消请求不等于工具停止 |
| query_task_review / query_tasks | 同事务计划详情、稳定分页和有界 DTO |
| query_memories/goals/proposals | 受控分页、来源版本/失效信息；查询中的过期清理仍由后端处理 |
| execute_memory_command / goal/proposal commands | 版本检查、幂等 command_id、Busy/Conflict/CleanupBlocked；管理操作保持现有门禁 |
| begin_shutdown / query_shutdown | 尽快返回的关闭进度；资源释放完成后才 Closed |

DTO 带 instance/session_id、command/request_id、entity_revision、query_generation、必要的 plan_hash 和 memory epoch。映射/列表/工具输出递归复制或规范化，不能仅靠 frozen dataclass 保证不可变。视图 DTO 不携带可执行权限；授权命令单独构造，每次明确绑定已展示 hash，默认无 grant。

先采用**后台有界轮询 + 版本化快照**，无需引入一整套持久 GUI 事件总线。相同查询合并、切页增加 generation、旧会话/旧 revision/关闭后的回调丢弃；跨 memory/workflow/scheduling 不伪称全局原子快照。audit_outbox 和 scheduling cursor 不是 GUI 事件订阅。若以后加推送，另冻结事件序列、丢失重同步与队列上限。

授权展示工具名、原始完整参数、风险、workspace、步骤和来源失效状态；长内容可滚动查看，不能只有截断摘要。当前授权按工具名绑定整份 hash 计划，不能包装成已支持逐路径/逐 call 授权。预览变化即要求重新审核，接受提议和点击通知不授予权限。

## 隐私、通知和生命周期

遗忘命令发出时先冻结并遮蔽相关正文视图，建立 memory epoch / request generation 屏障；详情、聊天、计划、结果、待通知内容与待投递 Qt 回调一起失效。必须区分受理前拒绝与受理后清理：Busy/Conflict/Rejected 且未建立删除工作单时明确显示“遗忘未提交”，允许用户主动重新加载当前事实；一旦删除工作单已提交或返回 Pending cleanup，则擦除对应缓存、持续屏障，失败只显示无正文阻断与重试，不能用刷新灌回旧正文。后台忘记范围与 GUI 缓存依赖要有同一映射；不独立持久化正文副本。

通知走 NotificationPort，默认只显示通用状态，不含记忆/目标/请求正文。持久去重键建议 entity_id + event_version + channel，存放在应用管理的存储中，不改变 Proposal 接受/执行状态；先记尝试再调用 OS，限定为“应用至多尝试一次”，承认崩溃可能漏提醒，不声称 OS exactly-once 送达。主窗口始终保留可查询状态。quiet 结束只做有界摘要；休眠/重启/点击时重查 TTL、来源版本和当前状态。点击只导航，不执行/确认/接受。

首版通知固定为新发生的 Task Waiting、Completed/Failed/Cancelled，以及仍有效的新 Proposal；不通知每次 Planning/Running 轮询。系统通知总开关默认关闭，窗口内状态始终可见。用户开启后 quiet 默认使用配置时区的 22:00–08:00，覆盖全部上述通知；结束时最多一条通用数量摘要，最多列入最新 20 个仍有效对象，不逐条补发。首次启动/首次开启建立历史基线，不为既有历史批量推送；重启保留去重记录，离线期间变化只更新窗口状态，不补发 OS 历史通知。关闭/遗忘立即撤销应用内尚未投递项。OS 已显示通知的撤回不作为 Qt 首版保证，故始终不含私人正文。

QSystemTrayIcon 的消息可能被 Windows 设置抑制；检测 tray/message 支持并回退窗口状态。没有托盘时不能隐藏唯一窗口。只存窗口位置、关窗行为和通知偏好到 QSettings，不存 API key 或授权。[Qt 托盘说明](https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QSystemTrayIcon.html)

生命周期冻结为 Initializing → Ready / CleanupBlocked / StartupFailed → Closing → Closed。关窗默认退出；“关窗留托盘”由用户显式开启且仅在 tray 可用时生效。托盘退出停止提交/通知/调度领取，协作取消并等待后台关闭；close=False 保留 Closing、锁与资源，界面可诊断/重试，不能假退出或重新开放提交。强制终止后的重启沿用 V0.4 审查，不自动重放。

初始化所有权不能随视图 generation 一起丢弃：用户在 create_application 尚未返回时退出，宿主记录关闭意图；迟到创建成功的 Application 仍由宿主接管并完整关闭，完成后才结束 Qt 事件循环。仅丢弃旧 UI 回调而遗失服务引用、线程或实例锁属于阻断缺陷。

## 分阶段与子 Agent 分工

每阶段先有可审查接口再并行；最多三名子 Agent 同时工作，主 Agent 审核一致性、竞态和验收证据。模型采用用户允许的 6.1 sol high / 6 luna high。以下是开工后的分工，本轮两名只读审查 Agent 已完成应用接入与验收审查。

| 阶段 | 主责 | 完成出口 |
| --- | --- | --- |
| 0 基线/探针/接口冻结 | 主 Agent + 架构审查（6.1 sol high） | 环境修正、Qt smoke、依赖闭包；Protocol/DTO、线程/权限/分页/关闭语义冻结；性能目标初测后冻结 |
| 1 后台桥接与生命周期 | 应用 Agent（6.1 sol high）；测试 Agent同步设计竞态 | 非阻塞启动/提交/确认/取消/退出；唯一 Core；Busy/失败/cleanup 状态可测 |
| 2 聊天/任务垂直切片 | GUI Agent（6.1 sol high） | 输入→队列→计划→明确授权→结果→恢复完整离线闭环；这一片通过后再扩页面 |
| 3 记忆/目标/提议 | 数据界面 Agent（6.1 sol high）+ GUI Agent | 真分页、版本冲突、遗忘缓存屏障、来源用途、接受不授权；分配不重叠文件 |
| 4 设置/静态资源/托盘通知 | 桌面 Agent（6 luna high） | 设置生效范围、图标回退、quiet/去重、隐藏恢复/退出；敏感边界由主 Agent 复审 |
| 5 整合/Windows/交付 | 测试 Agent（6.1 sol high）+ 主 Agent | 双 Python/Qt 回归、实机矩阵、源码/wheel 安装、最终同构建长测与报告 |

每个子 Agent 返回修改文件、设计理由、接口、测试命令/实际结果和剩余风险。共享模块只由指定 owner 修改；接口变更回到主 Agent 审核。测试从阶段 1 贯穿，不到最后才检查授权/遗忘/退出。未运行、被 skip、缺少硬件的项目独立标记，不计作通过。不承诺未经验证的日历工期。

## 交付文件

实现完成后新增 versions/v0.5/{interfaces,operations,delivery,test-report}.md；更新 ARCHITECTURE、README、依赖清单、版本和验收矩阵。报告记录实际源码/构建指纹、依赖、测试环境、未测项；不以本 planning 目录充当交付证明。用户自行提交 Git。
