# V0.2 本地工具后端交付

交付日期：2026-10-13。版本为 `0.2.0`，Python 3.11+。本轮由架构、Calendar/Timer、Document/Screenshot、测试子 Agent 分工，主 Agent 统一接口、权限与生命周期，审查并整合；修改保留本地，未提交或 push Git。

## 已实现

工厂默认提供 19 个工具，保留 V0.1 的聊天、四个基础工具、记忆、任务和日志。新增能力如下：

| 能力 | 实际行为 |
| --- | --- |
| Calendar | 本地 SQLite 创建/查询/修改/删除；显式 offset 时间存 UTC；一次性提醒生成可查询事件 |
| Timer | 创建快速返回，后台非阻塞调度；查询、取消、到期事件；持久化 deadline，重启补记漏到期事项 |
| Document | 工作区内文件名搜索、按扩展名分类、单文件复制/移动/删除；默认不覆盖，不跟随链接/Junction |
| Screenshot | 授权后捕获指定物理屏幕或区域，PNG 本地保存，限制像素和字节；返回元数据而非图像内容 |
| 三级权限 | Level 0 自动，Level 1 普通确认，Level 2 移动/删除使用独立高风险确认 |
| 原计划确认 | Waiting 展示全计划及逐项缺授权参数；confirm_task 执行同一任务私有原计划，不重新调用模型规划 |
| CLI | 自然语言请求、确定的 --tool/--arguments、双授权 flags，以及同进程确认/取消命令 |

授权只在指定请求/Waiting 任务内有效，模型不能改变注册风险等级。普通白名单不能放行 Level 2；确认时返回 DTO 的修改不会改变原计划。取消、结束或关闭后清理缓存，重启无法恢复内存 Waiting。架构审查发现的等待状态与取消竞态已修正并加入回归。

调度服务通过 SchedulingManager Protocol 注入工具，由 Application 启动/关闭，Core 不依赖具体调度、文件或截图服务。到期状态与事件同 SQLite 事务，来源事件游标非消费式；查询方须自行按事件 ID 去重。资源由 ExitStack 管理，组装失败也尝试释放全部已建资源，关闭调度后关闭记忆；清理失败仍封闭 Core。

## 文件与接口

- 新增 `src/deskpal/scheduling/` 的 DTO、Clock/SchedulingManager Protocol、SQLite 与线程实现。
- 新增 `tools/calendar.py`、`timer.py`、`documents.py`、`screenshot.py` 和 `planner/direct.py`。
- 更新 Agent DTO/Protocol/Core、Task Waiting 状态、ToolContext、Sandbox、Application factory/CLI、配置和版本元数据。
- 新增模块及确认/配置/集成测试，更新旧状态/授权/CLI 契约测试。
- 更新 README、ARCHITECTURE 与开发说明；新增本版本计划、接口及测试报告，V0.1 历史报告保留。

工具参数、返回值、授权和故障语义以 [V0.2 接口契约](interfaces.md) 为准。

## 环境与运行

用户 `deskpal_env` 保持 Python 3.12.4，已更新 editable 项目到 0.2.0，并安装可选截图依赖 mss 10.2.0。核心依赖仍为 python-dotenv；mss 使用 `screenshot` extra，不要求其他工具安装截图、Qt 或 Live2D 环境。现有用户 config.toml/.env 无需新增字段。

新增可选配置默认值如下，旧配置仍可加载：

```toml
[scheduling]
poll_interval_seconds = 1.0 # 大于 0 且不超过 60

[screenshot]
max_pixels = 16000000
```

截图 PNG 也受 `sandbox.max_file_bytes` 限制（旧默认 1 MiB）；整屏图片超过限额会失败，应按实际需求调整。物理屏幕编号从 1 开始，区域偏移相对选定屏幕；负屏幕原点由后端转换处理。

```powershell
conda activate deskpal_env
python -m deskpal --list-tools
python -m deskpal --tool document_classify --json
# 使用真实模型的交互模式：
python -m deskpal --config config.toml
```

离线交互可以输入 `tool timer_create {"duration_seconds":60,"label":"休息"}`，按实际返回的任务 ID 输入 `/confirm ID`。高风险计划使用 `/confirm-high-risk ID`，放弃使用 `/cancel ID`。单次 CLI 也可用 --authorize 或 --authorize-high-risk；该进程退出后不能继续确认同一内存计划。

## 验证证据

| 验证 | 结果 |
| --- | --- |
| Python 3.12.4 完整回归 | 573 passed，4 skipped |
| Python 3.11.17 完整回归 | 573 passed，4 skipped |
| 严格 mypy | 两环境均 48 个源码文件通过 |
| Windows 路径 | 本轮真实 Junction 测试通过；普通权限的两项符号链接用例因 WinError 1314 跳过 |
| DeepSeek | 全部 19 工具可发现，模型自动选择 calendar_create → Waiting → 原计划确认 → Completed/Reflect → SQLite 重开；2 次限量真实请求通过 |
| MSS 真实捕获 | 经明确许可，第一屏幕左上 32×32 区域 → 本地 PNG；未上传或显示像素；通过 |
| 打包 | 构建 deskpal-0.2.0 wheel，检查包含新增模块且不含凭据/运行数据；安装到隔离 target 后 Python 3.11/3.12 均实际导入 wheel 并通过 19 工具装配与离线 Core 请求 |

另外两项默认 skip 是需要显式打开的真实 API 测试；默认 suite 无网络、无真实采屏。详细证据和实测边界见 [V0.2 测试报告](test-report.md)。验证脚本、wheel 与隔离运行数据位于被 Git 忽略的 `data/validation/`，凭据没有输出或进入提交文件。

## 实际限制与下一阶段

本次是可信内置工具的应用层权限/路径策略，**没有完整操作系统沙箱、强制进程超时或资源隔离**，不能执行不可信 Python 插件。文件、记忆、日志与调度不是跨模块事务，失败可能已有副作用。无覆盖文件发布依赖文件系统 hardlink；不支持时明确失败，失败后可能保留空父目录。文件移动移除源失败会显式报告目标已创建、源仍存在。

Calendar/Timer 的“提醒”是持久化到期事件，当前无系统通知、循环日程、关闭后主动唤醒或任意工具的自动定时执行。单次命令退出即停止调度，重开首次轮询补记过期事件。Timer 创建 Completed 表示登记完成，不表示倒计时已经结束。

真实截图只实测上述一个小区域，多屏、DPI、整屏及远程桌面还需专项验收。截图不自动上传，模型视觉理解和推理模型 native 多轮工具续接未实现。Qt、Live2D、人格与情绪保持后续范围；完整 OS 隔离和桌面通知适合作为下一轮独立里程碑。
