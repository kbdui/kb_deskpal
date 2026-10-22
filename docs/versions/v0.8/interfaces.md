# V0.8 人格与情绪接口

2026-10-22 后续增量：H0–H5 的[实际接口与开发记录](humanization-development-status.md)新增 conversation/interaction/request_forget 控制面、用途化模型调用和 schema 2；本轮未验证。下文保留原最小版接口：旧 snapshot 与七字段 presentation 形状仍兼容；record_outcome 现在仅对明确 tool_success 的可信工具结果奖励，并增加 use_policy/occurred_at；运行中受理遗忘使用新 request_forget，旧 memory 管理门禁不冒充该能力。

更新：2026-10-21。本文件描述代码契约；用户指定最小自动化测试及strict结果见[测试报告](test-report.md)，其中T07并发受理及两环境跳过有明确边界，不代表完整版本验收。规划见[开发计划](../../planning/v0.8/development-plan.md)，历史较宽建议见[测试清单](test-plan.md)。

## 领域与所有权

`deskpal.companion.CompanionRuntime(db_path=None, *, timezone='Asia/Shanghai', wall_clock=None, monotonic_clock=None)` 使用标准库 SQLite。`None` 路径为临时内存库；生产工厂在取得实例锁后打开 `data_dir/companion.sqlite3`，配置时区来自 `proactive.timezone`。可选时钟用于后续确定性测试。Core 通过 `agent.ports.CompanionPort` 注入，不依赖 SQLite 或 Qt。

`snapshot() -> dict` 是查询投影；`command(action, **parameters) -> dict` 是带 CAS 的管理操作；`record_outcome(task_id, status, *, tool_success=None)` 是可信任务汇总入口；`style_instruction(request, *, cloud) -> str` 返回有限表达文字；`reset_for_privacy()` 清除派生/会话状态并推进代际；`close()` 由工厂资源栈拥有。领域不能调用工具或生成授权。

异常分为 `CompanionValidationError`、`CompanionRevisionConflict`、`CompanionStorageError`、`CompanionClosedError`，均无业务正文诊断。坏库或未知 schema 不自动修复或覆盖；工厂允许可选功能不可用，普通任务继续。快照降级为关闭、中性及固定 warning，管理写明确拒绝。初始化后的存储错误也由 Core 中性降级。

降级诊断区分 `companion_storage_unavailable`（真正无法读取，GUI不提交设置）和 `companion_expression_degraded`（存储仍可读，保留真实配置/版本/CAS，只将情绪和cue投影为中性，允许显式设置恢复）。两者均不改任务事实；成功的显式命令、隐私重置或可信后续终态可解除Core降级锁定。

## 管理操作

所有写操作必带原生非负整数 `expected_revision`，与当前 `revision` 不同则拒绝。禁止 bool 冒充整数、NaN/Infinity、未知字段和自由角色指令。

| action | 其余字段 | 语义 |
| --- | --- | --- |
| configure | 可选 enabled、emotions_enabled（严格 bool）、preset、style | 默认两开关均 false；预设 balanced/warm/concise；style 为六轴有限 0..1 数值的部分覆盖 |
| rollback | persona_version（正整数） | 从保留版本复制配置并产生新版本，不倒退版本号 |
| reset | target=affect/persona/all，默认 affect | 情绪回基线；persona 同时恢复 balanced 并建新版本；all 还清显式偏好，不删除记忆/任务或消费回执 |
| preference | scope=session/today/persistent；verbosity、humor_allowed 至少一项 | verbosity=brief/normal/detailed；humor_allowed 严格 bool；当前会话 > 今天 > 持久偏好 |
| cue | mode=neutral/supportive/focused；ttl_seconds=1..1800，默认1800 | 明确短时表达模式；neutral 清 cue；不自动分析用户心理 |

六轴为 formality、warmth、humor、verbosity、expressiveness、curiosity。用户保存预设/六轴、回退或重置人格会产生递增人格版本，最多保留最近 32 个。配置切换及重置均使情绪回基线、清 cue 并推进 reset_epoch，保留幂等记录及未过期额度。

session 偏好和 cue 仅内存，不跨重启；today 偏好保存明确 IANA 时区的下一午夜期限且最多24小时；persistent 持续保存。cue 到期不解除独立偏好。隐私重置清 cue/会话偏好和派生情绪，保留显式 today/persistent 设置；清这些设置使用 reset all。

## 快照与桌面入口

快照至少包含：schema_version=1、reducer_version=1、revision、reset_epoch、enabled、emotions_enabled、persona（version/preset/style）、persona_versions、affect（valence/arousal/mood/mode/expression）、preferences、constraints、cue、warnings、limits、timezone。Core 的中性降级快照提供展示所需字段；调用方不得要求降级结果含正常运行的全部诊断元数据。

`ApplicationService.companion_snapshot()` 可在任务运行时读取；`companion_command(action, **parameters)` 经 dispatcher 管理门禁，运行/关闭/清理期间不接受写。GUI 主线程不调用这些同步方法。

DesktopFacade 新增 `companion`（参数必须为空）和 `companion_command`（action 及对应字段）；数据沿用 session_id/memory_epoch，桥接信封沿用 request_id/generation/privacy_epoch/ok。`companion` 进入 read 通道，命令进入 command 通道。V0.7 `presentation` 七字段和状态选取不变。

GUI 只保留有界元数据，校验代际、修订、数值范围及新鲜度；用户配置编辑不被刷新覆盖。每次提交先读新鲜修订，失败/冲突须主动重试，不自动重放命令。隐私屏障、Closing 或关闭清空本地副本。

## 可信事件、动态规则与恢复

单数据目录代表一个 owner；任务 ID 是根来源效果键，无通用历史事件导入入口。仅 Completed/Failed 消费终态身份；其余状态不施加效果。Completed 且 tool_success=false 不奖励。每任务首次终态记录回执，即使功能关闭也消费该身份，避免之后启用重投旧任务。

快速 v/a 的半衰期为300/120秒；m 为7200秒，m 回归0、不反馈进 v。每可信完成的建议增量为 (+0.08,+0.04,+0.005)，失败为 (-0.08,+0.04,-0.005) 并短时 focused；v∈[-1,1]、a∈[0,1]、m∈[-0.25,0.25]。baseline 随有限 Persona 维度小幅变化。显示 v 仅叠加0.2m。

每60秒快速维度绝对增量各≤0.25、事件≤32；每3600秒 m 绝对增量≤0.05、事件≤256。逐事件先衰减、再按剩余额度裁剪，正负不能相抵；触及状态边界仍计额度，超额丢弃。状态/回执/预算同事务。查询纯投影，不推进状态或消耗额度。最多100000个任务回执，满后冻结新效果并告警，不驱逐旧键；最多256条窗口记录。

运行期按单调时间推进，快照保留 UTC 锚。正常重启使用 UTC 间隔，超30天或回拨等异常回基线并推进代际；不明年龄的预算从本次启动重新保守计满窗口。会话 cue 不恢复，历史事件不重播，不为漏掉的情绪重做工具。新库 schema1 与旧工作库/记忆库无迁移耦合。

Core 在最终持久 Task 状态提交后记录汇总，故**本轮回复使用此前的情绪，本轮结果影响后续读取/下一轮回复**。任务提交与 companion 是跨库边界：崩溃可能缺一次模拟效果，既有终态恢复不补演；不承诺跨库 exactly-once。

## 表达、隐私与成本

Planner 不接收 Persona/Affect，计划/hash/参数/工具风险及授权保持原契约。工具 Reflect 沿用原单次调用，附有限风格。无工具普通聊天仅启用且有有效风格时增加一次 tools=() 表达调用，草稿标为未验证 assistant 输出；失败回原草稿并 warning。关闭时保持原回复路径；明确记忆命令和确认/失败/取消/审查回执不风格化。candidate_review 原有附带评估另计。

云端表达仅由明确选择的固定 Persona preset 生成指令，忽略自定义六轴、本地互动偏好、cue 和 affect。当前请求的明确风格要求仍优先。自定义六轴和作用域偏好仅用于本地表达策略；当前 offline provider 的固定模拟回复不代表真实生成模型的风格验收。

遗忘开始后，Core 先推进内部代际，再在相同锁下重置派生表达。旧任务的风格发布及终态效果不能越过此屏障。重置失败保持清理 Pending；重试/启动续办须重新成功重置才完成遗忘。状态中只保存任务ID和有界控制元数据，不保存请求/参数/结果正文，显式持久设置有单独重置入口。

结构化任务/工具结果保持权威。提示词不能证明开放文字永无事实错误；真实模型的事实、临时要求和风格仍须独立评审。
