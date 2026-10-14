# V0.2 测试与验收报告

日期：2026-10-13。依据`docs/versions/v0.2/interfaces.md`与`docs/versions/v0.2/development-plan.md`的最终契约。本文记录最终19工具版本；V0.1历史报告保持独立，不覆盖此前证据。

## 文件与测试所有权

测试Agent本轮修改：

- `tests/test_cli.py`：新版JSON字段、19工具发现、直接工具JSON输入、独立高风险授权、同进程确认/取消。
- `tests/unit/test_tasks.py`：Waiting允许Running/Failed/Cancelled，原49状态对覆盖及幂等语义保留。
- `tests/unit/test_sandbox.py`：Level1/Level2独立白名单及严格frozenset/非空字符串校验，保留真实链接测试。
- 新增`tests/unit/test_confirmation.py`、`tests/unit/test_v02_config.py`、`tests/integration/test_v02_flow.py`。
- 新增本报告。其他模块Agent新增测试、产品文件、真实config/.env及历史报告未由测试Agent修改。

Calendar/Timer/Scheduling、Document和Screenshot的模块测试由对应Agent交付并纳入完整suite。默认测试均使用fake环境、临时目录与SQLite、可注入时钟/屏幕后端；测试Agent没有发起真实网络请求、捕获真实屏幕或读取用户配置/凭据。

## 设计与接口验证

确认采用同一Core内私有原计划，调用`confirm_task()`复用task_id，不调用Planner、不重新搜索记忆、不创建任务。两份授权按风险等级在该任务内累积；不足授权时整份计划仍Waiting，automatic步骤也不提前执行。Level2只接受`high_risk_authorized_tools`，Level1只接受`authorized_tools`。返回preview、Task快照及原Planner数据修改不能改变私有参数。

所有工厂资源由Application拥有。Scheduling真实线程/SQLite与Memory在成功、装配失败及关闭异常路径都释放；关闭先调度后记忆，清理报错仍封闭Core。恢复后的过期事件通过持久化记录补发并去重，不等同于程序关闭期间主动通知。

最终注册19工具：原4工具、Calendar/Timer共9工具、Document共5工具（包含扩展名分类）和Screenshot1工具。分类只依据后缀元数据，不读取内容或自动移动。截图默认测试仅使用fake PNG，不把图像内容写入工具返回、记忆、日志或反思。

## 关键测试行为

| 领域 | 已验证行为 |
| --- | --- |
| 原计划确认 | 同task执行/no replan；分次授权累积；不足授权零副作用；同名多个参数逐项展示；重复成功/失败确认不重跑 |
| 隔离与权限 | preview/planned_calls/Task/Planner源数据嵌套修改无效；Level1不能许可Level2，反向也独立；授权不传播另一任务；不合法grant容器无法放行 |
| 取消与生命周期 | cancelled/closed/terminal/unknown/no-cache拒绝；并发确认拒绝且保留计划；Waiting写入后立即cancel的hook返回Cancelled而非Waiting |
| 审计故障 | confirmation事件或Running转换审计故障安全Failed且不执行工具；失败清除缓存；后续确认不重复副作用 |
| 状态与配置 | 七状态完整合法/非法矩阵、同态幂等；新poll_interval有限范围0..60（0排除）、max_pixels正整数；env优先TOML，bool/nonfinite/错误类型拒绝 |
| Factory资源 | 成功关闭顺序与幂等；注册失败、调度start后Core构造失败均关闭已建资源；调度close抛错仍尝试Memory.close，Core永久拒绝后续run |
| 持久化 | 真实Calendar/Timer工具经Waiting确认后创建；关闭期间经过fake时间，重开补发过期事件；多次poll/重开稳定ID不重复；任务进程内存与持久化服务区分 |
| Document整合 | 真实单文件move确认原目标，普通授权保持Waiting，高风险授权后只移动到原目标；分类返回path/bytes/category，不读源内容或搬动文件 |
| Screenshot整合 | 未授权前backend枚举/capture计数零、无PNG；确认后fake保存正确，ToolResult仅元数据，PNG/base64不进入记忆/日志 |
| CLI | 新完整JSON字段与19工具；--tool/--arguments经Core；NaN/duplicate JSON/nonobject/格式错误在storage IO前拒绝；--authorize-high-risk独立 |
| 同进程CLI | /confirm不许可Level2，/confirm-high-risk对展示的同task生效；重复确认拒绝；/cancel移除待确认计划且无写入；旧UTF-8/退出码/配置/交互回归保留 |

架构审查发现的Waiting提交后取消竞态由主Agent修正，测试Agent添加对应公开行为回归，现通过。旧CLI DTO字段/工具数与Task/Level2的V0.1期望按正式V0.2契约更新；此类契约迁移不记为最终产品缺陷。

## 实际执行结果

测试Agent目标组（6个负责文件）最终：**118 passed, 1 skipped in 11.97s**。随后各环境只执行一次最终完整suite，显式关闭真实API gate：

```powershell
$env:DESKPAL_RUN_LIVE_API_TESTS = '0'
& 'D:\anaconda\Anaconda3\envs\deskpal_env\python.exe' -m pytest -q -rs
# 573 passed, 4 skipped in 18.73s
& 'E:\no_game\git\kb_deskpal\kb_deskpal\data\validation\py311\python.exe' -m pytest -q -rs
# 573 passed, 4 skipped in 20.46s
```

| 环境 | 完整回归 | 独立类型审查 |
| --- | --- | --- |
| deskpal_env，Python3.12.4 | 573通过，4跳过 | 主Agent严格mypy：48源码文件通过 |
| 隔离prefix，Python3.11.17 | 573通过，4跳过 | 主Agent严格mypy：48源码文件通过 |

四个默认skip清楚区分：2项live API未显式启用，2项普通symlink因Windows权限无法创建（Document与Sandbox）。真实Windows junction在本轮完整suite中实际通过，不以mock或历史提权证据声称本轮普通权限symlink通过。V0.1真实模型/运行证据保留在其后续报告。

## 主Agent独立真实验收

本节来自主Agent执行反馈；测试Agent没有读取这些helper中的真实凭据或查看截图像素。

**DeepSeek日程完整流程通过**：在全部19工具可用且没有forced tool_choice时，实际模型自动选择calendar_create，Core返回Waiting后confirm_task执行同task原计划，独立Reflect生成Completed结果，SQLite重开日程记录一致。共2次真实API请求，每次最多256输出token且reasoning.effort=none。初次受沙箱网络限制，按权限要求升权重跑成功；首次连接限制不作为业务API失败。隔离helper为ignored `data/validation/accept_v02_deepseek.py`。

**真实Screenshot局部验收通过**：经明确执行许可，MSS10.2对第一物理显示器左上32×32区域运行Core Waiting→零PNG→confirm_task→离线Reflect→本地PNG保存，header尺寸核验通过。没有上传或展示像素；仅验证该显示器该区域，不声称多屏、DPI或整屏行为已实测。隔离helper为ignored `data/validation/accept_v02_screenshot.py`，隔离运行数据保留。

独立真实验收与默认gate skip不是同一种证据；真实服务和局部截图已实跑，但默认测试继续无网络/无真实采屏。单次真实自动选工具成功不泛化为所有目标、模型、屏幕环境的保证。

## 问题与边界

主Agent另完成发布包检查：本地构建 `deskpal-0.2.0-py3-none-any.whl`，确认包含新增调度/工具/直接规划模块，未包含真实配置、凭据或运行数据。将 wheel 无依赖安装到仓库内隔离 target，两个解释器均用 `-I` 运行验收，断言实际 module 路径来自 wheel target 而非 editable src；19 工具装配、离线 echo/Core、资源 close 均通过。这是独立打包证据，不增加上述 pytest 计数。

最终没有待修复阻塞缺陷，双环境功能回归通过。当前普通权限真实symbolic symlink仍受WinError1314限制；junction实际通过。真实截图多屏/DPI/整屏未实测，不扩大局部验收范围。

V0.2仍是可信工具的应用层权限/路径策略，不提供OS进程隔离、恶意插件防护或管理员级文件系统竞态保证。Memory、文件、日志、调度之间无跨模块事务，失败可能已有副作用；没有后台系统通知、循环日程、关闭后主动唤醒或重启恢复私有Waiting计划。推理模型native多轮续接、截图上传/模型视觉理解也不在本轮范围。
