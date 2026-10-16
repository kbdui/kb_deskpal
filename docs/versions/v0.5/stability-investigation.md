# V0.5 测试稳定性调查

日期：2026-10-16。结论：**首次 Python 3.11 停滞的根因仍未定位；已单独复现并修补测试助手的 Ready 时序竞态。** 产品源码/pyproject 保持 `7b679308f34da7b8d89a77aee07b78416567a8c56844f5a3871f816df29c8bac`，没有为假设性原因修改产品。

## 原事件与证据边界

首轮全量在与其他回归/容量并发时，约 23 个完成标记后停滞超过 5 分钟，随后中断，没有原始线程栈。该边界对应 bridge 8 项、desktop 11 项、host 前 4 项之后，但不能排除前项 teardown 或后项 setup。此前带 faulthandler 的全量重跑、单项十次与前序组合均通过；这些结果不等于根因修复。

独立静态审查未发现可证明的应用层锁逆序或无界 retry。Host 的通知 clock 闭包存在 Python 引用环，以及 worker 在锁内发 Qt queued 唤醒，都是待取证的生命周期/原生阻塞线索，尚无线程栈证实发生了跨线程销毁或死锁。此次不关闭 GC、不改变 QObject 所有权，也不通过放宽断言获得通过。

## 原状有界诊断

`scripts/check_gui_stability.py` 使用独立子进程，完整收集测试以保留导入压力，再仅执行 bridge/desktop/host 组合。逐项输出 setup/call/teardown 和开始/结束，每项 20 秒线程转储，收集/退出阶段另设计时器，不宣称整段连续重复转储；每进程 90 秒外层时限、首失败即停，中断时回收本脚本的子进程。GC 观察可选，只记录阶段/线程，没有 QObject destroyed 记录，不强制收集或禁用收集。报告分别记录产品与测试起止指纹、脚本 SHA-256、子进程退出码和日志位置。

6 Luna 执行原状验证：

| 环境 | 独立进程重复 | 每轮收集 / 实际执行 | 结果 |
| --- | ---: | --- | --- |
| Python 3.11 | 10 | 1206 / 26 | 每轮26 passed、exit 0、无超时 |
| Python 3.12，GC观察开启 | 5 | 1206 / 26 | 每轮26 passed、exit 0、无超时 |

两组产品/测试指纹起止一致，测试指纹为 `c5b43ef972a11d2340dabe33a72c643be76c6bf2073b575259001f68cca970b0`。Py312 父日志可见 720 条 GC 事件，线程名均为 MainThread；该版写 fd 2，受 pytest 默认 fd capture 影响，部分 passing 用例中的记录可能被丢弃，不能代表全程 GC，更不能排除 worker GC。它们是 **390 个选中用例执行**，不是十五次完整全量，也不含原测试时的全部并发负载。

结果位于 Git 忽略目录 `data/validation/v05/gui-prefix-original-py311.json` 与 `gui-prefix-original-py312.json`，各自保留原始逐轮日志。诊断脚本初版因缺少仓库根导入路径，在收集时 `scripts` 导入失败，没有执行用例；对应 `gui-prefix-baseline-*` 文件保留，不能计入有效重复。

## 已证实的独立测试竞态

`make_host` 原先只等待后台 `bridge.state == Ready`。后台先更新属性、再发出 queued 状态信号，Window/Host 的 Ready 槽和初始 settings 查询可能尚未处理。后续 saturation 测试立即替换 `send`，晚到的启动请求可能污染精确调用计数。

新增 `test_host_factory_waits_for_queued_ready_and_initial_settings`，在真实后台线程把已有的属性→信号间隙扩大 0.3 秒。旧助手下，实际断言为 `window.lifecycle: Initializing != Ready`，exit 1、无超时，证明助手会提前返回。原始证据为 `ready-barrier-before-v2.log` / `.junit.xml`。

修补仅在测试助手中等待：后台 Ready、Window Ready、Host Ready 计时器已启动、桥接和窗口初始请求均排空。保留 saturation 的精确调用次数和 generation 断言。没有改变产品 Ready 语义、添加固定测试等待或自动重试授权。

初版回归曾尝试热替换注册 Qt Slot，触发 PySide MetaFunction 错误，未到达目标断言；其 `ready-barrier-before.*` 仅为废弃夹具证据。有效回归改为注入普通 Python 状态方法，不替换 Qt 原生槽。

修补后，该新增单项在 Python 3.11 **1 passed / 1.51 秒**、Python 3.12 **1 passed / 1.35 秒**；最终双 Python 回归各 **1203 passed / 4 skipped**，耗时 226.79 / 228.23 秒，均由 6 Luna 在外层时限内执行、无超时，产品指纹不变。**这项竞态只能解释测试时序错误，不能单独解释原先超过五分钟的停滞。**

诊断脚本后续改为在 pytest 启动前复制原日志 fd（不可继承，每个 child 一枚，进程退出时回收），GC callback 写该 fd，以保留 passing call/teardown 和退出阶段的记录，不改变 pytest 捕获或 GC 策略。额外日志 I/O 有观察开销；修补后的有限重复另记，不能用后续脚本行为解释旧日志。

## 完整 GC 与 QObject 销毁观察

日志修补后的 GC 重复均实际收集 1207 项、选择并通过 27 项；Py311 三轮、Py312 两轮，均 exit 0、无超时。产品和测试起止指纹一致，测试指纹为 `950b45bd02dd4872bf19b0563b491f6ea0884f326c832bd1f42c9bbf31c43019`。

| 环境 | GC 事件记录（含 start/stop） | 线程名称与记录数 |
| --- | ---: | --- |
| Py311，3轮 | 980（各490 start/stop） | MainThread 930、commands 32、reads 6、workflow 12 |
| Py312，2轮 | 780（各390 start/stop） | MainThread 748、commands 22、workflow 4、Dummy-1 4、Dummy-2 2 |

这些记录确实包含 worker 触发 GC，但 GC 发生不等于 QObject 跨线程销毁。Qt 对 QObject 线程归属和删除有明确限制，因此另观察实际销毁线程，依据见 [Qt Threads and QObjects](https://doc.qt.io/qt-6/threads-qobject.html)。

随后使用 `--trace-gc --trace-qobjects`，每环境仅执行一轮 27 项，均通过、无超时。观察器仅在诊断子进程中包装普通 Bridge 构造器，连接 Bridge/父 Host 的 destroyed 信号；回调只捕获原始元数据，没有显式持有被观察对象、调用 deleteLater 或改变 GC/销毁策略。新增连接和日志仍有观察开销，不构成原故障环境的完整复刻。

| 每个环境的 QObject 观察 | 创建 | 销毁 | 销毁线程编号与创建线程不同 |
| --- | ---: | ---: | ---: |
| ApplicationBridge | 16 | 16 | 0 |
| DesktopHost | 8 | 8 | 0 |

当前对象未迁移线程，以上仅说明本轮记录中的销毁发生在创建线程；不能推广为永远不会跨线程销毁，也没有复现原停滞。销毁观察在导入产品前显式优先加入仓库 src，并核对全部已加载 deskpal 模块路径：两环境各 81 个模块均来自当前 src。产品指纹保持 `7b679308…29c8bac`，测试指纹保持 `950b45bd…c43019`。

结果分别在 `gui-prefix-after-py311.json` / `gui-prefix-after-py312.json`、`gui-lifetime-py311.json` / `gui-lifetime-py312.json`，均位于 `data/validation/v05/`；脚本双 Python strict 89 文件通过。诊断开发期间的类型守卫修补不计产品故障或有效运行；有效运行均在 strict 通过后执行。

## 复现与后续取证

```powershell
python -m mypy --strict scripts/check_gui_stability.py
python scripts/check_gui_stability.py --python data/validation/py311/python.exe --repetitions 10 --timeout-seconds 90 --trace-gc --output data/validation/v05/gui-prefix-investigation.json
python scripts/check_gui_stability.py --python data/validation/py311/python.exe --repetitions 1 --timeout-seconds 90 --trace-gc --trace-qobjects --output data/validation/v05/gui-lifetime-investigation.json
```

使用 gui-dev 环境，临时合成数据，关闭真实 API gate，不加载个人凭据。再次遇到停滞时，先保留报告、最后阶段和所有线程转储，再判断是否需要原生栈；不要覆盖证据或把任何通过重跑视为已修复。人工 Windows 缺项继续见[实机记录](windows-checklist.md)。
