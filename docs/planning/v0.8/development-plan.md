# V0.8 开发计划

2026-10-22 后续设计：[拟人化优化方案](humanization-proposal.md)、[实现架构](humanization-architecture.md)、[实施与验收计划](humanization-delivery-plan.md)提出多轮/情境理解和表达闭环的增量路线；目前只写文档，未改代码。下文仍为原始 V0.8 最小实现的开发快照。

后续状态：用户在开发完成后已授权并执行[最小T01–T09](../../versions/v0.8/minimal-test-plan.md)，实际结果与范围见[测试报告](../../versions/v0.8/test-report.md)。下文“本轮不测试”是最初开发阶段的规划快照。

日期：2026-10-21。状态：范围已冻结，进入实现；**本轮按用户要求不执行测试**。本文件是开发规划，不是通过证明。Git 由用户提交，产品版本号暂保留 `0.5.0rc1`。

## 依据与范围

依据 [路线图](../roadmap.md)、[人格与情绪提案](../companion-model/personality-emotion.md)、[集成提案](../companion-model/integration-and-acceptance.md)、[开发约定](../../guides/development.md)、根目录 [架构](../../../ARCHITECTURE.md)及既有 V0.2–V0.7 实际接口。

本版实现声明式人格、显式互动偏好、受限情绪模拟、本地管理入口、现有回复链路的风格提示和静态桌宠映射。V0.6 继续按既有检查点暂停；Live2D 继续延期。V0.7 的资源增长和长测预览未决项继续见[后续清单](../../versions/v0.7/deferred-tests.md)，不能由 V0.8 开发完成覆盖。

默认关闭新功能，旧配置继续可用。不增加依赖、周期性云请求、后台情绪识别、关系积分、自动学习人格、任意角色卡、主动权限或完整多轮上下文。本版不解析自然语言情绪；临时 cue 由明确管理操作设置。启用人格且存在有效风格指令时，无工具聊天在 Planner 之后增加一次 `tools=()` 的表达生成，草稿明确作为未经验证的 assistant 输出，不提升为事实证据；失败回退原回复并警告。关闭人格时保持原路径。

## 架构与冻结接口

新增 Qt-free `companion` 领域，由 Application 工厂装配。生产状态使用独立 `companion.sqlite3`，在取得 data_dir 实例锁后打开；低层同步工厂使用内存状态。Core 仅依赖窄接口；领域不导入 Qt、Core、工具或授权实现。GUI 继续通过 `ApplicationBridge → DesktopFacade → ApplicationService` 后台通道访问。

运行接口为 `snapshot()`、`command(action, **parameters)`、`record_outcome(task_id, status, tool_success=...)`、`style_instruction(request, cloud=...)`、`reset_for_privacy()` 与 `close()`。管理操作携带 `expected_revision`，提供 configure、reset、rollback、preference、cue。Desktop 新增 `companion` 只读操作与 `companion_command` 写操作；V0.7 `presentation` 七字段契约保持。

1. Persona 提供 balanced/warm/concise 三个预设、有限数值风格维度、版本保存/回退/重置。拒绝未知字段及任意可执行角色指令。
2. 明确互动偏好支持 session/today/persistent 作用域；cue 仅会话内，最长 1800 秒。当天约束的期限按明确 IANA 时区计算；cue 到期不撤销独立互动约束。
3. Affect 仅从可信任务最终汇总产生小幅变化。Completed 必须在权威完成提交之后；工具失败不能伪称完成，取消/拒绝/等待/不确定不形成对用户的负向效果。
4. 每任务根来源幂等，重置及切换配置不清消费回执。消费回执有容量上限，满后拒绝新效果，不驱逐后重新奖励旧任务。任务正文、工具参数、结果正文不进入情绪库。
5. v/a/m 分别按半衰期 300/120/7200 秒衰减；单事件快速增量不超过 0.15，60 秒每维绝对增量合计不超过 0.25，m 的 3600 秒绝对增量不超过 0.05、范围为 [-0.25, 0.25]。窗口数量同时设硬上限；超额丢弃，不延后补发。查询是纯投影，不消费事件、不写入衰减锚点。
6. 状态、额度与消费标记同事务提交。重启可靠 UTC 间隔用于衰减，时钟异常回基线并保守保留窗口额度；reset_epoch 与修订抵御旧快照，隐私重置另由 Core 代际保护旧任务输出。
7. Planner、ToolContext、Plan/hash、授权、记忆资格、目标证据均不受影响。工具路径仅现有 Reflect 使用受限风格，不增加请求次数；普通无工具聊天启用风格时最多增加一次表达调用。云端风格只用固定 Persona 预设，不发送本地 cue、情绪值或偏好派生信息。
8. 固定任务状态始终权威。桌宠在Idle或Completed有限完成反馈结束后的静止阶段采用情绪装饰，确认/错误/审查/关闭等保持原状态映射；缺图、陈旧、关闭或降级回原映射。情绪图不会重启完成反馈计时。

## 任务分配与顺序

主 Agent 负责范围、任务划分、接口与架构审查、文档、整合及后续测试清单，不独自承担代码实现。关键代码分配给 **6.1 Sol，高思考**：

| 工作包 | 所有权 | 交付 |
| --- | --- | --- |
| A：领域 | `src/deskpal/companion/**` | Persona/StylePolicy、状态 reducer、SQLite 持久化、去重/额度/时钟/重置 |
| B：后端 | `src/deskpal/agent/**`、`src/deskpal/application/**`、根 README 操作说明 | 工厂所有权、Core 风格与最终结果接入、隐私代际、服务/CLI/Desktop API |
| C：界面 | `src/deskpal/gui/**`，必要时扩展 `presentation` 的独立映射 | 人格情绪面板、版本控制、后台请求、独立情绪装饰与生命周期 |
| 主 Agent | `docs/**`、`ARCHITECTURE.md`，必要的跨模块整合 | 计划/接口/运行/开发状态、审查、测试清单 |

实施顺序：阅读基线 → 冻结上述范围与接口 → 领域/后端/界面按独立文件实现 → 主 Agent 阅读变更并处理接口差异 → 更新实际接口/限制 → 提交待执行测试清单。本轮允许阅读代码和 diff 作设计审查；不运行 pytest、mypy、编译、GUI、真实 API、容量或长测，也不将静态阅读写成测试通过。

后续简单、固定、低风险测试命令委派 **6 Luna，高思考**；故障归因与权限审查由主 Agent 或 Sol 承担。测试失败保留原日志和退出码，不放宽断言。具体清单在开发完成后存入 `docs/versions/v0.8/test-plan.md` 并交用户审阅。

## 交付门槛与已知取舍

开发完成只表示实现和接口整合可供审查。版本验收须另行通过 [验收边界](acceptance-boundaries.md)对应测试。构造/持久化异常时新功能中性降级，写命令明确失败，不自动覆盖损坏库；不能因表现层失败重试工具。

本版不交付自由文本角色背景、RelationshipProfile/共同经历自动生成、文本情绪分类器、完整 ResponseEnvelope 重构或自然语言正确性的形式化保证。结构化工具结果和任务状态仍单独显示；开放生成文本的事实与风格适切性须通过后续模型评审，不能靠提示词声称已保证。
