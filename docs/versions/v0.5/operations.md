# V0.5 候选版操作指南

代码版本：**0.5.0rc1**。本版加入 Qt Widgets 主窗口、后台桥接、任务/记忆/目标管理、托盘和默认关闭的本地通知。Live2D、人格情绪、完整多轮上下文、OS 隔离与强制停止同步工具不在本版。

## 安装与启动

```powershell
conda activate deskpal_env
python -m pip install -r requirements-gui.txt -c constraints/windows-py312-gui.txt
python -m pip check
python -m deskpal.gui --config config.toml
```

开发与 GUI 测试使用 requirements-gui-dev.txt。主环境已安装 PySide6-Essentials 6.12.0、配套 shiboken6 和 pytest-qt 4.5.0；不需要完整 Qt SDK、PyQt、Live2D 或 GPU。

安装后的独立入口为 deskpal-gui。入口不在当前 shell PATH 时，使用上面的 python -m 方式；不修改全局 PATH。只安装后端的用户仍可使用 python -m deskpal，CLI 不强制导入 Qt。

使用 `python -m deskpal.gui --config config.toml --offline` 可在本次运行覆盖为离线 provider；不会改写 TOML。GUI 只展示有限的有效配置元数据，不提供密钥编辑器，密钥仍由环境或配置旁的 .env 读取，不发送到聊天。

## 请求与授权

1. 对话页输入请求并选择数据用途，后台异步返回排队回执；桥接立即返回的 request_id 不等于任务已经入队。当前每次输入独立处理；连续消息不代表完整多轮模型上下文。
2. 默认 local_only 阻止整次云规划。如果使用 DeepSeek 等云 provider，只有用户主动选择 cloud_allowed 后，该次请求才允许进入云链路；界面不会自动提升用途权限。
3. 在任务页选择条目，查看完整 Task、原计划、每步参数、风险、工作区和 hash。普通与高风险工具分别选择，默认全部未选。
4. 确认只授权已展示的整份原计划；参数/工具契约/来源变化后必须重新审核。当前授权粒度仍为整份计划中的工具名，不能宣称已支持逐路径或逐调用授权。
5. 取消按钮记录“请求取消”；正在执行的同步 LLM 或工具仍需返回，不显示为强制停止。结果不确定或部分成功时查看账本，不自动重放。

任务/记忆/目标/提议使用分页，默认每页 50 条、后端上限 100；列表导航标签以 ID/状态为主，选中后查看正文。慢请求期间窗口可继续操作；部分管理操作受后端 Busy 门禁限制，保留输入并在状态允许后主动重试，不自动重试授权。

## 记忆、目标和提议

记忆页支持显式记住、候选接受/拒绝、更正和遗忘，详情包含来源、用途和版本。记忆操作先清除界面持有的正文与授权；隐私操作使旧 epoch 回调失效。遗忘在受理前 Busy/版本冲突/QueueFull 时显示“未提交”，可主动重新加载；删除工作单已提交但 Pending 时持续遮蔽，使用“重试清理”，不能刷新恢复旧正文。

独立创建的目标/日程、历史日志/备份与外部副本继续沿用 V0.4 的保留边界；GUI 不另建正文聊天数据库。记忆更正可能产生新 ID 与 supersedes 关系，重新加载后以当前版本为准。

目标页提供 Goal/Milestone/Binding 操作，修改带 expected_revision，完成需要人工证据。Proposal 可查看、接受或忽略；接受只建立 Task，不授予工具权限，Task Completed 也不自动完成 Goal。

NeedsReview 根据已有计划重新审核；已有 attempt 或 Uncertain 不提供一键重放。重启时会话授权为空。更多后端状态语义见 [V0.4 操作指南](../v0.4/operations.md)。

## 设置、托盘与通知

GUI 偏好保存窗口几何、屏幕名称、关窗留托盘、通知开关和安静小时；通知控制器另将 ID/revision/channel 去重记录、游标和基线状态保存到 QSettings。不保存 API key、授权或用户正文。后端有效设置只读展示，调整原配置后完整退出并重新启动生效，不支持热重配。

默认关窗退出。显式启用“关窗留托盘”且系统托盘可用时，关窗隐藏；托盘恢复或退出。无托盘时关闭唯一窗口必须进入退出流程。退出超时保持可见 Closing 与实例锁，等待当前调用、查看诊断或重试，不能强退 Qt 后留下工作线程。

系统通知默认关闭。开启后先建立基线，不补发旧历史；只提醒新 Waiting、Completed/Failed/Cancelled 和有效 Proposal，Uncertain/NeedsReview 须在窗口查看。默认按后端配置时区的 22:00–08:00 安静，小时可调整；结束前重新检查状态/TTL/来源，最多一条通用摘要、最多 20 个有效对象。通知不含私人正文，点击仅打开对应页面并重新读取，不自动确认、恢复或接受。

Windows 可能抑制通知；“应用尝试投递”不代表 OS 已显示。去重先记录尝试，崩溃可能漏提醒；窗口中的当前状态为准。Qt 首版不保证撤回 OS 已显示的通知，因此仅显示通用数量文字。

## 实例与诊断

同 data_dir 只允许一个应用 owner。GUI 运行时 CLI 查询也会被锁拒绝；先完整退出 GUI 再使用 CLI。不含 IPC 唤醒/并行客户端；启动失败画面提供无敏感正文的诊断。

后台初始化、迁移和清理不占用 GUI 主线程。初始化过程中退出，迟到创建的 Application 仍由宿主接管关闭。CleanupBlocked 只开放诊断、清理重试和退出。

## 开发验证

```powershell
python -m pip install -r requirements-gui-dev.txt -c constraints/windows-py312-gui.txt
python -m pytest -q
python -m mypy --strict src scripts/benchmark_gui.py scripts/check_gui_displays.py scripts/check_gui_stability.py
python -m mypy --strict src scripts/check_desktop_capacity.py
python scripts/benchmark_gui.py --mode idle --seconds 300 --output data/validation/v05/gui-idle.json
python scripts/benchmark_gui.py --mode soak --seconds 1800 --output data/validation/v05/gui-soak.json
python scripts/check_gui_displays.py --output data/validation/v05/gui-displays.json
python scripts/check_gui_manual.py --max-seconds 900 --output data/validation/v05/gui-manual-session.json
python scripts/check_gui_stability.py --python data/validation/py311/python.exe --repetitions 10 --timeout-seconds 90 --trace-gc --output data/validation/v05/gui-prefix-investigation.json
```

资源/长测基准使用临时合成库、离线规划器和 Qt offscreen，不读取私人配置，不调用云 API。双屏脚本使用原生 windows QPA，自动移动自己的合成窗口、遍历页面、保存并恢复几何，只截取自己的窗口；不会更改系统缩放或断开显示器。offscreen 不能替代真实托盘、多 DPI 双屏或休眠验收，原生双屏脚本也不代表物理拔插/系统消息交互已测。真实 API 测试继续独立授权。

双屏和系统交互按[Windows 实机记录](windows-checklist.md)补测；已执行的实际结果见[测试报告](test-report.md)。

手工脚本打开原生临时窗口，使用离线固定规划器、独立数据/workspace/QSettings，安静小时仅在临时偏好中关闭；不读取私人配置或修改 Windows 通知设置。它不自动提交、授权、点击通知或操作托盘。按窗口提示启用关窗留托盘和通知，提交合成请求后保持工具授权未选，人工点击真实托盘及 OS 通知；最后从托盘退出。另加 `--default-close` 并使用不同输出路径，可单独检查默认关窗。日志只含事件、ID 和状态；通知投递事件、退出码 0 均不代表人工验收通过，须另记用户实际观察。时限到达只请求正常关闭，后台调用阻塞时仍可能停在 Closing。

低频停滞调查使用最后一条有界诊断命令；需要 gui-dev 与有效 Python 解释器路径。它完整收集、只执行 GUI 前序组合，保留每轮日志和线程转储，不代表额外完整全量通过；调查依据和限制见[稳定性记录](stability-investigation.md)。
