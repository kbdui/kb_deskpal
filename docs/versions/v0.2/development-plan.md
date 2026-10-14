# V0.2 开发计划与验收边界

本计划依据 `doc/` 两份原始路线及 V0.1 已交付的同步后端，范围由本轮开发决定冻结。稳定接口见 [V0.2 接口契约](interfaces.md)，环境准备见 [V0.2 环境评估](environment-assessment.md)。本文是实施计划，不表示功能已经验收通过。

## 本轮交付范围

| 模块 | V0.2 交付 | 不纳入本轮 |
| --- | --- | --- |
| Calendar | 本地日程 CRUD、SQLite 持久化、一次性提醒、事件轮询 | Google/Outlook 同步、循环日程、系统通知、关闭进程后主动唤醒 |
| Timer | 非阻塞创建、列表、取消、持久化到期时间、到期事件轮询 | 用 sleep 占住 Core、跨设备计时、UI 通知、开机启动 |
| Document | 工作区文件搜索、按扩展名分类、单文件复制/移动/删除 | 递归删除、工作区外整理、Office/PDF 内容解析、OCR |
| Screenshot | 显式确认后截屏或区域截图，PNG 保存到工作区 | 无授权采集、后台连续监控、自动上传图像、模型图像理解 |
| 权限 | 独立 Level 1/Level 2 白名单，Waiting 展示具体计划，同一 Core 确认原计划 | 模型自行授权、跨请求全局授权、重启后恢复内存 Waiting |
| 安全/生命周期 | 可信工具路径与权限策略、调度线程和 SQLite 受控关闭 | 操作系统完整沙箱、恶意插件隔离、Qt/Live2D |

继续保留 V0.1 工具、LLM/记忆/任务接口。Core 不导入 Calendar、Timer、截图或文件工具具体服务；由 Application 构造 SchedulingService、注册工具、启动调度，并在 close 或组装失败时释放线程和数据库。

## 模块与所有权

- `deskpal.scheduling`：SchedulingService、日程/Timer 持久化、到期事件、启动/关闭和轮询驱动。调度库独立于 Memory SQLite；Calendar/Timer 可以共享此服务的数据库事务。
- `deskpal.tools`：Calendar/Timer 工具工厂、Document/Screenshot Tool 实现，继续使用现有 Tool Protocol。注册元数据不可被调用者改写。
- `deskpal.sandbox`：工作区边界、链接对象限制、文件读写大小与 Level 0/1/2 权限检查。
- `deskpal.agent`：双白名单、具体授权 DTO、Waiting 私有快照、confirm_task；仍使用 Planner/Tools 等 Protocol。
- `deskpal.application`：所有具体组装、服务资源所有权、CLI 计划展示与同进程确认、截图可选依赖。
- 测试：模块边界及完整离线端到端；真实 API 是独立验证结果，不用 mock 冒充已联调。

## 实施顺序

1. 固定工具 schemas、返回值、权限 DTO 和 SchedulingService 时间/事件语义；保留 V0.1 兼容字段。
2. 实现 SchedulingService 与 Calendar/Timer 工具，用可注入 clock 测试到期、重启和竞态，不依赖长时间真实等待。
3. 实现 Document 工具和 Screenshot，复用工作区策略；mss 作为截图可选依赖，其缺失不影响其他工具。截图联调时才安装所需包。
4. 完成 Core 双白名单、私有 Waiting 原计划和 confirm_task；确认不调用 Planner，不创建新任务。保留授权后重新 run 的兼容方案，但明确其会重新规划。
5. Application 组装并启动服务，close 停调度、等线程退出后关数据库；增加 CLI 具体参数/风险展示与同进程确认。
6. 完成单元、集成、Python 3.11/用户 Python 3.12、Windows 路径和截图选择性实测，更新交付报告后再宣称 V0.2 完成。

## 关键决定与限制

Calendar 输入时间必须明确时区语义，UTC 存储，拒绝不明确的 naive 时间；timezone 的解释和转换按接口执行，不依赖 Windows 本地时区猜测。start/end/reminder 的先后关系在持久化前验证。一次性提醒和 Timer 到期只生成一次持久化事件，状态与事件同事务提交。事件轮询不消费，不因查询权限 Level 0 而删除数据；调用方用稳定 event_id 去重。

进程运行时调度产生到期事件；单次 CLI 结束即关闭服务，**关闭期间没有 UI/系统通知**。重开时恢复持久化截止时间，为尚未生成的过期事项补发事件。取消/修改与到期处理串行化；已产生事件不假装被撤销。Timer 创建工具完成只表示登记成功，不表示倒计时已结束。

Level 1 `authorized_tools` 与 Level 2 `high_risk_authorized_tools` 独立，普通白名单不能授权高风险工具。所有计划在任何工具执行前全量权限预检，缺授权则 Waiting；展示每次调用的工具名、参数、call_id、可信风险级别，并展示整个计划。相同工具多次调用不能只显示一个名称而隐藏其他目标。

`confirm_task` 只执行同一 Core 内私有保存的 Waiting 原计划：原始与新增授权在此任务内累积，不传播下一请求；不足授权仍 Waiting，未执行工具。返回 DTO/Task snapshot 的修改不能改变私有计划。取消、终态、close 清理缓存；无缓存、已结束或重启后的 Waiting 不可确认。兼容的重新 run 会产生新任务并重新规划，不能说成对旧参数的确认。

Document 只操作单个普通文件，移动/删除拒绝链接对象，避免 resolve 后误删链接目标。搜索与按扩展名分类不能跟随 Junction/符号链接穿越目录；分类只返回结果，不读取文件内容或自动移动。复制/移动不得默认覆盖；同源同目标、越界路径、目录删除须拒绝。文件写入、文件删除、SQLite 与日志不是跨模块事务：返回失败可能已有副作用，结果/审计必须说明，不能宣称完整回滚。无覆盖发布依赖同文件系统 hardlink 支持，不支持时失败，不回退为可能覆盖的非原子写入；失败可能遗留空父目录。

Screenshot 为 Level 1，未授权前不枚举或 grab；只返回图像文件元数据，不把像素或 base64 加入 ToolResult、日志、Memory 或云端 LLM。屏幕捕获的尺寸/区域、保存路径、覆盖行为和资源上限在接口固定；默认文件上限 1 MiB，即使未超过像素上限，大 PNG 仍会失败。缺少 mss、无交互桌面或捕获失败应可显示失败，不能伪称保存成功。

CLI 的 `--tool NAME --arguments JSON` 通过 DirectToolPlanner → Core 提出确定调用，不绕过权限或任务审计；`--authorize` 授权 Level 1，`--authorize-high-risk` 单独授权 Level 2。离线交互 `tool NAME JSON` 展示原计划后，通过 `/confirm ID`、`/confirm-high-risk ID`、`/cancel ID` 操作同进程 Waiting；单次命令结束后无可恢复的私有计划。工厂 `create_agent(config, *, planner=None, screenshot_backend=None)` 支持可信测试注入，Core close 即使清理回调报错也保持封闭。

三级权限与应用层路径策略不构成完整 OS 沙箱。V0.2 不承诺强制资源隔离、恶意 Python 插件隔离或管理员级文件系统竞态防护；不为测试未经请求启用 Windows 开发者模式、提权或改系统权限。

## 验收清单

| 领域 | 必须通过的验收 |
| --- | --- |
| 权限/确认 | Level 1 无法授权 Level 2；Waiting 含完整调用；confirm 参数不漂移、不重复规划；不足授权零工具执行；同名多调用逐项展示；取消/终态/关闭拒绝确认；外部 DTO 修改无效 |
| Calendar | CRUD 持久化与 UTC/时区规则；无效时间拒绝；修改清除字段语义；漏到期重开补发；提醒只生成一次；取消/删除与到期事件边界 |
| Timer | 创建快速返回且不阻塞 Core；有限正 duration；列表/取消；到期事件一次生成；重启恢复；查询游标重读/分页/来源过滤；关闭后线程停止且数据库可再次打开 |
| Document | Unicode 文件名、查询/分类结果上限；分类不读内容或自动移动；链接/Junction 不被搜索跟随；复制完整性、同名冲突、无覆盖；移动/删除仅单文件；绝对路径、盘符、ADS、设备名、逃逸拒绝；失败副作用说明 |
| Screenshot | 未授权捕获调用计数为零；授权后调用 mock capture；区域/尺寸/保存规则；mss 缺失与捕获错误；图像数据不流入日志/记忆/LLM；真实桌面截图为明确单独实测 |
| 生命周期/故障 | factory 失败释放已创建资源；close 幂等、先停 worker 后关 SQLite；调度/磁盘/日志失败明确报告；Core/工具失败不继续后续步骤；V0.1 回归仍通过 |
| 环境/模型 | Python 3.11 和用户 3.12 均有实测；Windows Junction 测试实际运行；符号链接权限不足标为环境限制；真实 API 测试记录真实结果，不把未验证写成通过 |

