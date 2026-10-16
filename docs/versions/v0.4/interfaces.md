# V0.4 实际接口与状态

本文按当前源码说明调用契约，不作为测试或发布通过证明。操作示例见 [运维与离线演示](operations.md)，验收要求见 [验收边界](../../planning/v0.4/acceptance-boundaries.md)。

## 装配、所有权与兼容

`deskpal.application.factory.create_application(config=None, *, planner=None, screenshot_backend=None, embedding=None, memory_evaluator=None) -> ApplicationService` 是生产入口。工厂先取得规范化 `storage.data_dir` 的 OS 实例锁，再打开工作库、Memory、Scheduling、Core 和 worker。Application 构造期间先续办删除、再恢复工作；工厂绑定执行所有权后调用 `start()`。同目录第二实例拒绝启动，同进程和路径别名也受约束。

`create_agent(...) -> AgentCore` 保留 V0.3 同步、进程内 Task 兼容入口，没有生产 Application 的持久队列、实例锁和恢复管理保证。Core 通过 `workflow.ports.ExecutionJournalPort` 使用账本，不访问 Goal 或 Scheduling SQL；宿主仍可注入 Planner/工具适配器。

Application 持有 `core/tasks/journal/store/scheduling/goals/proposals/rules`。仅一个 dispatcher worker 调用 Core；Scheduling 和本地规则线程不直接执行工具。取消等控制写入不排在执行 worker 后面。

关闭时停止新工作和规则，协作取消并等待执行 worker，投递审计，关闭依赖，最后释放实例锁。`close(timeout=None) -> bool` 返回 False 表示活动调用未结束，资源和锁继续保留，可稍后再调。上下文退出时若仍未关闭会抛异常；不能据此宣称工具已停止。同步 Python 工具不保证可强制中止。

关闭先停止 dispatcher 领取，再等待规则/执行/调度；`SchedulingService.shutdown(timeout=5.0) -> bool` 可有界等待，阻塞 poll 不会阻止超时返回，返回 False 保留连接。其兼容 `close() -> None` 仍在失败时抛错。调度未退出时 Application 不进入最终 ExitStack 回收，防止异常路径提前释放实例锁。

## Application 服务

| 实际方法 | 返回与约束 |
| --- | --- |
| `submit(request, *, command_id=None, authorized_tools=(), high_risk_authorized_tools=(), dependencies=(), use_policy='cloud_allowed')` | 返回持久 Task；同键不同请求/依赖/用途拒绝；满队列抛 QueueFull |
| `wait(task_id, timeout=None)` | 返回可报告的 AgentResult；timeout 不取消工作 |
| `run/handle_request(request, *, authorized_tools=(), high_risk_authorized_tools=())` | submit + wait；worker 使用既有 Task ID，不创建第二个 Task |
| `review_task(task_id)` | `{task, plan, attempts}`；plan 是完整 PlanSnapshot 或 None |
| `resume(task_id)` | 仅有 Submission、无 attempt 的 NeedsReview；无计划回队列，有计划恢复 Waiting/Blocked 并返回快照，后者不执行工具且继续占容量 |
| `confirm_task(task_id, *, authorized_tools=(), high_risk_authorized_tools=(), plan_hash=None)` | 校验准确 hash、当前契约与依赖；同进程可用已展示 hash，跨进程 CLI 必须明确提供 |
| `cancel_task(task_id)` | 返回 Task；运行中先记录 cancel_requested，已有效果与未决 attempt 不消失；Pending 遗忘期间只返回去正文控制回执 |
| `review_uncertain(task_id, verdict, evidence)` | verdict 为 occurred/not_occurred/unknown；只记录证据，Task 仍是 Uncertain |
| `accept_proposal(id, expected_revision, *, command_id)` | 原子接受、唤醒队列并返回 Task，不附带工具 grant |
| `proposal_summary(limit=10)` | 命令边界摘要，limit 1..10；关闭主动功能或处于 quiet 时为空 |
| `memory_command(action, **parameters)` | 受控 Memory 管理入口；forget 走跨库删除工作单 |
| `retry_cleanup()` | 返回清理是否完成；未完成前保持阻断；没有 pending 时为 no-op，不重新 recover 正常 Waiting |
| `diagnostics()` | cleanup/audit/规则/dispatcher 状态，不从日志执行 |

dependencies 接受 `DependencyRef(kind, object_id, revision)`、三元组或对应映射；Memory 旧二元组 `(id, revision)` 仍可用。共享索引按 goal/milestone/binding 区分作用域。底层存储 API 供适配器使用；用户操作应经过 Application/领域管理器，手工改 SQLite 不属于一致性保证。

## Goal、Milestone、SourceBinding

对象是不可变 DTO。`MemoryKind.GOAL` 是记忆分类，不创建权威 Goal；普通聊天、Memory 候选和模型文本不能直接完成目标。完成命令必须有用户明确 evidence，Task Completed 不自动完成 Goal/Milestone。

| 对象 | 状态/操作 | 版本 |
| --- | --- | --- |
| Goal | 初始 Active；Active/Paused 可互转、修改或转 Completed/Cancelled；终态不再修改 | revision 是 CAS 版本，execution_version 只在执行条件/状态实变时增加 |
| Milestone | 初始 Pending；可修改或转 Completed/Cancelled；终态不重新打开 | 独立 revision/execution_version |
| SourceBinding | 初始 Active；可改用途或转 Removed；Removed 不重新打开 | 独立 ID/revision，revision 是执行依赖版本 |

Goal 标题、完成标准、期限、用途和状态属于执行条件；进度备注只增加 revision。Milestone 的标题、标准、期限同理。新增 Milestone/Binding 消耗**父 Goal 的 expected_revision**并增加父 revision，不增加父 execution_version；随后操作父 Goal 应重新读取 revision。单独结束 Milestone 或移除/改版 Binding 仅阻断其作用域，Goal 级失效覆盖全部关联工作；普通进度备注不误伤兄弟任务。

`GoalManager(store, *, clock=None)` 的 clock 是返回 aware datetime 的 callable。公开接口如下，UNSET 表示省略参数，deadline=None 才表示清除：

```python
create(title, success_criteria, *, deadline=None, use_policy="local_only", command_id=None) -> Goal
get(goal_id) -> Goal
list(*, status=None, limit=100, offset=0) -> tuple[Goal, ...]
revise(goal_id, expected_revision, *, title=UNSET, success_criteria=UNSET,
       deadline=UNSET, progress_note=UNSET, use_policy=UNSET, command_id=None) -> Goal
set_status(goal_id, expected_revision, status, *, evidence="", command_id=None) -> Goal
add_milestone(goal_id, expected_revision, title, success_criteria,
              *, deadline=None, command_id=None) -> Milestone
get_milestone(milestone_id) -> Milestone
list_milestones(goal_id, *, limit=100, offset=0) -> tuple[Milestone, ...]
revise_milestone(milestone_id, expected_revision, *, title=UNSET,
                 success_criteria=UNSET, deadline=UNSET, progress_note=UNSET,
                 command_id=None) -> Milestone
set_milestone_status(milestone_id, expected_revision, status,
                     *, evidence="", command_id=None) -> Milestone
complete_milestone(milestone_id, expected_revision, *, evidence, command_id=None) -> Milestone
bind_source(goal_id, expected_revision, source, source_id,
            *, milestone_id=None, use_policy="local_only", command_id=None) -> SourceBinding
get_binding(binding_id) -> SourceBinding
list_bindings(goal_id, *, limit=100, offset=0) -> tuple[SourceBinding, ...]
revise_binding(binding_id, expected_revision, *, use_policy=None, status=None,
               command_id=None) -> SourceBinding
remove_binding(binding_id, expected_revision, *, command_id=None) -> SourceBinding
```

Binding source 只接受 calendar/timer/task；关联里程碑必须属于同一 Goal 且为 Pending。分页 limit 1..100、offset 非负，按创建时间/ID 排序。Goal title 上限 500 UTF-8 字节，标准/进度/完成证据上限 8000 字节；Milestone 修改字段按其管理器实际校验，标题仍应保持简短。期限须带明确 offset，不解析自然语言日期或无时区时间。

Goal/Milestone/Binding 管理器命令域内，同 command_id、同操作/载荷返回**原成功结果快照**，包括原 revision；不会用当前对象替换回执。当前状态需再次 get。同键不同载荷或新命令使用过期 expected_revision，抛 GoalConflict。CLI 省略 command ID 时生成 UUID；可靠重试需要保留自己的键和原载荷。

## Proposal、用途与原子接受

首批规则仅使用 Goal/Milestone 的 24 小时截止窗口、显式绑定 Calendar/Timer 到期，以及绑定 Task 完成后提示复核 Milestone。Task 完成规则要求 milestone scope；无绑定私人日程不进入规则。没有后台云建议生成器。

状态为 `Pending -> Accepted/Ignored/Expired/Invalidated`。Proposal 保存 Goal execution_version、Milestone execution_version、Binding revision、rule/root/kind、due/expiry、用途、来源快照与唯一 accepted_task_id。业务唯一键含作用域版本、rule_id、root_source_key、kind；规则升级不应通过更换 ID 重发同一建议。

`ProposalManager(store, goals, tasks, scheduling=None, *, clock=None)` 要求同一工作库。主要方法：`get(id)`、`list(*, status=Pending, limit=100, offset=0)`、`preflight(id)`、`accept(id, expected_revision, *, command_id) -> Task`、`ignore(id, expected_revision, *, command_id=None)`、`validate_task(task_id, refs=())`。`list(status=None)` 查全部历史；Pending 查询/接受检查当前资格。刷新为有界扫描，状态不依靠全库即时遍历；执行资格仍逐边界复核。

首次 accept 在工作库事务外读 Scheduling 当前快照，再在同一个工作库事务内核对 Pending/revision/TTL、Goal/Milestone/Binding 版本和队列容量，创建唯一 Task/Submission并标 Accepted。失败整体回滚，队列满不留下孤儿 Task。重复点击返回同一 Task，同接受 command 改载荷拒绝；已成功回执在以后来源/TTL 变化后仍可幂等读取，但不恢复执行资格。

跨 Scheduling/Memory 库没有全局事务。来源可在预检后改变，因此出队、确认、云出口和步骤边界还要复核。工作库内取消/删除/依赖检查与新 Started 提交同事务排序；已开始效果不可撤回。

用途取严格交集：Goal、Binding 默认 local_only，绑定 Task 还继承原 Task 用途。Task 提交默认 cloud_allowed 不能提升来源权限；接受建议也不能扩权。只有所有来源允许云处理才可出云。修改原 Goal/Binding 用途产生新版本，旧工作仍失效。在云 Provider 下 local_only 任务保持 NeedsReview；离线配置可规划本地工作。API key 和来源用途许可都不代替工具 grant。

## Scheduling 公共元数据与消费者

| 公共成员 | 含义 |
| --- | --- |
| `store_identity` | 持久 ScheduleStoreIdentity(store_id, generation) |
| `source_snapshot(source, source_id)` | 当前 SourceSnapshot 或 None，含完整 canonical payload、fingerprint、身份、occurrence_revision |
| `list_events(source, *, after_id=0, limit=100)` | 历史事件，附 occurrence_revision、source_store_id/source_generation |
| `event_anchor(source, event_id)` | 所选流真实事件完整指纹；0 为 empty，缺失为 None |
| `event_cursor_state(source)` | 该流 tail_id/anchor |
| `poll_due()` | 可靠写入当前到期事件，返回新增数，不消费事件 |

Calendar occurrence revision 只随提醒时刻变化，不是全字段版本；改标题仍改变完整 fingerprint。删除源对象让当前 snapshot 缺失，历史 event 仍可查；旧 label 不得作为当前执行参数。

`LocalRuleEngine.poll() -> int` 返回本轮新 Proposal 数。disabled 时不接收也不初始化 cursor；首次启用按 timer/calendar 分别 tail 已有事件。每流每批最多 100，每轮新提议最多 10。接收事务提交 inbox+接收 cursor；处理事务提交 Proposal/合法跳过原因+scope_offset。接收 cursor 仅表示可靠入库；预算余项和单事件多作用域 continuation 都持久保留。

delivery key 含 local_rules、store_id、generation、stream、event_id。两个流不能共用接收 cursor。`consumer_status()` 暴露身份、received_id、anchor、Active/NeedsReview、error；`inbox_status()` 暴露顺序、scope_offset、Pending/Done、outcomes。身份/代际、尾部倒退或锚点变化停止消费，绝不自动清游标。

```python
review_consumer(stream, *, expected_store_id, expected_generation,
                strategy="tail", after_id=None, max_events=None, command_id=None) -> dict
```

tail 不接受历史参数；bounded_history 必须给所选流真实 after_id 或 0，以及 1..10000 的 max_events。它保存复核时尾界和剩余预算，跨轮/重启继续；到预算后明确跳到该复核尾界并审计，后来新事件照常接收。原 delivery/business 去重键保留。显式 reanchor 会把该流旧 Pending inbox 终结为 consumer_explicitly_reanchored；这是用户放弃/重建接收范围的决定，不能自动作为重试动作。

TTL 从事件 due_at 起算，默认 86400 秒；截止窗口从 deadline 起算 TTL，迟到事件不会获得“从现在再算一天”。quiet 按 IANA 时区支持跨午夜，起止相同表示不设 quiet，只限制命令边界摘要，不阻止可靠接收或授予执行权。持久时点用 aware UTC，monotonic 只用于当前进程间隔/Timer。

## Task、准确计划与恢复

Task 保留七状态并增加两个保守状态；Submission 与 Task 分开。

| Task 状态 | 含义 | 重启/后续动作 |
| --- | --- | --- |
| Created | 请求已建，Submission 另有 Queued | 未开始工作进入 NeedsReview，用户 resume 后才规划 |
| Planning | 规划中 | 无步骤时进入 NeedsReview，不自动补云 |
| Running | 执行中 | 按 attempt 证据分类，不能只信此字段 |
| Waiting | 原计划待授权 | 清空全部 grant，进入 NeedsReview，重新展示确认 |
| Completed | 可靠终结 | 无未知/不一致 attempt 时保留历史，不再执行 |
| Failed | 可靠失败 | 保留可能的部分效果，不自动重试 |
| Cancelled | 取消在安全边界落定 | 保留效果和取消意图，不等于回滚 |
| NeedsReview | 排队/恢复/失效等待审查 | 无 attempt 才能 resume；部分执行须新建剩余意图 |
| Uncertain | Started 无可靠结果或账本不一致 | 仅证据复核，原 Task 不直接恢复执行 |

容量计入 Submission 的 Queued/Claimed/Blocked，默认 100。正常等待授权和恢复后的未执行计划保持 Blocked，继续占容量；confirm.requeue 后才转 Queued。恢复不能绕过容量建立无限内存待审缓存；没有 Submission 的低层 Task 不允许 resume。提交成功只证明工作持久，不证明运行/完成。

PlanSnapshot 有 ID/revision 元数据以及完整 canonical JSON/hash。当前 hash 输入字段准确为 task_id、reply、steps（准确参数与稳定 call_id）、tool_specs（schema/权限/contract_version）、workspace_dir、memory_refs、policy_version、dependencies、use_policy；DTO 的 ID/revision 不额外加入 canonical JSON。每个 Task 只允许一个不可变快照，revision 当前为 1。超限拒绝可执行快照，不截断 JSON 后冒充完整计划。ToolSpec 默认 contract_version='1'，行为/契约变化要显式升级。

普通/高风险 grant 分开，只留当前进程 Task/准确计划上下文；数据库不保存可执行 grant。重启全部清空。跨进程 CLI confirm 必须给完整 64 位小写 SHA-256，并列本次授权；“曾确认”审计不是持久许可。

| 重启证据 | 实际恢复行为 |
| --- | --- |
| attempt 未提交 | 工具未获得该步骤资格；未开始工作待审查 |
| Started 已提交、无可靠结果，包括尚未调用或效果已发生 | Uncertain，不自动重放 |
| 部分步骤 Succeeded，后续未开始 | NeedsReview，保留结果，不续跑原多步计划 |
| 可靠 Failed | 保留结果/部分效果线索，不自动重试 |
| 全部 Succeeded，Task 终态/Reflect 未写 | 复核来源/版本/取消；有效时按结果收尾，附 warning，零工具/云重放 |
| 全部成功且取消已提交 | 有效结果可收尾 Cancelled，保留效果；失效仍待审查，不覆盖取消意图 |
| 未决 Started 且取消已提交 | Uncertain 与 cancel_requested 同时保留 |
| 旧可靠终态但账本/计划破损或存在未知步骤 | 未知证据优先，不仅凭旧 Completed 报成功 |

`ExecutionJournal.start_step(task_id, index)` 调用前持久 Started；`finish_step(attempt_id, ToolResult)` 返回后持久 Succeeded/Failed。重复开始/覆盖结果拒绝。review_uncertain 仅存 verdict/evidence；重试须独立 command/Task/计划和重新授权，旧未知记录保留。

## 当前配置和上界

`load_config(path=None, *, environ=None, provider_override=None)`：环境 > 配置旁 .env > TOML > 默认。显式 environ 映射不读 .env，加载不创建目录。CLI 本地管理强制 offline 且保留 data/workspace；submit/resume/confirm/accept-proposal 保留原 Provider。凭据只能放环境变量。

以下是 load_config 上界，不等于所有工具字段业务上限；数值须正且有限，整数不接受浮点/布尔。workflow/proactive 拒绝未知 TOML 键，数值类型额外严格。

| TOML 项 | 默认 | 范围/上界 | 环境变量 |
| --- | --- | --- | --- |
| workflow.queue_capacity | 100 | 整数 1..10000 | DESKPAL_WORKFLOW_QUEUE_CAPACITY |
| workflow.request_max_bytes | 65536 | 整数 1..16777216 | DESKPAL_WORKFLOW_REQUEST_MAX_BYTES |
| workflow.plan_max_bytes | 262144 | 整数 1..16777216 | DESKPAL_WORKFLOW_PLAN_MAX_BYTES |
| workflow.result_max_bytes | 262144 | 整数 1..16777216 | DESKPAL_WORKFLOW_RESULT_MAX_BYTES |
| workflow.shutdown_timeout_seconds | 5.0 | (0,60] | DESKPAL_WORKFLOW_SHUTDOWN_TIMEOUT_SECONDS |
| proactive.enabled | false | 布尔，环境 true/false | DESKPAL_PROACTIVE_ENABLED |
| proactive.poll_interval_seconds | 1.0 | (0,60] | DESKPAL_PROACTIVE_POLL_INTERVAL_SECONDS |
| proactive.event_batch_size | 100 | 整数 1..100 | DESKPAL_PROACTIVE_EVENT_BATCH_SIZE |
| proactive.max_proposals_per_poll | 10 | 整数 1..10 | DESKPAL_PROACTIVE_MAX_PROPOSALS_PER_POLL |
| proactive.proposal_ttl_seconds | 86400 | 整数 1..2592000 | DESKPAL_PROACTIVE_PROPOSAL_TTL_SECONDS |
| proactive.quiet_start / quiet_end | 22:00 / 08:00 | HH:MM，相同表示无 quiet | DESKPAL_PROACTIVE_QUIET_START / DESKPAL_PROACTIVE_QUIET_END |
| proactive.timezone | Asia/Shanghai | 可用 IANA 名称，缺数据拒绝 | DESKPAL_PROACTIVE_TIMEZONE |
| scheduling.poll_interval_seconds | 1.0 | (0,60] | DESKPAL_SCHEDULING_POLL_INTERVAL_SECONDS |
| memory.max_context_bytes | 8000 | 整数 1..65536 | DESKPAL_MEMORY_MAX_CONTEXT_BYTES |
| memory.max_candidates | 5 | 整数 1..20 | DESKPAL_MEMORY_MAX_CANDIDATES |
| memory.semantic_threshold | 0.55 | (0,1] | DESKPAL_MEMORY_SEMANTIC_THRESHOLD |
| memory.evaluation_timeout_seconds | 15.0 | (0,60] | DESKPAL_MEMORY_EVALUATION_TIMEOUT_SECONDS |
| memory.evaluation_max_output_tokens | 2048 | 整数 1..8192 | DESKPAL_MEMORY_EVALUATION_MAX_OUTPUT_TOKENS |
| llm.timeout_seconds | 30.0 | 正有限数，无统一最大值 | DESKPAL_LLM_TIMEOUT_SECONDS |
| sandbox.max_file_bytes | 1048576 | 正整数，无统一最大值 | DESKPAL_MAX_FILE_BYTES |
| agent.max_tool_steps | 8 | 正整数，无统一最大值 | DESKPAL_MAX_TOOL_STEPS |
| screenshot.max_pixels | 16000000 | 正整数，无统一最大值 | DESKPAL_SCREENSHOT_MAX_PIXELS |

其他配置：llm.provider 为 offline/openai，环境 DESKPAL_LLM_PROVIDER；model/base_url/api_key_env 对应 DESKPAL_LLM_MODEL、DESKPAL_LLM_BASE_URL、DESKPAL_LLM_API_KEY_ENV，默认空 model、https://api.openai.com/v1、OPENAI_API_KEY 变量名。openai 要求明确 model、非空 key、HTTP(S) base_url。storage.data_dir/sandbox.workspace_dir 对应 DESKPAL_DATA_DIR/DESKPAL_WORKSPACE_DIR，默认 data/workspace，相对路径以配置文件目录（无文件时 cwd）为基准。

memory.mode 默认 explicit_only，可选 candidate_review/paused，对应 DESKPAL_MEMORY_MODE；embedding_provider 默认 disabled，可选 fastembed，对应 DESKPAL_EMBEDDING_PROVIDER；model_cache_dir 默认 data/models/fastembed，对应 DESKPAL_MODEL_CACHE_DIR。主动规则不请求云模型；前台 Planner/显式候选学习仍服从各自配置。完整配置见 [config.example.toml](../../../config.example.toml)。

## 数据、删除、审计和备份

workflow.sqlite3 当前 schema_version=1，保存工作域对象、Task/Submission/计划/attempt、consumer inbox/cursor、outbox、deletion job。Memory/Scheduling 保留各自库。未知版本或已有未识别表的工作库明确拒绝，没有任意旧 schema 自动迁移器。

`WorkflowStore.transaction()` 使用 RLock、外层 BEGIN IMMEDIATE。同线程嵌套复用连接，内层失败使外层整批回滚，即使调用方吞掉内层异常也不能提交部分结果。业务变更与必要 outbox 同事务，跨库读取在写事务外进行。

受控 `memory_command('forget', memory_id=..., expected_revision=...)` 先提交 deletion job/使用阻断，再幂等删除 Memory，擦除明确依赖来源的工作副本，最后完成工作单。启动先续办；失败返回 Pending 并保持显示/派发阻断。擦除含相关 Task 请求/结果、计划正文、attempt 输出/复核正文、未投递审计副本。规则不从 Memory 自动创建目标/提议，独立 Goal/Calendar 不按文字相似度删除；回执说明历史日志、备份、外部副本等保留范围。Uncertain 留不透明 ID、结果结论/取消意图，正文被忘不代表效果从未发生。

默认字节门槛见表。计划超限执行前拒绝；超大/无法完整编码的结果正文可明确省略，success/failure 结论仍保留。历史没有默认自动删除时限；Proposal TTL 是执行资格，不是正文保留期。

WorkflowExecutionLog 默认仅存白名单结构化元数据，不复制请求/参数/模型正文。`deliver_audit` 按 event_id 投递 JSONL，成功后确认；投递/确认失败可重试，同 event_id 可能多行。JSONL 是观察副本，不恢复执行命令。`store.pending_audit(limit=100)`、diagnostics 暴露积压；投递失败不改写工具效果，不重执行。

`store.backup(Path(destination))` 使用 SQLite 在线 backup，包含当时工作库 WAL。CLI `--workflow backup --backup-path ...` 只备 workflow；默认拒绝任何已存在的目的路径，创建新文件后原子发布，不能覆盖 Memory/Calendar 等库。选择新明确文件名。它不是 Memory/Scheduling/JSONL 全局快照。

恢复前必须停止该 data_dir 实例并保留现状副本。当前没有任意跨库恢复协调器；单独替回一个旧库不能视为三库一致。使用协调的停机备份，启动后检查删除工作单、Task 账本、消费者身份/锚点；NeedsReview 要用户明确选 tail/有限历史。外部效果没有 exactly-once/自动回滚保证；旧备份和已投递日志不属于在线遗忘能追回的范围。
