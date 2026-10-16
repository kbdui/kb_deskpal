# V0.4 测试报告

日期：2026-10-15。代码版本 **0.4.0**，约定的 Windows 后端功能验收通过。长测开发树与最终构建的证据范围分别记录，不扩大为干净机、跨平台或 Beta 长期认证。

## 已执行

最终 Python 3.12.4 与 3.11.17 全套均 **1101 passed、4 skipped**，分别 127.27/127.49 秒；两环境全 src 与三个检查/benchmark 脚本 strict 共 **77 文件通过**。最终回归包含关闭边界、新备份/恢复容量/审计崩溃等全部新增测试；没有用各次子集计数相加代替全套执行。

四个跳过为旧真实 API gate 关闭两项、Windows symlink WinError1314 两项。新增实例锁、junction/大小写别名和真实子进程崩溃没有常规 skip。

双环境 pip check 通过。tzdata 2026.5 的无系统 TZPATH 回退可加载 Asia/Shanghai 和 America/New_York。Windows 3.12 约束包含 44 项项目闭包，不是个人 Conda 全量 freeze 或跨平台哈希锁。

## 容量与交互

两个 Python 均真实创建 1000 Goals、10000 Tasks、20000 attempts、20000 Proposals，分页最多 100，复读顺序稳定、SQLite integrity 通过。容量 fixture 实际调用 EchoTool 并记录结果，Started/results 按批事务生成；它验证容量，不声称是逐步骤先提交的 crash 验收，后者另由真实 Application 子进程测试证明。

| 100 次预热 p95（ms） | Python 3.12 | Python 3.11 | 门槛 |
| --- | --- | --- | --- |
| submit | 2.39 | 2.19 | ≤200 |
| accept | 14.63 | 18.23 | ≤200 |
| Goal list | 5.38 | 5.37 | ≤200 |
| Task list | 16.22 | 19.22 | ≤200 |
| Proposal list | 37.67 | 36.55 | ≤200 |

报告：忽略目录 data/validation/v04/workflow-capacity-py312-resumed.json 与 workflow-capacity-py311-resumed.json。本地 SQLite/状态管理耗时不含 LLM、模型推理或用户等待。

## 资源

Windows 11 build 26200、AMD64、16 logical CPU；embedding 关闭、离线 provider、合成独立库。CPU 使用 process_time/monotonic，单逻辑核 100% 口径；内存使用 GetProcessMemoryInfo，不是系统总占用。

5 分钟 idle 已完成：300.000 秒，平均单核 CPU 0.6042%，最大工作集增量 2.328 MiB（包含整个 backend 启动），线程 4→4、句柄 219→208、队列 0，关闭 0.047 秒；通过 ≤2%/≤64MiB/≤5s 门槛。报告 workflow-idle-py312-resumed.json。

30 分钟 soak 已完整结束：monotonic 1800.000 秒，上海时间 20:03:59→20:33:59，60 个周期样本；1366 轮直提与事件建议接受、失败 0，线程 4→4、句柄 218→207（长期稳定 207），活动队列采样峰值 0，关闭 0.109 秒。OS 真实高水位工作集增量 **20.801 MiB**；30 秒采样最大增量 18.730 MiB，不能把后者冒充 OS 峰值。报告 workflow-soak-py312-resumed.json。

持续有请求/事件的 soak 单核 CPU 平均 **96.34%**；≤2% 只针对无事件 idle，这里不宣称忙时低 CPU 或固定吞吐 SLA。持久业务历史增长是预期行为；本次线程/句柄/活动队列未单调增长，不等于 Beta 长期可靠性证明。

完整 soak 启动时载入的是 V0.4 开发树，安装元数据当时为 0.3.0；期间最后补入的缓存隔离、取消竞态、遗忘范围、备份、恢复容量补丁没有热加载。最终源码另做 300.078 秒/275 轮持续负载补测：零失败、线程 4→4、句柄 218→207、队列峰值 0、OS 工作集峰值增量 9.352 MiB、关闭 0.094 秒。报告 workflow-soak-final-smoke.json 明确 acceptance_duration_met=false；不能称最终同一构建已经完成 30 分钟测量。最后关闭路径另由阻塞工具/规则/调度线程的双环境故障测试及安装验证覆盖。

用户暂停前的短样本标 stopped_by_user、acceptance_duration_met=false，不计达标证据。资源报告和 pytest XML 位于忽略目录 data/validation/v04，不包含个人数据。

## 验收矩阵映射

| 编号 | 实际证据 |
| --- | --- |
| V04-01 工作库 | workflow 单测：初始化/重开、schema 拒绝、事务失败/嵌套失败回滚；safety_review：WAL 在线备份、新路径原子发布、失败清理/竞争不覆盖 |
| V04-02/03 目标/证据 | goals/proactive/CLI：revision CAS、execution_version、scope、终态、幂等；Task 完成不改 Goal，用户 evidence 单独完成 |
| V04-04 持久提交 | 并发 command、同键异载荷、忙碌排队、背压、接受事务原子回滚、恢复 Waiting 仍占容量 |
| V04-05 原计划授权 | 精确 hash、工具版本/参数/工作区依赖、DTO/缓存隔离；重启普通/高危 grant 同时清空 |
| V04-06 步骤/恢复 | 真实子进程：Started 前后、效果后结果前、部分成功、全部结果、取消+未知/全部结果；Uncertain 人工复核零重放 |
| V04-07/08 事件/来源 | 100 个交错事件、多轮预算/重启、单事件多 milestone continuation、身份/锚点/回退、TTL/标题/提醒/删除、有限历史重建 |
| V04-09 取消/版本 | 原子 start-step 门禁、cancel 与 claim/version 竞争、同 milestone 与兄弟 scope 区分、运行中取消后不执行后续步骤 |
| V04-10 隐私 | local_only 云 provider 零调用；删除三个真实崩溃窗口、共享来源闭包、无关 corrected/expired 历史保留、Pending 显示/派发/审计/控制回执去正文 |
| V04-11 默认/时间 | 主动默认关闭、首次 tail、跨午夜/DST quiet、过期不集中展示、CLI 边界有界摘要 |
| V04-12 所有权 | Windows 真实跨进程锁、同进程/大小写/junction 别名、异常退出；工具/规则/调度阻塞关闭保连接/锁 |
| V04-13 审计 | worker claim/结果落盘失败可观测且 waiter 有限失败；真实 JSONL append 后 ack 前崩溃仅重投同 event_id，工具/Planner/LLM 均零重放 |
| V04-14 交付 | 双 Python 全套/strict、pip check、时区 fallback、容量/idle/soak、wheel/CLI 隔离验证与文档检查 |

真实审计/inbox 崩溃测试还覆盖 inbox+cursor 已提交、处理前退出，重启只形成一条 Proposal，不创建 Task 或云请求。每项仅声明上述具体用例与合成边界，不推及任意故障下的外部效果 exactly-once。

## 云与发布

本轮没有调用真实云 API，没有复用 V0.3 的有限授权；LLM adapter 协议未改，新增来源/授权门禁用合成 provider 验证。V0.3 真实 DeepSeek 与 embedding 结果只是历史证据，不充作本轮新测试。

正式 wheel 为 `data/validation/v04/wheels/deskpal-0.4.0-py3-none-any.whl`，SHA-256：`3a927042e298800bf58e64db4b76dcc04dc6b337329baa7337dd606fcc80f7c0`。通过 `--no-index --no-deps` 安装到两个独立 target，再用 Python 3.11/3.12 的 `-I` 模式验证实际 __file__ 来自 wheel：19 工具、目录重复 owner 拒绝、Goal 持久化、Memory forget、Waiting 重启/准确 hash 重新确认、文件效果和步骤结果、CLI Goal 查询均通过，network_attempts=0。

安装验证使用已具有 python-dotenv/tzdata 的两个环境，不是全新无依赖 Windows 安装证明。deskpal_env 本地 editable 元数据已更新为 0.4.0，pip check 通过。最后关闭修复另有当前源码五秒 idle smoke（不充作五分钟验收）；正式 wheel 是该修复后的最终代码。

文档相对链接、tracked diff 空白检查和 wheel 源码一致性均在交付前检查；Git 未暂存、未提交、未 push。完整冻结矩阵见 [验收边界](../../planning/v0.4/acceptance-boundaries.md)。
