# 文档导航

当前基线：2026-10-30，Phase 01–05 已实现，Phase 06 待实施。各阶段的定向验证记录只证明对应范围；最新全量检查状态见[整理记录](maintenance/2026-10-30-repository-cleanup.md)。

| 分类 | 入口 | 内容 |
|---|---|---|
| 使用 | [用户指南](user-guide/README.md) | 启动、模型配置、角色与聊天、任务、记忆、维护 |
| 手机访问 | [使用方法](user-guide/mobile-access.md)、[实现边界](developer-guide/mobile-access.md) | 单用户网页、配对认证、局域网/FRP 连接和共享后台 |
| 开发 | [开发者指南](developer-guide/README.md) | 模块、执行链路、存储、扩展及测试纪律 |
| 设计约定 | [28 日完整讨论](planning/companion-model/2026-10-28-character-memory-redesign-discussion.md)、[29 日确认结论](planning/companion-model/2026-10-29-redesign-follow-up-decisions.md) | 原路径恢复完整原文；后续补充与阅读说明见[目录](planning/companion-model/README.md)，摘要不替代原文 |
| 实施 | [六阶段开发与验收](planning/companion-redesign/README.md) | P1–P5 实施证据、P6 后续工作、公共契约与数据库设计 |
| 按需整理规划 | [quiet 与记忆整理](planning/companion-model/2026-11-12-quiet-and-memory-consolidation.md) | 已确认待实现：普通安静结束跳过学习、后台整理例外、未整理材料补齐与每轮流程 |
| QQ 插件规划 | [QQ 插件开发设计](planning/plugins/2026-11-09-qq-plugin-design.md) | 主体保持一对一；插件数据隔离、复用执行链及 quiet 收尾、局部整理和选择性投递经历 |
| 通用多人备选 | [历史接入设计](planning/companion-model/2026-11-11-multi-person-integration-design.md) | 已由插件隔离路线替代；人物归属等设计可下沉插件，不再作为主体改造与迁移任务 |
| 持续报告 | [实施日志](reports/implementation-log.md) | GUI 优化及后续短报告统一追加，按日期记录 |
| 长期方向 | [路线图](roadmap/README.md) | 当前收尾与正式版后拓展 |
| 维护记录 | [本次整理](maintenance/2026-10-30-repository-cleanup.md)、[测试目录](../tests/README.md) | 路径变更、退役依据、测试现状 |
| 历史 | [存档导航](archive/README.md) | 旧版本、审计、交接、讨论原文及未通过的实验 |

分类规则：用户怎么用写入 user-guide，代码怎么接写入 developer-guide，已确认产品含义写入 design，阶段任务和验收写入 planning。短报告统一更新 implementation-log；只有一次性维护记录另存 maintenance。文件采用英文小写 `kebab-case.md`；日期用于一次性记录，稳定指南不把日期写进文件名。

设计约定以完整讨论、确认结论及用户后续明确决定为依据，摘要和实施记录不能自行改写约定；仍未验证的事项保留未验证状态。不要把存档中的真实资源路径、旧测试计数或封存对象当成可以再次操作的授权。
