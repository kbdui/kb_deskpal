# 模块与核心代码说明

本文是代码阅读索引，具体调用顺序见[请求执行](request-lifecycle.md)，文件树见[仓库导航](repository-map.md)。

## Application 与工作流

| 文件/对象 | 作用与阅读重点 |
| --- | --- |
| [application/factory.py](../../src/deskpal/application/factory.py) | `create_application/create_agent/_create_core`，依赖注入和失败资源回收 |
| [application/service.py](../../src/deskpal/application/service.py) | `ApplicationService`，提交、确认、取消、恢复、来源 guard、清理和关闭总入口 |
| [application/dispatcher.py](../../src/deskpal/application/dispatcher.py) | `SubmissionDispatcher`，唯一 Core 执行 worker、有界提交、等待、故障与缓存管理 |
| [application/desktop.py](../../src/deskpal/application/desktop.py) | `DesktopFacade.execute(operation, parameters)`，Qt-free 的 DTO 查询/命令边界 |
| [application/cli.py](../../src/deskpal/application/cli.py) | CLI 分流、参数校验、JSON 与退出码；维护入口在普通配置加载前分流 |
| [application/workflow_cli.py](../../src/deskpal/application/workflow_cli.py) | 工作流动作列表、参数适用范围、CAS/证据校验与执行 |
| [workflow/store.py](../../src/deskpal/workflow/store.py) | `WorkflowStore`，连接、事务、大小限制和 schema 初始化 |
| [workflow/tasks.py](../../src/deskpal/workflow/tasks.py) | `PersistentTaskManager`，持久任务、提交与依赖引用 |
| [workflow/journal.py](../../src/deskpal/workflow/journal.py) | `ExecutionJournal`，不可变计划、步骤 attempt、恢复、删除工作单 |
| [workflow/humanization.py](../../src/deskpal/workflow/humanization.py) | `HumanizationStore`，回合引用、上下文快照、用途化调用预算、隐私与效果账本 |
| [application/audit.py](../../src/deskpal/application/audit.py) | `WorkflowExecutionLog`，生产事件写入工作库 outbox |

## Core、计划、模型与表达

`AgentCore` 通过 [agent/ports.py](../../src/deskpal/agent/ports.py) 等 Protocol 协作，`execute_task()` 执行已有持久任务，`_run()` 包含主要规划、权限、执行和反思分支。`restore_pending()` 恢复原计划，`revoke_pending()` 处理失效授权/计划。先理解公开方法与状态边界，再读内部辅助函数。

[planner/models.py](../../src/deskpal/planner/models.py) 定义 Plan，[llm_planner.py](../../src/deskpal/planner/llm_planner.py) 将模型响应转换为有界静态步骤，[direct.py](../../src/deskpal/planner/direct.py) 仅提出一个可信 ToolCall，不能绕过权限。

[llm/ports.py](../../src/deskpal/llm/ports.py) 定义 `LLMProvider.chat/generate` 和可选 `NativeJSONProvider`；[providers.py](../../src/deskpal/llm/providers.py) 包含确定性 Offline 与兼容 Responses 的 OpenAI adapter。传输、响应解析、重定向与秘密保护属于 adapter；[url_policy.py](../../src/deskpal/llm/url_policy.py) 负责端点策略。

[llm/calls.py](../../src/deskpal/llm/calls.py) 的 `ModelCallGateway.bind()` 按 Task/purpose/guard 绑定调用，统一预留和结束调用账本。planning、expression、memory_evaluation 共享任务约束，不能为了表达失败自行再开无限重试。

[response/composer.py](../../src/deskpal/response/composer.py) 的 `ResponseComposer` 与 `parse_envelope()` 构造、解析表达及候选，审核重复键、有限 JSON、字段和范围；[protocol.py](../../src/deskpal/response/protocol.py) 与 [templates.py](../../src/deskpal/response/templates.py) 管理协议与固定模板。模型输出始终是待验证数据。

## 会话与陪伴

| 文件 | 作用 |
| --- | --- |
| [conversation/runtime.py](../../src/deskpal/conversation/runtime.py) | `ConversationRuntime`，open/reserve/freeze/publish/invalidate，有限轮数/字节与 epoch |
| [application/interaction.py](../../src/deskpal/application/interaction.py) | `InteractionCoordinator`，中性规划检索、用途化表达投影、类别许可、来源检查和结果发布 |
| [application/privacy.py](../../src/deskpal/application/privacy.py) | `PrivacyCoordinator`，隐私控制入口和持久阻断 |
| [companion/runtime.py](../../src/deskpal/companion/runtime.py) | 人格设置、风格读取、有限情绪状态和重置 |
| [companion/interaction_runtime.py](../../src/deskpal/companion/interaction_runtime.py) | 身份、云许可、偏好候选与互动状态 |
| [companion/signals.py](../../src/deskpal/companion/signals.py) | 自然语言偏好/情境候选的证据校验 |
| [companion/repository.py](../../src/deskpal/companion/repository.py) | 独立 SQLite 持久化和 revision |
| [application/effects.py](../../src/deskpal/application/effects.py) | `CompanionEffects`，终态后有限效果的可靠协调 |

## 记忆、目标与调度

[memory/contracts.py](../../src/deskpal/memory/contracts.py) 是来源、UsePolicy、revision、检索包与管理协议的主入口。[coordinator.py](../../src/deskpal/memory/coordinator.py) 适配记忆操作与使用资格，[repository.py](../../src/deskpal/memory/repository.py) 管持久记录，[retrieval.py](../../src/deskpal/memory/retrieval.py) 融合词法/语义检索，[embedding.py](../../src/deskpal/memory/embedding.py) 管 cache-only FastEmbed adapter。`manager.py` 保留旧接口兼容，不应绕过 Coordinator 覆盖现存记录。

本地 BGE 从 `config → application.factory → FastEmbedProvider → MemoryRetriever → MemoryCoordinator` 接入；当前工作区已引用 `data/models/bge-small-zh-v1.5`。adapter 使用 FastEmbed 0.9.0 和 ONNX Runtime CPU，检查 512 维有限值并归一化，查询前缀和 token 限制属于其预处理契约。`maintenance/cache.py` 负责显式下载/托管清单，运行时 adapter 不下载；缓存 revision 和索引 `EmbeddingManifest` 的区别见[存储与维护](storage-and-maintenance.md)。

[goals/manager.py](../../src/deskpal/goals/manager.py) 维护 Goal/Milestone/SourceBinding、CAS、执行条件版本与完成人工证据。[proactive/manager.py](../../src/deskpal/proactive/manager.py) 管 Proposal 的复核和原子接受，[engine.py](../../src/deskpal/proactive/engine.py) 消费本地事件；cursor、inbox 和 continuation 保证有界续办，不能将其改为任意后台云规划。

[scheduling/service.py](../../src/deskpal/scheduling/service.py) 管独立日程/计时库、到期事件和来源身份。工具依赖 [SchedulingManager Protocol](../../src/deskpal/scheduling/ports.py)，Core 不读其私有 SQL 连接。

## 工具与文件策略

[tools/models.py](../../src/deskpal/tools/models.py) 定义 `ToolSpec/ToolCall/ToolContext/ToolResult`，[ports.py](../../src/deskpal/tools/ports.py) 定义工具接口，[manager.py](../../src/deskpal/tools/manager.py) 冻结注册元数据、校验 schema 和权限、分发执行并固定异常分类。`builtin.py/calendar.py/timer.py/documents.py/screenshot.py` 是具体工具实现。

[sandbox/workspace.py](../../src/deskpal/sandbox/workspace.py) 负责工作区路径、链接和文件限制。截图通过可注入 backend 测试，mss 惰性导入；新增文件工具必须复用 Sandbox，而不是只在界面检查路径。

独立配置、缓存、归档与凭据的详细代码说明见[存储与维护](storage-and-maintenance.md)；Qt 代码见[GUI 实现](gui.md)。
