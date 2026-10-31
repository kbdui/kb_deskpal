# 扩展与修改

新增可信工具时实现既有 Tool 协议，提供严格参数 Schema、可信 permission_level 与 contract_version，并在应用工厂注册。参数和权限来自注册契约，不接受模型自报的权限。复用工作区与业务服务，覆盖非法参数、拒绝时零副作用、契约变化及未知结果恢复。当前进程内工具不具备第三方代码 OS 隔离。

模型服务在 llm 适配层实现统一 Provider，厂商结构与错误只在适配器转换。所有调用经过既有预算和来源边界；多轮规划与最终表达使用同一有效角色背景。严格协议仍检查重复键、非有限数、字段类型与大小，不能以旧 cue envelope 作为新协议。

内部管理接口复用对应服务，主动触发方法注册到 ProactiveCoordinator；普通主动聊天进入同一 Dispatcher/Core，内部整理进入同一反馈模块。禁止另开执行者绕过权限、来源或任务账本。

修改 schema 时同步迁移、只读归档检查、备份恢复、未知版本拒绝和调用方；修改会话与检索时保留来源修订和删除阻断。模型理由可以变化，真实来源关联由程序维护。新依赖声明在 pyproject.toml，并按[依赖规则](dependencies.md)更新安装分组。

验证方法见[开发与测试](development-and-testing.md)，验收依据见[有效设计](../design/companion/character-memory-decisions.md)与当前阶段文档。
