# V0.4 交付说明

日期：2026-10-15。代码版本 **0.4.0**，本版功能交付完成；双 Python 各 1101 项通过、4 项明确跳过，验证范围与长期测量局限见 [测试报告](test-report.md)。Git 提交由用户处理。

## 交付行为

用户显式创建有完成标准的 Goal/Milestone，并绑定日程、计时器或 Task。本地规则可生成待查看 Proposal，接受后在同一工作库原子创建唯一 Task/Submission；单 worker 规划、展示原计划并按权限执行，结果与步骤可靠落盘。重启后能查询已完成步骤和未知区间，未开始工作先复核，未知步骤不会自动重试。Task 成功不自动完成 Goal，用户仍需明确提交完成证据。

| 内容 | 实际接口 |
| --- | --- |
| 生产装配 | create_application → ApplicationService；run/handle_request 为 submit+wait |
| 工作库 | workflow.sqlite3，Task/Plan/attempt/Goal/Proposal/依赖/outbox/删除 job 同库 |
| 目标 | Goal/Milestone/SourceBinding，revision CAS 与 execution_version，命令幂等 |
| 本地规则 | 默认关闭；截止窗口、显式绑定日程/计时到期、Task 完成复核 |
| 事件可靠性 | timer/calendar 独立 cursor、inbox/continuation、业务去重、身份/锚点审查 |
| 队列与控制 | 有界提交、单领取、直接取消事务、运行中协作停止 |
| 恢复 | NeedsReview/Uncertain、准确 Plan/hash、全部 grant 重置、零自动重放 |
| 记忆清理 | 先持久阻断、跨库幂等删除续办、明确来源派生副本和缓存擦除 |
| 运维 | 来源身份、显式 tail/有限历史重建、诊断、新路径备份、审计待投递 |

详细参数与数据限制见 [接口契约](interfaces.md)，可运行示例见 [操作指南](operations.md)。

## 兼容与环境

Python 3.11+，本轮在 Windows CPython 3.11/3.12 验证。新增 Windows 条件 tzdata，已安装 2026.5；无需 Qt/Live2D、ORM、APScheduler、额外向量数据库或队列服务。requirements 仍统一引用 pyproject，Windows 3.12 约束增至 44 项，见 [依赖维护](../../guides/dependencies.md)。

create_agent 保留 V0.3 低层同步、进程内任务契约，不提供 V0.4 持久运行保证；生产 CLI 已使用 create_application。Memory schema v2/画像/可选 cache-only embedding、19 个工具与原权限保持兼容。个人 .env/config、数据库和模型缓存不进 Git。

## 使用与恢复边界

新 Goal/Binding 默认 local_only。接受建议、配置云 key 不会扩大来源用途，也不会持久保存可执行工具授权。云模式受限意图为 NeedsReview；用户可明确选择离线处理，或更新来源用途并形成新意图。管理 CLI 不需要云 key；本地建议不发后台云请求。

正常停止或异常退出后的未完成工作都先审查。没有 attempt 的原计划可重新确认；部分成功的计划本版不续跑，剩余意图另建 Task。Uncertain 只记录人工判断/证据，不能一键重试原步骤。Started 早于取消提交的工具仍可能产生效果，不承诺即时中断或撤回。

JSONL 是元数据观察副本，允许同 event_id 重投；它不决定是否执行。遗忘保持永久来源阻断并清理明确派生工作副本；失败为 Pending，启动先续办。独立 Goal/Calendar 与历史日志/备份/导出/外部副本有独立保留范围，不保证物理取证级擦除。

## 未纳入本版

Qt/Live2D、人格与情绪、Goal 自动拆解/DAG、后台云建议生成、完整 WorkingContext、系统通知/托盘、无人值守执行、OS 隔离、同步工具强杀、多实例接管、外部效果 exactly-once，以及任意独立三库备份恢复的一致性。后续阶段见 [路线](../../planning/roadmap.md)。
