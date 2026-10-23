# 开发与扩展约定

2026-10-23 产品化增量：本地配置/缓存/备份恢复维护独立于生产 Application 生命周期。修改这些入口先读[V0.9 实际接口](../versions/v0.9/interfaces.md)及[开发记录](../versions/v0.9/development-status.md)，保持 Qt-free CLI、秘密不出回执、实例/缓存锁和恢复标记屏障。开发与测试分开；用户已启动按[最小验收清单](../planning/v0.9/productization-minimal-test-plan.md)集中验证，实际结果与未验证边界见[测试记录](../versions/v0.9/test-report.md)。

## 开始修改前

阅读根目录 `ARCHITECTURE.md`、[V0.5 接口](../versions/v0.5/interfaces.md)、[V0.4 持久后端接口](../versions/v0.4/interfaces.md)、[V0.3 记忆接口](../versions/v0.3/interfaces.md)与[V0.2 工具接口](../versions/v0.2/interfaces.md)。当前候选版范围和证据以 [V0.5 状态](../versions/v0.5/delivery.md)及[测试报告](../versions/v0.5/test-report.md)为准；`doc/` 与 planning 用于长期设计。

Calendar/Timer 工具依赖 `SchedulingManager` Protocol，SQLite/线程实现由 Application 装配和关闭；截图通过可注入 `ScreenshotBackend`，mss 惰性加载。Core 不导入这些具体服务，UI 和直接工具请求仍需经过同一个 Tool Manager/Sandbox 入口。

使用 Python 3.11 或更高版本。运行依赖尽量保持标准库；新增依赖必须有明确用途。业务模块只依赖接口和数据对象，具体实现由 Application 组装。

依赖统一在 pyproject.toml 维护，按 requirements 分组入口安装；新增库、升级验证与锁定约束遵循 [依赖维护](dependencies.md)。

## 新增模型服务

在 `llm` 内实现 `LLMProvider.chat()` 和 `generate()`，将厂商消息、工具调用和错误转换为统一对象。Core 和 Planner 不得导入厂商 SDK 或解析厂商响应。模拟传输必须覆盖正常响应、工具参数、异常及凭据保护；真实服务验证与离线测试分别报告。

## 新增可信工具

提供 `ToolSpec` 与 `execute(arguments, context)`，通过 Tool Manager 注册。参数和权限来自注册对象，不能接受模型提供的权限等级。文件操作必须使用 Sandbox 的路径策略。需要用户确认的工具声明相应权限等级，不能自行绕过授权检查。

当前版本只支持可信工具。第三方 Python 代码拥有进程权限，当前 Sandbox 不能将其隔离；完整插件执行隔离应作为独立架构改动设计和验证。

## 替换任务或记忆存储

维持 Task Manager 的合法状态转换与快照语义，或者 MemoryRepository/MemoryRuntime 的来源、CAS修订、审核、删除、用途过滤和 epoch/revision 契约。旧 MemoryManager 由 Coordinator 适配，不能用旧 save 覆盖现存记录。存储连接、SQL、文件格式与向量数据库类型不得进入 Core。修改持久化格式时必须说明旧数据迁移及故障恢复策略。

## 接入桌面界面

V0.5 UI 通过 `ApplicationBridge → DesktopFacade → ApplicationService` 进入既有后端；初始化、查询、命令、关闭留在后台通道，Qt 主线程只处理有界 DTO 和展示。只有既有 dispatcher 执行 Core；工具继续经过 Tool Manager/Sandbox。同 data_dir 的 CLI 与 GUI 互斥，不另建执行者。

Waiting/NeedsReview 展示持久原计划、参数、风险、workspace 和 hash；普通与高风险授权默认都为空，明确同意后使用 `enqueue_confirmation` 返回非阻塞回执。现有授权按整份计划中的工具名，不宣称逐调用或逐路径授权。重启可以恢复审查所需快照，但会话授权清空、有 attempt 的计划不自动重放；记忆依赖和来源变化仍失效旧计划。通知只提醒并导航复查，同步调用仍是协作取消。生命周期、隐私屏障和真实 Windows 缺项见 V0.5 接口与测试报告。

## 测试任务委派

按用户于 2026-10-16 的要求，主 Agent 先判断测试任务性质：简单、机械、低风险且预期结果明确的执行（例如固定命令的回归、strict、证据和链接核对），委派使用 **6 Luna** 的子 Agent；沿用高思考强度。主 Agent 负责设定命令、范围、时限和通过标准，并审查结果。

故障归因、架构审查和修补方案由主 Agent 或 6.1 Sol 高思考 Agent 承担。执行 Agent 遇到失败需保留日志、退出码与失败项，不自行放宽断言或跳过检查。每项返回修改文件、设计说明、接口影响、测试结果与潜在问题；Git 提交由用户处理。

## 验证与交付纪律

- 修改共享接口前审查所有调用方，更新接口文档。
- 添加能验证实际行为、错误和边界的测试，避免只复制实现逻辑。
- 在仓库根目录运行 `python -m pytest`；静态检查使用 `python -m mypy --strict src scripts/check_memory_api.py scripts/benchmark_memory.py`。
- 全仓 GUI 开发使用 requirements-gui-dev.txt；Qt 脚本与容量脚本分组检查及有界诊断命令见 [V0.5 操作指南](../versions/v0.5/operations.md)。
- 配置、日志和数据库使用测试临时目录；不读取真实密钥或修改用户工作区数据。
- 交付记录修改文件、设计理由、接口、测试结果及限制。
- 不提交真实配置、环境凭据、运行日志、数据库或生成缓存。
