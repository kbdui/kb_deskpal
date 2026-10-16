# V0.6 环境、工具与依赖评估

日期：2026-10-16，Asia/Shanghai。状态：**开发前评估，未实现、未通过隔离验收**。基线为当前工作区 `0.5.0rc1`，并非假定已提交的 main；用户自行处理 Git。开发范围见[计划](development-plan.md)，发布条件见[验收边界](acceptance-boundaries.md)。

## 结论

现有 Windows/Python/Qt 环境足以开始设计和原型。V0.6 的新增重点是 Windows 执行隔离与能力授权，不需要 Live2D、CUDA、新 embedding、向量数据库、消息队列或新的模型 API。**目前不能声称本机已具备通过验证的安全 worker 环境**：受限 Python 启动、IPC、Job 限制、运行时目录 ACL 和实际文件/网络拒绝均须先做原型。

建议分为 A「隔离执行与精细授权」和 B「有限本地自动行为」两个连续门槛。A 没有通过，B 保持关闭；只完成 A 时交付阶段成果，不称完整 V0.6。V0.5 未结实机项和 Python 3.11 首次停滞调查继续跟踪，不能通过版本升级消账。

## 当前实现的起点

| 已有实现 | V0.6 所需变化 |
| --- | --- |
| `tools/manager.py` 同进程调用 `Tool.execute()`；`sandbox/workspace.py` 明确仅为可信工具应用策略 | 增加 ToolExecutor/Windows WorkerLauncher，工具注册和权限判定继续由宿主管理 |
| `ToolContext` 保存普通/高风险工具名集合，持久原计划 hash 已有 | 引入绑定 task/call/参数/资源/策略版本/期限的能力租约；名称白名单不足以长期自动授权 |
| `agent/core.py` 在执行前写 attempt、返回后写结果；已有 Uncertain/NeedsReview | 补启动、broker 操作、结果落账、终止和进程回收证据，保留副作用未知状态 |
| 唯一 Core 线程、独立 GUI 控制通道、协作取消与 Closing 保锁 | 独立 supervisor 控制 worker，GUI 不等待进程阻塞；确认回收后才报告停止完成 |
| 日程/计时器持有 SchedulingService，记忆和目标持有宿主数据库 | 使用窄 broker 操作，不把数据库对象、SQLite 连接或全部配置送入 worker |
| 云调用、embedding、记忆候选处理仍在宿主链路 | 本版不得把工具进程可停止表述为所有同步模型/存储调用可强停 |

## Windows 路线选择

首选原型是 **AppContainer/可行时 LPAC + Job Object + 窄能力 broker**。AppContainer 提供按身份与能力约束的 OS 访问控制；普通 AppContainer 仍可访问部分公共资源，LPAC 更严格，具体可用资源必须实测。不能宣称“worker 对整台机器零读取”。见 Microsoft [隔离说明](https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-isolation)与[启动 AppContainer/LPAC](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)。

Job 用于进程组终止、内存/CPU/进程数限制和宿主退出清理；它不能独立替代文件、网络及令牌权限边界。拟采用挂起创建、加入 Job 并设置限制、核验 token 后才允许运行业务；禁止 breakaway。见 [Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)、[创建标志](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags)。

受限 token/低完整性单独作为对照方案，不自动获得本项目要求的网络和读取隔离保证。Windows Sandbox/独立 VM 用于测试，不作为产品必须启动的执行后端；WSL/Docker 不承担 Windows 桌面能力隔离。若首选原型失败，必须重新评审方案或保持功能不可用，禁止静默退回普通子进程并显示为“安全”。

## 本机只读盘点

本轮由6 Luna执行固定环境查询，主Agent核对用途与限制；没有借此执行隔离测试。

| 项目 | 实际观察 | 能说明什么 |
| --- | --- | --- |
| Windows / 位数 | 版本接口返回10.0.26200.9711，.NET返回10.0.26200.0；64位进程 | 与既有Windows 11 build26200证据一致；CIM OS查询被拒，未补推系统SKU |
| 当前核验权限 | 该命令进程不在管理员角色 | 工具运行环境受限，不能断言真实桌面用户权限，更不能当作产品标准用户启动通过 |
| 主环境 | `D:\anaconda\Anaconda3\envs\deskpal_env\python.exe`，CPython3.12.4 / AMD64 | 版本/架构查询与pip check均exit0 |
| 兼容环境 | `data/validation/py311/python.exe`，CPython3.11.17 / AMD64 | 版本/架构查询与pip check均exit0 |
| 编译工具 | CMake3.28.0-rc3可运行；ninja和cl不在PATH | CMake是预发布构建，不作为新稳定构建锁；不需要仅为评估升级 |
| MSVC | vswhere可运行，发现 `E:\no_game\vsGenerateTool\BuildTools` 和14.44.35207 x64 cl.exe | 发现C++组件/路径，未编译链接；以后在Developer环境验证，不全局改PATH |
| Windows SDK | Include/Lib下存在10.0.22621.0、10.0.26100.0及UM/UCRT目录 | 未验证闭包和实际链接，不等于native helper已构建通过 |
| 测试设施 | 沿用本机100%/200%双屏证据 | 本轮未核验VM、快照、独立标准用户或对抗环境，列为待准备/待确认 |

| 包元数据 | 主Python3.12 | Python3.11 |
| --- | --- | --- |
| python-dotenv / tzdata | 1.2.4 / 2026.5 | 1.2.4 / 2026.5 |
| PySide6-Essentials / shiboken6 | 6.12.0 / 6.12.0 | 6.12.0 / 6.12.0 |
| pytest / pytest-qt / mypy | 9.1.1 / 4.5.0 / 2.4.0 | 9.1.1 / 4.5.0 / 2.4.0 |
| fastembed / onnxruntime / mss | 0.9.0 / 1.31.0 / 10.2.0 | 未安装这些optional extras |
| pywin32 / psutil | 未安装 / 未安装 | 未安装 / 未安装 |

两环境Qt/shiboken实际导入成功，主环境fastembed/onnxruntime/mss导入成功，检查exit0；未验证本轮真实推理/模型缓存/截图权限。3.11缺少这些extras不阻止执行隔离开发，不为凑齐包列表安装模型/截图库。

依赖文件已核实：`constraints/windows-py312.txt`为44条有效pin；`constraints/windows-py312-gui.txt`继承它并增加3条GUI pin。它们存在且可读，不是哈希锁，也不是3.11或V0.6执行依赖的安装保证。

## 依赖分组与准备方式

| 项目 | 判断与行动 |
| --- | --- |
| Python 3.11/3.12 x64、标准库 | 保留支持；`ctypes`、JSON、hashlib、SQLite 等可复用。原型优先将 Win32 绑定集中在一个 adapter，检查 ABI、结构体对齐和句柄所有权 |
| pywin32 | 候选适配依赖，可简化部分 Job/token/ACL 调用；不预设它覆盖全部启动属性。阶段0决定是否采用并核验两个 Python wheel；如采用，只加 Windows execution extra，不加入所有后端用户的基础依赖。项目来源见 [PyPI](https://pypi.org/project/pywin32/) |
| MSVC x64 与 Windows SDK | 仅选择薄 native launcher/helper 或构建对抗探针时成为构建前提；发现安装目录不等于编译、链接、受限启动已成功 |
| 专用最小 worker runtime | 必须补准备与 manifest；只含解释器、必要标准库/内置工具模块。不能给整个个人 Conda、仓库、模型缓存或工作区放宽 ACL 来解决启动错误 |
| PySide6-Essentials、pytest-qt | 沿用 V0.5；只在宿主/GUI 测试环境，worker 不导入 Qt、不获得桌面句柄 |
| pytest、mypy | 沿用；增加 Windows 真进程与 broker 故障注入测试，失败退出码/日志由 supervisor 保留 |
| psutil、Process Explorer/ProcMon | 可选诊断工具；资源判据优先用 Job/进程实际计数，不把监测库当作限制机制。无须为评估安装 |
| mss / fastembed / onnxruntime | 保持既有 optional；mss 只能经显式桌面代理，embedding 不放入首版执行 runtime，不新增模型下载 |

`subprocess` 可以管理子进程，但创建普通进程不是安全边界。只终止一个进程也不等于回收整个 Job；不使用 `multiprocessing` 的 pickle 通道接收不可信结果。见 Python 3.12 [subprocess](https://docs.python.org/3.12/library/subprocess.html)与[multiprocessing](https://docs.python.org/3.12/library/multiprocessing.html)。

依赖继续以 `pyproject.toml` 为唯一直接声明，requirements 为 extra 入口，constraints 为验证过的版本约束。当前只评估，不创建空的“已锁定”文件、不修改版本号、不安装候选包。阶段0选型后再决定是否新增 `requirements-execution.txt`、`requirements-execution-dev.txt` 及 Windows execution constraints；原生 helper 的编译器/SDK版本、产物哈希与最小 runtime 清单另存 manifest，不能用 pip freeze 代替。维护规则见[依赖指南](../../guides/dependencies.md)。

## 开工前必须补齐的能力验证

1. 标准用户、两个 Python 均能启动已验证 token 的 worker，完成受控 IPC；不依赖管理员模式运行产品。
2. runtime和所有worker可达持久写区有最小ACL/真实额度；默认不授予scratch写权。AppContainer profile、TEMP、缓存及其他可写位置都须实际拒写或有可强制总量约束，发现无界可写位置即阶段0不通过；broker staging额度、轮询大小或退出后清理不能替代。真实配置、宿主数据目录、API key和模型缓存不被传递或额外授权。环境变量用白名单，不继承 `os.environ`；runtime不加载 `.env`、用户site或当前目录模块。
3. IPC 首选显式继承的匿名管道/句柄白名单；若 AppContainer 不兼容才评审本地具名管道的 SID/DACL、客户端身份、抢占与连接范围。匿名管道也有安全描述符及继承语义，不能因为无公开名称就跳过核验。见 [CreatePipe](https://learn.microsoft.com/en-us/windows/win32/api/namedpipeapi/nf-namedpipeapi-createpipe)。
4. Job 实测杀死/回收、宿主异常退出清理、子进程禁止、CPU/commit-memory限额、stdout/IPC淹没限制；无法设置关键限制时启动失败关闭，不给出伪成功。
5. 文件 broker 在临时 NTFS 测试根完成句柄/文件身份、junction/reparse/hardlink、替换竞态和原子提交验证；不得直接把原有 `Path.resolve()` 复查提升为 OS 安全保证。重解析点处理参考 [Microsoft 文档](https://learn.microsoft.com/en-us/windows/win32/fileio/reparse-points-and-file-operations)。
6. 准备独立标准用户/干净 Windows 测试环境，最好使用可恢复快照的 VM；使用合成 canary 文件和本地测试服务验证拒绝，不尝试读取真实密钥或攻击其他应用。

AppContainer profile、测试目录 ACL、runtime缓存创建属于下一轮原型工作；只改本项目专用对象并可清理，不要求放宽系统防火墙、给网络 loopback exemption 或整体目录授权。当前评估未执行这些更改。

## 既有基线与未结项

V0.5 证据为双 Python 各1203 passed/4 skipped、strict89文件、同产品源码30分钟长测及100%/200%双屏194项检查；人工托盘/通知/勿扰补测已完成。它们证明旧实现的范围，不证明新隔离边界。125%/150%、物理拔屏、锁屏/休眠、严格冷缓存和首次3.11停滞仍未结；见[实机记录](../../versions/v0.5/windows-checklist.md)和[稳定性调查](../../versions/v0.5/stability-investigation.md)。

阶段0可在这些缺项保留记录的同时开展独立原型；V0.6正式发布前须补齐继承的必需实机矩阵，并评审停滞调查结果。不能先宣称V0.5完整通过，也不能把未知停滞归因到将来才实现的worker。

## 本轮边界

本次只读代码、盘点环境、核对官方文档并编写规划。未实现 worker/授权/自动化，未启动隔离或编译探针、未调用 DeepSeek、未安装包、未更改系统安全配置，未 stage/commit/push。后续原型结果应保存到新的版本证据中。

评估校验：6.1 Sol高思考Agent完成源码缺口和三文档一致性审查；6 Luna完成环境/pip check及7份文档的106个本地链接、围栏、尾空白核验，失败0，Git diff检查exit0。产品源码+pyproject指纹仍为 `7b679308f34da7b8d89a77aee07b78416567a8c56844f5a3871f816df29c8bac`。这些是规划和既有环境核验，不是V0.6实现测试或隔离通过证据。
