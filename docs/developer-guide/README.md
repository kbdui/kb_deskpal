# 开发者指南

当前基线为 Phase 01–05；Phase 06 待实施。[总体架构](../../ARCHITECTURE.md)解释调用方向，[有效设计](../design/companion/character-memory-decisions.md)与[执行决定](../design/companion/execution-lifecycle-decisions.md)说明业务边界。

| 文档 | 内容 |
|---|---|
| [开发与测试](development-and-testing.md) | 环境、测试分组、委派及证据纪律 |
| [仓库地图](repository-map.md)、[模块](modules.md) | 文件位置与职责 |
| [架构边界](architecture.md)、[请求链路](request-lifecycle.md) | 服务层、Core、工具、学习与发布 |
| [GUI](gui.md)、[GUI 状态边界](gui-refactor.md) | 主线程、后台桥、来源与迟到回调 |
| [手机入口](mobile-access.md) | 共享后台、HTTP 认证、命令白名单、断线恢复与生命周期 |
| [存储与维护](storage-and-maintenance.md) | 四库、角色切换、迁移和恢复 |
| [扩展方法](extending.md) | 工具、模型与后台能力接入 |
| [依赖](dependencies.md)、[Embedding](embedding-setup.md) | 安装分组与可选本地检索 |

精确阶段证据见[实施目录](../planning/companion-redesign/README.md)，旧接口和旧机制说明已移入[历史指南](../archive/guides/developer-guide/README.md)。
