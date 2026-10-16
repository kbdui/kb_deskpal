# V0.5 候选版测试报告

日期：2026-10-16；版本 **0.5.0rc1**。已修复双屏补测发现的布局超屏、跨屏尺寸增长和重开位置偏移；100%↔200% 原生跨屏/恢复 194 项检查通过。最终冻结源码连续 1800.234 秒混合长测通过。此前已按用户要求暂停，现已获明确恢复指令，继续证据核对及稳定性调查；当前进度见[开发交接](development-status.md)。真实 Windows 交互仍按[实机记录](windows-checklist.md)逐项补测。

## 环境、隐私与构建

Windows 11 build 26200 / AMD64 / 16 逻辑核。主环境 deskpal_env：Python 3.12.4；项目内兼容环境：Python 3.11.17。PySide6-Essentials/shiboken6 6.12.0、pytest-qt 4.5.0、python-dotenv 1.2.4、tzdata 2026.5。两环境 pip check 通过。

全部新增测试使用临时合成配置、数据库、QSettings 与离线/fake planner；不读取私人 .env/config/运行数据库，不调用真实云 API。合成 LLMError 不代表真实 HTTP/DeepSeek 链路通过；没有沿用旧版一次性授权。产品源码在长测前冻结；后续测试与文档补充未改产品源码。

| 指纹 | SHA-256 / 说明 |
| --- | --- |
| GUI/安装源码指纹 | `7b679308f34da7b8d89a77aee07b78416567a8c56844f5a3871f816df29c8bac`；排序路径 + src/**/*.py 字节 + pyproject.toml |
| 容量源码指纹 | `28ab4dbd8abc32f5b80f6d73b0f4ec2536870c3f08b4b30024013b1b765d29df`；排序路径 + src/**/*.py 字节，不含 pyproject.toml，因此与上一行不同 |
| wheel | deskpal-0.5.0rc1-py3-none-any.whl；187040 bytes；`4205ebc0ef5bd19ec9df96fe5f685da6aaacb466545504fbfa1af9d6bf5002fa` |

构建核对 wheel 中 **86 个 Python 源文件**与冻结 src 逐字节一致，存在 CLI/gui_scripts 入口，未打入 .env、TOML、SQLite/DB 或 JSONL 用户数据。以上不是签名或干净 Windows 安装器认证。

## 自动化回归

最终产品源码保持不变；补一项 Ready 屏障回归并修正测试助手后，全量（strict-markers/strict-config、faulthandler_timeout=30、每进程外层时限600秒）：**Python 3.12：1203 passed、4 skipped，228.23 秒；Python 3.11：1203 passed、4 skipped，226.79 秒**。均 exit 0、无超时，由 6 Luna 执行。此前 1202 项通过是新增回归前的测试集，不与本次相加。

Python 3.11 首轮与其他回归/容量并发时，在约第 24 项附近超过 5 分钟未继续输出，已中断且不计通过；该次未得到线程栈，原因未定位。随后带 faulthandler 的完整重跑通过；可疑 saturation 单测、Host 7 项、前序组合和原状有界重复也均通过。此次另复现测试助手在 Window 仍为 Initializing 时提前返回，修补仅等待 Ready 槽及初始 settings 回调完成，原精确断言不变；该竞态不能解释原先五分钟停滞，产品源码未改。详细重复范围、负向证据、GC可见性与限制见[稳定性调查](stability-investigation.md)。

两 Python 各执行 `mypy --strict src scripts/benchmark_gui.py scripts/check_gui_displays.py scripts/check_gui_stability.py`（89 文件）通过；容量脚本的 `mypy --strict src scripts/check_desktop_capacity.py`（87 文件）已有同产品源码通过记录，二者独立运行，避免 benchmark_workflow 被不同模块路径重复发现。

6 Luna 已核对最终证据汇总 **35 项通过**。原状 15 轮、完整 GC 5 轮、QObject 销毁观察 2 轮均有界通过；这些只运行选中的 GUI 前序组合，不计成全量。完整 GC 记录见到后台线程收集，但两次 QObject 样本中各 16 个 Bridge / 8 个 Host 的创建/销毁线程编号均相同；不据此宣称原停滞已修复，详细边界见稳定性调查。

| 范围 | 专项实际证据 |
| --- | --- |
| 界面和权限 | 最终源码 GUI 共 76 项通过，窗口专项 37 项；完整参数/hash/风险、普通/高风险默认不授权、精确写工具确认、接受提议不授权、纯文本显示、有界聊天/回调/缓存；大字体、正常窗口尺寸/工作区夹紧及 Ready 启动屏障回归 |
| 迟到界面 | 任务命令发出、mutation 成功、页面选择变化/消失均失效 generation；迟到 Waiting 不恢复旧确认资格；切换 Goal 清空空 deadline |
| 记忆 | 真实 bridge/app 候选审核、更正新 ID/supersedes、遗忘 Pending、迟到正文屏障、重试清理；受理前 Busy/Conflict/Rejected/QueueFull 明确未提交 |
| 后端契约 | desktop 集成 26 项；非阻塞确认、持久 command 幂等、组合快照、真分页、记忆 DTO epoch、通知游标、关闭先停止领取；万级候选过期过滤只解码到期行 |
| 跨进程实例 | 新增 9 项通过、0 skip：GUI→GUI、GUI→CLI、CLI→GUI × 原路径/Windows 大小写/真实 junction；失败实例不释放 owner 锁，退出后可重开 |
| 故障响应 | 新增 3 项通过：真实 factory 中阻塞 embedding、独立 SQLite 写连接争用、合成 planner LLMError；Qt heartbeat/输入可用，控制可响应或明确 pending，Closing 保锁，放行后完整关闭 |
| 通知与托盘逻辑 | quiet/DST/TTL 重核验、disable→enable 新基线/旧 token 失效、旧查询不复用、饱和结果缓冲可恢复、模拟托盘消失后恢复窗口；不算真实 OS 操作证明 |

四项 skip 沿用旧版：两个真实 API gate、两个 Windows symlink 权限项。不把 skip 当通过；真实 junction 与进程竞争没有跳过。

## 最终容量与本地延迟

脚本 scripts/check_desktop_capacity.py --samples 100，双 Python 使用独立临时库并发测量，SQLite trace 开启；约 202.51 / 205.28 秒完成，acceptance_passed=true。同机还运行回归和 GUI 长测，数据反映该次负载。

合成 fixture：1000 Goals、10000 Tasks、20000 attempts、20000 Proposals，另直接 SQL 批量生成 10000 个有效 Memory heads。fixture 绕过正常写入、学习与 embedding，只证明容量，不代替真实执行/写前账本测试。实际延迟闭环增加数据后分别为 10210 Tasks、20210 attempts、20105 Proposals、10210 Memory。

| 每页 100 条，100 个暖态测量样本 | Python 3.12 p95 | Python 3.11 p95 |
| --- | ---: | ---: |
| Tasks facade | 9.07 ms | 8.86 ms |
| Memory facade | 245.52 ms | 241.65 ms |
| Goals facade | 18.34 ms | 22.25 ms |
| Proposals facade | 37.58 ms | 39.51 ms |

全部在既定 300 ms 门槛内。应用 submit/accept 本地段各 100 样本亦在脚本 200 ms 门槛内；不包含最终模型/工具时长。Facade 数据不是所有 GUI 页面端到端渲染延迟，GUI 入队/确认/取消见最终长测。

四类列表均全库稳定翻页、重复读取排序相同、0 重复 ID、DTO 最大 100。内部 SQL 使用最多 LIMIT 101 探测 has_more，2040 次受限查询、0 无界有序 SELECT；禁用兼容全量 list 路径后仍通过。通知 205 条无关事件只用 3 页、每页最多扫描 100，固定上界且游标前进。4 个并行读线程共 100 请求，0 失败、仍只有 1 个 Core worker。两进程关闭均小于 0.5 秒。

## 空闲、原生冒烟与最终长测

最终源码 5 分钟空闲：Python 3.11、Qt offscreen、embedding 关闭、5 秒热身后 **300.141 秒**，业务无失败、源码一致、时长门槛通过。平均 CPU **0.937%**（单逻辑核 100%，不按 16 核摊薄）；RSS 70799360→71184384 bytes，增长 **0.367 MiB**。线程 6→6，句柄 312→293；heartbeat 4787 样本、p95 63 ms、最大 79 ms；正常关闭 0.25 秒。CPU≤2%、工作集增长≤64MiB 均通过；同机后台有独立长测，非整机空载性能。

另补 **原生 windows QPA** 空闲：Python 3.12、同源码/5 秒热身，**300.187 秒**，平均单核口径 CPU **0.921%**；RSS 155693056→156692480 bytes，增长 **0.953 MiB**。线程 6→6，句柄 1301→1280；heartbeat 4784 样本、p95 63 ms、最大 125 ms；关闭 **0.313 秒**。同源码、完整时长与预算通过；原生与离屏绝对内存/句柄不同，保留原值而不混用。

最终原生 windows QPA 冒烟：20.015 秒、10 个终态任务、8 个步骤 attempt、2 次预期取消，0 未完成、0 非预期失败，同源码指纹，关闭 0.266 秒。它证明本机原生 Qt 创建/任务/关闭，20 秒不算 1800 秒长测通过。

**最终同源码混合长测：1800.234 秒，897 次提交、897 个终态、0 未完成、0 非预期失败。** 含 224 次预期取消、673 次核对过的步骤 attempt；每个非取消终态恰好一次 attempt，预期取消没有工具 attempt，确认写入内容一致，任务 ID/终态无重复。执行提交、分页/任务审查、准确确认、慢 planner、取消、通用通知及每轮隐藏/恢复；通知控制器产生 784 次通用投递请求，不等于 OS 已展示。最大 pending 为 2；最终正常关闭 **0.188 秒**，源码起止一致，脚本 exit 0。

| 最终长测指标 | 实际值 | 门槛与结果 |
| --- | --- | --- |
| 50ms heartbeat | 28674 样本，p95 63 ms、最大 266 ms | p95≤100 / 最大≤500 ms，通过 |
| 提交入队回执 | 897 样本，p95 47 ms、最大 94 ms | p95≤200 ms，通过 |
| 准确确认回执 | 224 样本，p95/最大 16 ms | p95≤200 ms，通过 |
| 持久取消请求回执 | 224 样本，p95/最大 16 ms | p95≤300 ms，通过；不代表同步调用被强制停止 |
| 线程 | 全程采样 6，结束 6 | 无持续增加 |
| 句柄 | 343→342，采样范围 341–353；最后十次均为 342 | 无持续增加 |
| 工作集 | 73900032→86695936 bytes，增长 12.203 MiB；OS 峰值 87982080 bytes | 保留绝对值和曲线；最后十次采样约 86.20–86.87 MB，不能据一次 30 分钟断言长期无泄漏 |
| 忙时 CPU | 平均 16.371%，单逻辑核口径 | 混合负载实测，与空闲≤2%分开 |

修复前约 1384 秒和中途约 210 秒的长测均主动中断并标为 superseded，不满足时长门槛、不与新长测累加。以上结论同时核对了时长、账本、heartbeat、回执、资源和关闭，未只依据 passed=true。

## 原生双屏与窗口恢复

原生探针约 55.468 秒，**194 项检查通过**。屏幕 0 的 Qt 逻辑工作区为 `[0,0,1920,1080]`、DPR 1（100%）；屏幕 1 为 `[1920,0,1280,800]`、DPR 2（200%），按 DPR 换算为 2560×1600 物理像素，未单独读取 EDID。两屏逻辑 DPI 均为 96；不把它与设备缩放混同，也没有改动系统缩放设置。

三轮往返的 7 次屏幕访问中，100% 客户区保持 1080×780、200% 保持 1080×769，无累积增长；主/子页、翻页/刷新、业务与表单按钮在滚动后均处于屏幕和所有祖先滚动视口内。两屏保存后重建的 frame 与保存值完全一致，2 逻辑像素容差门槛通过；最小化/最大化恢复、显式调整大小后 hide/show、完整关闭与线程退出均通过。自身窗口截图已抽查；这不是手工拖动、托盘交互或物理拔插证明。

父进程 Popen→flushed Ready **2.266 秒**；脚本内 PySide 导入前→Ready **2.063 秒**。这是缓存已准备、embedding 关闭的一次本机启动，低于 5 秒参考值；没有清空 OS 文件缓存或验证模型冷启动，不宣称完整冷机性能门槛通过。

## 安装与可选依赖

最终 wheel 在两个已有 Python 环境中安装到独立 target；以 -I 启动并断言每个 deskpal 模块来自 wheel target，而非 editable src。两环境 backend/GUI 探针均通过：离线任务 Completed、1 次 attempt、19 工具、GUI 最终 Closed。

另外创建两个 **Python 3.11 干净 venv**（不继承系统包），从本地 wheel 目录离线安装：

- clean-backend：只有项目、python-dotenv、tzdata 与环境自带 pip/setuptools，没有 PySide6/shiboken；backend 真实任务通过。
- clean-gui：增加 PySide6-Essentials/shiboken6，没有 mss、fastembed、onnxruntime、pytest/pytest-qt；backend 和 GUI 真实离线任务/关闭通过。

四环境 pip check 均通过。缺 Qt 的 backend 探针额外禁止 Qt 导入；包清单确认其确实未装 Qt。干净环境证明 wheel 的最小运行依赖，不证明正式安装器、独立 exe 或任意机器兼容。

另在 Python 3.12 wheel target 与干净 3.11 GUI 环境调用未修改的 packaged main：真实 argparse/load_config/QApplication/DesktopHost 从 Ready→Closing→Closed、exit 0、后台线程完整退出。harness 仅把环境和 QSettings 指向临时目录、在 Ready 请求正常关闭；不是人工点击生成的 exe。干净 backend 环境无 Qt 时，同一入口给出安装提示、预期 exit 2，不读取用户配置。

## Windows 缺项与版本结论

实际 100%↔200% 双屏自动跨屏与保存/重建恢复已通过。23:05–23:21 人工会话中，用户确认真实托盘点击激活和菜单恢复、普通通知出现/点击进入任务页/保持未授权、托盘退出及第二窗口默认关窗均正常；事件日志吻合。点击后任务页快照为 Waiting、0 attempts、授权全部未选，该快照不能单独证明点击后新发查询完成，点击前窗口可见，隐藏时通知点击恢复未在此轮单独证明。两种退出 Closing→Closed 分别0.125/0.25秒，后台线程结束、实例锁重新取得、合成文件未写入、产品指纹一致。人工脚本双 Python strict 各87文件通过；原始事件为 `gui-manual-session.json` / `gui-default-close-session.json`，用户观察与范围记录在 `gui-manual-attestation.json`。

23:22–23:27 另开通知抑制会话：用户确认 Windows 勿扰开启时无横幅，任务页仍可刷新、Waiting且0次执行。共3个独立任务和3次投递尝试，关闭前最后观测均Waiting/0 attempts。用户确认系统原状态已恢复、窗口及托盘退出；Closing→Closed 0.25秒，后台线程结束、实例锁重新取得、合成文件未写入、源码一致。此项证明本机该模式下的降级可用性，未测OS内部送达或通知中心留存。原始事件为 `gui-notification-suppression-session.json`。

完整 100/125/150/200% 单屏矩阵、物理拔屏、锁屏/休眠仍**待测**。用户选择本次先完成跨屏/窗口恢复，暂不配合物理拔插；具体证据与步骤见[Windows 实机记录](windows-checklist.md)。

维持 **0.5.0rc1 候选版**。自动化通过也不能替代上述 P1 物理验收；所有 P0/P1 按[验收边界](../../planning/v0.5/acceptance-boundaries.md)完成后再宣称完整 V0.5。

## 原始证据与复现

原始文件在 Git 忽略目录 data/validation/v05/：最终 junit 为 pytest-py312-final.xml / pytest-py311-final.xml，容量为 desktop-capacity-final-py312.json / desktop-capacity-final-py311.json，资源为 gui-idle-final-py311.json / gui-idle-native-final-py312.json / gui-soak-final-py312.json，原生为 gui-native-final-smoke.json、gui-displays-final.json、gui-displays-launch-final.json；自身窗口截图位于 gui-displays-final-captures/，安装核对为 installation-final.json、entrypoints-final.json；wheel 在 wheels/。本报告保留结论，避免必须提交临时环境与大型合成数据库。

superseded、aborted、smoke-resume 和旧无 final 容量文件仅保留排查历史，不计最终门槛。早期 fixture 误用 write_text 的 content 而非 text、源码中途变化、旧空闲 CPU 超标以及提前中断的长测均已排除；修复后重新取得上述证据。

复现命令见[操作指南](operations.md)。未 stage、commit 或 push，Git 提交由用户处理。
