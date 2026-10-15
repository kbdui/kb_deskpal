# V0.3 开发计划

状态更新：**0.3.0 已实现并通过约定范围验收**（2026-10-15）。实际接口见 [V0.3 契约](../../versions/v0.3/interfaces.md)，交付范围见[交付说明](../../versions/v0.3/delivery.md)，证据见[测试报告](../../versions/v0.3/test-report.md)。本文后续章节保留实施前计划，不再表示当前源码仍未实现。

## 计划到 V0.3 的实际映射

| 计划项 | 当前实际范围 |
| --- | --- |
| 接口/来源/状态冻结 | contracts.py 已实现来源、断言、revision、claim type、use policy、bundle/receipt；最终签名以版本契约为准 |
| 持久化 | schema v1 验证备份后迁移 v2；legacy 隔离；CAS、默认七天候选 TTL、共享来源闭包删除与 tombstone |
| 评估 | explicit_only 默认；candidate_review 单根 task 一次独立无工具评估、严格 JSON/quote/分数校验；失败只降级，不自动接受 |
| 用户模型 | 仅 preferred_name、reply_language、verbosity；global/local owner 明确已确认事实投影，不是完整关系/人格画像 |
| 更正/时间 | 普通更正 superseded，temporal 更正保留旧有效区间，新 ID 替代；历史 as_of 检索与索引资格过滤 |
| 检索 | 词法+真实 FastEmbed CPU 语义、完整证据预算、无 recent fallback、local_only 云过滤、revision/epoch 与 Waiting 授权失效 |
| 运维/入口 | CLI 离线无 key 管理、export/rebuild-index、关联 Task 擦除、资源所有权和辅助失败 warning |
| 模型/效果 | Python 3.12 真实模型、独立校准阈值 0.55、held-out Recall@5=0.8375/无关误召回=0.10；5,000条warm p95=1.7971s、增量峰值297.4MiB，详见报告 |
| 真实 API | 本轮三次有限预算调用，最终两次完整候选审核/更正/遗忘与 echo/Reflect 流程通过；脱敏证据见报告 |
| 未交付 | 多轮 WorkingContext、跨事件派生总结/依赖 DAG、完整隐身、importer、Goal/Proposal、人格/情绪/UI |

后续原计划中的 MemoryCandidate/UserProfile 等拟议名称不是额外可调用类型；实际使用 MemoryDraft/MemoryItem/ProfileEntry。旧 SQLiteMemoryManager 仅作 schema v1 兼容类与迁移 fixture，应用由新 Coordinator 提供旧 save/search/delete 适配，禁止覆盖已有记录。

## 原始规划快照（2026-10-14）

状态：**建议范围与顺序，尚未实施或冻结 API**。更新日期：2026-10-14。根据 [分阶段路线](../roadmap.md)，本版收窄为长期记忆、用户模型与真实语义检索；2026-10-13 草案中的目标系统和主动提议移至 V0.4，原始能力目标保留。环境准备见 [环境评估](environment-assessment.md)，验收规则见 [验收边界](acceptance-boundaries.md)。

## 现状与目标

| 当前 V0.2 | V0.3 需要补齐 |
| --- | --- |
| MemoryRecord 有五类 kind 与 importance/confidence/decay_rate | 字段有实际评估/衰减语义，来源证据、候选/确认、矛盾更正及失效规则 |
| Core 仅按“记住”语法保存事实；工具摘要截取前 2000 字符 | 独立 MemoryEvaluator、结构完整的有界摘要、明确事实与模型推测的区别 |
| 字面包含搜索，找不到就取最近五条 | 只检索可用记忆，相关性排序、上下文预算；语义索引可替换且可重建 |
| RELATIONSHIP 只是 enum，无可查询用户模型 | 从已确认事实生成 UserProfile；不等于人格/情绪系统 |
| GOAL enum 与持久 Scheduling 事件 | 为 V0.4 留接口边界；本版不交付 GoalManager、主动服务或任务恢复 |

## 架构与契约方向

机制细化见[记忆设计](../companion-model/memory.md)与[集成验收](../companion-model/integration-and-acceptance.md)：明确 accepted 不等于客观证实、显式记忆独立入口、来源版本/派生失效、外发用途及 Waiting 计划依赖；仍属待冻结提案。

保留 Application → Core → Planner/Task/ToolManager → Sandbox → Tools。Core 通过接口访问记忆检索/评估，不 import embedding SDK、目标 SQLite 或具体通知服务。Application 组装、串行调度并关闭新增服务。

- `MemoryEvaluator.evaluate(context) -> candidates`：独立请求、不提供工具；输入限定来源与预算，输出严格校验的 MemoryCandidate，不直接更新用户事实。
- `MemoryRepository`：候选/确认/更正/删除及迁移；SQLite 为事实来源。保留旧 MemoryManager save/search/delete 兼容层，避免旧调用各自造一套存储。
- `MemoryRetriever` / `EmbeddingProvider`：过滤状态、检索及有界上下文；索引记录模型、维度、内容版本，失效可重建。基础 SQLite 内的小规模向量索引不要求独立数据库服务。
- `UserProfile`：以已确认、未被替代的事实为投影；含来源与更新时间，用户可更正/删除。偏好推断先候选，不将分数当真实性证明。

GoalManager 与 ProactiveController 留待 V0.4 定义；记忆中的 goal/relationship 类别不自动变成目标管理或人格系统。

上述名称是拟议 Protocol/DTO，不代表现有可调用 API；开工第一阶段需冻结参数、错误、资源所有权和兼容策略。

## 实施顺序与子 Agent 所有权

| 阶段 | 主要负责人 | 工作及门槛 |
| --- | --- | --- |
| 0：冻结接口 | 主 Agent + 架构 Agent | 定义来源/候选/确认状态、画像、检索 DTO；冻结迁移和评估失败语义 |
| 1：持久化 | Memory Agent | schema version 迁移、legacy 标识、来源/证据/版本与 supersedes、更正/删除；迁移/故障测试通过 |
| 2：评估与画像 | Evaluator Agent | 独立 JSON 评估，候选制、明确记忆/更正确认、用户模型投影、结构化摘要；拒绝幻觉与无证据候选 |
| 3：检索与索引 | Retrieval Agent | 可用记忆过滤与词法排序；完成选定 CPU 模型真实语义检索、索引失效/重建及预算验证后再宣称语义能力 |
| 4：整合验收 | 主 Agent + Testing Agent | Application 资源所有权、CLI 记忆/画像管理入口、旧功能回归、真实评估与 embedding 独立验收，形成实际报告 |

按最多四个并发席位调度；依赖契约未冻结时只做只读评估，不并行修改相同文件。主 Agent 审查共享 DTO、Core、工厂、迁移、权限及最终文档；子 Agent 返回文件/设计/接口/测试/问题，Git 仍由用户处理。

## 数据、评估与检索规则

首选检索基线为 BAAI/bge-small-zh-v1.5 + FastEmbed CPU；安装/模型选择见 [Embedding 指南](../../guides/embedding-setup.md)。具体权重快照、前缀策略、分块和已解析依赖仍须实测冻结，模型支持声明不等于本机通过。

来源必须有可持久定位的 source_id、类型、有限证据摘录和时间；Task 本身是进程内对象，不能只保存 task_id 后就声称重启可追溯。旧库保留全部原记录并标为 legacy，不把旧默认 confidence=1 自动升级为已确认用户事实。

显式“记住/更正”可以确认；普通聊天的模型抽取先产生候选。工具输出、文档内容、assistant 推测不可自动变成用户自述。冲突展示给用户，确认后旧事实标为 superseded，不再进检索上下文；删除同时清理画像与索引，历史日志是否保留需明确，不承诺删除数据库即擦除已有日志。

importance/confidence 的范围、来源和用途要分开；模型 confidence 不是事实真值。衰减先降低排序或进入待清理列表，默认不永久删除。查询访问更新时间不能无条件让每次选中的低价值记忆永不过期。用户更正优先于推断，不能靠更高相似度恢复旧事实。

可选自动评估失败时允许原工具任务完成，并明确返回评估降级告警，避免用户误重试已产生副作用的操作；显式记忆请求存储失败必须失败。两种失败语义与预算需进入新 DTO/测试，当前 Core 尚未提供该分层。

语义检索作为推荐独立里程碑：SQLite 保存事实和小规模索引，检索同时检查 accepted/current、内容版本与模型版本；不同维度/模型不得混排。模型不可用时显式降级到词法，不发送无关“最近五条”填满上下文。全版语义验收需真实中文数据和真实模型，未完成时只能交付已验收的基础记忆阶段。

## V0.4 交接约束（不作为本版验收）

推荐默认：主动行为关闭、后台云 LLM 调用关闭、每次提议数量/调用预算有限。启用本地事件提议后，不采屏、不读任意文件、不自动发消息或沿用旧授权。后台只产出提议；用户在前台接受后进入 Core 正常规划和 Waiting 精确确认，提议接受与工具授权分开。

事件游标推进与 Proposal 入库需在同一拥有事务中提交；跨 Scheduling 库读取以稳定事件 ID 和唯一去重键桥接，不宣称跨 SQLite 库全局原子事务。目标取消、版本变化或提议过期后不得再执行。

Core 忙时排队，关闭先停止新入队/主动轮询，再等活动请求结束或明确报告无法停止，最后释放依赖。不得在 Core 仍用记忆时关闭连接。建议接受、创建 Task、执行工具不是持久化原子操作；崩溃后的 dispatching/执行未知状态应标为需要人工复核，不自动重放有副作用的请求。旧 Waiting 缓存重启丢失后必须重新规划并再次确认，不能假装 resume。

## 推荐决策与范围

本版采用自动抽取候选制和 CPU 语义索引独立验收；目标/提议/任务恢复在 V0.4，桌面交互在 V0.5，隔离与窄授权自主在 V0.6，Live2D 在 V0.7–V0.8。具体模型快照、迁移路径与基准开工时冻结。尚未完成真实语义验收时可交付基础记忆子阶段，但不能宣称完整 V0.3 通过。
