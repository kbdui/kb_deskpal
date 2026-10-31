> 历史存档：保留当时的设计、失败与验收边界；不代表当前实现或新的执行授权。当前入口见 [文档导航](../../README.md)。

# GUI 重构 T1 测试记录

后续更新：10月27日用户反馈后已修补滚轮栈溢出、首次加载、窄窗与管理页布局，并由Agent执行原生交互复验。最新结果和未验证子项见[10月27日修补记录](2026-10-27-repair-report.md)；下文保留10月26日当时事实。

同日继续完成全界面可读性与比例优化，437个唯一用例通过、strict165源文件通过，最终阶段证据见[可读性记录](../../reports/implementation-log.md#全界面可读性与比例调整)。

日期：2026-10-26。状态：**本轮最小自动化集合已完成修补与复测，去重537 passed / 1 skipped / 0 failed/error；strict检查164个源文件通过。** 实机交互按用户决定保留未验证，后续由用户自行检查；不宣称完整发布验收通过。开发静态核对和历史版本通过数不并入本次结果。

依据：[最小测试清单](development-test-plan.md)、[开发范围](development-status.md)、[工作方式](../handoffs/2026-10-24-gui-refactor.md)。Git 由用户提交。

## 执行边界

固定执行由 6 Luna 高思考承担，主 Agent 审查结果、判断首败原因，必要修补交 6.1 Sol 高思考。B1、B2、B3、strict 按顺序执行；失败保留原始证据后定向修补复测，不放宽权限或删除保障断言。

使用项目 `deskpal_env` 解释器，各批独立临时工作目录与数据、合成配置/模型/凭据适配。证据存放 `data/validation/` 下本轮独立目录；不读取生产配置、用户数据库或已有系统凭据，不请求真实模型，不恢复此前暂缓实验。

GUI 自动化使用 Qt offscreen，不能推定真实 Windows 焦点、输入法、托盘通知、多屏 DPI 和休眠行为。跳过、环境限制、超时和未执行单列，不能凭 pytest 部分输出或 XML 将超时批次写为通过。

## 批次结果

| 批次 | 范围 | 当前结果 |
| --- | --- | --- |
| 环境核对 | 项目解释器及测试/Qt 依赖 | exit 0，依赖可用，见下文 |
| B1 | 后端新能力、配置与凭据 | 最终新能力134 passed / 1 skipped；配置127 passed，两批exit 0 |
| B2 | GUI、桌宠与宿主 | 最终首组137 passed；第二组63 passed，两批exit 0 |
| B3 | 迁移、恢复、旧接口与权限回归 | 整文件71 passed；定向5 passed，两批exit 0 |
| strict | src 与两份记忆脚本 | strict-03：164个源文件通过，exit 0 |
| W1 | 最小清单的 Windows 真实操作路径 | 未验证，按用户决定后续自行测试 |

## 最终证据与计数

| 最终采用的 label | 结果 |
| --- | --- |
| `B1-types-final-01` | 134 passed / 1 skipped，exit 0；含3个真实Qt PNG/JPEG解码与预览用例 |
| `B1-config-01` | 127 passed，exit 0 |
| `B2-types-final-01` | 137 passed，exit 0 |
| `B2-pet-final-01` | 63 passed，exit 0 |
| `B3-first-01` | 71 passed，exit 0 |
| `B3-target-01` | 5 passed，exit 0 |
| `strict-03` | 164源文件无issues，exit 0 |

以上全部未超时，执行期间 `src/tests/scripts` Python 源码前后哈希一致。配置和B3的已通过批次未受后续GUI/Qt类型修补影响，未无变化重复执行；受影响的素材/会话/GUI/桌宠路径已作最终复测。六份最终JUnit按 classname+name 去重，538个唯一用例：537 passed、1 skipped、0 failed/error，冲突节点0。定向复测和失败批次不重复计入通过数。汇总为 `data/validation/gui-refactor-20261026/summary-final.json`。

唯一skip是 `test_symlink_parent_and_source_target_overlap_are_rejected`：平台不能创建合成symlink，**该路径仍未验证**。故意构造重复ZIP清单产生的warning不代表验收失败，拒绝该恶意归档的断言通过。没有真实模型联网或已有凭据库读写测试，实机结果不能从offscreen推定。未stage、commit或push。

## 首败、修补与复测

证据根目录为 `data/validation/gui-refactor-20261026/`。

- `probe`：项目解释器为 `D:/anaconda/Anaconda3/envs/deskpal_env/python.exe`，Python 3.12.4、SQLite 3.53.4、pytest 9.1.1、mypy 2.4.0、PySide6/Qt 6.12.0、pytest-qt 4.5.0、python-dotenv 1.2.4，退出码 0。
- `B1-first`：7 文件共收集 132 项，按 `-x` 在第二项停止，1 passed / 1 failed / 0 skipped，pytest exit 1，未超时。首败为 `test_schema_two_backup_default_protection_and_no_fabricated_history`；`WorkflowStore.backup` 的 `os.link` 返回 WinError 5，尚待区分执行沙箱限制与产品故障。没有修改或跳过备份保障。
- 首批 pytest 的退出码、stdout/stderr、XML 均已保存；runner 随后打印非 ASCII 内容遇到 GBK 编码异常，不改变已保存的 pytest exit 1。后续 runner 将显式使用 UTF-8，不覆盖首批证据。
- `hardlink-probe` / `hardlink-native`：在本轮新建目录内，普通文本及关闭后的合成 SQLite 在受限沙箱均 WinError 5；获准的原生权限执行均成功，链接数 1→2。归因为执行沙箱限制，不修改备份/素材的链接保护。后续涉及链接行为的批次使用获准的原生执行上下文，差异保存在证据中。
- `B1-native-01`：原范围 7 文件，14 passed / 1 failed / 0 skipped，exit 1，未超时。首败 `test_unstarted_recovery_without_plan_exposes_only_valid_resume`：无完整计划的 NeedsReview 同时暴露 resume/confirm。后端应要求真实持久计划才能 confirm，无计划仅允许满足条件的 resume；修补及定向复测另记。
- 修补 `TaskReviewService` 的 confirm 投影，增加 `snapshot is not None` 前提，保留其他权限、来源及隐私条件。`B1-resume-target-01-rerun01` 定向复测 1 passed，exit 0，源码前后哈希一致。原 `B1-resume-target-01` 因 runner 将 `::nodeid` 当作文件名，在 pytest 启动前拒绝，仅有 launcher-error 记录；修复 runner 后用新 label 实际执行一次，未覆盖证据。
- `B1-native-02`：16 passed / 1 failed / 0 skipped，exit 1，未超时，源码前后哈希一致。失败 `test_historical_review_survives_unrelated_epoch_and_scrub_never_returns_body` 的 `json.dumps` 直接收到领域 Task 的 datetime。JSON 规范化实际在 DesktopFacade；测试改为通过真实 Facade 的完整 DTO 检查，并增加清理前确有正文的断言。未改变产品序列化或放宽正文清理断言，后续定向/批次复测另记。
- `B1-scrub-target-01`：修补后的隐私 DTO 场景 1 passed，exit 0。`B1-native-03` 原范围 7 文件全批 131 passed / 1 skipped / 0 failed，exit 0，未超时，源码前后哈希一致。唯一 skip 为 `test_symlink_parent_and_source_target_overlap_are_rejected`：平台不能创建合成 symlink，该攻击路径仍未验证；不影响其他独立批次继续。唯一 warning 来自故意构造重复 `manifest.json` 的恶意 ZIP，该拒绝用例通过。
- `B1-config-01`：配置、凭据和维护三文件 127 passed / 0 skipped，exit 0，未超时，源码前后哈希一致。
- `B2-first-01`：首组 GUI 7 文件收集 118 项，29 passed / 1 failed / 0 skipped，exit 1，未超时，源码前后哈希一致。失败 `test_pet_chat_reopens_default_with_shared_drafts_and_no_new_submission` 捕获 Qt 事件循环异常：排队的聊天滚动回调访问已销毁的 QScrollArea/QScrollBar。正在为回调绑定 QObject 生命周期及当前页面代际；不屏蔽 pytest-qt 的异常。
- 修补聊天的排队滚动与卡片定位：绑定 view/card context，同时校验会话、分页和重绘代际；coordinator、window、settings、host 的同类 singleShot 也绑定自身 context。新增真实 QObject 析构及旧滚动失效用例。`B2-scroll-target-01` 在新增析构测试 teardown 首错，21 passed / 1 error，pytest exit 3；stderr 含 PySide 访问冲突，不能记为通过。测试同时手动删除 QObject、交给 qtbot 清理并 monkeypatch 实例方法，teardown 的 undo 访问已删除的 C++ 对象；正在修正测试夹具所有权，保留真实析构和事件循环异常检查。
- 修正析构用例的所有权，取消已析构 Qt 实例上的 monkeypatch；仍真正删除对象并处理事件循环，保留异常捕获。`B2-scroll-target-02` 共 32 passed，exit 0，未超时，源码前后哈希一致。
- `B2-first-02`：42 passed / 1 failed，exit 1，未超时，源码前后哈希一致。首败 `test_uncertain_task_receipt_is_checked_before_any_replay[confirm]`：测试 fixture 停止了 coordinator timer，却未调用 poll 即期待 command_status。保留产品 pending 门禁，测试显式驱动真实 poll，并加强核查前/在途不重放、known=false 后才使用同 command_id 再发一次的断言。
- `B2-receipt-target-01`：上述 confirm/cancel 两参数 2 passed，exit 0。`B2-first-03` 随后 48 passed / 1 failed，exit 1；空列表已清除选择，但订阅者 `show_detail({})` 重建了空详情根。修补 `PagedView.show_detail` 对空 DTO 直接清空，非空和审核树不变；`B2-selection-target-01` 三参数 3 passed，exit 0。
- `B2-first-04`：67 passed / 1 failed，exit 1。关闭状态的发送被阻止，但 `submit` 在可用性检查前调用 `draft()`，重新创建空草稿条目；正在将生命周期门禁前移，并阻止不可用状态的输入编辑。原关闭清屏断言不变。上述批次均未超时，源码前后哈希一致。
- `submit/edit_draft` 在不可用状态提前返回，输入只读；Ready 但模型未配置仍允许保存草稿。新增不可用五状态及未配置模型的定向保障，`B2-closing-target-01` 共 38 passed，exit 0。
- `B2-first-05`：83 passed / 1 failed，exit 1，未超时，源码前后哈希一致。真实 Bridge 的目标/步骤/绑定/建议操作场景，在保存目标后等待 goal_status 回执超时；正在核对刷新回执、所选版本与在途容量，未延长超时或删除 CAS 保障。
- `B2-goals-diagnostic-01`：仅该真实 Bridge 场景 1 failed，exit 1。原 5 秒等待与业务断言保持，诊断只记录操作计数/队列/版本。发操作前 Goals channel 已有 12 项在途（goals/tasks/schedule_items 各3、bindings 2、milestones 1）；发操作后出现容量提示，没有第二个 goal_status send。证明重复只读刷新占用命令容量，正在按五个读取流合并请求，保持原 Channel 上限与写操作语义。
- GoalsPage 改为五个固定读取流，各最多一个在途和一个最新待重读；成功写后必须尾随新读，写操作不合并/重发，旧父目标/分页/隐私代际不应用。另补原 G19 遗漏的消息用户头像显示，主窗/小窗共享应用、替换、重置；图片 clear 同时使旧元数据回执失效。
- `B2-goals-avatar-target-01`：41 passed / 1 failed，exit 1。清敏时列表同步选择信号写回空 snapshot，调整清空顺序，全部控件清屏后再清 snapshot/source。Goal 选择辅助同时等待本次逻辑读取流完成，不能把缓存存在 ID 当作刷新已完成；保留5秒和全部 CAS 断言。
- `B2-goals-avatar-target-02`：42 passed / 1 failed，exit 1。聊天及三个 Goals 保障通过，真实目标流程完成到建议受理；最终任务跟踪超时。导航到无会话任务未切换“安排”的任务子标签，卡片留在隐藏页；另该测试关闭了自动轮询。正在修导航并在该真实场景恢复正常轮询，不手动改缓存或跳过权限。上述批次均未超时，源码前后哈希一致。
- 无会话任务导航现显式进入“任务”标签后装配卡片；真实桥接测试恢复原1800ms coordinator 轮询，保留5秒等待和无授权/无文件效果断言，并新增任务卡可见检查。后续批次验证该路径。
- `B2-goals-avatar-target-03`：43 passed，exit 0。`B2-first-06` 首组7文件 137 passed / 0 skipped，exit 0。两批均未超时，源码前后哈希一致。
- `B2-pet-01`：第二组6文件 28 passed / 1 failed，exit 1。人格清屏旧回执测试把全局新请求都计为重发，而 Ready 状态清理会合法重读日常会话目录。正在将该场景的禁止重发断言明确到 companion 读取/写命令，同时保留清屏和关闭后旧回执不能恢复正文的保障，不移除生产目录重载。
- 该人格测试分别计数 companion/companion_command，保留清屏、关闭、禁用展示并增加关闭后人格读/写不得重发的检查。`B2-persona-clear-target-01` 两参数 2 passed，exit 0；`B2-pet-02` 第二组6文件完整 63 passed / 0 skipped，exit 0。均未超时、源码前后哈希一致。B2 两组共200项通过，Qt offscreen 证据仍不代表实机交互。
- `B3-first-01`：迁移、恢复、Application/Desktop 旧接口和 CLI 五文件 71 passed，exit 0。`B3-target-01`：既定五个精确节点 5 passed，exit 0。均无 skip、未超时、源码前后哈希一致。至此初版最小 pytest 集合去重534 passed / 1 skipped；后续类型修补若改变运行路径，需定向复测。
- `strict-01`：既定 src 与两份记忆脚本检查164个源文件，报告65处错误，exit 1，未超时、源码前后哈希一致。涉及 Qt QByteArray/格式参数、Optional 控件、变量用途混淆、QWidget.render 方法名冲突、回调返回类型及少量 SQL/JSON 类型边界。正在保持 strict 规则修补，不通过 ignore 或删检查目标绕过。
- 类型修补：任务卡方法更名 `render_review`，恢复 QWidget 原生 render 接口；Qt 可空项用真实分支收窄，滚轮按 QWheelEvent 收窄，表单按 QFormLayout 校验，回调明确返回 None，SQLite/已验证 manifest 仅精确表达其真实类型。未降低 strict 规则。
- Qt 纯内存适配探测发现当前 runtime 接受字符串 PNG format，但字节 format 与类型声明不一致；编码改为双方一致支持的 QImageWriter 显式 PNG，QByteArray 使用 `.data()` 后固定为 bytes，PNG 读取省略冲突格式参数并保留 PNG 签名/格式边界。新增真实 PNG（半透明/透明）与 JPEG 归一化/预览测试，检查像素、尺寸、透明度与元数据移除。
- `strict-02`：剩1项 bytes-like 返回类型，exit 1；使用 `bytes(encoded.data())` 明确不可变输出后，`strict-03` 检查164个源文件全部通过，exit 0，未超时、源码前后哈希一致。新增真实图片测试与受影响运行路径正在复测。
- 最终复测完成：`B1-types-final-01` 134 passed / 1 skipped，三个真实Qt图片场景实际执行并通过；`B2-types-final-01` 137 passed、`B2-pet-final-01` 63 passed。全部exit 0、未超时、源码前后哈希一致，后续没有产品源码修改。

## 最终只读收尾核对

证据为 `data/validation/gui-refactor-20261026/final-static/audit-final.json`。`git diff --check` exit 0；36份指定Markdown的481个本地路径链接无缺失，测试清单的文件/节点引用均存在；四旧GUI模块缺失，源码/测试/脚本旧标识搜索零匹配。Git变更仅做路径级核查，无真实配置、环境秘密、数据库、凭据文件或模型数据变更候选。

七个最终采用记录均exit 0、未超时、执行期间Python源码哈希一致；当前304个Python源码/测试/脚本哈希与最后代码版本的strict及三组复测收据完全一致。较早配置/B3批次与当前的差异来自后续GUI修补及SQL/JSON精确cast；后者不改变返回值或锁语义，相关持久会话/归档路径已经最终B1复测，GUI路径已经最终B2复测，没有将这些正常后续修改冒充执行期间哈希不变。

`strict-01` 原输出的65条错误实际位于10个文件，工具末行却称“in 1 file”；原输出完整保留，未据此少计错误或修改工具。最终判据为strict-03实际exit 0与164源文件无issues。

## Windows 原生尝试与未验证边界

`w1/native_w1.py` 使用真实 Launcher/Host、Qt `windows`、新建的合成配置/四库/workspace/图片和显式独立 QSettings；offline、embedding disabled、网络与凭据访问被阻止，通知默认关闭。脚本属于忽略证据目录中的验收夹具，不改变生产入口。

`w1/run-20261026-122727-fa1e1037b4/lifecycle.json` 记录原生 Ready、两个屏幕（DPR 1/2）元数据，以及 900 秒上限触发的普通关闭：Closing→pet_settled/host_finished→Closed→event_loop_returned(0)。没有强杀或操作用户数据。这只证明该隔离实例的启动和普通生命周期，不能推定视觉或交互通过。

Computer Use 在窗口选择后的首次状态/激活操作返回 `Computer Use app approval timed out`，未取得界面截图或完成输入动作；重新枚举时该隔离实例已按其15分钟上限正常退出。监督进程也以 exit 0 结束。输入法、实际 Enter/滚轮、图片导入显示、桌宠点击/拖动/小窗焦点、跨屏 DPI、托盘通知和休眠交互仍未验证；不会用直接脚本驱动控件冒充物理桌面结果。

用户随后明确选择：**完成自动化，实机交互先记为未验证，由用户之后自行测试。** 本轮不再请求窗口控制或开展 W1 交互，操作要点见[用户手工检查表](manual-checklist.md)，完整范围见[W1 操作路径](development-test-plan.md#w1-物理桌面路径)。
