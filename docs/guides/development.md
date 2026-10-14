# 开发与扩展约定

## 开始修改前

阅读根目录 `ARCHITECTURE.md` 与 [V0.2 接口](../versions/v0.2/interfaces.md)。前者定义边界，后者规定数据对象、方法参数、失败语义及任务状态转换。原始 `doc/` 文档用于理解长期目标；当前实现范围以 [V0.2 计划](../versions/v0.2/development-plan.md) 为准。

Calendar/Timer 工具依赖 `SchedulingManager` Protocol，SQLite/线程实现由 Application 装配和关闭；截图通过可注入 `ScreenshotBackend`，mss 惰性加载。Core 不导入这些具体服务，UI 和直接工具请求仍需经过同一个 Tool Manager/Sandbox 入口。

使用 Python 3.11 或更高版本。运行依赖尽量保持标准库；新增依赖必须有明确用途。业务模块只依赖接口和数据对象，具体实现由 Application 组装。

依赖统一在 pyproject.toml 维护，按 requirements 分组入口安装；新增库、升级验证与锁定约束遵循 [依赖维护](dependencies.md)。

## 新增模型服务

在 `llm` 内实现 `LLMProvider.chat()` 和 `generate()`，将厂商消息、工具调用和错误转换为统一对象。Core 和 Planner 不得导入厂商 SDK 或解析厂商响应。模拟传输必须覆盖正常响应、工具参数、异常及凭据保护；真实服务验证与离线测试分别报告。

## 新增可信工具

提供 `ToolSpec` 与 `execute(arguments, context)`，通过 Tool Manager 注册。参数和权限来自注册对象，不能接受模型提供的权限等级。文件操作必须使用 Sandbox 的路径策略。需要用户确认的工具声明相应权限等级，不能自行绕过授权检查。

当前版本只支持可信工具。第三方 Python 代码拥有进程权限，当前 Sandbox 不能将其隔离；完整插件执行隔离应作为独立架构改动设计和验证。

## 替换任务或记忆存储

维持 Task Manager 的合法状态转换与快照语义，或者 Memory Manager 的保存、查询和删除契约。存储连接、SQL、文件格式与向量数据库类型不得进入 Core。修改持久化格式时必须说明旧数据迁移及故障恢复策略。

## 接入桌面界面

UI 通过 Application 创建 Agent，调用 `handle_request()` 并显示 `AgentResult`。界面可根据任务 ID 查询状态。同步请求应由界面的工作线程执行，避免阻塞主线程。SchedulingService 已有独立调度线程，UI 从 events 工具读取持久化到期事件；系统通知与抢占取消仍需专门设计，不能仅改变提示就声称支持。

当结果为 Waiting 时，展示全计划与逐项缺授权参数/风险，用户同意后调用同一 Core 的 `confirm_task`，分别传普通和高风险授权，执行原计划。关闭/重启后无法恢复私有计划；重新 `run` 是新任务且会重新规划。

## 验证与交付纪律

- 修改共享接口前审查所有调用方，更新接口文档。
- 添加能验证实际行为、错误和边界的测试，避免只复制实现逻辑。
- 在仓库根目录运行 `python -m pytest`；静态检查使用 `python -m mypy src --ignore-missing-imports --disallow-untyped-defs`。
- 配置、日志和数据库使用测试临时目录；不读取真实密钥或修改用户工作区数据。
- 交付记录修改文件、设计理由、接口、测试结果及限制。
- 不提交真实配置、环境凭据、运行日志、数据库或生成缓存。
