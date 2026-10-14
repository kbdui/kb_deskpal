# 文档导航

当前代码基线为 **V0.2**。V0.3 文档是下一阶段的建议与验收草案，尚未实现；历史测试报告只说明对应版本当时的证据。

## 推荐阅读顺序

1. [根目录总体架构](../ARCHITECTURE.md)：模块边界、调用方向和安全保证。
2. [V0.2 交付说明](versions/v0.2/delivery.md)与[接口契约](versions/v0.2/interfaces.md)：当前可用功能和 API。
3. [开发约定](guides/development.md)：扩展与测试纪律。
4. [V0.3 环境评估](planning/v0.3/environment-assessment.md)、[开发计划](planning/v0.3/development-plan.md)、[验收边界](planning/v0.3/acceptance-boundaries.md)：下一阶段准备。

## 分类与命名规则

```text
docs/
  README.md                         文档入口
  guides/
    development.md                  跨版本开发约定
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
  planning/
    v0.3/
      environment-assessment.md
      development-plan.md
      acceptance-boundaries.md
```

文件名采用小写英文与连字符；版本由目录表示，不再重复加版本前缀。`guides` 保存持续维护的约定，`versions` 保存交付计划、契约及历史证据，`planning` 保存尚未交付的评估和提案。未来实际开工时先冻结契约；交付后将版本规划归入 `versions/v0.3` 并补充实际测试/交付报告，不能把规划自动视为通过。

Markdown 链接相对于当前文档；正文中的 `src/...`、`docs/...` 等代码路径按仓库根目录解释。原始长期设计资料仍位于仓库根的 `doc/`，本次未移动或重写它们。真实配置、密钥、运行数据和模型缓存不属于文档目录。

## 版本资料

| 版本 | 计划与接口 | 测试证据 | 交付/环境 |
| --- | --- | --- | --- |
| V0.1 | [计划](versions/v0.1/development-plan.md)、[接口](versions/v0.1/interfaces.md) | [测试计划](versions/v0.1/test-plan.md)、[原报告](versions/v0.1/test-report.md)、[后续补测](versions/v0.1/followup-test-report.md) | [交付](versions/v0.1/delivery.md) |
| V0.2 | [计划](versions/v0.2/development-plan.md)、[接口](versions/v0.2/interfaces.md) | [测试报告](versions/v0.2/test-report.md) | [交付](versions/v0.2/delivery.md)、[开工前环境评估](versions/v0.2/environment-assessment.md) |

V0.2 接口覆盖 V0.1 的双授权、Waiting 原计划确认等变更；未改变的基础 LLM/Tool/Memory 契约仍可追溯到 V0.1。V0.2 环境评估中的“缺少 mss”是开工前状态，实施后的安装与验收以交付报告为准。
