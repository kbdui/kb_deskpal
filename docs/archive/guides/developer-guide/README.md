> 历史存档：保留当时的设计、失败与验收边界；不代表当前实现或新的执行授权。当前入口见 [文档导航](../../../README.md)。

# Deskpal 开发者指南

适用代码版本：`0.5.0rc1`，按 2026-10-23 仓库实现整理。本目录解释当前文件组织、核心执行路径、接口边界和扩展方法，适合接手项目、定位代码和评估改动影响。

## 阅读路径

| 顺序 | 文档 | 解决的问题 |
| --- | --- | --- |
| 1 | [仓库与文件导航](../../../developer-guide/repository-map.md) | 代码、配置、脚本、测试和历史资料在哪里 |
| 2 | [架构与设计边界](../../../developer-guide/architecture.md) | 各层怎样依赖，哪些状态具有权威性 |
| 3 | [请求执行与生命周期](../../../developer-guide/request-lifecycle.md) | 从提交到规划、确认、执行、恢复和关闭 |
| 4 | [模块与核心代码说明](../../../developer-guide/modules.md) | 关键类、文件和定位入口 |
| 5 | [GUI 与桌宠实现](../../../developer-guide/gui.md) | 线程、桥接、DTO、隐私代际和关闭所有权 |
| 6 | [存储、隐私与维护](../../../developer-guide/storage-and-maintenance.md) | 四库职责、事务、遗忘、缓存和恢复屏障 |
| 7 | [扩展与修改指南](../../../developer-guide/extending.md) | 新增工具、Provider、界面操作及迁移的方法 |
| 8 | [开发环境与验证](../../../developer-guide/development-and-testing.md) | 安装分组、测试分层和证据边界 |

建议先读前三篇，再按改动领域查阅。面向使用者的安装与操作说明在[用户指南](../../../user-guide/README.md)，跨版本约定见[原开发指南](../development-conventions.md)，总体架构见[根架构文档](../../../../ARCHITECTURE.md)。

## 当前基线

生产集成使用 `create_application()`，一个规范 `data_dir` 对应一个实例锁、一个生产 Application 和一个执行 Core worker。GUI 与 CLI 复用该后端。`create_agent()` 保留低层同步兼容契约，不能替代持久生产生命周期。

当前代码已包含静态桌宠、人格/模拟情绪、有界会话、用途化模型网关及独立配置/备份维护。源码 `execution/` 与大量 Windows 诊断脚本属于暂停的隔离实验，尚未接入生产工具执行。Live2D、强制终止、细粒度授权和有限无人值守执行属于后续范围。

本工作区已在本地配置中启用 BGE，模型位于 `data/models/bge-small-zh-v1.5`；代码默认值未改，模型与真实配置不随 Git 分发。实际装配/清单/租约见[存储与维护](../../../developer-guide/storage-and-maintenance.md)，运行环境、独立诊断和[真实链路证据](../../releases/v0.9/managed-embedding-live-report.md)见[开发环境与验证](../../../developer-guide/development-and-testing.md)。

`docs/versions/v0.9` 是产品化增量记录，不能据目录名将包版本改称 0.9。自动化、真实模型、原生桌面和发布验收分别看待，最新证据入口为[产品化报告](../../releases/v0.9/test-report.md)、[HM08 改进记录](../../releases/v0.8/hm08-improvement-report.md)和[桌宠交付记录](../../releases/v0.7/delivery.md)。

## 维护本指南

修改公开入口、工具契约、配置、持久结构或关闭顺序时，同时更新本目录对应主题和[用户指南](../../../user-guide/README.md)。计划文档记录意图，版本报告记录证据；本目录聚焦当前实现，不把历史计划写成已上线行为。代码链接用于快速定位，细粒度 DTO/操作参数仍以对应接口文档和源码为准。
