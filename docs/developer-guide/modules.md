# 模块职责

路径均相对于 `src/deskpal/`。当前精确数据库字段见[数据库设计](../planning/companion-redesign/database-design.md)。

| 模块 | 职责与主要调用 |
|---|---|
| application | factory 装配；service/desktop 提供接口；dispatcher 串行执行；interaction 准备上下文；profiles 管档案；management_tools 接内部操作 |
| agent | Core 多轮协调，context/ports 定义边界，loop 管轮次，feedback 管同步学习收尾 |
| planner、llm、response | 统一模型请求、调用预算、规划协议、自然语言回复与严格表达协议 |
| companion | 角色卡、学习习惯、当前理解及修订；legacy 仅作兼容 |
| conversation | 会话、时间线、双方历史、工作摘要及来源有效性 |
| memory | 长期记录与来源、检索、统一学习缓冲、复核和学习记录 |
| workflow、tasks | 持久任务、轮次、执行/模型账本、授权、控制、迁移及状态 |
| proactive | 主动检查机会、复查和通知策略；经已有调度进入 Core 或反馈服务 |
| tools、scheduling、sandbox | 工具契约与实际操作、日程定时器、工作区路径保护；不是 OS 沙箱 |
| maintenance、config | 配置和凭据、模型缓存、备份恢复、维护互斥 |
| gui、presentation | Qt 展示和桥接、桌宠与有界状态投影；不在 UI 线程直接执行业务 |
