# V0.8 最小测试报告

后续代码变更提示：2026-10-22 [拟人化 H0–H5](humanization-development-status.md)已经修改源码但尚未运行验证。本文是此前最小实现的历史测试证据，不表示当前工作区通过；后续按[HM01–HM08](../../planning/v0.8/humanization-minimal-test-plan.md)集中验收。

日期：2026-10-21（Asia/Shanghai）。执行范围为用户指定的[最小T01–T09](minimal-test-plan.md)，不使用旧建议清单的编号。测试与必要修补已完成；**T07有并发受理范围限制，T09有两个环境跳过，不宣称全版本或全部Windows行为验收通过。** Git由用户提交。

## 结果与结论

三个新增定向测试批共76个节点通过、无跳过。主环境既有回归首轮1019通过、1失败、2跳过；唯一失败是旧测试对只读端点的过时断言，经审查更新后在受影响集119项中通过。strict首轮4项错误已修补，最终106源文件检查通过。Python3.11受影响兼容集171项通过、无跳过。各批存在重叠，不能把通过数简单相加。

| 用户编号 | 定向节点 | 结果与实际覆盖 |
| --- | --- | --- |
| T01 配置并发与重置 | 4 | 两线程同实例/独立SQLite连接以及两个Desktop客户端的同revision冲突；回退建新版本；重置保留真实临时记忆和任务 |
| T02 人格与执行不变性 | 1 | 三预设/cue下完整Planner输入一致；同一Task原canonical/hash/参数/两类风险/缺授权结果保持，重确认不规划、不执行工具 |
| T03 隐私与调用预算 | 5 | 所有Provider chat/generate被录制；禁用聊天1次、启用聊天2次、工具2次、管理0次；candidate_review单列；云wire不因本地轴/偏好/cue/affect变化，local_only意图零请求 |
| T04 去重与提交失败 | 9 | 根任务重复与reset/config后不刷增量；确认命令重试只有一次工具/一次终态效果；完成提交失败不奖励成功；步骤回执失败为Uncertain且不重做 |
| T05 状态计算一致性 | 9 | 三半衰期、60/3600精确边界、正负绝对预算、32/256数量上限、逐条/分批与不同投影频率同as_of一致；投影不写锚点 |
| T06 时间与重启 | 4 | 正常离线衰减与存活实例对照；重启保留去重/额度；cue/session不恢复；回拨和超过30天跨度回基线且额度保守冻结。历史完成反馈另由T08/旧pet基线测试确认不重播 |
| T07 遗忘与旧任务竞争 | 2 | **受限覆盖**：生产入口在模型等待时Busy拒绝、未建删除job；Core窄隐私屏障使迟到表达/终态效果被丢弃；空闲后真实遗忘可完成；reset失败Pending且重试成功。不支持、也没有验证“执行中受理真实遗忘” |
| T08 GUI竞态与生命周期 | 42 | 真实Qt事件循环及QImage/QPixmap；新鲜预读CAS、旧回包拒绝、保留编辑、隐私/Closed清屏、单在途、缺图/错误/过期回退、有限完成反馈不重启，迟到回包不复活窗口 |
| T09 既有回归 | 首轮1022 | 任务/工具/记忆/授权/恢复、真实GUI/CLI进程同目录竞争及七字段兼容；首轮唯一过时断言已定向复测；两符号链接权限项保持跳过 |

T07的限制不是用低层模拟冒充生产并发删除：测试先通过真实Application入口观察Busy拒绝，单独调用Core屏障验证代际，然后在任务空闲后再完成真实删除。若产品要求在运行中受理遗忘，需另行设计管理通道及跨存储清理时序，本轮没有偷偷放宽管理门禁。

## 环境、执行与批次证据

主解释器为 `D:/anaconda/Anaconda3/envs/deskpal_env/python.exe`（3.12.4），兼容解释器为仓库 `data/validation/py311/python.exe`（3.11.17）。两者pytest9.1.1、pytest-qt4.5.0、PySide6-Essentials6.12.0、mypy2.4.0；实际QtCore/QtWidgets导入成功，qVersion=6.12.0。GUI用offscreen真实事件循环，不能代替物理桌面DPI/托盘/输入验收。

主Agent冻结范围/命令/时限、阅读首败与修补；关键测试及修补由6.1 Sol高思考完成，固定执行和证据收集由6 Luna高思考完成。所有运行使用临时合成库、workspace/QSettings、注入时钟与录制Provider；清除外部凭据/配置环境，禁用live API，不读取真实配置或用户数据库。

证据根为 [`data/validation/v08/minimal-20261021`](../../../data/validation/v08/minimal-20261021/)。各批JSON记录完整命令、解释器、时限、退出码及起止UTC，stdout/stderr和JUnit XML独立保存。

| 批次 | 结果 | 墙钟耗时/上限 | 证据 |
| --- | --- | --- | --- |
| integration-r1 | 12通过，exit0 | 7.063s / 300s | [命令](../../../data/validation/v08/minimal-20261021/integration-r1.json)、[日志](../../../data/validation/v08/minimal-20261021/integration-r1.stdout.txt)、[XML](../../../data/validation/v08/minimal-20261021/integration-r1.xml) |
| unit-r1 | 22通过，exit0 | 2.813s / 300s | [命令](../../../data/validation/v08/minimal-20261021/unit-r1.json)、[日志](../../../data/validation/v08/minimal-20261021/unit-r1.stdout.txt)、[XML](../../../data/validation/v08/minimal-20261021/unit-r1.xml) |
| gui-r1 | 42通过，exit0 | 5.984s / 300s | [命令](../../../data/validation/v08/minimal-20261021/gui-r1.json)、[日志](../../../data/validation/v08/minimal-20261021/gui-r1.stdout.txt)、[XML](../../../data/validation/v08/minimal-20261021/gui-r1.xml) |
| regression-r1 | 1019通过/1失败/2跳过，exit1 | 134.078s / 600s | [完整选择集](../../../data/validation/v08/minimal-20261021/regression-r1.json)、[首败](../../../data/validation/v08/minimal-20261021/regression-r1.stdout.txt)、[XML](../../../data/validation/v08/minimal-20261021/regression-r1.xml) |
| recheck-r2 | 默认导入收集失败，exit2 | 2.125s / 300s | [首败日志](../../../data/validation/v08/minimal-20261021/recheck-r2.stdout.txt) |
| recheck-r3（最终主环境复测） | 119通过，0跳过，exit0 | 16.344s / 300s | [命令](../../../data/validation/v08/minimal-20261021/recheck-r3.json)、[日志](../../../data/validation/v08/minimal-20261021/recheck-r3.stdout.txt)、[XML](../../../data/validation/v08/minimal-20261021/recheck-r3.xml) |
| strict-r2（最终类型检查） | 106源文件无问题，exit0 | 0.641s / 180s | [命令](../../../data/validation/v08/minimal-20261021/strict-r2.json)、[日志](../../../data/validation/v08/minimal-20261021/strict-r2.stdout.txt) |
| compat311-r1 | 171通过，0跳过，exit0 | 19.656s / 300s | [命令](../../../data/validation/v08/minimal-20261021/compat311-r1.json)、[日志](../../../data/validation/v08/minimal-20261021/compat311-r1.stdout.txt)、[XML](../../../data/validation/v08/minimal-20261021/compat311-r1.xml) |

`regression-r1`的tests/gui含新增42项，与gui-r1重叠。recheck-r3含全部76项新增测试、旧pet controller和window；兼容集另外含七字段表现测试。没有因类型/命名修补重跑无关1022项或扩展V0.6实验。

最终默认命令入口（实际执行命令含独立XML/basetemp，见上表）：

```powershell
python -m pytest -q tests/unit/test_v08_companion_runtime.py tests/integration/test_v08_companion_integration.py tests/gui/test_v08_companion_gui.py tests/gui/test_v07_pet_controller.py tests/gui/test_window.py
python -m mypy --strict src scripts/check_memory_api.py scripts/benchmark_memory.py
```

## 首败、修补与保留缺口

1. 旧v0.7测试只允许presentation，新增companion只读查询使其失败。改为精确允许这两个端点并断言全部空参数，继续禁止任何submit/confirm/tool；reply helper按receipt定位presentation的generation，不拿最后一个其它请求。其余断言未放宽。
2. strict首轮4项：runtime分支内重复声明edits；GUI字段style覆盖QWidget.style()；两个跨for变量类型冲突。分别改为style_edits/preference_edits、style_inputs及独立有明确类型的循环变量；GUI测试仅同步控件字段名，不改协议style字段或业务断言。
3. 三个新文件原先同名test_v08_companion.py，单文件批可通过、合并默认收集发生import mismatch。永久改成runtime/integration/gui三个不同basename，最终使用默认pytest导入模式通过。临时importlib模式的recheck-r2b 119通过仅保留为诊断，不作为最终门槛，不靠全局配置隐藏问题。
4. 两个环境skip为test_documents的符号链接能力与test_sandbox的WinError1314（没有创建符号链接权限），原skip原因保留；本轮未提权补测，不宣称这些权限项通过。
5. 首Qt探针在查询不存在的完整PySide6发行包metadata时退出，被执行Agent误归因为导入失败；主Agent审查纠正。修正探针先输出实际Qt导入/qVersion，并明确Essentials与完整PySide6发行包差异，原失败保留。最终环境以qt-probe-r2与规范化盘点为准。
6. **证据限制**：执行Agent的strict wrapper硬编码文件名，误覆盖首次strict-r1原始文件。首次4条诊断曾经由主Agent完整工具读取，另有[明确标为重建的记录](../../../data/validation/v08/minimal-20261021/strict-r1-first-failure-reconstructed.txt)；它不是原始日志，不能作为原件引用。覆盖后的strict-r1文件不用于报告结论，最终通过只引用独立strict-r2。

本轮没有真实模型风格/事实评审、物理DPI/托盘测试、安装构建或资源长测。V0.7长测预览/内存未决项继续保留，V0.6保持暂停，Live2D延期。当前证据支持上述最小自动化范围与类型检查，不能扩大为完整产品验收。
