# 拟人化增量 H0–H5 开发记录

日期：2026-10-22。状态：**H0–H5 已实现，H6 验收材料完成，用户授权后的集中测试及确定性修补已收尾；离线已执行用例与 strict 通过，HM08 真实体验未通过，整体未验收通过。** 下文记录原开发交付时的范围与边界；当时按要求未运行验证。实际首败、修补、定向复测及未完成项见[拟人化测试记录](humanization-test-report.md)，不得把此前 V0.8 最小测试的通过结果应用到这些新代码。Git 由用户提交，产品版本号暂不调整。

设计依据：[总体方案](../../planning/v0.8/humanization-proposal.md)、[架构](../../planning/v0.8/humanization-architecture.md)、[实施顺序](../../planning/v0.8/humanization-delivery-plan.md)。H0–H6 全部完工后统一执行[HM01–HM08](../../planning/v0.8/humanization-minimal-test-plan.md)。桌宠表现没有新增实现，仍只保留规划中的提示契约。

2026-10-23 后续 HM08 修补另见[改进计划](../../planning/v0.8/hm08-improvement-plan.md)及[实现/验证记录](hm08-improvement-report.md)：共享表达协议、范围证据、草稿来源、工具补充和原生 JSON 可选能力已纳入实现；下文保留 H0–H5 原交付边界，新的实际测试结果不覆盖旧首败。离线复测通过，真实调用失败及表达偏差按用户要求[暂缓](hm08-deferred-issues.md)，整体体验未验收。

主 Agent 负责设计决策、跨模块协议、Core/Application 整合与源码审查；三个 6.1 Sol 高思考子 Agent 按限定文件分别实现持久基础、companion/response 和 conversation/GUI/CLI。没有分发产品设计，也没有运行测试或提交 Git。

## 1. 已实现的范围

| 阶段 | 源码落点 | 已接入的行为 |
| --- | --- | --- |
| H0 | `companion/{runtime,repository,reducer,interaction_runtime}.py`、`response/{composer,models,ports,templates}.py`、`agent/core.py` | 分离存储/纯状态计算/互动策略，抽出单次回复生成与确定性工具摘要；Core 保留执行与审批骨架 |
| H1 | `llm/calls.py`、`workflow/{migrations,humanization,store,journal,tasks,ports,models}.py` | schema 2 迁移、任务隐私代际、按目的调用账本、规划/表达来源分离、原计划兼容与可靠终态 outbox |
| H2 | `companion/{policy,models,interaction_runtime}.py`、`response/composer.py`、`application/interaction.py` | 五类云用途许可、有限角色身份、获准自定义轴/偏好/线索/情绪进入最终表达，关键工具事实先由模板显示 |
| H3 | `companion/{signals,interaction}.py`、`memory/{coordinator,evaluation}.py` | 当前消息的明确句式约束、真实生成调用返回的情境候选、来源区间校验、短期 cue、显式偏好确认及作用域/纠正处理 |
| H4 | `conversation/*`、`agent/context.py`、`application/{interaction,effects}.py` | 进程内有界会话、顺序和双投影、来源传递/预算、根任务效果去重及本地投影；普通聊天不奖励数值 |
| H5 | `application/{privacy,service,dispatcher,desktop,cli}.py`、`gui/{bridge,conversation_state,conversation_panel,window}.py` | 公共入口执行中受理遗忘，先持久阻断使用，安全点清理及 Pending/重试；新会话/用途许可/候选/清理状态的 GUI 和 CLI 控制 |

这是源码交付范围，不是通过证明。内部类型、SQL、Qt 事件循环、真实模型输出及升级路径仍须集中验证。

## 2. 一次请求的实际调用链

`ApplicationService.submit` 在原 dispatcher 的提交事务中绑定 conversation/turn，缺省仍是独立单轮。单 worker 读取 Memory bundle 后，通过 `InteractionCoordinator.prepare` 冻结中立规划视图与表达视图。

内置 `LLMPlanner.plan_context` 使用 task/purpose 绑定客户端，只获得当前 request、中立 Memory 和历史用户引用/确定性工具事实；不获得 persona、cue、affect 或历史助手自由回复。生产检索通过 `MemoryCoordinator.retrieve_for_planning` / `MemoryRetriever.retrieve_for_planning` 在排名及条数/字节预算之前排除 profile 来源，防止资料偏好间接挤占执行事实；随后复核真实来源、修订及 epoch。历史只作指代背景，不作为新指令或授权。自定义旧 Planner 仍支持单轮；需要历史执行上下文时明确拒绝不支持的旧接口，不把历史伪造成记忆。

工具继续经过原计划确认、ToolManager/Sandbox 与 attempt 账本。`ResponseComposer` 使用受限表达客户端，当前消息、获准历史/记忆与未核验草稿都放在数据区；无工具调用能力、无修复请求或自动重试。工具结果先显示确定性摘要，再显示可选补充文字；失败/取消/未知/审查保留既有固定反馈。

Composer 的合法正文可独立保留；非法候选区整体丢弃。非法顶层/正文或工具调用会回退。隐私或来源失效时连原草稿也不能发布；仅表达设置代际变化且中立来源仍有效时，允许回退普通草稿。终态可靠提交后才发布上下文与候选；情境候选影响后续轮次，当前回复直接理解用户原话。

## 3. 状态与用途的实际边界

- 默认仍关闭 companion；新增 custom_persona/preferences/cue/affect/history 五类云许可默认全部关闭。开启功能而未扩大许可时仍只使用原固定预设。
- `interaction_snapshot` 是独立 schema 1 DTO；原 `companion` snapshot 的字段形状与七字段 `presentation` 保持。角色身份另有 local_only/cloud_allowed 来源用途，类别许可不能解除来源限制。
- 明确称呼/语言资料复用 Memory profile；表达覆盖由 companion 管理。已过滤的 profile 只向表达视图提供获准项。当前严格语法消费的 verbosity 不再重复交给长期记忆评估。
- 自然语言明确称呼/持久语言请求可由同一次 Composer 返回 `preferred_name` / `reply_language` 候选，与表达偏好合计最多 2 项。姓名限定 NFC 后 40 字符/120 UTF-8 字节且无控制字符，语言限定 zh/en；来源仍是当前原话和精确 span。可靠终态提交后，在发布屏障内以稳定任务时间及幂等来源写入 **未接受** Memory 候选，沿用七天候选 TTL、修订确认、冲突处理和遗忘墓碑。确认前不会成为生效资料；memory.mode=paused 时不写入。与本次表达候选重合的 profile 字段不再交给可选记忆评估重复提议。
- once 只约束本轮，session 随会话，today 至配置时区次日午夜且最长 24 小时；长期偏好要通过明确设置或候选确认。候选保存有主机回执和 command_id 幂等，不让模型自行写配置或声称已保存。
- 当前自然语言情境由表达模型处理；本地规则只识别明确命令和保守来源限制，不以情绪关键词表代替理解。span 校验不能证明模型语义正确，反讽/否定等效果留待 HM08 评审。
- companion 关闭时不抽取/保存自然互动候选；情绪关闭时不保存 cue，明确偏好候选仍可用。关闭结构化 cue 不等于云模型无法理解本次主动发送的原话。
- 云情绪使用固定预设基线、独立窗口预算及允许来源事件投影；本地轴、local_only 事件或本地预算占用不会经共享数值间接进入该投影。

会话上限为 4 个，闲置 30 分钟释放；每个最多 8 轮/16 条/16 KiB 正文与事实预算。每项来源最多 128 个、一次上下文最多 512 个；规划另限 32 项/16 KiB，按最近完整轮次选取，不截断来源集合。模型网关对消息和工具描述的序列化总额另限 128 KiB，超限拒绝调用。

WorkingContext 不跨启动恢复，不从任务列表回填。既有 task.request/result 仍落盘；必要的中立规划快照也落在工作库用于审查，因此不宣称完整临时/不落盘聊天。规划/表达依赖都参与遗忘闭包，只有规划视图进入计划 hash。

## 4. 生产入口与调用预算

新增服务接口：

| 接口 | 行为 |
| --- | --- |
| `conversation_open()` | 创建当前进程的会话，返回 conversation_id/epoch |
| `submit(..., conversation_id=..., expected_conversation_epoch=...)` | 同 command_id 的重试绑定同一轮次；不能更换会话 |
| `conversation_snapshot(conversation_id)` / `conversation_close(conversation_id, expected_epoch)` | 有界查询/关闭；关闭后线索、候选和 session 覆盖失效 |
| `interaction_snapshot()` / `interaction_command(action, **parameters)` | 身份、用途、偏好候选确认/拒绝、清 cue/约束；写操作使用 expected_revision，可携带稳定 command_id |
| `request_forget(memory_id, expected_revision, command_id)` | 执行中也能先受理使用阻断，返回 AcceptedPending/Completed 及 privacy_epoch |
| `cleanup_status()` / `retry_cleanup()` | 查询 Pending 与等待 worker 的状态，重试幂等清理 |

Desktop 端点同名。GUI 新会话页只导航到既有 task 审查/审批，不新增“聊天同意即授权”的通道。称呼/语言候选通过“查看称呼 / 语言候选”进入原记忆页审核；详略、幽默和追问候选在会话页确认。用途撤销通过明确 `privacy_invalidated` 回执推进 GUI epoch，旧回包拒绝；普通编辑保留未保存输入。

CLI 增加 `--conversation interactive`（本地有界会话）、`--conversation-cloud`（当前请求按配置允许云处理）、`--interaction ACTION --interaction-parameters JSON`，以及 `--privacy status|forget|retry`。管理路径强制离线；未给 `--conversation-cloud` 的显式会话也使用离线演示。表达偏好候选仅在当前进程内，CLI 需在同一交互进程通过 `/interaction` 确认，不支持退出后取回该候选；称呼/语言资料候选由持久 Memory 管理，可通过既有记忆审核入口查询及确认。

每根任务 planning/expression/memory_evaluation 各最多一次尝试。普通禁用聊天通常 1 次，启用表达聊天/工具通常最多 2 次；candidate_review 另占 1 次。明确开启历史也可能触发表达生成，即使没有角色风格。Waiting、管理、轮询和空闲不增加模型请求。超时/发送未知消耗槽位，重启或确认不刷新；记忆评估原有独立超时/输出设置通过目的客户端保留。

真实生成目前仍依赖配置的云模型。OfflineLLMProvider 是固定命令演示，无法承担通用自然语言对话或生成 cue；本轮没有虚构本地大模型能力。

## 5. 事件和遗忘的时序

可靠 Completed/Failed 与效果 outbox 同工作库事务；只有真实步骤账本核验通过的工具成功/失败符合资格。没有工具的聊天、模型失败、取消和拒绝不产生数值奖励。已有单 worker 在 idle 边界做有界本地投影，不创建第二个执行者或周期性云任务。

投影以可靠终态时间计算衰减和额度，不以重投递时刻刷新奖励；超过 60 秒、显著未来、跨隐私代际或已跨重置锚点的事件跳过。companion 状态/收据/预算同事务，跨库确认失败可重复投递而不重复增量。outbox 最多 4096 项；持续存储故障达到上限时省略后续可选效果并记诊断，不阻断工具结果或靠重做补齐。

遗忘公共入口先在工作库原子记录 job、永久来源阻断、全局使用门禁与新 privacy_epoch；不等待在途模型或同步工具。后续发送许可、Started 和结果发布均复核屏障。此前已授予发送许可的请求和 Started 工具可能继续产生外部效果，不宣称能撤回。

worker 到达安全点后继续处理：Memory 共享来源删除 → workflow 直接/间接上下文与任务副本 → 受污染任务生成的候选/情景记忆 → 关联的已确认自然偏好 → 缓存及旧效果。明确独立配置、Goal、Calendar、历史日志/备份/导出/云副本有各自保留边界。失败保持 Pending，启动优先续办；相关步骤可重试，不恢复旧授权或自动执行旧计划。

如果受理后来源修订发生竞争，旧 revision 不会自动获得删除新修订的资格；保持阻断并等待显式新修订请求。新修订被明确授权并清除该来源后，相关旧 Pending job 可以一并完成。

## 6. 与原提案的实现调整

1. 使用 `workflow/humanization.py` 统一同库的 turn/context/privacy/call/effect 窄接口，避免拆出多个只转发 SQL 的空仓库；Core 仍不持有 SQL/线程实现。
2. `PreparedTurn` 与注入的 `TurnInteractionPort` 承载两种视图和票据字段；Application 拥有模型网关、来源校验和发布锁。旧 Planner 通过新增 `plan_context` 入口渐进适配。
3. companion 存储与校验提取为 repository，互动操作独立 facade 层，衰减/增量提取为纯 reducer；不同时更改原半衰期和限幅常数。
4. 保留已有记忆评估流程，改用受控目的客户端；不支持受控客户端的自定义评估器跳过自动学习并给降级提示，不能绕过网关。
5. 云策略新增独立 affect/budget，避免本地数据通过派生基线/窗口间接外发。用途撤销先推进持久代际，再跨库改设置，失败时旧任务也保持失效。
6. 继续保留 companion 存储不可用时的中性降级；工作库升级失败则拒绝启动。两库没有全局迁移事务，不将个性化不可用误报为完整迁移成功。
7. 自然语言资料候选复用现有 Memory 审核，而非把个人事实放进 companion 配置或跨库直接接受。中立检索在排序之前排除 profile；原 `retrieve()` 低层接口保持原行为，生产自定义 retriever 则必须实现 `retrieve_for_planning()`，不能用事后过滤冒充规划不变性。

## 7. 升级、兼容和待办

工作库 schema 1 → 2，迁移前 online backup 到 `workflow.sqlite3.schema-1-<uuid>.backup`；companion SQLite schema 1 → 2，备份到 `companion.sqlite3.schema1.<uuid>.bak`。两个库单独事务、未知 schema 拒绝。旧 Persona/收据/预算保留，新字段默认不增加云许可。旧计划 canonical/hash 原样读取；新计划 policy_version=2 包含中立上下文 digest/refs。降级程序需选择一致备份恢复，不可仅降低 schema 数字。

H6 已准备的旧测试适配及验收材料：

- 旧 schema 1 自动加索引测试需区分工作库 schema 已升级至 2，presentation DTO 仍为 1。
- 原情绪数学夹具中省略 tool_success 的 Completed/Failed 现在不应获奖励；数学基线事件要明确传 True/False，并保留原数值断言，另覆盖 None 无奖励。
- 原 Reflect/表达调用内容的快照需对照新的 Composer 数据区与事实摘要更新；不能放宽权限、来源、次数或结果真实性断言。
- 自定义 Memory/retriever 夹具需提供中立检索接口；补足 profile 在排名前隔离及自然语言资料候选未接受/接受/冲突/遗忘的集中样本，复用 HM01–HM03、HM05、HM07，不额外新增测试批次。

新增 `test_humanization_domain.py`、`test_humanization_application.py`、`test_humanization_migration.py`、`test_humanization_conversation_gui.py`，复用旧 companion 和系统回归；`scripts/check_humanization_live.py` 提供 6 场景/24 轮的有界真实模型材料。固定执行由 6 Luna 高思考负责，权限/故障归因及关键修补由主 Agent/6.1 Sol 高思考负责。

集中首轮及修补后的当前结论以[测试记录](humanization-test-report.md)为准。测试中补齐了 outbox 无新任务启动恢复、隐私状态只读查询及检索返回 DTO 校验；另修复 Python 3.11 Windows 默认时钟量化导致终态效果误判为未来事件的问题，保留注入时钟、TTL、预算、去重和 reset 边界。主环境定向复测 428 项通过，最终时钟回归主环境/Python 3.11 各 91 项通过，strict 132 文件通过；批次重叠不累加。两项既有符号链接权限仍未验证，真实模型 26 次尝试暴露结构契约不稳、偏好范围误判及草稿事实扩写，HM08 未通过且 S4 两轮未运行。未修改桌宠表现、未恢复 V0.6、未关闭 V0.7 未结项，也没有新增依赖、训练人格或周期性云请求。
