# V0.3 交付说明

代码版本 **0.3.0**，日期 2026-10-15。本版约定范围的发布门槛已通过，实际证据与测量局限见[测试报告](test-report.md)，可调用参数以[接口契约](interfaces.md)和源码为准。Git 提交由用户处理。

## 交付能力

本版在 V0.2 工具后端上接入来源化、版本化长期记忆：显式记忆先于 LLM 保存，自动提取默认关闭，启用后只产生候选。CAS 审核/更正、七天候选 TTL、时间区间和 revision/epoch 校验共同约束使用资格；画像只投影明确的称呼、回复语言和详略偏好。accepted 不意味着事实已获客观证实。

默认词法检索可独立运行；可选本地 FastEmbed CPU 生成真实 512 维向量，与词法排名融合。检索允许空结果，取消旧 recent fallback，执行完整证据数量/字节预算。模型不可用时显式降级，启动不会下载权重。`local_only` 在云检索前过滤；记忆改动会撤销依赖旧事实的 Waiting 计划及其授权。

SQLite schema v1 自动备份后事务迁移至 v2，旧数据保留为 legacy，不冒充确认事实。忘记操作删除共享来源闭包内的正文、修订、索引和画像，并通过 Core 清理关联进程内 Task。删除回执明确历史日志、备份和外部副本仍可能保留。

## 文件与职责

| 路径 | 交付内容 |
| --- | --- |
| `src/deskpal/memory/contracts.py` | 来源、断言、revision、状态、用途、画像、检索与删除契约 |
| `memory/repository.py` | schema v2、v1 备份迁移、CAS、共享来源删除、幂等与向量持久化 |
| `memory/coordinator.py` | 显式写入、候选学习、审核、更正、导出及旧 MemoryManager 适配 |
| `memory/retrieval.py` | 资格/时间/出云过滤、画像投影、词法/向量融合和完整证据预算 |
| `memory/embedding.py` | FastEmbed cache-only CPU adapter、资产 manifest、长输入拒绝 |
| `memory/evaluation.py` | 单次无工具评估、严格 JSON/quote/画像校验 |
| `agent/core.py`、`agent/models.py`、`tasks/manager.py` | 独立显式记忆、warning/effects、计划依赖失效、Task 擦除 |
| `application/factory.py`、`application/cli.py`、`config` | 注入与资源释放、离线管理命令、记忆配置与预算 |
| `scripts/benchmark_memory.py`、`tests/fixtures/memory-retrieval-zh.json` | 独立校准、held-out 效果、容量和资源测量 |
| `scripts/check_memory_api.py` | 必须显式授权的真实候选/工具反馈检查，有限调用预算 |
| `constraints/windows-py312.txt`、requirements 与 pyproject | 可选依赖与 Windows CPython 3.12 的观察版本约束 |
| V0.3 tests 与 docs | 存储/检索/学习/隐私/生命周期/配置回归及交付证据 |

表中的 `memory/...` 按 `src/deskpal/` 解释；完整变更集合以用户审核工作区 diff 为准。

## 验证与发布状态

- 双 Python 回归、strict mypy、真实模型、中文效果、资源性能和 API 必须分别记录，mock 不代替真实模型或服务成功。
- Python 3.11.17 和 3.12.4 各 **815 passed, 4 skipped**，耗时分别41.06/42.02秒；每环境跳过两个未开启旧 live API、两个 Windows symlink 权限不足用例。两环境严格 mypy 检查 src+两脚本，56 sources 通过。
- Python3.12 pip check 无依赖损坏，43项约束无 mismatch；0.3.0 wheel 构建及两环境 `-I` 从 wheel 安装加载的19工具/记忆/profile/forget/echo流程通过。wheel SHA-256和命令见报告。
- 独立中文 held-out Recall@5=0.8375、无关误召回=0.10；词法 Recall@5=0.625，语义补救 17 条。阈值 0.55 来自独立 24 正/12 负校准集，不得依据 held-out 重调。
- 5,000 条已有向量的容量复测通过：20 条预热完整查询 p95=1.7971 秒（≤2秒），模型/检索峰值增量=297.4 MiB（≤1GiB），进程峰值=331.9 MiB。证据 `data/validation/v03/benchmark-memory-optimized.json`；固定复用 0.55 阈值，无 held-out 重调。旧版 p95 约65秒和索引构建耗时单列历史，不能冒充优化后冷启动数据。
- 本轮真实 DeepSeek API gate 通过：三次调用，每次最多 1024 输出 tokens；最后两次完成候选提取/审核/更正/遗忘和 echo/真实 Reflect。完整流程证据 `data/validation/v03/api-retry-20261015-complete.json` 为 passed=true、calls=2；首次单次提取成功但 scope 不符检查假设，已显式规范并验证。此前三次失败中仅最后一次明确超时，不能写成全超时。

## 明确局限

当前只支持 local owner/global scope 的检索与三项画像；没有多轮 WorkingContext、跨事件派生总结、完整隐身模式、导入器、Goal/Proposal、Qt/Live2D、人格或情绪运行系统。`paused` 只是自动记忆暂停，日志仍有既定行为。提案中的关系画像、衍生 DAG、情绪数值、Presentation DTO 均未交付。

FastEmbed 真实推理只在 Python 3.12 验证；3.11 的离线回归不能证明该环境已安装/验证模型。约束文件固定 43 个观察依赖版本，不含哈希，不保证跨平台或 3.11 完整复现。中文样本为小规模合成项目基准，不代表真实私人对话或公开 benchmark。

数据库删除不是日志/备份/云端擦除或介质安全擦除。迁移备份保留旧原文。记忆、任务、工具、调度与日志没有跨模块原子事务；工具已完成后辅助模型失败不会倒退工具效果。Sandbox 仍是可信内置工具的应用层权限/路径策略，不是 OS 隔离。
