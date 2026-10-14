# V0.2 接口与行为契约

本文覆盖本轮新增/变更契约；未改变的同步 Tool/LLM/Memory 等接口沿用 [V0.1 契约](../v0.1/interfaces.md)。实现与计划状态分别见 [V0.2 开发计划](development-plan.md) 和最终测试报告，不能仅凭本文宣称已完成。

## Agent、授权与 Waiting

```python
Agent.run(request: str, *, authorized_tools: Iterable[str] = (),
          high_risk_authorized_tools: Iterable[str] = ()) -> AgentResult
Agent.handle_request(request: str, *, authorized_tools: Iterable[str] = (),
                     high_risk_authorized_tools: Iterable[str] = ()) -> AgentResult
Agent.confirm_task(task_id: str, *, authorized_tools: Iterable[str] = (),
                   high_risk_authorized_tools: Iterable[str] = ()) -> AgentResult
Agent.cancel_task(task_id: str) -> Task
Agent.close() -> None
```

白名单均是本次请求/指定 Waiting 任务的用户显式授权，转换为 frozenset；字符串不能误作字符序列。ToolContext 在原有字段后新增 `high_risk_authorized_tools: frozenset[str] = frozenset()`。权限来自可信注册的 ToolSpec：AUTOMATIC=0 不需额外授权；CONFIRM=1 只检查 authorized_tools；HIGH_RISK=2 只检查 high_risk_authorized_tools。不能用模型参数、用户修改 DTO 或 Level 1 白名单改变风险等级。

AgentResult 保留 task_id/status/response/tool_results/required_authorizations/error，新增：

- `authorization_requests`：每个缺授权调用的快照，字段 name、arguments、call_id、permission_level；同名不同参数的调用分别保留。
- `planned_calls`：整个原计划的有序调用快照，用于展示执行前的具体内容。
- `required_high_risk_authorizations: tuple[str, ...]`：缺少 Level 2 授权的去重工具名。

required_authorizations 兼容地包含所有缺授权的去重名称，不代表这些名称都能用普通白名单授权。arguments 为独立 JSON 快照；所有风险级别来自注册信息。Waiting 在任何工具执行前返回，tool_results 为空；未知工具直接失败。权限预检检查工具注册、权限与 workspace context，参数 schema/实际文件路径在授权执行阶段继续验证；Waiting 并不宣称参数/文件已经可执行。

Core 私有缓存保存 Waiting 的原请求、原计划和任务内授权。confirm_task 不调用 Planner、不重新搜索记忆、不创建任务；将原授权与本次新增授权分别按等级合并后，对整个私有计划重新权限预检。不足授权仍返回 Waiting；充足授权允许 Waiting → Running，并执行原参数。V0.2 Task 状态图覆盖 V0.1 的 Waiting 限制，允许 Waiting → Running/Failed/Cancelled。反射仍是独立新 LLM 请求、tools=()，不执行新的工具计划。

confirm_task 必须拒绝关闭 Core、其他请求正在执行、已取消/终态任务和没有私有缓存的任务；不存在 task_id 使用 KeyError，非法状态/无缓存使用明确 ValueError，关闭/并发沿用 RuntimeError。重复确认已执行任务不能重复副作用。取消、完成、失败、close 清理私有缓存；重启不恢复缓存。Task DTO、AgentResult、planned_calls 或授权展示数据被调用者修改，不能改变缓存。

兼容方式 `run(original_request, authorized_tools=..., high_risk_authorized_tools=...)` 仍可用，但它创建新任务并重新规划；与之前展示的参数无绑定。CLI 的同进程确认应优先调用 confirm_task，并分别展示 Level 1 与 Level 2 授权方式；单次 CLI 请求可使用请求范围双白名单，不能把一次授权自动延续至下个交互请求。

工厂为 `create_agent(config: AppConfig | None = None, *, planner: Planner | None = None, screenshot_backend: ScreenshotBackend | None = None) -> AgentCore`；具体服务仍由工厂拥有。调度数据库为 storage.data_dir/scheduling.sqlite3，使用 scheduling.poll_interval_seconds；截图像素限额来自 screenshot.max_pixels。资源清理通过 ExitStack，即使一项 close 失败仍尝试其他回调；Core 标记 closed 并清缓存后调用清理，回调报错也不能继续 run/confirm。

CLI 的互斥入口为 `--request TEXT`、`--tool NAME`、`--list-tools`；`--arguments JSON` 仅配 --tool，默认空对象，拒绝重复键/非有限值/非对象。--tool 使用 `deskpal.planner.direct.DirectToolPlanner(ToolCall)` 注入工厂，经 Core 的预检、确认、执行、总结、日志与任务生命周期，不能绕过 Tool Manager。`--authorize NAME` 和 `--authorize-high-risk NAME` 可重复，仅用于单次 --request/--tool，不能配交互模式。API 模式的 direct tool 请求仍使用配置的 LLM 做独立 Reflect。

离线交互支持 `tool NAME JSON` 提出确定工具调用；同进程 `/confirm ID` 批准已展示计划的缺少 Level 1 调用，`/confirm-high-risk ID` 明确批准该计划缺少的 Level 1 与 Level 2 调用，`/cancel ID` 取消 Waiting 任务。命令只操作当前交互进程保存的 Waiting；缺少/已结束的 ID 拒绝。CLI 展示全部 planned_calls、每个缺授权调用的风险级别和参数；单次模式退出后缓存销毁，后续新进程不能凭旧 ID 确认。

## 工具名与风险级别

| 工具 | 级别 | 参数 |
| --- | --- | --- |
| calendar_create | Level 1 | title、start_at、timezone；可选 end_at、reminder_at |
| calendar_list | Level 0 | 可选 start_after、start_before、limit=100 |
| calendar_update | Level 1 | id；可选 title/start_at/timezone/end_at/reminder_at、clear_reminder、clear_end |
| calendar_delete | Level 1 | id |
| calendar_events | Level 0 | after_id=0、limit=100 |
| timer_create | Level 1 | duration_seconds；label="Timer" |
| timer_list | Level 0 | 可选 status、limit=100 |
| timer_cancel | Level 1 | id |
| timer_events | Level 0 | after_id=0、limit=100 |
| document_search | Level 0 | 可选 path（省略为工作区根）、query=""、limit=100 |
| document_classify | Level 0 | 与 search 相同 path/query/limit，按扩展名分类，不读内容/自动移动 |
| document_copy | Level 1 | 单文件 source、destination；overwrite=false |
| document_move | Level 2 | 单文件 source、destination；overwrite=false |
| document_delete | Level 2 | 单文件 path |
| screenshot_capture | Level 1 | path 必填；monitor=1、region 可选、overwrite=false |

所有工具沿用 Tool.spec / execute(arguments, context) → ToolResult；JSON Schema 使用现有明确子集并拒绝额外参数。权限/路径拒绝仍抛 PermissionDenied/PathViolation；普通参数与工具业务错误返回失败 ToolResult，SQLite/日志/服务生命周期故障不可静默吞掉或伪报成功。

## SchedulingService 与资源所有权

```python
from deskpal.scheduling import SchedulingService
from deskpal.scheduling.ports import SchedulingManager

SchedulingService(db_path, *, execution_log=None, clock=None, poll_interval=1.0)
service.start() -> None
service.poll_due() -> int
service.close() -> None
from deskpal.tools.calendar import calendar_tools
from deskpal.tools.timer import timer_tools
calendar_tools(service) -> Sequence[Tool]
timer_tools(service) -> Sequence[Tool]
```

服务数据库独立于 Memory；Calendar、Timer 和到期事件共用调度库。`Clock.now() -> aware datetime`、`Clock.monotonic() -> finite float` 是可注入测试时钟，默认 SystemClock；poll_interval 为有限秒数，`0 < value <= 60`。poll_due 返回本次新增到期事件数；list/events 只读已有记录，不因查询隐式触发事件。Core 只能接触服务提供的 Tools，不 import SchedulingService。

Application 完成构造与注册后 start，close 先唤醒并停止调度、等待 worker 退出，再关闭 SQLite；close 幂等，关闭后不能继续查询/写入/轮询。不要持有服务状态锁同时 join 正在等待同一锁的线程。组装失败须关闭所有已创建资源；一个资源关闭失败不能阻止尝试关闭剩余资源。服务不得在线程里静默退出而继续对外显示健康。

创建/取消/修改与到期扫描用同一服务事务/锁串行化。截止状态和事件插入同事务提交；事件有唯一约束，重复 poll 或重开不能重复生成同一事项同一版本的到期事件。SQL 动态值参数绑定，返回 DTO/JSON 不持有活动 cursor。构造函数只恢复 deadline，不自动产生到期事件；首次 start worker 的 poll 或显式 poll_due 才补发。变更先写 intent 审计（schedule_due_requested 在到期批次 SQL 事务内），审计失败回滚对应数据库批次；JSONL intent 不等于事务已提交成功。

## Calendar 与 Timer 时间行为

工具构造依赖 SchedulingManager Protocol（包含 lifecycle、Calendar CRUD、Timer create/list/cancel 和来源事件查询），而非具体 SQLite 类；Application 注入 SchedulingService。该 Protocol 与 Clock 位于 `scheduling/ports.py`，服务 DTO 位于 `models.py`。

Calendar 的 title 非空且最多 200 字符；start_at/end_at/reminder_at 使用含秒及显式 offset/Z 的 ISO8601 时间，存储和输出均为 UTC+00:00、固定 microseconds。timezone 为经 ZoneInfo 验证的可用 IANA 展示元数据；输入 offset 决定绝对时刻，不要求它与 timezone 展示 offset 相同，允许 Z 输入配 Asia/Shanghai。naive 时间拒绝；Windows 若无所需时区数据应明确失败，再按需要补 tzdata，不猜机器时区。end_at 必须严格晚于 start_at，提醒不晚于日程开始。清除可选字段用 clear_end/clear_reminder；同字段同时设置新值并请求清除须拒绝，不把缺省参数当清空。

calendar_list 条件为 start_at >= start_after、start_at < start_before，并要求 after < before；按 start_at/id 升序，limit 整数 1..1000、默认 100。calendar_update 至少提供一个更新字段，未提供字段保持原值；只有 reminder_at 的绝对时刻变化才重置 reminder_fired/新提醒版本，修改 title 或相同提醒时刻不再次触发。delete 返回是否存在并删除，历史事件保留。循环日程/重复提醒不在本轮。

Timer duration_seconds 是有限数，范围 1..2678400 秒（31 天），拒绝 bool；label 非空且最多 200 字符，默认 Timer。创建快速登记并返回，不等待到期。状态为 pending/fired/cancelled；timer_list 的 status 为 all/pending/fired/cancelled，默认 all，按 due_at/id 升序，limit 整数 1..1000。cancel 只改变 pending，未找到/已 fired/已 cancelled 返回 cancelled=false。持久化 UTC 截止时间，进程内使用 monotonic 截止时间，恢复时根据持久化 due_at 重建剩余时长。Agent Task Completed 表示创建/查询工具调用完成，Timer 仍可能未到期。

运行中 poll_due 生成到期事件；重开时为尚未生成的过期日程提醒/Timer 补发。进程关闭期间不触发系统通知，也不承诺按时执行外部任务。已经持久化的到期事件保留，之后取消/删除不会假装撤回；取消与到期的先后以事务提交顺序为准。

## 事件轮询与隐私

calendar_events/timer_events 均支持 `after_id: int = 0`、`limit: int = 100`，after_id 为非负 SQLite int64（最大 2**63-1）、limit 为 1..1000 整数。事件 ID 在同一调度数据库全局递增，两工具各自只返回对应来源、ID 大于 after_id 的事件，按 ID 升序分页。查询不消费/删除、不重复生成事件。

调用者为 Calendar 和 Timer 各保存自己的游标，使用已返回的最大 ID 推进；不能拿另一个来源的最新 ID 跳过尚未读取的事项。重复轮询可以返回同一事件，消费方以稳定事件 ID 去重。源记录删除后历史事件仍可查询；重启后的旧游标继续可用。限额不能通过 bool、负值或无限/NaN 数量绕过。

审计保存事项 id、时间、来源和状态等必要显式信息，不记录 API 凭据或内部推理。日程列表内容可能经 Reflect 在 API 模式发送给配置的云端服务，应按用户主动请求使用。截图只返回 PNG 文件元数据，不把图像内容流入此链路；捕获不等于上传授权。

工具成功 output 形状固定如下，时间均为 UTC ISO8601：

| 输出 | JSON 字段 |
| --- | --- |
| calendar_create/update | 单条 `{id,title,start_at,timezone,end_at,reminder_at,reminder_fired,created_at,updated_at}` |
| calendar_list | 上述记录的直接 JSON list |
| calendar_delete | `{id,deleted:bool}` |
| timer_create | 单条 `{id,label,duration_seconds,due_at,status,created_at}` |
| timer_list | 上述记录的直接 JSON list |
| timer_cancel | `{id,cancelled:bool}` |
| calendar_events/timer_events | 直接 JSON list，每项 `{id,source,source_id,label,due_at,recorded_at,overdue}`；source=calendar/timer，overdue 标识记录时已晚于截止时间 |

## Document 与 Screenshot 文件行为

具体实现为 `deskpal.tools.documents` 的 DocumentSearchTool/DocumentCopyTool/DocumentMoveTool/DocumentDeleteTool(sandbox)。搜索 path 省略为工作区根，给定时为非空工作区相对目录；query 为文件名 casefold 子串、默认空串；limit=100，整数 1..1000。输出 `{files:[{path,bytes}],truncated,scanned_entries}`。遍历 max_entries=10000、max_depth=32；越扫描预算失败，达到深度截断标记。搜索不跟随符号链接或 Junction；所有直接操作拒绝链接路径。不能把 `..`、绝对路径、驱动器或设备名解释为查询根，只返回相对路径及元数据，不自动读取全部内容。

分类实现为 `DocumentClassifyTool(sandbox, *, extension_categories: Mapping[str,str] | None = None)`，使用相同搜索 schema/路径/遍历限额，成功 output `{files:[{path,bytes,category}],truncated,scanned_entries}`。category 为 text/image/audio/video/document/archive/other；使用文件名最后一个 suffix 的 casefold 映射，例如 .tar.GZ 按 .gz 判为 archive，未知/无 suffix 为 other。默认扩展名表集中在不可变 DEFAULT_EXTENSION_CATEGORIES；自定义映射完全替换默认表，复制后冻结，不能被外部修改。分类只是文件名类型推断，不读取内容、不自动复制/移动。

copy/move/delete 只接受单个普通文件，copy/move 参数 source/destination/overwrite=false；拒绝同源同目标，目标存在且未显式 overwrite=true 时失败。move/delete 必须检查原始路径对象而非仅检查 resolve 后目标，不得用递归删除实现单文件 delete。文件大小复用 sandbox 上限，binary copy 不要求 UTF-8。输出 copy `{source,path,bytes_written}`，move `{source,path,bytes_moved,completed}`，delete `{path,bytes_deleted}`。不覆盖的 move 用 hardlink 发布目标后移除源，源移除失败返回失败 ToolResult 与 `completed=false,destination_created=true,source_removed=false`，明确两份文件仍在；覆盖 move 用 replace。write_bytes/copy/截图采用 staging 后无覆盖 hardlink 发布或覆盖 replace；不支持 hardlink 的文件系统失败，不回退危险覆盖。失败可能遗留空父目录，不能宣称跨模块回滚。overwrite=true 同样必须经过该工具所需的授权。

`deskpal.tools.screenshot.ScreenshotCaptureTool(sandbox, *, backend=None, max_pixels=16000000)` 注册 screenshot_capture。参数 path 必填且扩展名 .png；monitor 默认 1，为物理显示器 1-based 索引，不含 MSS 合成的所有屏幕；可选 region `{left,top,width,height}` 相对选定显示器，left/top 非负，width/height 正整数，区域不能越界；overwrite=false。物理屏幕全局坐标可为负值，不应与 region 的局部非负坐标混淆。

Screenshot 在 manager 权限检查通过后才枚举/捕获屏幕；实际像素数不超过 max_pixels，PNG 大小受 sandbox max_file_bytes 限制（默认 1 MiB，未超过像素限额不等于 PNG 一定能保存）。后端为 `ScreenshotBackend.monitors() -> Sequence[Monitor]`、`capture(region: Monitor) -> bytes`，其中 Monitor 字段 left/top/width/height；MSSScreenshotBackend 延迟导入 mss。缺少 `deskpal[screenshot]` 依赖返回可显示失败，不在模块 import 或 factory 构造时阻塞其他工具。验证 PNG 头部尺寸匹配捕获区域，保存失败不得报告成功。成功 output `{path,bytes_written,width,height,monitor}`，只含工作区路径/元数据，不能包含像素/base64 或自动上传；不强制安装 Pillow。

## 新增配置与依赖

AppConfig 新增 `scheduling: SchedulingConfig` 与 `screenshot: ScreenshotConfig`，均有兼容旧配置的默认值。TOML `[scheduling].poll_interval_seconds` 默认 1.0、有限且 `0 < value <= 60`，环境变量为 `DESKPAL_SCHEDULING_POLL_INTERVAL_SECONDS`；`[screenshot].max_pixels` 默认 16000000、正整数，环境变量为 `DESKPAL_SCREENSHOT_MAX_PIXELS`。沿用进程环境 > 配置目录 .env > TOML > 默认值，显式 environ 仍跳过 .env。

runtime 必须依赖只有 python-dotenv；可选 `screenshot` extra 声明 `mss>=10.0,<11`，本轮安装并实测 10.2.0。没有 mss 时仍可创建工厂、使用其他工具或注入自定义 ScreenshotBackend。MSS 的内存 PNG 编码依据 [官方示例](https://python-mss.readthedocs.io/stable/examples.html)。

