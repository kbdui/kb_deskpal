# V0.5 开发交接与暂停记录

更新时间：2026-10-16 23:28，Asia/Shanghai。16:44 已按用户要求在测试结束后暂停，随后获明确恢复指令；**本轮自动化收尾、有界调查及人工托盘/通知补测已完成**。产品源码继续保持下述冻结指纹，尚有其他实机验收及原停滞调查未结，不宣称完整 V0.5。

新增工作约定：简单、机械、低风险、结果明确的测试执行，主 Agent 判断后委派 **6 Luna 子 Agent**；故障归因、设计与修补审查由主 Agent 或高思考审查 Agent 负责。

本次恢复由 6 Luna 执行固定验证：最终证据35项通过；原状Py311十轮/Py312五轮（每轮26项）、修补后GC三轮/两轮、QObject观察各一轮（每轮27项）均有界通过。新增 `scripts/check_gui_stability.py` 保留完整收集压力及逐项阶段，20秒故障栈、90秒外层时限、首失败即停，保存产品/测试起止指纹和实际导入路径；GC/对象销毁观察可选。原状GC父日志的捕获缺口已另行补正，解释见[稳定性调查](stability-investigation.md)。

另复现并修补测试助手提前返回：旧助手在 Window 仍为 Initializing 时返回；现在等待两端 Ready 槽和初始settings完成，原精确断言保留。产品源码未改，新增一项回归，两环境各1203 passed / 4 skipped。完整日志见测试报告；**该测试竞态不等于首次五分钟停滞的根因**。

## 当前实现与冻结点

版本 **0.5.0rc1**。已实现 GUI/后台桥接、任务与准确计划授权、记忆审核/更正/遗忘、Goal/Milestone/Binding/Proposal、设置、托盘和默认关闭的通用通知；详见[接口](interfaces.md)和[操作指南](operations.md)。

双屏补测修复布局超屏、跨屏往返尺寸增长、保存位置重复叠加标题栏偏移、临时工作区夹紧覆盖用户正常尺寸。独立审查完成后冻结产品源码，后续只补证据和文档。

- GUI/安装指纹（src/**/*.py 与 pyproject.toml）：`7b679308f34da7b8d89a77aee07b78416567a8c56844f5a3871f816df29c8bac`。
- 容量指纹（仅 src/**/*.py）：`28ab4dbd8abc32f5b80f6d73b0f4ec2536870c3f08b4b30024013b1b765d29df`。
- 最终 wheel：`data/validation/v05/wheels/deskpal-0.5.0rc1-py3-none-any.whl`，187040 bytes，SHA-256 `4205ebc0ef5bd19ec9df96fe5f685da6aaacb466545504fbfa1af9d6bf5002fa`。

## 已完成验证

1. Python 3.12：1203 passed / 4 skipped，228.23 秒；Python 3.11：1203 passed / 4 skipped，226.79 秒，外层600秒均未超时。GUI 76 项、窗口 37 项、desktop 集成 26 项包含在全量中。
2. 双 Python strict：src + GUI/双屏/稳定性脚本89文件通过；src + 容量脚本87文件已有同产品源码通过记录。
3. 双 Python 容量测试通过：1000 Goals / 10000 Tasks / 20000 attempts / 20000 Proposals / 10000 Memory 合成基线，真实稳定分页与有界通知游标；详见[测试报告](test-report.md)。
4. Windows 原生 100% / 200% 双屏：194 检查通过；三轮往返尺寸稳定、两屏保存/重建位置一致、最小化/最大化恢复及各层滚动视口按钮可达。暖缓存 Popen→Ready 2.266 秒。
5. 最终源码原生任务冒烟：20.015 秒，10 个终态任务、8 次 attempt、2 次预期取消、0 非预期失败，关闭 0.266 秒。
6. 离屏空闲 300.141 秒：单核口径 CPU 0.937%、RSS 增长 0.367 MiB；原生窗口空闲 300.187 秒：CPU 0.921%、RSS 增长 0.953 MiB。两者线程保持 6、关闭小于 0.5 秒、预算通过。
7. 四环境 wheel 安装/逐字节源码核对与 pip check 通过；干净 backend 不含 Qt，干净 GUI 不含截图/embedding/测试库；packaged main 的正常关闭和缺 Qt 提示已验证。
8. 23:05–23:27 与用户完成三次原生临时会话：托盘点击/菜单恢复与退出、默认关窗、普通通知展示/点击保持未授权、勿扰无横幅但任务仍可刷新。关闭分别0.125/0.25/0.25秒；三次均线程结束、实例锁重新取得、未写入测试文件、产品指纹一致。用户确认系统勿扰恢复原状态；所有临时验收窗口已退出。手工脚本双Python strict各87文件通过。

Python 3.11 首轮全量在高并发验证期间停滞，已中断；后续带 faulthandler 的完整重跑，以及可疑单测/Host/前序组合/十次独立重复均通过。**原停滞原因未定位，不能把重跑通过视为根因修复。** 4 个 skip 为旧版 API gate 与 Windows symlink 权限项；真实 junction/大小写/跨进程实例竞争没有跳过。

## 暂停前最后完成的测试

`scripts/benchmark_gui.py --mode soak --seconds 1800 --warmup-seconds 5 --output data/validation/v05/gui-soak-final-py312.json`，主环境 Python 3.12，临时合成数据、离线 planner、Qt offscreen；记录开始时间约 16:13:13，计量在 5 秒热身后开始。

最终 **1800.234 秒**，897 个提交/终态、0 未完成、0 非预期失败；224 次预期取消、673 次步骤 attempt，任务/终态无重复，确认写入内容一致。heartbeat p95 63 ms / 最大 266 ms；提交回执 p95 47 ms，确认/取消各 p95 16 ms。线程一直 6，句柄 343→342（最后十次均342），RSS 增长 12.203 MiB、OS 峰值 87982080 bytes；忙时单核口径 CPU 16.371%。最大 pending 2，正常关闭 0.188 秒，源码起止一致、exit 0。完整指标见[测试报告](test-report.md)。

结束时没有本任务仍在运行的测试或子 Agent 工作；仅保存记录后暂停。此前 superseded/aborted 长测不与本次累计。

## 恢复后的待办与边界

- 继续调查首次 Python 3.11 全量停滞；当前没有可复现根因，不做猜测性源码修补。
- 三次人工会话已正常结束，证据为 `gui-manual-session.json`、`gui-default-close-session.json`、`gui-notification-suppression-session.json`，用户观察与限制另存 `gui-manual-attestation.json`。`scripts/check_gui_manual.py` 可复用；其退出码和 manual_acceptance_complete=false 保留完整矩阵边界，不能替代用户确认。原始事件的机械核对见 `gui-manual-evidence-check.json`。
- 125% / 150% 与系统设置缩放切换、物理拔屏、锁屏/休眠，以及严格冷缓存启动仍未测。首次通知点击前窗口可见，隐藏状态点击恢复未在本次单独证明；慢阻塞调用时的托盘退出由自动化覆盖，本次实机退出均无阻塞调用。[Windows 实机记录](windows-checklist.md)保留步骤和缺项。
- 用户本次选择先完成跨屏与窗口恢复，暂不配合物理拔插；不要把该项计为通过。
- `data/validation/v05/check_final_evidence.py` 已在此次恢复后由 6 Luna 执行，35 项通过；原始汇总在 `final-evidence.json`。它只核对已有自动化证据，不消除人工缺项或首次停滞调查项。
- 保持 rc1，不宣称完整 V0.5。后续产品源码若变化，应重新取得相应最终回归/安装/同源码长测证据。

原始 JSON、junit、窗口截图、wheel 与临时测试环境均在 Git 忽略的 `data/validation/v05/` 及 `data/validation/py311/`；不要提交个人配置、密钥或这些大型环境。没有读取私人配置/数据库或调用真实云 API；旧版有限云授权不延伸。

工作区改动保留给用户审阅和自行提交；未 stage、commit 或 push。
