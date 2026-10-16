# V0.4 开发记录

更新时间：2026-10-15。**开发与整合已完成，代码版本 0.4.0**。此前暂停任务已恢复并收尾，不再有待续办的开发任务或运行中的 benchmark 子进程。Git 未暂存、未提交、未 push；提交由用户处理。

实际范围见 [交付说明](delivery.md)，当前契约见 [接口](interfaces.md)，运行方法见 [操作指南](operations.md)，全部证据与局限见 [测试报告](test-report.md)。

## 已完成

- 新增 workflow 工作库、持久 Task/Submission、Plan/hash、逐步骤账本、版本/用途门禁和保守恢复。
- 新增 Goal/Milestone/SourceBinding、本地 Proposal、原子接受、可靠事件 inbox/cursor/continuation、显式来源复核和有限历史。
- 生产 CLI 使用 ApplicationService、OS 数据目录实例锁与单执行 worker；保留 create_agent 低层兼容行为。
- 遗忘持久阻断、跨库续办、真实 deleted 来源闭包清理、缓存/待投递副本擦除；无关 corrected/expired 历史不误删。
- 未决 Started 为 Uncertain；部分完成仅复核；恢复全部 grant 清空；取消/来源变化不允许后续步骤。
- worker 故障可观测、waiter 不无限挂起；结果缓存按条数/字节有界并隔离 DTO；恢复 Waiting 仍计容量。
- 备份拒绝已有目标，完整快照后原子发布；关闭先停领取并在工具/规则/调度阻塞时保留连接和实例锁。
- requirements 仍引用 pyproject，Windows tzdata 2026.5 已安装，两环境时区 fallback/pip check 通过，Windows 3.12 约束 44 项。
- 更新架构、README、文档导航/路线、接口、运维、交付和测试报告，版本号为 0.4.0。

## 最终验证

双 Python 全套各 **1101 passed、4 skipped**，strict 各 77 文件通过。四个跳过仅是旧真实 API 默认关闭两项和 Windows symlink 权限两项；新增 Windows 实例锁与真实子进程崩溃没有常规 skip。

两个 Python 均完成 1000 Goals/10000 Tasks/20000 attempts/20000 Proposals 容量与分页验证，100 次预热管理操作 p95 均低于 200ms。真实 idle 300s 与开发树 soak 1800s 已结束；最终补丁另有 300s 当前源码负载和最后关闭故障/短测。**不能称最终同一构建已做 30 分钟测量**；准确源版本/时长/CPU口径在报告中单列。

最终 0.4.0 wheel 已在两个独立 target 用 -I 加载、离线流程和 CLI 验证，network_attempts=0。deskpal_env editable 安装已更新为 0.4.0。没有本轮真实云 API 调用，也未复用旧 DeepSeek 限额授权。

## 历史暂停

用户曾要求暂停，子 Agent 与旧 idle/soak session 均精确中断；旧报告 stopped_by_user 不算验收。恢复后重新执行完整时长、补 CLI/安全/关闭测试、合并和安装验证。旧暂停清单上的缓存、worker 故障、配置上界、consumer CLI、cleanup 重启线程、容量/长测、依赖/文档等项均已处理，勿按旧快照重复开工。

## 后续边界

V0.5 Windows UI 等仍未开始。完整 Qt/Live2D、人格情绪、无人值守执行、OS 隔离、跨库任意恢复、外部效果 exactly-once 不属于 V0.4。若需真实云测试，须新的明确有限授权；不要读取/输出个人 .env、配置密钥或私人数据库。下一步 Git 提交仍由用户自行完成。
