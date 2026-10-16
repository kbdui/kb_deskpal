# V0.4 环境、工具与依赖评估

> 实施前评估快照。V0.4 已补 Windows tzdata 并验证双 Python；实际依赖与证据见 [交付说明](../../versions/v0.4/delivery.md)，下文待准备项记录原评估时点。

日期：2026-10-15。状态：**本机准备评估与开发建议，尚未实现 V0.4**。基线为当前工作区的 0.3.0；V0.3 的功能证据见[交付说明](../../versions/v0.3/delivery.md)和[测试报告](../../versions/v0.3/test-report.md)。开发内容见[开发计划](development-plan.md)，通过规则见[验收边界](acceptance-boundaries.md)。

## 结论

本机可以开始 V0.4，无需先安装 Live2D、Qt、CUDA、Redis、独立数据库或新的 Agent 框架。推荐范围是**显式长期目标、本地规则提议、持久任务/计划、串行提交队列和可审查恢复**。新增复杂度主要在状态、事务与崩溃窗口，不在模型规模。

当前开发环境没有阻塞性缺包。V0.4 应补一项可移植性依赖：Windows 的 `tzdata`。当前 Conda 自带 IANA 数据，不能据此认定普通 Windows Python 也具备它。本轮只评估，不安装依赖、不修改 pyproject/requirements、个人配置或真实数据库，也不调用付费 API。

## 本机实查

| 项目 | 2026-10-15 实查 | 对 V0.4 的意义 |
| --- | --- | --- |
| 主环境 | `deskpal_env`，CPython 3.12.4；解释器 `D:/anaconda/Anaconda3/envs/deskpal_env/python.exe` | 继续作为主要开发环境 |
| 最低版本环境 | `data/validation/py311/python.exe`，3.11.17 | 继续验证 Python 3.11 兼容，不使用仅 3.12 才有的接口作为必需路径 |
| SQLite | 两环境均 3.53.4；主环境 `sqlite3.threadsafety=3` | 无需外部数据库服务；代码仍需明确连接所有权与写入串行化 |
| 开发工具 | pytest 9.1.1、mypy 2.4.0、python-dotenv 1.2.4 | 可复用现有单元/集成/类型检查 |
| 既有语义模块 | FastEmbed 0.9.0、ONNX Runtime 1.31.0、模型缓存存在 | 保持 V0.3 兼容；不作为本地目标/提议运行的前提 |
| 时区 | 两环境 `ZoneInfo('Asia/Shanghai')` 成功；`tzdata` 包均不存在，数据来自各环境 `share/zoneinfo` | 当前可开发，部署依赖声明仍有缺口 |
| Windows 锁 | `msvcrt.locking` 存在；实际跨进程互斥、正常释放、`os._exit` 后释放均通过 | 可用标准库做单实例 adapter，仍须单独验收应用接入 |
| 非必需包 | 主环境未发现 PySide6、APScheduler、SQLAlchemy、Pydantic、psutil | 不为本版安装；缺少它们不阻塞开发 |
| V0.3 基线 | 已有报告：两 Python 各 815 passed、4 skipped；strict 检查 56 个源码/脚本通过 | 本轮未重复整套测试；V0.4 开工与交付各自记录新结果 |

没有重新测量当前空闲 RAM、磁盘或 GPU；不沿用旧快照冒充当前容量。V0.4 不应后台常驻加载额外 embedding/聊天模型，启用原有语义功能时仍以其实际资源报告为准。

### 已执行的临时能力探测

两个解释器均在临时目录完成：启用 WAL、外键拒绝无效引用、显式事务回滚、子进程未提交写入后异常退出、重开完整性检查；另以真实子进程验证一字节文件锁的竞争与释放。结果分别保存在本地忽略目录：

- `data/validation/v04/environment-py312.json`
- `data/validation/v04/environment-py311.json`

全部通过，且未打开用户 memory/scheduling 库。这些只证明环境原语可用，**不证明持久任务、应用单实例或 V0.4 恢复已经实现**；恢复验收还需要在产品路径中注入崩溃。

## 依赖选择与维护

| 能力 | 推荐依赖/工具 | 安装判断 |
| --- | --- | --- |
| Goal、Proposal、状态与 DTO | `dataclasses`、`enum`、`typing.Protocol`、`datetime`、`uuid` | 标准库足够 |
| 工作库与提交队列 | `sqlite3` + 显式事务/CAS/唯一约束 | 不引入 ORM、Redis、Celery 或消息中间件 |
| 串行 worker 与唤醒 | `threading`、`queue`、`Event`、`time.monotonic` | 内存队列仅作唤醒，持久队列才是待办权威 |
| Windows 单实例 | `msvcrt.locking` adapter，持有打开的文件句柄 | 当前可零新增包实现；不使用 PID 文件或时间租约冒充互斥 |
| 时区与静默窗口 | `zoneinfo` + Windows 条件依赖 `tzdata` | **V0.4 实施时新增声明并双环境验证** |
| 崩溃与竞争测试 | pytest、`subprocess`、临时目录、可注入时钟/故障点 | 无需管理员权限或真实文件副作用 |
| 原模型/截图 | 继续使用既有 extras | 不更换模型，不扩大后台截图/外发权限 |
| 资源观察 | `time.process_time`/`perf_counter`，Windows 自带资源工具或既有 working-set 采样 | psutil 可以以后按需要选，非必需依赖 |

依赖仍以 [pyproject.toml](../../../pyproject.toml) 为唯一直接声明来源，requirements 文件保持安装入口。建议 V0.4 在 runtime dependencies 中增加 `tzdata; sys_platform == 'win32'`，实施时选择并记录实际验证版本，更新 [Windows 3.12 约束](../../../constraints/windows-py312.txt)；不是现在把未安装版本填进约束。无需另建重复的 requirements-v04 清单。维护规则见[依赖指南](../../guides/dependencies.md)。

普通 Windows 环境可能缺 IANA 数据，Python 官方建议需要该能力的跨平台应用声明 `tzdata`；本次 Conda 的成功不能替代干净环境验证。[Python zoneinfo 数据来源](https://docs.python.org/3.12/library/zoneinfo.html#data-sources)

### 现有安装入口

在仓库根目录使用既有环境即可；下列安装命令是按需复现步骤，本轮未执行：

```powershell
conda activate deskpal_env
python -m pip install -r requirements-dev.txt -c constraints/windows-py312.txt
python -m pip check
```

V0.4 真正增加 tzdata 声明并更新约束后，同一入口会安装所需条件依赖，不要求用户手工维护第二套包版本。只做目标/提议开发不必重装模型、下载权重或提供新 API key。

## 存储、线程与测试环境要求

新增单个 `workflow.sqlite3` 保存 Goal/Proposal/Task/Plan/attempt/提交与消费状态；memory、scheduling 保留独立库。共享数据目录在数据库初始化和 scheduler 启动前取得运行实例锁。相同目录的第二实例快速拒绝；本版不做多进程执行者或分布式调度。

工作库建议显式事务、外键、有限 busy timeout、保守持久化设置；Python 3.11/3.12 的事务配置差异需写清。`check_same_thread=False` 只是放宽检查，不能代替应用级串行策略。[Python sqlite3 连接与事务说明](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.connect)

若采用 WAL，仅在本地文件系统运行并验证 checkpoint/备份策略；WAL 仍只有一个 writer，不会把跨库操作变成原子事务。共享盘、运行中同步数据库文件和任意跨库历史恢复不属于本版支持范围。[SQLite WAL 并发与限制](https://www.sqlite.org/wal.html)

Windows 标准库可以锁定文件的指定字节区域，实际持锁必须贯穿服务生命周期，失败路径也不能提前释放后仍继续执行。应用还需测试同进程重复构造、相对路径/大小写别名、异常启动与退出。[Python msvcrt.locking](https://docs.python.org/3.12/library/msvcrt.html#msvcrt.locking)

## 外部服务和后续环境边界

V0.4 必需的主动提议采用本地确定性规则，**不包含后台云 LLM 生成器**；现有前台 Planner/Reflect 和显式启用的候选学习保持各自语义。Key 已配置不代表后台获准调用。若未来增加云提议，必须另做持久调用额度、超时/token/输入限额、出云资格和崩溃消费规则；当前 LLMResponse 没有 usage，不能承诺精确金额预算。

Qt/托盘/系统通知留 V0.5；受限 worker/强制终止/native IPC 留 V0.6；Live2D SDK/Core/模型许可和图形验证留 V0.7。不需要为 V0.4 预装这些环境。真实模型/API 回归只在相关实现改变时作为独立门槛评估，使用合成文本及新的明确调用预算；本轮不消耗之前已用完的测试授权。
