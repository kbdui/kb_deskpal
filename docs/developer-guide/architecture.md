# 架构边界

以[根架构图](../../ARCHITECTURE.md)为总览，以[公共契约](../planning/companion-redesign/architecture-and-contracts.md)为实施依据。GUI/CLI 通过应用层操作，只有既有 Dispatcher 执行 Core；主动模块也不另开执行者。

ApplicationService 负责组合业务接口与生命周期。InteractionCoordinator 准备有来源的角色和会话背景并管理模型调用。AgentCore 负责多轮决策，ToolManager 负责注册契约、参数和权限，具体工具访问受限服务。FeedbackLearning 处理模型理解和缓冲提交；存储层维护事务、修订与迁移。

程序负责可验证的归属、范围、权限、预算、时间与幂等，模型负责语义理解和下一步选择。助手建议和工具事实分开，重新规划不自动扩大授权。插件列表不表示任意第三方代码已有隔离执行能力。

旧六轴人格预设、独立 cue 状态和人工偏好候选不是新流程。底层保留的兼容结构不能被文档描述成当前用户功能。
