# 请求执行与生命周期

主路径从 [ApplicationService](../../src/deskpal/application/service.py) 和 [SubmissionDispatcher](../../src/deskpal/application/dispatcher.py) 进入 [AgentCore](../../src/deskpal/agent/core.py)。下面按一次生产请求解释关键节点。

## 1. 装配与启动

`application/factory.py:create_application()` 的顺序具有资源所有权含义：

1. 加载/接受 `AppConfig`，取得规范数据目录的 `InstanceLock`。
2. 调用 `assert_restore_ready()`，恢复未完成时拒绝初始化生产数据库。
3. 装配 CompanionRuntime、WorkflowStore、PersistentTaskManager、ExecutionJournal、GoalManager。
4. `_create_core()` 装配 Provider、Planner、Sandbox、Memory、Scheduling 和可信工具。
5. 装配 ProposalManager、LocalRuleEngine、ApplicationService，并将执行 guard 注入 Core。
6. Application 构造期间先续办删除；允许时恢复任务状态。`start()` 再启动生产 dispatcher 和可选本地提议线程。

工厂使用 `ExitStack` 处理失败回收，成功后转移所有权。新增资源必须明确由谁关闭，不能只在正常路径释放。

## 2. 提交与上下文

`submit()` 校验应用可用性、隐私阻断、用途、请求大小和幂等命令 ID，再创建/取得持久 Task/Submission。会话请求携带 `conversation_id` 与预期 epoch，避免把过期上下文绑定到新回合。

`InteractionCoordinator` 冻结本回合的规划与表达投影、依赖引用和摘要；`HumanizationStore` 记录回合引用、上下文用途、模型调用账本与隐私 epoch。投影是显式准备的上下文，不是读取全部聊天历史。

GUI 的桥接 request ID 表示本地异步操作身份；Task ID 才是持久任务身份；模型 call ID、工具 call ID、command ID 各有用途，不能互换。

## 3. 规划与计划冻结

dispatcher 单 worker 调用 `Core.execute_task(task_id)`，不会再次提交同一个请求。Core 取记忆，调用 Planner，获得有界静态 Plan；`DirectToolPlanner` 也走同一 Core 路径。

持久计划的 canonical JSON 和 SHA-256 覆盖准确参数、reply、稳定调用 ID、工具 contract/schema/权限、工作区、用途和依赖版本。过大的请求或计划在执行前拒绝。依赖检查在接受、出队、规划/云出口和每步前重复执行。

## 4. 确认与执行

整份计划先预检全部工具权限。缺授权则 `Waiting`，没有工具副作用。授权仅在本进程绑定 Task 与原计划；确认前核对 hash、工具契约、路径、来源版本，不能重新规划后静默替换。

每步执行顺序：检查取消/用途/依赖 → 提交 Started → Tool Manager 参数与权限检查 → 工具执行 → 提交 Succeeded/Failed。外部调用不占用长工作库写事务。结果过大时明确省略正文，保留 outcome。

可靠结果形成后进入 Reflect/受限表达与收尾。表达失败不能重放工具；风格、模拟情绪和候选仅通过独立受限路径处理，不能更改权威任务事实。

## 5. 取消、崩溃与恢复

取消直接持久写入短事务，不等待排在工具执行队列后。若 Started 先于取消提交，当前同步调用仍可能产生效果；下一边界阻止后续步骤。

| 重启时证据 | 恢复处理 |
| --- | --- |
| Queued/Planning/Waiting，未有 attempt | 先 NeedsReview，不自动补云/执行；无 attempt 时可按规则 resume/重新确认 |
| 部分步骤成功，后续未开始 | NeedsReview，保留账本，剩余意图另建任务 |
| Started 无可靠结果或账本不一致 | Uncertain，人工复核不授予原步骤重试资格 |
| 全部步骤可靠成功，仅收尾中断 | 复核来源/取消后据账本收尾，不补模型或工具调用 |
| 可靠终态 | 历史查询，不再执行 |

TaskStatus 的具体枚举在 [tasks/models.py](../../src/deskpal/tasks/models.py)，队列 submission 另有状态；不要给 TaskStatus 添加一个仅属于队列展示的 Queued 值来混淆模型。

## 6. 关闭

Application 先停止提交、本地规则与领取，协作取消并等待 Core，join 执行 worker，再关闭 Core 拥有的调度/记忆/模型，最后关闭工作库并释放实例锁。`close(timeout)` 超时返回 False，在用资源与锁保留，可再次关闭。

GUI 还需等待桥接后台 Closed、图片 worker 退出、宠物窗口销毁，再由 Host/Launcher 统一结束 Qt 或进入维护。维护 worker 也必须实际结束后才能重启 Application。详见[GUI 实现](gui.md)。
