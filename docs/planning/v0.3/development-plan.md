# V0.3 开发计划

状态：**建议范围与顺序，尚未实施或冻结 API**。日期：2026-10-13。原路线要求长期记忆、用户模型、目标系统和主动行为；本方案把主动行为限定为可查看/接受的提议，不默认执行外部副作用。环境准备见 [环境评估](environment-assessment.md)，验收规则见 [验收边界](acceptance-boundaries.md)。

## 现状与目标

| 当前 V0.2 | V0.3 需要补齐 |
| --- | --- |
| MemoryRecord 有五类 kind 与 importance/confidence/decay_rate | 字段有实际评估/衰减语义，来源证据、候选/确认、矛盾更正及失效规则 |
| Core 仅按“记住”语法保存事实；工具摘要截取前 2000 字符 | 独立 MemoryEvaluator、结构完整的有界摘要、明确事实与模型推测的区别 |
| 字面包含搜索，找不到就取最近五条 | 只检索可用记忆，相关性排序、上下文预算；语义索引可替换且可重建 |
| GOAL/RELATIONSHIP 只是 enum | 持久 GoalManager、从已确认事实生成的 UserProfile；不等于人格/情绪系统 |
| Scheduling 产生到期事件，Core 一次只处理一个请求 | 独立 ProactiveController/提议队列；前台串行接入 Core，不在调度线程直接 run |

## 架构与契约方向

保留 Application → Core → Planner/Task/ToolManager → Sandbox → Tools。Core 通过接口访问记忆检索/评估，不 import embedding SDK、目标 SQLite 或具体通知服务。Application 组装、串行调度并关闭新增服务。

- `MemoryEvaluator.evaluate(context) -> candidates`：独立请求、不提供工具；输入限定来源与预算，输出严格校验的 MemoryCandidate，不直接更新用户事实。
- `MemoryRepository`：候选/确认/更正/删除及迁移；SQLite 为事实来源。保留旧 MemoryManager save/search/delete 兼容层，避免旧调用各自造一套存储。
- `MemoryRetriever` / `EmbeddingProvider`：过滤状态、检索及有界上下文；索引记录模型、维度、内容版本，失效可重建。基础 SQLite 内的小规模向量索引不要求独立数据库服务。
- `UserProfile`：以已确认、未被替代的事实为投影；含来源与更新时间，用户可更正/删除。偏好推断先候选，不将分数当真实性证明。
- `GoalManager`：独立持久目标、里程碑、截止时间和任务关联；状态建议 draft/active/paused/completed/cancelled，与 TaskStatus 分离。
- `ProactiveController`：有开关、预算、冷却/安静时段，消费事件生成持久 Proposal；支持 list/accept/dismiss/expiry。接受建议不等于授权具体工具。

上述名称是拟议 Protocol/DTO，不代表现有可调用 API；开工第一阶段需冻结参数、错误、资源所有权和兼容策略。

## 实施顺序与子 Agent 所有权

| 阶段 | 主要负责人 | 工作及门槛 |
| --- | --- | --- |
| 0：冻结接口 | 主 Agent + 架构 Agent | 定义来源/候选状态、Goal 状态、提议状态与授权关系；冻结迁移和失败语义 |
| 1：持久化 | Memory Agent | schema version 迁移、legacy 标识、来源/证据/版本与 supersedes、更正/删除；迁移/故障测试通过 |
| 2：评估与画像 | Evaluator Agent | 独立 JSON 评估，候选制、明确记忆/更正确认、用户模型投影、结构化摘要；拒绝幻觉与无证据候选 |
| 3：检索与索引 | Retrieval Agent | 可用记忆过滤与词法排序；完成选定 CPU 模型真实语义检索、索引失效/重建及预算验证后再宣称语义能力 |
| 4：目标系统 | Goal Agent | CRUD、里程碑/状态、任务关联、授权和持久化；Task 完成不自动宣布 Goal 完成 |
| 5：主动提议 | Proactive Agent | 复用持久事件、唯一键与游标事务、默认关闭、过期/取消失效、accept/dismiss；不自动执行工具 |
| 6：整合验收 | 主 Agent + Testing Agent | Application 生命周期/串行请求、CLI 管理入口、旧功能回归、真实模型与 embedding 独立验收，形成实际报告 |

按最多四个并发席位调度；依赖契约未冻结时只做只读评估，不并行修改相同文件。主 Agent 审查共享 DTO、Core、工厂、迁移、权限及最终文档；子 Agent 返回文件/设计/接口/测试/问题，Git 仍由用户处理。

## 数据、评估与检索规则

来源必须有可持久定位的 source_id、类型、有限证据摘录和时间；Task 本身是进程内对象，不能只保存 task_id 后就声称重启可追溯。旧库保留全部原记录并标为 legacy，不把旧默认 confidence=1 自动升级为已确认用户事实。

显式“记住/更正”可以确认；普通聊天的模型抽取先产生候选。工具输出、文档内容、assistant 推测不可自动变成用户自述。冲突展示给用户，确认后旧事实标为 superseded，不再进检索上下文；删除同时清理画像与索引，历史日志是否保留需明确，不承诺删除数据库即擦除已有日志。

importance/confidence 的范围、来源和用途要分开；模型 confidence 不是事实真值。衰减先降低排序或进入待清理列表，默认不永久删除。查询访问更新时间不能无条件让每次选中的低价值记忆永不过期。用户更正优先于推断，不能靠更高相似度恢复旧事实。

可选自动评估失败时允许原工具任务完成，并明确返回评估降级告警，避免用户误重试已产生副作用的操作；显式记忆请求存储失败必须失败。两种失败语义与预算需进入新 DTO/测试，当前 Core 尚未提供该分层。

语义检索作为推荐独立里程碑：SQLite 保存事实和小规模索引，检索同时检查 accepted/current、内容版本与模型版本；不同维度/模型不得混排。模型不可用时显式降级到词法，不发送无关“最近五条”填满上下文。全版语义验收需真实中文数据和真实模型，未完成时只能交付已验收的基础记忆阶段。

## 主动行为与故障恢复

推荐默认：主动行为关闭、后台云 LLM 调用关闭、每次提议数量/调用预算有限。启用本地事件提议后，不采屏、不读任意文件、不自动发消息或沿用旧授权。后台只产出提议；用户在前台接受后进入 Core 正常规划和 Waiting 精确确认，提议接受与工具授权分开。

事件游标推进与 Proposal 入库需在同一拥有事务中提交；跨 Scheduling 库读取以稳定事件 ID 和唯一去重键桥接，不宣称跨 SQLite 库全局原子事务。目标取消、版本变化或提议过期后不得再执行。

Core 忙时排队，关闭先停止新入队/主动轮询，再等活动请求结束或明确报告无法停止，最后释放依赖。不得在 Core 仍用记忆时关闭连接。建议接受、创建 Task、执行工具不是持久化原子操作；崩溃后的 dispatching/执行未知状态应标为需要人工复核，不自动重放有副作用的请求。旧 Waiting 缓存重启丢失后必须重新规划并再次确认，不能假装 resume。

## 推荐决策与范围

采用“自动抽取候选制、后台云调用关闭、目标完成由用户或明确证据确认、CPU 语义索引单独验收”。这是本次评估建议，实施时再冻结具体模型、版本、迁移路径和验收数据集。Qt/Live2D、系统弹窗、循环自主工具执行、完整 OS 沙箱、推理模型 native 续接保持独立里程碑，不将它们悄悄并入本版。
