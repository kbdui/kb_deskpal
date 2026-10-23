# 文档导航

当前代码为 **0.5.0rc1**，加入 GUI 与后台交互；V0.4 持久后端继续保留。候选版实际范围与验收缺项见 [V0.5 状态](versions/v0.5/delivery.md)和[测试报告](versions/v0.5/test-report.md)；历史报告只说明当时证据，规划不是验收结果。

**切换聊天：**V0.6 以[2026-10-19 14:57 暂停检查点](versions/v0.6/pause-checkpoint-2026-10-19-1457.md)和[执行环境架构重评](versions/v0.6/execution-environment-architecture-review-2026-10-19.md)为最新边界；阶段0仍未通过，尚未接入生产执行，不恢复封存实验。更早的[交接](handoffs/2026-10-17-v0.6.md)和恢复记录仅作历史参考。

2026-10-21：V0.7「静态图片桌宠表现层」已实现，按用户要求带未决项[归档](versions/v0.7/delivery.md)，未全部验收通过；Live2D 延至正式版后的更新。[验收边界](planning/v0.7/acceptance-boundaries.md)、[开发计划](planning/v0.7/development-plan.md)和[环境评估](planning/v0.7/environment-assessment.md)记录范围，[接口](versions/v0.7/interfaces.md)、[运行说明](versions/v0.7/operations.md)和[测试报告](versions/v0.7/test-report.md)记录实现及证据。[内存简析](versions/v0.7/memory-review-2026-10-21.md)与[后续测试清单](versions/v0.7/deferred-tests.md)保留收尾判断和未执行项。原 Live2D 提案/调研保留为历史；不改变0.5.0rc1版本或V0.6暂停状态。

2026-10-21：V0.8「人格、情绪与文本风格」完成实现后，用户授权[最小T01–T09](versions/v0.8/minimal-test-plan.md)。新增76项定向、修补后主环境119项及兼容171项通过，strict106源文件通过；T07并发受理和两项符号链接权限仍有边界，见[测试报告](versions/v0.8/test-report.md)。[开发计划](planning/v0.8/development-plan.md)、[验收边界](planning/v0.8/acceptance-boundaries.md)、[接口](versions/v0.8/interfaces.md)、[操作说明](versions/v0.8/operations.md)及[开发记录](versions/v0.8/development-status.md)区分规划、实际契约及证据，不宣称完整版本验收。

## 推荐阅读顺序

2026-10-22 新增[拟人化优化方案](planning/v0.8/humanization-proposal.md)、[代码组织与状态协议](planning/v0.8/humanization-architecture.md)和[实施/验收计划](planning/v0.8/humanization-delivery-plan.md)。主 Agent 独立完成设计后，已组织实现至 H5 并准备 H6 验收材料，见[源码开发记录](versions/v0.8/humanization-development-status.md)。用户已授权集中执行[最小 HM01–HM08](planning/v0.8/humanization-minimal-test-plan.md)；首败、修补、定向复测及真实模型未通过项见[本轮测试记录](versions/v0.8/humanization-test-report.md)，不宣称整体通过。桌宠仅预留接口。

1. [根目录总体架构](../ARCHITECTURE.md)：模块边界、调用方向和安全保证。
2. [V0.4 交付说明](versions/v0.4/delivery.md)、[接口契约](versions/v0.4/interfaces.md)、[操作指南](versions/v0.4/operations.md)、[测试报告](versions/v0.4/test-report.md)：持久目标/任务、来源用途与恢复边界；V0.3 目录保留记忆契约，V0.2 保留工具契约。
3. [开发约定](guides/development.md)：扩展与测试纪律。
4. [V0.3 环境评估](planning/v0.3/environment-assessment.md)、[开发计划](planning/v0.3/development-plan.md)、[验收边界](planning/v0.3/acceptance-boundaries.md)：实施前目标与当前范围映射。
5. [V0.3 至 V1.0 路线](planning/roadmap.md)：五个大阶段与版本门槛；[Embedding 安装指南](guides/embedding-setup.md)：模型选型、CPU 安装及验证。
6. [依赖清单与维护](guides/dependencies.md)：requirements 分组入口、版本声明与锁定规则。
7. [记忆、人格与情绪机制](planning/companion-model/README.md)：基于 V0.2 的长期设计提案；部分记忆机制已映射到 V0.3 实现，人格、情绪等仍为未来设计。
8. [V0.4 环境评估](planning/v0.4/environment-assessment.md)、[开发计划](planning/v0.4/development-plan.md)、[验收边界](planning/v0.4/acceptance-boundaries.md)：实施前规划快照，实际证据以 versions/v0.4 为准。
9. [V0.5 操作指南](versions/v0.5/operations.md)、[接口](versions/v0.5/interfaces.md)、[状态](versions/v0.5/delivery.md)、[测试](versions/v0.5/test-report.md)：Windows GUI 候选版；[原环境评估](planning/v0.5/environment-assessment.md)、[计划](planning/v0.5/development-plan.md)、[验收边界](planning/v0.5/acceptance-boundaries.md)保留为实施前快照。
10. [V0.6 环境评估](planning/v0.6/environment-assessment.md)、[开发计划](planning/v0.6/development-plan.md)、[验收边界](planning/v0.6/acceptance-boundaries.md)：用户已要求[保存进度并暂停](versions/v0.6/pause-checkpoint-2026-10-19-1457.md)，阶段0仍未通过，尚未接入生产执行；[架构重评](versions/v0.6/execution-environment-architecture-review-2026-10-19.md)是后续路线建议，不是实际迁移批准。[进度](versions/v0.6/development-status.md)、[证据与剩余门槛](versions/v0.6/stage0-evidence.md)、[runtime 调查](versions/v0.6/runtime-investigation.md)、[权限静态审查](versions/v0.6/runtime-security-review.md)与[工具迁移表](versions/v0.6/tool-migration.md)记录实际范围。
11. [V0.7 验收边界](planning/v0.7/acceptance-boundaries.md)、[开发计划](planning/v0.7/development-plan.md)、[环境评估](planning/v0.7/environment-assessment.md)：可选静态PNG桌宠、透明/拖动/点击、中性状态、DPI/多屏、加载/窗口生命周期与资源验收；人格情绪留 V0.8，Live2D 留正式版后更新。
12. [2026-10-20 研究适配与测试取舍](planning/v0.7/research-and-test-scope-2026-10-20.md)：Coopanion/Cortico报告对V0.7的适用边界、推荐五张静态表情差分、状态防闪烁和精简T01–T08测试矩阵；不是已实现/已测试记录。
13. [V0.8 开发计划](planning/v0.8/development-plan.md)、[验收边界](planning/v0.8/acceptance-boundaries.md)、[接口](versions/v0.8/interfaces.md)、[开发记录](versions/v0.8/development-status.md)、[最小测试报告](versions/v0.8/test-report.md)：默认关闭的声明式人格、有限情绪、显式偏好、后台管理与静态映射；测试范围与限制独立记录。
14. [V0.8 拟人化优化提案](planning/v0.8/humanization-proposal.md)：多轮上下文、自然语言情境、稳定表达和可控偏好；[架构](planning/v0.8/humanization-architecture.md)与[实施计划](planning/v0.8/humanization-delivery-plan.md)保留设计依据，[开发记录](versions/v0.8/humanization-development-status.md)记录 H0–H6 材料。按[最小 HM01–HM08](planning/v0.8/humanization-minimal-test-plan.md)集中测试的实际结果见[本轮报告](versions/v0.8/humanization-test-report.md)；真实模型体验仍未验收通过。
15. [HM08 改进与新测试计划](planning/v0.8/hm08-improvement-plan.md)、[实现与验证结果](versions/v0.8/hm08-improvement-report.md)：2026-10-23 按实际首败继续修补协议、范围、草稿事实和工具补充，加入显式原生 JSON 通道；离线复测通过，真实模型问题按用户要求列入[暂缓项](versions/v0.8/hm08-deferred-issues.md)，未整体验收。

## 分类与命名规则

```text
docs/
  README.md                         文档入口
  guides/
    development.md                  跨版本开发约定
    dependencies.md                 依赖分组、安装与维护
    embedding-setup.md              中文向量模型安装与验证
  versions/
    v0.1/
      development-plan.md
      interfaces.md
      test-plan.md
      test-report.md
      followup-test-report.md
      delivery.md
    v0.2/
      development-plan.md
      interfaces.md
      environment-assessment.md
      test-report.md
      delivery.md
    v0.3/
      interfaces.md
      test-report.md
      delivery.md
    v0.4/
      interfaces.md
      operations.md
      test-report.md
      delivery.md
      development-status.md           开发交接及恢复记录
    v0.5/
      interfaces.md
      operations.md
      test-report.md
      delivery.md
      windows-checklist.md            实际屏幕与系统交互验收
      development-status.md           开发交接及暂停记录
      stability-investigation.md      低频停滞调查与测试屏障证据
    v0.6/
      development-status.md           阶段0实际进度与恢复点
      runtime-investigation.md         受限启动与持久写入边界调查
      runtime-security-review.md       文件/注册表权限盲点及实验提案
      stage0-evidence.md               实机证据、覆盖缺口及依赖顺序
      tool-migration.md               19个工具的目标执行类别
  planning/
    roadmap.md                      V0.3 至 V1.0 分阶段路线
    companion-model/
      README.md                     记忆、人格、情绪总体设计
      memory.md                     记忆来源、修订、检索与删除
      personality-emotion.md        人格、关系、情绪与表达
      integration-and-acceptance.md 集成契约、阶段与验收
    v0.3/
      environment-assessment.md
      development-plan.md
      acceptance-boundaries.md
    v0.4/
      environment-assessment.md
      development-plan.md
      acceptance-boundaries.md
    v0.5/
      environment-assessment.md
      development-plan.md
      acceptance-boundaries.md
    v0.6/
      environment-assessment.md       本机盘点与Windows隔离选型
      development-plan.md            A执行隔离→B本地自动规则
      acceptance-boundaries.md       真进程/权限/停止/恢复门槛
    v0.7/
      environment-assessment.md       既有Qt、静态PNG、线程与资源环境
      development-plan.md            状态契约→图片窗口→交互→实机验收
      acceptance-boundaries.md       表现层边界与待执行验收矩阵
      live2d-*-2026-10-19.md         延至正式版后的历史提案与调研
      evidence/                     无敏感数据的环境与文档检查记录
```

文件名采用小写英文与连字符；版本由目录表示，不再重复加版本前缀。`guides` 保存持续维护的约定，`versions` 保存交付计划、契约及历史证据，`planning` 保存尚未交付的评估和提案。V0.3 实际契约与报告已在 `versions/v0.3`；原 planning 文档保留为规划快照并附状态映射，不能把规划自动视为通过。

Markdown 链接相对于当前文档；正文中的 `src/...`、`docs/...` 等代码路径按仓库根目录解释。原始长期设计资料仍位于仓库根的 `doc/`，本次未移动或重写它们。真实配置、密钥、运行数据和模型缓存不属于文档目录。

## 版本资料

| 版本 | 计划与接口 | 测试证据 | 交付/环境 |
| --- | --- | --- | --- |
| V0.8 最小测试已执行 | [开发计划](planning/v0.8/development-plan.md)、[接口](versions/v0.8/interfaces.md) | [实际报告](versions/v0.8/test-report.md)、[最小计划](versions/v0.8/minimal-test-plan.md)，有明确边界 | [开发记录](versions/v0.8/development-status.md)、[运行说明](versions/v0.8/operations.md) |
| V0.7 静态桌宠归档 | [开发计划](planning/v0.7/development-plan.md)、[验收边界](planning/v0.7/acceptance-boundaries.md) | [实际报告](versions/v0.7/test-report.md)；T08未整体通过 | [归档](versions/v0.7/delivery.md)、[后续测试](versions/v0.7/deferred-tests.md)、[环境评估](planning/v0.7/environment-assessment.md)；Live2D延至正式版后 |
| V0.6 阶段0 | [开发计划](planning/v0.6/development-plan.md)、[验收边界](planning/v0.6/acceptance-boundaries.md) | 原型开发中，未通过完整隔离/自动规则验收 | [进度](versions/v0.6/development-status.md)、[工具迁移表](versions/v0.6/tool-migration.md)、[原评估](planning/v0.6/environment-assessment.md) |
| V0.5 rc1 | [接口](versions/v0.5/interfaces.md)、[操作指南](versions/v0.5/operations.md)、[验收边界](planning/v0.5/acceptance-boundaries.md) | [实际报告](versions/v0.5/test-report.md)、[Windows 实机记录](versions/v0.5/windows-checklist.md)、[稳定性调查](versions/v0.5/stability-investigation.md) | [候选状态](versions/v0.5/delivery.md)、[进度交接](versions/v0.5/development-status.md)、[原评估](planning/v0.5/environment-assessment.md) |
| V0.4 | [实际接口](versions/v0.4/interfaces.md)、[操作指南](versions/v0.4/operations.md)、[原计划](planning/v0.4/development-plan.md) | [测试报告](versions/v0.4/test-report.md) | [交付](versions/v0.4/delivery.md)、[环境评估](planning/v0.4/environment-assessment.md) |
| V0.3 | [实际接口](versions/v0.3/interfaces.md)、[原计划](planning/v0.3/development-plan.md) | [测试报告](versions/v0.3/test-report.md) | [交付](versions/v0.3/delivery.md) |
| V0.1 | [计划](versions/v0.1/development-plan.md)、[接口](versions/v0.1/interfaces.md) | [测试计划](versions/v0.1/test-plan.md)、[原报告](versions/v0.1/test-report.md)、[后续补测](versions/v0.1/followup-test-report.md) | [交付](versions/v0.1/delivery.md) |
| V0.2 | [计划](versions/v0.2/development-plan.md)、[接口](versions/v0.2/interfaces.md) | [测试报告](versions/v0.2/test-report.md) | [交付](versions/v0.2/delivery.md)、[开工前环境评估](versions/v0.2/environment-assessment.md) |

V0.2 接口覆盖 V0.1 的双授权、Waiting 原计划确认等变更；未改变的基础 LLM/Tool/Memory 契约仍可追溯到 V0.1。V0.2 环境评估中的“缺少 mss”是开工前状态，实施后的安装与验收以交付报告为准。

2026-10-16 状态：V0.3/V0.4 已有交付记录；V0.5 为实现后的 rc1，自动化与实机缺项分别记录。历史计划不代替实际接口与测试报告。
