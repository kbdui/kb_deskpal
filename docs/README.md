# 文档导航

当前代码为 **0.3.0 正式版本**。实际接口/范围以 V0.3 交付目录为准，约定范围的验证已通过，证据与局限见测试报告；规划不是验收结果，历史报告只说明当时证据。

## 推荐阅读顺序

1. [根目录总体架构](../ARCHITECTURE.md)：模块边界、调用方向和安全保证。
2. [V0.3 交付说明](versions/v0.3/delivery.md)、[接口契约](versions/v0.3/interfaces.md)、[测试报告](versions/v0.3/test-report.md)：当前记忆能力、证据与发布门槛；V0.2 目录保留工具契约。
3. [开发约定](guides/development.md)：扩展与测试纪律。
4. [V0.3 环境评估](planning/v0.3/environment-assessment.md)、[开发计划](planning/v0.3/development-plan.md)、[验收边界](planning/v0.3/acceptance-boundaries.md)：实施前目标与当前范围映射。
5. [V0.3 至 V1.0 路线](planning/roadmap.md)：五个大阶段与版本门槛；[Embedding 安装指南](guides/embedding-setup.md)：模型选型、CPU 安装及验证。
6. [依赖清单与维护](guides/dependencies.md)：requirements 分组入口、版本声明与锁定规则。
7. [记忆、人格与情绪机制](planning/companion-model/README.md)：基于 V0.2 的长期设计提案；部分记忆机制已映射到 V0.3 实现，人格、情绪等仍为未来设计。
8. [V0.4 环境评估](planning/v0.4/environment-assessment.md)、[开发计划](planning/v0.4/development-plan.md)、[验收边界](planning/v0.4/acceptance-boundaries.md)：目标、本地提议、持久任务与审查恢复；尚未实现。

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
```

文件名采用小写英文与连字符；版本由目录表示，不再重复加版本前缀。`guides` 保存持续维护的约定，`versions` 保存交付计划、契约及历史证据，`planning` 保存尚未交付的评估和提案。V0.3 实际契约与报告已在 `versions/v0.3`；原 planning 文档保留为规划快照并附状态映射，不能把规划自动视为通过。

Markdown 链接相对于当前文档；正文中的 `src/...`、`docs/...` 等代码路径按仓库根目录解释。原始长期设计资料仍位于仓库根的 `doc/`，本次未移动或重写它们。真实配置、密钥、运行数据和模型缓存不属于文档目录。

## 版本资料

| 版本 | 计划与接口 | 测试证据 | 交付/环境 |
| --- | --- | --- | --- |
| V0.4（规划） | [计划](planning/v0.4/development-plan.md)、[验收边界](planning/v0.4/acceptance-boundaries.md) | 尚未实现/验收 | [环境评估](planning/v0.4/environment-assessment.md) |
| V0.3 | [实际接口](versions/v0.3/interfaces.md)、[原计划](planning/v0.3/development-plan.md) | [测试报告](versions/v0.3/test-report.md) | [交付](versions/v0.3/delivery.md) |
| V0.1 | [计划](versions/v0.1/development-plan.md)、[接口](versions/v0.1/interfaces.md) | [测试计划](versions/v0.1/test-plan.md)、[原报告](versions/v0.1/test-report.md)、[后续补测](versions/v0.1/followup-test-report.md) | [交付](versions/v0.1/delivery.md) |
| V0.2 | [计划](versions/v0.2/development-plan.md)、[接口](versions/v0.2/interfaces.md) | [测试报告](versions/v0.2/test-report.md) | [交付](versions/v0.2/delivery.md)、[开工前环境评估](versions/v0.2/environment-assessment.md) |

V0.2 接口覆盖 V0.1 的双授权、Waiting 原计划确认等变更；未改变的基础 LLM/Tool/Memory 契约仍可追溯到 V0.1。V0.2 环境评估中的“缺少 mss”是开工前状态，实施后的安装与验收以交付报告为准。

2026-10-14 路线调整建议：V0.3 聚焦记忆/用户模型/检索，原草案的目标和主动提议移至 V0.4；V0.3 已交付实现，最终门槛见对应测试报告，V0.4 及后续仍是规划。
