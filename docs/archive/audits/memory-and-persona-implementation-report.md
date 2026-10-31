> 历史存档：保留当时的设计、失败与验收边界；不代表当前实现或新的执行授权。当前入口见 [文档导航](../../README.md)。

# 当前记忆与人格机制：实现审计与技术讲解

审计日期：2026-10-27  
审计范围：当前工作区中与记忆、对话上下文、人格、互动偏好、短时 cue、模拟情绪、模型调用和遗忘有关的实现。  
证据优先级：生产代码 > 已存在测试的断言 > 版本交付记录 > 长期设计提案。  
执行范围：本次只读调查并新增本报告；没有运行测试、应用、类型检查或静态检查，没有修改生产代码、配置或既有测试。

文中的“已实现”表示源码中存在正常业务入口及可识别的持久化/调用路径；它不表示本次执行验证通过。提到测试时只说明仓库中存在相应断言或历史测试记录，不代表本次重跑。

## 1. 执行摘要

当前系统有三类容易被统称为“记忆”、但职责不同的数据：

1. **长期、可审核记忆**：存于 memory.sqlite3。用户明确“记住”会直接写入已接受的语义断言；有限的工具执行结果会写为情景事件；可选的自动学习只生成候选。检索必须经过状态、来源、范围、有效时间和云端用途过滤，再按词法/可选向量相关性排序，按预算把证据送给 Planner。
2. **对话时间线与有界工作上下文**：会话和已发布轮次存于工作流存储，ConversationRuntime 按最近轮次构造有界的规划/表达上下文。它不是 MemoryKind.WORKING，不从旧任务库自动拼造对话。热窗口/交互状态在进程内；持久会话时间线可在之后重新加载。
3. **人格与伴侣状态**：存于 companion.sqlite3 的有限预设、六个风格轴、配置版本、身份、部分互动偏好以及工具任务驱动的模拟情绪。cue、候选和会话级偏好有进程内部分。它们会在许可条件下进入表达模型，不进入内置 Planner 的规划提示。

因此，系统目前有**版本化的人工人格配置、有限的事件驱动情绪模拟和用户可审核偏好**，但**没有由经历自动改变长期人格倾向的机制**。模型收到表达指引不等于措辞效果有硬保证；人格不会授予工具权限。

三个最重要的边界：

- MemoryKind 的五个值只是兼容分类，不是五套都完整可用的产品记忆功能。当前生产记忆主要写 SEMANTIC 和 EPISODIC；Goal 由独立领域管理。
- 会话上下文、长期记忆、UserProfile、Persona、互动偏好和 affect 不是同一份可自由编辑的 prompt。它们由不同服务读取，带不同来源和用途限制。
- 遗忘操作已有持久屏障、代际失效及重试机制，但明确不承诺擦除已产生的执行日志、备份、导出、外部副本或已开始的外部效果；某些精确竞态没有单独测试证明穷尽。

## 2. 项目实际架构与关键模块地图

### 2.1 数据所有权

| 数据 | 实际所有者/存储 | 主要代码 | 用途与边界 |
|---|---|---|---|
| 来源化记忆、修订、画像投影、向量 | memory.sqlite3 | memory/repository.py、memory/coordinator.py、memory/retrieval.py | 长期记忆权威数据。默认检索只用符合资格的当前 accepted 修订。 |
| 会话目录、消息时间线、任务与计划、执行账本、隐私任务、交互上下文快照和 outbox | workflow.sqlite3 | conversation/repository.py、workflow/store.py、workflow/tasks.py、workflow/journal.py | 对话显示和任务/授权恢复使用；不能把旧 Task 自动当成聊天历史。 |
| Persona、Affect、预设修订、身份、互动设置和事件收据 | companion.sqlite3 | companion/repository.py、companion/runtime.py | 受版本、代际和用途约束的表达状态。 |
| 工具执行观察日志 | execution.jsonl | execution_log/__init__.py、application/factory.py | 审计/诊断用途；不属于 MemoryRepository 的遗忘闭包，忘记记忆不会保证擦除它。 |
| 临时对话热窗口、cue、session 偏好、候选和待发布状态 | 进程内对象 | conversation/runtime.py、companion/interaction.py、companion/runtime.py | 有界、超时或关闭时清理；部分持久会话内容可由工作流库重新加载。 |

create_application() 在真实桌面服务中创建 CompanionRuntime、工作流存储、GoalManager、MemoryRepository、Retriever、MemoryCoordinator、AgentCore 和 ApplicationService，并将数据放在用户数据目录（src/deskpal/application/factory.py:142-195；记忆路径 :84-112 附近）。低层 Agent factory 若未收到 companion 文件路径，可建立内存型 CompanionRuntime；不要把测试/低层构造方式误认为桌面生产持久化路径（src/deskpal/application/factory.py:35-65,157-169）。

### 2.2 单轮用户交互的真实调用图

~~~mermaid
flowchart TD
  UI[GUI 或 CLI] --> S[ApplicationService.submit]
  S --> D[SubmissionDispatcher 单 worker]
  D --> C[AgentCore._run]
  C --> R[MemoryCoordinator.retrieve_for_planning]
  R --> MB[MemoryRetriever 资格过滤 / 词法与向量 / 预算]
  C --> I[InteractionCoordinator.prepare]
  I --> CR[ConversationRuntime.freeze]
  I --> P[Persona / profile / cue / affect 的表达投影]
  C --> PL[Planner.plan_context]
  PL -->|中立记忆 + 用户历史引用 + 工具事实| LLM1[planning purpose 模型调用]
  C --> AUTH[提交计划后的授权与工具安全检查]
  AUTH --> TOOL[ToolManager / Sandbox / 执行账本]
  C --> RC[ResponseComposer.compose]
  RC -->|获准的表达上下文| LLM2[expression purpose 模型调用]
  C --> MEM[工具结果记忆 / 可选候选评估]
  C --> COMMIT[任务终态提交]
  COMMIT --> PUB[对话时间线与 interaction signals 发布]
~~~

主要源码链：

- GUI/CLI 把命令交给 ApplicationService/desktop facade，而不是直接写 SQL 或调用 QWidget 内的 Agent（src/deskpal/application/desktop.py、application/service.py、application/cli.py）。
- ApplicationService.submit 将提交与会话身份绑定，再由 dispatcher 单 worker claim 并调用 Core（src/deskpal/application/service.py:274-319、application/dispatcher.py:311-340）。
- AgentCore._run 先识别显式记忆请求；否则取 Memory retrieval bundle，要求其 revision 有效，再冻结 interaction context 并规划（src/deskpal/agent/core.py:525-590）。
- 计划通过实际用户授权、ToolManager 与 Sandbox 执行；之后 Core 生成回复、可选记录情景记忆/学习候选，提交任务终态后发布会话轮次与 interaction signals（src/deskpal/agent/core.py:606-780）。

## 3. 记忆机制详解

### 3.1 分类和数据契约

#### MemoryKind 与新版事实契约

旧分类定义在 src/deskpal/memory/models.py:6-24：

| MemoryKind | 当前实际含义 |
|---|---|
| working | 兼容分类；不是当前 ConversationRuntime 的工作上下文，也没有生产写入的持久工作记忆流程。 |
| episodic | 当前用于工具执行事件记忆；不是完整聊天记录。 |
| semantic | 当前用于用户明确记忆、部分用户偏好和有限画像字段。 |
| goal | 可由分类/旧兼容存储表示，但目标权威状态属于 goals/workflow 域，不靠此类记忆管理。 |
| relationship | 可由分类/旧兼容存储表示；没有完整 RelationshipProfile 写入和生命周期。 |

旧 MemoryRecord 含 kind、content、importance、confidence、created/last-access 时间、decay_rate、task_id 等（src/deskpal/memory/models.py:15-24）。SQLiteMemoryManager 保留旧 SQLite API（src/deskpal/memory/manager.py:86-203）。新版正常业务使用来源化 DTO，不能把两种 API 及它们的检索行为混成一个机制：

- MemorySource：正文、来源 origin、owner/session/task、创建时间和 use_policy（src/deskpal/memory/contracts.py:54-64）。
- MemoryDraft：陈述内容/依据、claim type、kind、subject/predicate/value、scope/profile_key、importance、抽取置信度、有效区间、expiry、pinned 和 half-life（:66-84）。
- MemoryItem：稳定 ID、revision、status、source、draft、记录时间、accepted 时间、supersedes 和修改原因（:86-97）。
- MemoryStatus 区分 candidate/accepted/disputed/superseded/archived/rejected/deleted/legacy；ClaimType 区分 user_report/observed_event/external_claim/hypothesis/legacy；UsePolicy 区分 cloud_allowed/local_only（:30-51）。

这三个维度不能互相替代：类别回答“这是什么种类的记忆”，claim type 回答“它以什么性质陈述”，status 回答“目前处于什么审核/生命周期状态”，use policy 回答“它可以用于什么外部处理”。accepted 也不代表现实世界已经独立核验。

#### 实际可用的记忆形态

| 形态 | 状态 | 结构、写入和读取 |
|---|---|---|
| 来源化长期记忆 | **已实现** | memory.sqlite3 保存 source、revision/head、状态、索引与删除标记。明确保存或候选审核产生可审阅记录；eligible accepted 才可能被下一次查询召回。 |
| Semantic 用户事实/偏好 | **部分实现** | 明确“记住”直接保存 user report；严格识别的姓名、语言、详略偏好可以投影为 profile。不是开放式用户画像，也不自动推断任意生活偏好。 |
| Episodic 工具事件 | **部分实现** | 每个有工具调用结果的任务可记录一条简化步骤数组；不保存所有聊天、不将工具原始输出一概作为长期事实，也不做跨事件总结。 |
| Candidate memory | **已实现（可选模式）** | candidate_review 模式下评估器可生成候选；候选在用户接受前不作为默认事实检索。 |
| UserProfile | **部分实现** | 只投影 preferred_name、reply_language、verbosity 这三个白名单 key（src/deskpal/memory/retrieval.py:45-77）。当前允许值经过限制，不是任意心理画像。 |
| 多轮对话时间线 | **已实现** | 工作流存储独立持有会话目录及发布的用户/助手轮次；按边界构造最多 8 个最近轮次、16 条消息、16 KiB 表达历史（src/deskpal/conversation/runtime.py:16-24,142-228）。它不同于 MemoryKind。 |
| Session WorkingContext / 临时偏好 | **部分实现** | bounded history 可从已发布会话时间线重载；活跃热窗口最多 4 个，闲置 30 分钟释放。交互 session 偏好/cue 是进程态，重启后不恢复（conversation/runtime.py:56-105；companion/interaction.py:13-22）。 |
| 会话摘要/跨事件记忆摘要 | **未实现** | 未找到生产摘要器将多条记忆聚合成有依赖版本的 summary。 |
| Goal memory / Relationship memory | **未作为记忆功能实现** | Goal 有单独数据所有者；RelationshipProfile/共同经历自动画像没有对应生产写入流程。 |
| 向量索引 | **部分实现** | 可选本地 embedding manifest/vector 存入 SQLite；重建失败时保留词法检索。向量是索引，不是另一份事实来源（memory/repository.py、memory/embedding.py、memory/retrieval.py:209-263）。 |

### 3.2 写入来源与完整生命周期

#### A. 用户明确说“记住……”

调用链为：

1. AgentCore._run 用固定前缀表达式识别 记住：、记住: 或 记住 （src/deskpal/agent/core.py:525-559）。
2. 命中后创建任务并走显式保存分支；它不等待 Planner 或普通云端规划成功。
3. 通过当前任务来源用途调用 MemoryCoordinator.remember（:545-555）。
4. remember 校验文本、policy/scope，按有限规则识别允许的 profile 字段，检查同 key 的 accepted 冲突；创建 source 和 draft，以 accepted=True 写库（src/deskpal/memory/coordinator.py:155-188）。
5. 保存结果回执“已保存用户陈述”，Task 与 Memory revision 关联。

该路径有意将用户明确陈述视为“用户说过这个”，而不是现实已验证事实。非精确前缀形式未必进入此专用分支。若输入本身有多条混合陈述，代码没有通用事实分句和用户逐项确认流程。

#### B. 工具任务结果

工具结果只在任务确有结果、记忆模式非 paused 时，进入 record_execution（src/deskpal/agent/core.py:707-720）。Core 将调用压缩成 steps：工具名、调用 ID、success 布尔值。Coordinator 以 origin=tool_result、claim_type=observed_event、kind=episodic、predicate=execution 写入；同一 task 已有记录则避免重复覆盖用户修订（src/deskpal/memory/coordinator.py:190-214）。

这保存的是执行事件摘要，不是“工具返回的所有文字都是真实且永久成立”。例如一次 calendar_list 结果不等于建立某长期日程偏好；任务成功与外部世界状态也需按各工具的结构化结果解释。

#### C. 自动 Candidate Review

自动学习默认不启用；需配置 candidate_review、有 evaluator 且来源 policy 允许云处理。MemoryCoordinator._learn 先 claim task_id，再调用 evaluator、限制候选数和字段、校验来源 span/数据形状，最后以幂等 fingerprint 写为 candidate（src/deskpal/memory/coordinator.py:291-344）。本地来源跳过云 evaluator；模式暂停不写。candidate 缺省 TTL 为 7 天，显式接受时会移除默认候选 expiry（src/deskpal/memory/repository.py:155-165,565-588）。

V0.8 的表达信号另可提出少量偏好候选。模型返回的是候选，不直接改设置；主机检查来源 span/属性后，用户仍需通过 memory 或 companion 候选审核确认（src/deskpal/application/interaction.py:257-315、companion/signals.py、companion/interaction_runtime.py:121-195）。

### 3.3 检索、排序和上下文注入

#### 资格过滤

memory/retrieval.py:_eligible 只允许当前 accepted、local owner、非 legacy claim、global scope、有效时间内且未过期的记忆；云模式排除 local_only（src/deskpal/memory/retrieval.py:21-34）。检索并不根据五个 MemoryKind 各配一套独立生命周期规则。

UserProfile 有额外的来源和字段白名单；重复同一 key 的 accepted 值若互相冲突，投影不猜一个“正确值”（:45-77）。

#### 相关性和预算

MemoryRetriever 默认最多 5 条、8,000 UTF-8 字节、语义阈值 0.55；该阈值来自项目版本化合成校准数据，不是通用正确率保证（src/deskpal/memory/retrieval.py:175-207）。

检索先取 accepted 当前记录并做资格过滤，再并行计算词法和可用时的语义相似度；语义索引不存在/失败时退化到词法。排序以 reciprocal-rank fusion 合并词法/语义顺序，profile 为独立优先项，importance × 半衰期激活值和时间只作次级排序（:276-390）。half_life_days 的表达式只降低召回优先级，不会定时修改或删除记忆。

记录必须完整放入字节预算，不按字符随意截断。最终入选前重查 revision/资格及 epoch；检测到并发更改时返回空证据包并附警告，Core 在 Planner 调用前后再次检查 bundle validity（retrieval.py:362-390；agent/core.py:562-590）。

#### 它实际进入哪个模型

- InteractionCoordinator.retrieve() 调用 retrieve_for_planning()，这个方法在排序和预算之前排除 profile 记录（src/deskpal/application/interaction.py:66-76；memory/retrieval.py:267-274,287-313）。
- Core 将 bundle 记录内容作为 memories 交给 Planner.plan_context（agent/core.py:562-588）。内置 LLMPlanner 使用这些证据构造规划数据，不把 profile、persona、cue 或 affect 放入规划输入（src/deskpal/planner/llm_planner.py:22-77）。
- 合法表达上下文另含获准的 profile、bounded 会话历史、Planner 草稿和可信工具结果；记忆来源、有效 revision 与 use policy 随准备过程绑定（application/interaction.py:78-95,129-220）。
- 记忆存在不等于每次相关请求一定会带上它：必须通过 eligibility、召回排名、条数/字节预算、云策略和并发复核。关键词/向量漏召回和预算排除均可能发生。

### 3.4 更新、冲突、去重、过期与删除

- **版本与 CAS：**每次更正、审核使用 expected_revision，过期 UI 版本不能静默覆盖最新修订。Repo 维护 revision 与 current head（src/deskpal/memory/repository.py:459-467,559-626）。
- **冲突：**有限 profile key 的相反 accepted 值要求带 revision 的更正路径；profile projection 遇到冲突会跳过该 key，而不是自动取多数（src/deskpal/memory/coordinator.py:163-174；memory/retrieval.py:70-77）。
- **幂等：**ingest 可用稳定 key/fingerprint，重放相同输入返回既有记录，不同内容冲突；tool event按 task 防重复，Candidate evaluator 按 task claim 只消费一次（repository.py:495-542、coordinator.py:197-214,307-343）。
- **候选 TTL：**候选有缺省 7 天过期；Repository 操作会处理到期候选并清理 payload/留下 tombstone（repository.py:671-682）。它不是所有 accepted 记忆的统一 TTL。
- **活跃度衰减：**检索 priority 使用 importance * 2^(-age_days / half_life_days)（retrieval.py:356-360），不做后台巩固或删库。
- **传统旧表迁移：**schema v1 先备份，旧记忆导入新版时改成 status=LEGACY 和 claim_type=LEGACY，不自动成为确定事实（repository.py:246-311）。
- **记忆层 forget：**CAS 校验后按该 ID 所有版本引用的 source 计算共享来源闭包，删除本地正文、修订、embedding、画像投影和派生任务关联；保留不含正文的 tombstone/去重阻断。单纯数据库 forget 不删除 execution.jsonl、旧备份、用户导出、云服务副本或介质残留（repository.py:628-716；docs/versions/v0.3/interfaces.md:47-49）。
- **Application 隐私屏障：**真实公开 request_forget 先以稳定 command_id 建删除任务、持久阻断隐私使用、增加 privacy epoch、清弃 outbox，再失效 interaction/cache 并安排清理/重试（application/privacy.py:55-70、application/service.py:505-536,717-787）。worker 在安全边界续做并对工作流、会话、候选和 companion 来源做清理。已经准入的外部请求、已开始工具副作用、旧日志/备份/外部副本是明确保留项。

## 4. 人格、互动偏好、cue 与 Affect 机制

### 4.1 当前人格由什么构成

#### Persona 配置

companion/policy.py 定义三种有限预设 balanced、warm、concise 和六个轴：formality、warmth、humor、verbosity、expressiveness、curiosity（src/deskpal/companion/policy.py:8-17）。预设只是轴值组合，不是开放式角色卡、模型微调或自动学习的人格。

CompanionRuntime 的持久存储可保存启用开关、情绪模拟开关、当前 preset/style、persona version、reset_epoch 和 affect 值/anchor；生产 desktop factory 使用 companion.sqlite3（src/deskpal/companion/runtime.py:43-75、application/factory.py:162-169）。版本历史有上限 32（companion/constants.py:8-16；companion/repository.py:348-369）。

新状态默认 `enabled=false`、`emotions_enabled=false`、preset=`balanced`（src/deskpal/companion/repository.py:168-183）。因此人格表达机制与模拟情绪默认关闭；这不等于默认模型运行在本地。模型提供方选择是另一条配置路径，不能由这些 Companion 默认值推断。

configure 只接受已知 preset 和白名单 style key，轴值需在 0..1。切 preset/轴会创建新版本并清当前短期状态；rollback 读取指定历史值，但以新版本记录回退，不把 revision 倒退（companion/runtime.py:284-344）。GUI 在设置/人格调用 CAS 风格命令，明确保存配置（gui/persona_settings.py:179-201,267-330）。

另外有受约束身份字段如 display_name、role_kind、self_reference、greeting_style、followup_style，保存在 interaction config，并有 identity policy/version；不是任意 system prompt 文字字段（companion/repository.py:39-57）。自由文本角色卡未发现生产入口。

#### Interaction preferences 与用户 Profile

长期 Memory Profile 仅含姓名/回复语言/详略；Companion interaction preferences 支持有限 verbosity、humor_allowed、followup_allowed。scope 包括 once、session、today、persistent：once 只用于当前请求；session 在进程内；today 到配置时区次日午夜且最长 24h；persistent 写 companion 存储（companion/runtime.py:377-421、companion/interaction_runtime.py:90-118）。preference_sources/policy/span/revision用于追溯和用途约束；模型建议是 candidate，必须用户确认。

Companion 对云端资料类别采用明确默认拒绝：`custom_persona`、`preferences`、`cue`、`affect`、`history` 五项初始均为 false（src/deskpal/companion/models.py:9；companion/repository.py:39-41）。这控制的是对应 Companion 状态/资料及历史是否可进入云端表达请求，不是“整个云模型请求开关”：如果用户选择云模型并提交聊天，当前请求文本仍是该次模型调用的输入；仅关闭 persona/history 等类别不能把当前问题本身留在本地。Memory 又独立按 `cloud_allowed` / `local_only` 做用途过滤（memory/contracts.py:30-51、memory/retrieval.py:21-34）。因此“默认使用云模型”与“默认不上传可选人格、偏好、情绪和历史资料”是两项不同产品设置，应在 GUI 中分别解释、明确展示并取得用户选择，不能把二者混成一个隐含权限。

#### Cue 与 Affect

- **Cue** 是短时表达模式 neutral/supportive/focused。显式设置 TTL 限 1–1800 秒；默认进程内，随新 generation/重置/过期清除（companion/runtime.py:422-443；companion/interaction.py:33-73,92-132）。自然语言信号通过 signals.py 对 span、引用和范围进行结构校验，但这并不证明模型理解语义完全正确。
- **Affect** 是三个归一化值（valence、arousal、mood），有固定 preset baseline、半衰期和每窗口额度。示例半衰期为 300、120、7,200 秒，单窗口 delta 有上限（companion/constants.py:8-16）。
- values_at 按 baseline + (current - baseline) * 2^(-elapsed/half_life) 计算衰减（companion/reducer.py:12-16）。outcome_delta 对 completed/failed 产生很小的有界数值变化，并受窗口预算约束（:18-26）。
- 只接受满足时间、状态、配置、用途和重置代际条件的任务终态事件；task_id receipt 做去重；普通聊天、取消和不确定结果不更新该状态（companion/runtime.py:447-538）。状态持久化，但只是一种有限、可解释的模拟，不是对用户或角色真实心理的推断。
- Local 与 cloud affect/budget 隔离。云端许可按 category 和 source policy 控制，不能把 local-only 状态派生文本再间接带入云请求（companion/interaction_runtime.py:53-81,392-436；companion/repository.py:39-85）。

### 4.2 一次交互中人格如何生效

InteractionCoordinator.prepare 顺序冻结会话历史、当前记忆依赖、profile 和 companion 表达状态；若功能关闭或存储不可用，则没有表达 style 或退化到中性反馈（src/deskpal/application/interaction.py:97-220）。

规划与表达是分开的：

1. **Planner** 得到当前请求、合格的普通长期记忆，以及由 ConversationRuntime.freeze 生成的中立历史用户引用/工具事实。内置 planner 不收到 Persona、Affect、表达偏好或助手过去自由回复（planner/llm_planner.py:22-77；conversation/runtime.py:143-228）。
2. **ResponseComposer** 在 Reflect/表达阶段可收到 planner 草稿、允许的对话历史、允许的记忆/Profile、结构化工具结果、persona identity 与生成的有限风格说明（agent/core.py:661-705；response/composer.py:21-67,235-294）。
3. policy.instruction() 将轴值与 scope 转成有限中文风格指引和 [EXPRESSION_POLICY]，identity 以 typed [PERSONA_DATA] 传递；工具结果仍有确定性结构化摘要（companion/policy.py:39-121、response/composer.py）。
4. 表达 purpose 的 gateway 不提供工具调用；响应 schema/空正文/工具调用等有机械校验，但“温和”“简洁”“幽默”等表达质量本身没有确定性语义证明。
5. 终态成功提交后才发布会话轮次与 response signals；信号经过主机校验后变成可确认候选或短时状态，不由模型直接改 persona 数据库（agent/core.py:754-780、application/interaction.py:257-315）。

因此不同 preset 会改变后续准许表达请求中的有限 style 文本/参数，但无法保证模型一定写出不同或符合风格的句子。Persona、cue 和 affect 不改变已确认 Planner 计划或授权集合；相关集成测试对比了预设/cue 下的规划输入与权限（tests/integration/test_v08_companion_integration.py::test_t02_presets_cues_do_not_change_planner_or_same_task_plan_authority）。

### 4.3 能否长期演变：按 A–F 判断

| 类型 | 状态 | 代码事实 |
|---|---|---|
| A. 固定提示每轮重复使用 | **部分实现** | enabled 时为每次允许的表达调用从当前 persona/preferences/cue 重新生成有限 style instruction；不是单一静态 prompt。 |
| B. 用户改配置后表现随之变化 | **已实现** | preset/轴/identity 经明确 command、revision 与配置版本保存，之后的表达请求重新读取；模型遵循效果仍需实测。 |
| C. 当前上下文产生临时风格变化 | **已实现/部分实现** | cue 和本轮 request constraints 可影响当前表达；自然语义抽取只做有界候选或 cue，span 校验不等于语义准确。 |
| D. 模拟情绪被持久化并在后续使用 | **部分实现** | 有界 affect 数值在 companion SQLite 持久化、事件驱动并按半衰期回归 baseline；只有受约束工具任务结果触发，不是所有互动情绪。 |
| E. 经历证据主动改变长期人格倾向 | **未实现** | 没有累计证据后自动改 preset/style/identity 的生产路径；偏好候选须用户确认，不是长期人格演变。 |
| F. 审计/撤销自动人格变化 | **部分实现** | 人工配置有版本历史与回滚；没有自动 persona proposal/验真/采纳/撤销闭环。Affect reset 可清模拟状态。 |

## 5. 记忆与人格如何共同工作

| 问题 | 当前实现 |
|---|---|
| 人格影响记忆写入/筛选吗？ | 没有证据表明 preset 或 affect 决定 memory eligibility。Memory 依赖来源/状态/use policy；偏好候选的生成是在模型表达信号之后，但候选采纳仍经独立审核。 |
| 记忆影响人格吗？ | accepted Profile 的有限键可作为表达 preferences；普通 episodic/semantic evidence可供当次规划/表达；不存在把检索命中累积回写为人格轴或 affinity 的机制。 |
| 两者如何连接？ | ApplicationInteraction.prepare 汇总检索结果、Profile、会话投影、CompanionSnapshot 和来源 policy 到一次 PreparedTurn，并记录 planning/expression refs、privacy epoch、conversation epoch（application/interaction.py:97-220、agent/context.py:16-31）。它们没有合成一个可任意写 prompt 的共享数据库。 |
| 状态版本是否一致？ | Memory revision/epoch、Companion revision/reset_epoch、conversation context epoch、workflow privacy epoch 分别复核。一次请求准备后有 refs/digest 和 gateway guard；它不是所有库的全局原子事务。 |
| 人格会影响权限吗？ | 不会。操作授权留在 dispatcher/Core 的本次内存授权集，工具每步仍由 Task/Plan/hash、ToolManager/Sandbox 验证；表达模型没有 tools（application/dispatcher.py:85-127,311-340、agent/core.py:606-659、llm/calls.py:66-139）。相关测试为 test_t02_presets_cues_do_not_change_planner_or_same_task_plan_authority。 |
| 遗忘是否清人格？ | Forget 会清理依赖来源派生出的 interaction cue/preferences/候选和相关上下文；人工显式配置的 today/persistent preference 与独立 persona config 有各自边界。Persona reset 不删除 Memory。 |

### 两种历史不要混为一谈

- ConversationRepository 在 workflow store 保存已发布的对话 turn；ConversationRuntime.freeze() 可从该仓库重新加载历史，之后只取满足轮数、字节、来源数及 cloud policy 的片段。故不能说应用重启后“所有聊天上下文都消失”。
- 进程热缓存有上限；Companion InteractionState 中的 session cue/session preference 不是持久对话正文，关闭或重启不恢复。
- 任务 request/result 可能独立持久化以用于审查，但它们不会自动全部被转成新会话的聊天历史。归档/清理会话与清理记忆也是不同命令。

## 6. 八个真实场景案例

以下按源码与仓库已有测试检查。各测试只代表其具体断言；本报告任务没有重跑它们。

### 案例一：用户说“我喜欢少糖的奶茶”

- **初始条件：**用户以显式“记住：……”开头，或已启用 candidate_review。
- **调用与变化：**显式路径 AgentCore._run → MemoryCoordinator.remember 写 accepted Semantic user report；普通语句不会自动变长期记忆，除非候选学习模式有资格且生成 candidate。Profile extractor 当前仅映射三个白名单键，奶茶偏好通常不是可直接投影的 profile key。
- **下一次购买请求：**若记忆 accepted、global、有效且用途允许，词法/可选向量召回可能把它送给 Planner；最多 5 条且受 8,000-byte 默认预算约束。未召回时 Planner 看不到。
- **可观察结果/限制：**系统能存原陈述，但不承诺将“少糖奶茶”自动绑定到任意后续购买语义；也不自动执行采购或外部交易。
- **证据/测试：**实现 agent/core.py:525-590、memory/coordinator.py:176-188、memory/retrieval.py:264-390。tests/fixtures/memory-retrieval-zh.json 有合成中文样本，但不是这个用户故事端到端的实时测试；tests/unit/test_v03_learning.py::test_explicit_remember_retains_exact_source_without_evaluation 与 tests/unit/test_v03_retrieval.py 验证底层路径，不证明任意自然语言意图理解。
- **状态：**记忆写入/检索机制**已实现**；此具体偏好跨任务自动应用**部分实现/效果尚未验证**。

### 案例二：昨天的会话，今天新开应用继续问计划

- **初始条件：**同一个持久会话包含已发布轮次。
- **调用与变化：**会话 timeline 存在 workflow storage；新请求通过绑定的 conversation_id/epoch 使 ConversationRuntime.freeze() 从仓库加载并裁剪历史。Planner只拿中立用户引用/可信工具事实；Composer可在 history policy 允许时看到更丰富的有限历史。
- **重启后保留：**会话 timeline 与 Memory 数据持久；本进程 hot window、session-level cue/preferences、未发布表达草稿不自动重建。独立旧 Task 不会自动导入为聊天历史。
- **可观察结果/限制：**同一会话最近轮次可成为上下文；切换到另一会话不会带入其他会话正文。已接受长期 Memory 另由 query 检索，不是全量历史回放。
- **证据/测试：**conversation/runtime.py:56-105,143-228；conversation/repository.py；tests/integration/test_persistent_conversations.py::test_persistent_directory_timeline_and_bounded_context_survive_cache_expiry；tests/integration/test_humanization_application.py::test_hm02_multiturn_has_neutral_and_expression_projections_and_session_isolation。
- **状态：****已实现**（有界会话/隔离），并非无限历史对话理解。

### 案例三：切换人格 preset A 到 B

- **初始条件：**Companion 已启用且 GUI/CLI 提交合法 preset 与 expected revision。
- **调用与变化：**CompanionRuntime.command(configure) 校验枚举、在 SQLite transaction 中更新 persona style/版本、递增 revision/reset_epoch，并中和/清除 cue/affect generation。下一次 InteractionCoordinator.prepare 重新读取快照，policy.instruction() 派生不同表达指引。Planner输入不含该 preset。
- **可观察结果/限制：**配置状态一定有机会变化；模型生成内容是否明显不同、是否准确遵循风格取决于模型，不能由配置写入本身证明。
- **证据/测试：**companion/runtime.py:284-322、companion/policy.py:39-121、application/interaction.py:151-161；tests/unit/test_v08_companion_runtime.py::test_t01_rollback_creates_a_new_persisted_persona_version；集成 test_t02_presets_cues_do_not_change_planner_or_same_task_plan_authority。
- **状态：****已实现**配置链；语言效果**部分实现/须真实模型评审**。

### 案例四：对话后设置 5 分钟专注 cue

- **初始条件：**用户设置 focused，ttl_seconds=300，或自然表达被模型抽取并校验为短时 cue。
- **调用与变化：**Companion 命令仅允许 1–1800 秒；cue 属于当前 generation、对表达策略投影，过期时 snapshot/模式读取返回 neutral。切配置/重置可清 cue；session cue 不跨重启。
- **会话历史影响：**已发布对话文本可能仍在 session timeline 的有界历史中；即使 cue 对象过期，模型也可能从获准历史文字获得语气线索。因此“cue 已过期”不等于“之后模型完全不可能受先前语境影响”。
- **证据/测试：**companion/runtime.py:151-158,422-443、companion/interaction.py；tests/unit/test_v08_companion_runtime.py::test_t06_normal_offline_restart_matches_live_decay_without_restoring_cue。
- **状态：**cue 结构及 TTL **已实现**；30 分钟后真实模型输出行为**尚未由此测试充分验证**。

### 案例五：重复提交同一成功任务终态

- **初始条件：**相同 task_id 的 workflow effect 重投，或执行记忆重试。
- **调用与变化：**终态效果以 task_id 构造稳定 event ID 并由 workflow outbox 插入去重；ApplicationEffects 消费后记录 Companion receipt，Companion 按 task receipt 拒绝重复 affect delta；episodic tool memory 亦按 task_id 检查，ingest 另有幂等指纹。
- **可观察结果/限制：**数据库/跨库重复投递可发生，但期望只对 Companion 数值状态加一次效果。重试不是工具 exactly-once；已开始同步工具在崩溃时可能状态不明，恢复会要求审查而非盲目重放。
- **证据/测试：**workflow/store.py:296-304、application/effects.py:27-50、companion/runtime.py:447-538、memory/coordinator.py:197-214；tests/unit/test_v08_companion_runtime.py::test_t04_root_task_is_not_rewarded_again_after_reset_or_configure；tests/integration/test_humanization_application.py::test_hm04_outbox_cross_store_restart_is_idempotent。
- **状态：**状态增量/事件投递幂等**已实现并有测试**；任意工具外部效果 exactly-once **未实现**。

### 案例六：重置人格但保留记忆与任务

- **初始条件：**用户提交 persona 或 all reset。
- **调用与变化：**Companion Runtime 重置 preset/identity、情绪/偏好等其定义的 companion state，并增加新版本/reset_epoch；没有调用 MemoryRepository 清除，也没有删除 Workflow goal/task。
- **可观察结果/限制：**记忆、会话任务、独立目标按各自生命周期保留。reset all 指人格/companion 设置范围，不是“全应用恢复出厂”。
- **证据/测试：**companion/runtime.py:345-376；tests/integration/test_v08_companion_integration.py::test_t01_two_desktop_clients_conflict_rollback_new_version_and_reset_preserves_work。
- **状态：****已实现**，但名字“全部人格设置”必须与“清除所有用户数据”区分。

### 案例七：用户要求忘掉某段经历

- **初始条件：**用户从可用 Memory ID/revision 发起 forget。
- **调用与变化：**GUI/public facade 调 ApplicationService.request_forget，以稳定 command_id 持久写 deletion job、阻断隐私操作、递增 privacy_epoch、抛弃过期 outbox 并失效交互缓存。等待 worker 安全边界清理 Memory source 闭包、Workflow task/request/result/context 副本、来源派生候选、Companion来源和相关缓存；失败留 Pending，可重试。
- **可观察结果/限制：**立即表示 AcceptedPending 不等于物理清理已全部完成。遗忘闭包不覆盖 execution.jsonl、备份、导出/外部副本、已经发出的云请求和已开始工具效果。已开始工具的副作用不能回滚。
- **旧任务重新写入：**代码在学习/写入/发布前后检查 privacy/interaction boundary，旧 epoch 作废。HM05 对 planning、expression、started_tool 阶段有竞争测试；未找到单独卡住 memory_evaluation 的完整穷尽竞态测试，不能作形式化无竞态保证。
- **证据/测试：**application/privacy.py:55-70、application/service.py:505-536,717-787、agent/core.py:707-780；tests/integration/test_humanization_application.py::test_hm05_public_forget_orders_inflight_send_started_and_late_publication、test_hm05_cleanup_failure_stays_pending_and_restart_retries_public_forget。另有 tests/integration/test_v08_companion_integration.py::test_t07_privacy_reset_failure_keeps_deletion_pending_until_successful_retry。
- **状态：**持久阻断、依赖清理和重试**已实现并有指定路径测试**；日志/备份/外部效果全清与所有微观时序穷尽**未实现/尚未验证**。

### 案例八：多年互动后人格自动成熟/变化

- **初始条件：**用户多次表达同一风格偏好或评价回复。
- **调用与变化：**只有 narrow signals 可产生有来源的偏好 candidate；memory profile 的可持久 key 很有限；Companion 的候选通过明确确认进入一次配置。没有生产任务定期累计互动，判断“人格应成熟”，再自动修改 style/preset/identity 的链路。
- **可观察结果/限制：**用户明确配置会长期改变风格；短时 cue 或 affect 可改变有限表达；Memory 可以保留一部分偏好并影响表达；但这三者不是经历驱动的人格演变。
- **证据/测试：**application/interaction.py:257-315、memory/coordinator.py:291-344、companion/interaction_runtime.py:121-195。规划也明示不做自动人格漂移，但本结论来自代码不存在自动更新路径，规划仅作旁证。
- **状态：**长期人格演变**未实现**。

## 7. 能力与限制矩阵

| 领域 | 状态 | 实际机制与证据 | 已有测试证据（本次未运行） |
|---|---|---|---|
| 短期对话上下文 | **已实现/有界** | 持久 conversation timeline + bounded projection；recent rounds 有数量/字节上限。conversation/runtime.py:143-228 | tests/integration/test_persistent_conversations.py；test_hm02_multiturn_has_neutral_and_expression_projections_and_session_isolation |
| Task 历史恢复成对话 | **未实现** | 明确拒绝从旧 Task fabricated history；查看任务卡与聊天历史分开。conversation/repository.py module contract | tests/integration/test_persistent_conversations.py::test_schema_two_backup_default_protection_and_no_fabricated_history |
| 长期记忆写入 | **已实现（有限入口）** | explicit remember→accepted；tool execution→episodic；可选 candidate-review | test_v03_learning.py、test_agent_flow.py |
| 自动聊天学习 | **部分实现/默认关闭** | candidate_review+evaluator+云用途才能产生待审核候选；普通聊天不默认写入 | test_learning_is_opt_in、test_local_only_never_reaches_evaluator |
| Profile | **部分实现** | 三个 profile key、来源/状态/用途限制 | tests/unit/test_v03_retrieval.py、tests/gui/test_v08_companion_gui.py |
| Memory retrieval | **已实现，但准确率有限** | accepted qualification、词法+可选 embedding 融合、rank、时间与字节预算、revision复核 | tests/unit/test_v03_retrieval.py |
| 五种 MemoryKind 用户功能 | **未实现为五套功能** | Enum/兼容 schema 存在；当前实际写入 semantic/episodic，goal 独立，working 有界上下文另实现，relationship profile 不完整 | tests/unit/test_memory.py::test_all_memory_fields_and_kinds_round_trip_with_generated_uuid 只证明字段往返 |
| 长期自动总结 | **未实现** | 未发现 production consolidator/summary dependency DAG | 无对应实现证据 |
| 更正/审核/冲突 | **已实现（边界明确）** | candidate accept/reject、CAS、revision history、profile 冲突显式更正 | tests/unit/test_v03_repository.py、tests/unit/test_v03_learning.py |
| 遗忘 | **部分实现（逻辑清理和隐私屏障）** | 来源闭包、索引/画像/上下文清理、privacy epoch、pending retry；不覆盖日志/备份/云端/外部效果 | tests/integration/test_v03_privacy_lifecycle.py、HM05 |
| 固定 Persona | **已实现（有限参数）** | 3 presets + 6轴、验证、CAS revision、最多32历史版本、回滚新版本 | tests/unit/test_v08_companion_runtime.py |
| 人格身份描述 | **部分实现** | 受控枚举/字段；无任意角色卡/LLM自由写身份 | tests/gui/test_v08_companion_gui.py |
| Interaction preferences | **已实现（有限 scope）** | once/session/today/persistent；候选确认；来源/用途控制 | tests/integration/test_humanization_application.py |
| 临时 Cue | **已实现（进程内与有界）** | mode+expiry+generation；TTL 1..1800s；重启不复原 session cue | tests/unit/test_v08_companion_runtime.py::test_t06_normal_offline_restart_matches_live_decay_without_restoring_cue |
| 模拟 Affect | **部分实现** | 工具终态驱动小幅变化、预算、半衰期、SQLite持久化、重置；不由普通聊天更新 | T04/T05/T06 runtime cases；test_t02 integration |
| 自然时间衰减 | **已实现（数学状态投影）** | 读取时按 half-life 计算，不是定时写库或删除 | tests/unit/test_v08_companion_runtime.py |
| 长期人格演变 | **未实现** | 无多事件证据汇总后改 persona 的生产调用链 | 无对应自动演变测试 |
| 人格一致性验证 | **部分实现** | 参数/schema/来源用途校验；风格文本没有模型语义保证器 | HM08报告记录真实体验差异 |
| 重置/回滚 | **已实现（companion范围）** | persona版本回滚、affect/persona/all reset；不重置 memory/task | test_t01_rollback_creates_a_new_persisted_persona_version、test_t01_two_desktop_clients_conflict_rollback_new_version_and_reset_preserves_work |
| 隐私用途 | **已实现（边界化）** | planner/expression/evaluation purpose + category/source use policy gate、请求前后 epoch 验证 | test_t02、privacy integration tests |
| 并发/恢复 | **部分到已实现** | 单 worker、command id、revision/CAS、outbox、started-step不盲重放；不是跨任意数据库全局事务 | HM04/HM05与 workflow tests |
| 执行授权 | **隔离已实现** | persona/profile/affect不授工具权限；每步仍需当前操作授权和Sandbox检查 | test_t02_presets_cues_do_not_change_planner_or_same_task_plan_authority |

## 8. 当前实现的关键限制与待验证项

| 项目 | 条件与用户影响 | 现有保护 | 剩余范围/应补证据 |
|---|---|---|---|
| 记忆漏召回或误召回 | 中文词法/向量阈值/语义相似度与 Top-K、预算共同影响结果；相关记忆未必进入 Planner | 资格过滤、synthetic calibration、revision复核、完整项预算 | 检索样本不等于私人真实语料；饮品故事没有自然意图到正确语义绑定的端到端证明。补真实同意数据的离线评测和用户可理解的“为何召回”。 |
| Profile 范围窄 | 仅姓名、语言、详略的投影被确定性识别 | 白名单、冲突不猜测、来源和 policy | 不能声称饮食、家庭、长期目标已形成结构化用户画像。 |
| 模型对 Persona 不必然服从 | style 影响表达请求，但模型可误解温度/细节；schema只检查格式、不证明语义 | prompt data隔离、纯表达无工具、模板提供结构化工具事实 | HM08历史报告已记录真实模型偏差/延迟；不能把离线确定性校验扩成自然语言验真。 |
| 模拟 affect 不等同真实情绪 | 仅工具 task terminal outcomes 触发有限数值 | 状态规则有上限、半衰期、唯一receipt、reset | 不代表识别用户情绪；没有普通聊天/长期相处的完整心理状态估计。 |
| Cue 到期后的语境仍可能存在 | cue对象过期，但有许可的聊天历史可能仍包含该表达方式 | 历史受上限、policy和privacy epoch约束 | cue过期不保证模型忘掉会话文本；若产品要求“只在这次回复有效”，需单独定义对话历史作用边界与测试。 |
| Memory/Companion 多库一致性 | 记忆、工作流、Companion分别用SQLite与事务 | purpose refs、epoch、outbox、安全边界、待重试 deletion job | 没有任意跨库全局事务；报告/回执应分别显示pending状态，失败不应谎称全部清除。 |
| Forget 的绝对范围 | 用户要求遗忘时，服务阻断未来读取和新副作用，但已开始请求/工具不可撤销，日志/备份可能残留 | privacy epoch、队列阻断、清理闭包、重试 | HM05覆盖 planning/expression/started_tool竞态；未找到独立卡住 memory_evaluation 的专用穷尽竞态用例，不能断言所有时序均验证。 |
| 人格 reset 与记忆清理 | 两个生命周期刻意分离 | Companion reset 不调用 MemoryRepository；记忆 forget 另有 privacy API | GUI字面“全部”容易被误解为全应用清理，产品文案应继续标明范围并独立验收。 |
| 任务副作用 exactly-once | 进程在工具已开始但结果未提交时崩溃 | journal标记 uncertain/needs review，不盲目重试 | 外部效果可能已发生；不能用幂等 task receipt 声称外部世界只发生一次。 |
| 本报告没有运行测试 | 静态审计不能证明当前工作区当下行为通过 | 仓库存在相关测试和历史版本测试报告 | 本报告没有启动应用或重跑测试；测试列表是可执行证据索引，不是本轮通过声明。 |

## 9. 面向未来的开发建议

这些是基于当前缺口的建议，不是本次创建的开发任务。

### A. 完善现有能力

1. **建立真实语料边界清晰的 Memory retrieval 评估集。** 现有校准/held-out 证据是有限合成集。复用 MemoryRetriever，增加多轮中文同义、否定、时间有效期、相近人名和隐私 policy 用例；按检索漏项/误召回、完整证据预算报告，而非只测 fixture recall。
2. **把“候选/已接受/冲突/遗忘进行中”及来源解释做成面向用户的解释。** 复用 MemoryItem/source refs/revision/CAS，让用户看到为何被记住、为何可用于本次上下文、哪些地方仍待清理；不暴露内部 JSON 作为主视图。
3. **对 Companion 的真实模型表现设置单独验收门槛。** 复用有限 style policy/schema、按 warming/concise、语言、cue过期、历史许可和模型错误设计评审集；格式通过与风格/事实通过分开记录。
4. **补充 forget race 的卡点覆盖。** 复用 privacy epoch、task-bound client guard 和 public request_forget，显式暂停 memory_evaluation、候选入库和最终发布阶段，在每个卡点提交 forget 后确认无新写入/外发，并验证 Pending/retry 回执。

### B. 当前缺失的能力

1. **长期偏好到任意语义 Profile 的谨慎映射。** 当前 Profile 只有三个 key。若增加饮食/计划类 Profile，应先设计类型、有效期、来源、scope、冲突与用户审核；可复用 MemorySource/Draft、候选审批、CAS和 Retriever，不应直接加自由文本画像。
2. **跨事件摘要及可失效依赖图。** 当前没有自动 summary consolidation。新增前需构建明确输入事件、引用revision、推导边、冲突保留、删除传递失效和预算策略；可复用来源 revision 与删除闭包，但还需独立 schema/服务/测试。
3. **长期人格演变的受控提议流程。** 不能把 affect delta 或“用户多夸了几次”直接改 persona。最低需：明确长期证据、用户可审核 proposal、核心人格不变量、变更版本、可回滚、最小化更新频率、隐私用途及 Planner/tool 权限隔离。可复用 PersonaVersion/CAS/rollback 与 profile候选，但没有当前的自动 policy evaluator/提议仓库。
4. **主动行为。** 当前主动建议是独立目标/提醒与本地规则域；没有“基于情绪/人格随时主动聊天”的专用 agent。若扩展，应复用持久 Goal/Proposal/outbox/通知去重与授权分离，新增可关闭触发器、预算、静默时间、去重/取消、用户明确控制；不能由 cue 或风格配置默默开启。

### C. 暂时不应增加的复杂度

1. **不要先做持续后台反思/自动改人格。** 当前模型自然语言风格本身尚不保证稳定；应先强化候选审核、版本回退、真实模型验收和偏差监控。
2. **不要把所有聊天都无限保存并直接注入每轮。** 现有 bounded timeline、Memory来源机制和外发过滤更可控；需要摘要时先引入有证据依赖和删除闭包。
3. **不要把五个 MemoryKind 做成五个自由写入槽。** WorkingContext、Goal权威对象、Relationship互动约定应由各自生命周期服务所有；enum存在不代表应把业务状态重复写入 memory 表。
4. **不要把模拟 Affect 解释成真实心理状态。** 当前值来自工具任务结果和规则；在没有安全、体验和效用证据前，不应据此做用户画像、主动联系或权限升级。

## 10. 关键代码与测试索引

### 10.1 代码索引

| 主题 | 主要路径与位置 |
|---|---|
| 旧 MemoryKind、兼容 MemoryRecord | src/deskpal/memory/models.py:6-24；memory/manager.py:86-203 |
| 来源/草稿/状态/用途/结果 DTO | src/deskpal/memory/contracts.py:30-145 |
| SQLite source/revision/CAS/migration/delete closure | src/deskpal/memory/repository.py:155-165,246-311,459-716 |
| 显式记忆/工具执行/自动学习/profile/审核 | src/deskpal/memory/coordinator.py:155-214,216-344,348-459 |
| 资格过滤/profile/rank/预算/版本复核 | src/deskpal/memory/retrieval.py:21-77,175-390 |
| 显式 remember、retrieve、Planner、tool result 与交互发布 | src/deskpal/agent/core.py:525-590,606-780 |
| 会话 runtime/freeze 的 bounded context | src/deskpal/conversation/runtime.py:16-24,56-228 |
| 持久对话目录/消息 | src/deskpal/conversation/repository.py |
| App 初始化和文件归属 | src/deskpal/application/factory.py:65-195 |
| 两路 planning/expression projection、policy、epoch 检查 | src/deskpal/application/interaction.py:66-247 |
| Persona presets/instruction shaping | src/deskpal/companion/policy.py:8-121 |
| Affect衰减与event delta公式 | src/deskpal/companion/reducer.py:12-26 |
| Companion数据库生命周期/配置、版本、reset、cue、事件 receipt | src/deskpal/companion/runtime.py:43-210,284-443,447-565 |
| cue、session、preferences与候选 | src/deskpal/companion/interaction.py、companion/interaction_runtime.py:90-350 |
| User signal 候选校验 | src/deskpal/companion/signals.py:136-285 |
| 回复表达约束/有限响应检查 | src/deskpal/response/composer.py:21-67,235-294 |
| 遗忘隐私屏障与重试 | src/deskpal/application/privacy.py:55-70、application/service.py:505-536,717-787 |
| 执行授权与 effect 去重 | src/deskpal/application/dispatcher.py:85-127,311-340、workflow/store.py:296-304、application/effects.py:27-50 |

### 10.2 测试索引（只列存在的证据；本轮未运行）

| 文件 | 相关测试/主题 |
|---|---|
| tests/unit/test_memory.py | test_all_memory_fields_and_kinds_round_trip_with_generated_uuid、旧 last-access 行为；只证明 legacy DTO/storage 行为。 |
| tests/unit/test_v03_repository.py | v1 legacy migration、candidate TTL、source closure delete、CAS原子性、forget rollback。 |
| tests/unit/test_v03_learning.py | explicit remember 不评估、learning opt-in、候选不默认接受、local_only 禁止送 evaluator、execution idempotence。 |
| tests/unit/test_v03_retrieval.py | eligibility/隐私过滤、importance不能压过相关性、完整 UTF-8 预算、并发 mutation fail closed。 |
| tests/integration/test_agent_flow.py | test_plain_chat_not_saved_and_explicit_fact_survives_factory_close。 |
| tests/integration/test_persistent_conversations.py | 持久时间线、缓存失效后 bounded history、旧 Task 不伪造聊天历史及删除/导航语义。 |
| tests/integration/test_v03_privacy_lifecycle.py | 过期发生于 planning boundary 时阻止 Planner；TTL 后 forget 清理原 Task payload。 |
| tests/integration/test_v08_companion_integration.py | test_t01_two_desktop_clients_conflict_rollback_new_version_and_reset_preserves_work；test_t02_presets_cues_do_not_change_planner_or_same_task_plan_authority；test_hm05_public_forget_orders_inflight_send_started_and_late_publication；test_hm05_privacy_reset_failure_keeps_deletion_pending_until_successful_retry。 |
| tests/unit/test_v08_companion_runtime.py | T01 版本/CAS回滚，T04–T06 affect/dedup/decay/restart cue边界。 |
| tests/integration/test_humanization_application.py | test_hm02_multiturn_has_neutral_and_expression_projections_and_session_isolation；test_hm04_outbox_cross_store_restart_is_idempotent。 |
| tests/integration/test_humanization_migration.py | humanization schema/migration 验收。 |
| tests/gui/test_v08_companion_gui.py | Persona、偏好和 companion GUI 命令映射。 |
| tests/gui/test_humanization_conversation_gui.py | 对话上下文/UI工作流相关断言。 |
| tests/gui/test_management_regressions.py | Memory正文、元数据与原始记录分离展示。 |
| tests/test_cli.py | test_memory_cli_local_remember_profile_cas_correct_export_and_forget。 |

版本测试记录应单独阅读：docs/versions/v0.3/test-report.md 是记忆子系统版本证据；docs/versions/v0.8/humanization-test-report.md 与 hm08-deferred-issues.md 记录拟人化功能的有限通过与真实模型待解决项。它们不是本次工作区的复测报告，也不能证明未覆盖自然语言场景。
