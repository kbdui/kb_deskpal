# V0.3 测试报告

日期：2026-10-15。对应 **0.3.0**，本报告将离线行为、真实本地模型、中文效果、容量及真实云服务分开记录；未完成项不能视为通过。本版约定范围的发布门槛已通过，局限单列。

## 离线与类型检查

| 环境/检查 | 最终证据 |
| --- | --- |
| Python 3.11 完整 pytest | 3.11.17：**815 passed, 4 skipped**，41.06 秒；`data/validation/v03/pytest-py311.xml` |
| Python 3.12 完整 pytest | 3.12.4：**815 passed, 4 skipped**，42.02 秒；`data/validation/v03/pytest-py312.xml` |
| 严格 mypy | Python 3.11/3.12 均通过；`mypy --strict src scripts/benchmark_memory.py scripts/check_memory_api.py`，56 source files |
| 依赖一致性 | Python 3.12 `pip check`: No broken requirements；Windows py312 约束 43 pins 无 mismatch |

实际回归命令为 `python -m pytest -q -ra --junitxml=data/validation/v03/pytest-py312.xml`，3.11 将产物名换为 `pytest-py311.xml`。每环境四个 skip：两个旧 live_api 用例未开启（专用真实 API 脚本另行通过），两个 Windows 符号链接用例因权限不足 WinError 1314 跳过。跳过不算通过；其他路径拒绝/Junction 等行为以已执行测试为准。

主要覆盖：v1 迁移/备份/回滚/未知 schema、来源身份与 quote 校验、候选 TTL/CAS/消费幂等、普通更正与时间更正、共享来源闭包删除、历史/画像/索引同步失效、owner/scope/local_only、as_of、完整证据预算、坏向量/模型降级、Waiting 授权撤销、关联 Task 擦除、显式记忆零模型调用、辅助失败不重做工具、CLI 管理离线无 key、Core 生命周期及 V0.2 工具回归。

默认测试使用临时数据、模拟 Provider/Embedding/HTTP，不读取真实私密库、不自动下载、不采屏、不调用真实云模型。live API 用例跳过应单列，不能计为功能通过。

## 真实本地语义模型

已在 Windows CPython 3.12 使用 FastEmbed 0.9.0、BAAI/bge-small-zh-v1.5、CPUExecutionProvider、四线程运行真实模型，维度 512，归一化/有限值验证。模型来自预先存在的缓存；运行 adapter 与 benchmark 为 local_files_only，不发起自动下载。Python 3.11 未进行真实 FastEmbed 推理认证。

实际 adapter 将资产 SHA-256、query prefix/strip/L2 与 512 token 拒绝策略写入 manifest；不同 manifest 不混用索引。超长断言完整保留在事实库，拒绝语义编码并走词法路径，没有静默截断。另在新进程中将 socket.connect/connect_ex/create_connection 全部设为拒绝，初始化真实 FastEmbed、512 维 query、SQLite 检索与删除通过，network_attempts=0。证据为 `data/validation/v03/cache-offline.json`；主机未物理断网，不宣称系统抓包审计。

## 中文检索效果

使用版本化 `tests/fixtures/memory-retrieval-zh.json`，不含真实私人资料。校准集为 24 正例/12 负例，独立选择阈值 0.55；held-out 为 80 正例/20 负例。冻结后不依据评估结果重调阈值或重写标签。

| 指标 | 词法 | 词法+真实语义 | 门槛 |
| --- | --- | --- | --- |
| 正例 Recall@5 | 0.625 | 0.8375 | ≥0.80 |
| 无关例误召回接受率 | 0.05（1/20） | 0.10（2/20） | ≤0.10 |
| 语义补救正例 | — | 17/80 | 描述性指标 |

质量证据为 `data/validation/v03/benchmark-memory-v1.json`；fixture SHA-256 为 `2b5fe272b64fe04d8eb4e673f3b5322f9843a3e96a84a094b597f48df01eca1d`。该报告记录校准候选阈值、固定 hybrid 评估仅一次及全部逐题结果。模型 manifest 为 `fastembed-0.9.0:cpu:assets-sha256=576c5dfb0a285fe8420ed36f17e31346576223bf7725521a9e9a0ff272ff74fd:strip-v1:query-prefix-zh-v1:l2-v1:token-limit-512-reject-v1`。仅此小合成集上达到质量目标，不能推及所有中文事实与多语言长文。

## 容量、延迟与内存

门槛为 5,000 条记忆预热完整查询 p95 ≤2 秒、模型/检索增量峰值内存 ≤1 GiB。优化后两项均通过。证据为 `data/validation/v03/benchmark-memory-optimized.json`，复用合成库 5,000 条已有索引、固定阈值 0.55；没有重新运行 held-out 或重选阈值。

| 指标 | 最终实测 |
| --- | --- |
| 环境 | Windows 11 build 26200、CPython 3.12.4、16 logical CPU |
| 模型加载 | 1.5501 秒 |
| 首次完整检索（已有索引） | 1.6428 秒 |
| 预热完整检索 | 20 queries；p50 1.5692 秒，p95 **1.7971 秒**，max 1.8342 秒 |
| 进程峰值 working set | 348,041,216 bytes（约 331.9 MiB） |
| 模型+检索峰值增量 | 311,889,920 bytes（约 **297.4 MiB**） |
| 首次索引构建（旧完整报告） | 5,000 条 221.0253 秒；合成数据写入 40.5319 秒，非优化后重新构建 |
| query embedding（24 条质量运行） | 首次 0.00631 秒，非 5,000 条完整检索耗时 |

CPU 只确认 AMD64 Family 25 Model 80 Stepping 0 AuthenticAMD / 16 logical CPU；系统硬件查询被拒绝，当前报告不推断 CPU 商品名或总内存。内存方法为 Windows GetProcessMemoryInfo 进程 high-water working set，增量基于模型加载前进程基线；不是整个系统内存，也不是独立逐阶段硬件峰值。

初版 `benchmark-memory-v1.json` 预热 p95 约 65 秒、首次完整检索 59.26 秒未通过；`benchmark-memory-index-baseline.json` 为中间优化，仍未过门槛。最终优化将重复逐条 SQL 读取改为批量 JOIN，在单次查询内复用记录和向量快照，并合并向量归一化与点积；没有跨查询内容缓存。每次仍检查资格、时间、revision 和 epoch，独立隐私/更正/删除回归不能省略。容量通过只限本次 5,000 条合成数据、已有向量与 20 条预热查询，不表示无界规模或冷启动 SLA。压力查询自身的 Recall 不替代独立 80/20 质量集。

本次性能复测的实际参数：

```powershell
python scripts/benchmark_memory.py --stress-only --threshold 0.55 --resume-synthetic data/validation/v03/synthetic-stress-v1.sqlite3 --output data/validation/v03/benchmark-memory-optimized.json
```

`--resume-synthetic` 只接受该脚本生成的合成库，不复用用户真实 memory.sqlite3。质量完整运行和性能复测分离；以上增量模型资源已低于 1 GiB，warm p95 已低于 2 秒。

## 真实云 API

脚本 `scripts/check_memory_api.py --live` 需要单独明确授权，最多三次请求；本次重试授权上限为三次、每次最多 1024 输出 tokens。此前三个真实调用均失败：前两次只记录通用错误，最后一次明确为 LLM request timed out；不能全部写成超时。不把 mock 的严格 JSON/CAS 测试算作真实抽取成功。

本轮 DeepSeek 真实 API gate **通过**：共三次真实请求，每次上限 1024 输出 tokens。第一请求已成功提取候选，但 scope 不符合检查脚本假设；随后 Evaluator 提示明确无范围限定偏好使用 global，脚本也显式核验 scope。余下两次完成真实候选提取→审核→更正→遗忘，以及 echo 本地执行/真实 Reflect。最终脱敏证据 `data/validation/v03/api-retry-20261015-complete.json` 中 passed=true、calls=2；这是完整流程该轮两次，不能混作本轮总请求数。API 检查不记录 key、请求头或私人文本。

## 打包与安装验证

正式版本 `0.3.0` wheel 使用 `--no-deps --no-build-isolation` 成功构建，产物为 `data/validation/v03/wheels/deskpal-0.3.0-py3-none-any.whl`，SHA-256 为 `fd2f69fe01da1acccfb7fbbe343420ad5da94ea9f4107d738508db76a00686de`。随后 `--no-index --no-deps` 安装到独立 target，Python 3.11/3.12 都以 `-I` 隔离模式从 wheel 的真实 `__file__` 加载，完成 19 工具注册、记忆/profile/forget 和 echo 流程。

此验证证明 wheel 内容和已具依赖环境中的安装入口，不能称为全新无依赖 Windows 机器的一键安装测试。模型缓存、个人配置和运行库不是 wheel 包内容。

## 结论与局限

0.3.0 在约定范围内通过双 Python 离线回归、严格类型检查、Python 3.12 真实 CPU embedding、固定 held-out 质量、5,000 条预热资源门槛、缓存网络阻断检查、有限预算真实 DeepSeek 流程及 wheel 验证。未测范围包括 Python 3.11 真实模型、物理断网抓包、跨平台/干净机依赖复现、无界容量、多轮 WorkingContext、完整隐身、跨事件总结、导入、Goal/UI/人格/情绪。详细范围见[交付说明](delivery.md)和[接口契约](interfaces.md)。
