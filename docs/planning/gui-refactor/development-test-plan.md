# D1–D5 开发交付后的最小测试清单

初稿：2026-10-25；按最终源码核对更新：2026-10-26。用户现已授权启动 T1，实际命令、首败与复测结果见[测试记录](test-report.md)。本清单只定义范围，不能把列出的用例或 AST 无语法错误当成验收通过。

设计验收编号见[实施与验收](implementation-and-acceptance.md)，源码接口见[开发者说明](../../developer-guide/gui-refactor.md)，实际开发范围和限制见[开发记录](development-status.md)。

## 执行顺序

在装有项目 GUI 开发依赖的测试环境、仓库根目录执行。全部使用临时目录与合成数据；不复用用户生产配置、数据库和密钥。固定执行按交接由 6 Luna 高思考承担，首败归因和修补交主 Agent/6.1 Sol 高思考，记录命令、退出码、失败与定向复测。

本次核对补入最后开发阶段已迁移、原清单未列出的配置/凭据/维护、旧确认调用方、恢复和当前 schema 回归。B3 使用明确文件或用例，避免“按差异选择”遗漏。以下为本轮首轮最小集合，不自动扩展到全仓或恢复暂缓实验；参数化后的实际用例数、耗时和环境缺项留待执行记录。

| 批次 | 覆盖与停止条件 | 对应设计 |
| --- | --- | --- |
| B1 后端新能力 | 默认保护/迁移、提交事务、持久历史/有限上下文、清理屏障、确认幂等/重启/完整权限、提醒与资源目录、素材/备份 | G01–G07、G11、G13、G17–G21 |
| B2 图形组件和宿主 | 五入口、共享草稿、审核状态、分页定位、图片回执、输入/滚轮、缩放与关闭、维护敏感通道 | G03–G10、G12、G14、G16、G18–G20 |
| B3 既有行为回归与类型检查 | 原有 memory/workflow/用途和权限、旧库迁移、CLI Qt-free、桥接容量与主机故障 | G07、G10、G13、G15 |
| W1 真实 Windows | 输入法、焦点、托盘、透明穿透、混合 DPI/屏幕变化、静默/休眠/重开 | G08–G11、G14 |

先执行 B1/B2 的最小集合；出现首败先保留证据并定向解决，再继续下一批。不用放宽断言、取消权限校验或忽略未知结果制造通过。接口有意变化的旧测试按新事实迁移，原始安全保障保留。

## B1 后端与配置

```powershell
python -m pytest -x tests/integration/test_persistent_conversations.py tests/integration/test_gui_refactor_d2.py tests/unit/test_appearance_assets.py tests/unit/test_appearance_archives.py tests/unit/test_gui_model_connections.py tests/unit/test_product_setup.py tests/unit/test_product_archives.py
python -m pytest -x tests/unit/test_config.py tests/unit/test_product_credentials.py tests/unit/test_product_maintenance_service.py
```

主要应证明：

- schema1/2 到3、旧计划/hash/attempt不改写；默认身份/名称/归档/删除保护，超过旧四会话/八轮限制仍能浏览存档。
- submit 的任务/回合/消息一致提交，重复与失败不留下半条记录；Waiting 到终态更新，不混入未完成事实。
- 精确计划、完整普通/高风险权限、CAS、受理与执行分离；旧 receipt 不恢复 grant，重启仅由唯一 worker 恢复未尝试计划。
- 到期与当前计时/日程分开；短计时晚关联补时间线、同任务多提醒、源修改/取消、身份世代变化、未读重算、清理竞争不复活正文。
- 素材 token/generation/profile CAS、透明/超限/损坏拒绝、差分回退、受控发布中断恢复、格式2素材往返与格式1兼容；第三硬链接/坏归档保留现场拒绝。
- 云默认未配置不发请求、本地回环无云回退、旧凭据源保持、keep/replace/remove/CAS失败保护；凭据和图片不进入通用回执或云。
- 显式离线/系统凭据模式不读取未选中的环境密钥；维护 `can_start` 与模型 `llm_ready` 区分，错误和回执不泄露密钥，配置失败不改写原文件。

## B2 新版 GUI 与桌宠

```powershell
python -m pytest -x tests/gui/test_chat_refactor.py tests/gui/test_pet_refactor.py tests/gui/test_window.py tests/gui/test_bridge.py tests/gui/test_host.py tests/gui/test_host_faults.py tests/gui/test_product_maintenance_gui.py
python -m pytest -x tests/gui/test_v07_pet_window.py tests/gui/test_v07_pet_controller.py tests/gui/test_v07_pet_assets.py tests/gui/test_v07_pet_host.py tests/gui/test_v08_companion_gui.py tests/gui/test_humanization_conversation_gui.py
```

Qt 无显示环境可在测试进程中选 offscreen；其结果只能说明组件逻辑。不得用 offscreen 代替 W1 原生实机结论。

GUI 测试入口在缺少 PySide6 时可整体跳过；这种结果必须记录为依赖/环境缺项，不能因退出码为 0 就写为 GUI 通过。执行前确认项目 GUI 开发依赖与 pytest-qt 可用；本次清单核对没有探测或安装环境。

关注同 revision 迟到回执、清理命令自身的新 epoch 回执、两窗同草稿只提交一次、早期历史有界分页与旧任务精确定位、普通聊天不显示内部任务卡、审核失败停用旧控件。图片需要覆盖选择新图时旧回执、保存中切槽、失败不忙死、重置与 crop/mode 实际同步。

原 test_window/Host 测试从旧内部属性迁到共享协调器和新页面，不能因为界面结构变化删除关闭、隐私与防重放保障。没有被源码静态核对发现问题，也不能写为测试已通过。

## B3 回归与类型检查

```powershell
python -m pytest -x tests/integration/test_humanization_migration.py tests/integration/test_product_restore.py tests/integration/test_v04_application.py tests/integration/test_v05_desktop.py tests/test_v04_cli.py
python -m pytest -x tests/unit/test_v04_workflow.py::test_reopen_and_online_backup_preserve_queue tests/unit/test_v07_presentation_models.py::test_existing_current_schema_gains_indexes_once_without_changing_tables tests/integration/test_humanization_application.py::test_hm02_same_intent_exact_plan_hash_across_personality_and_cue tests/integration/test_v08_companion_integration.py::test_t02_presets_cues_do_not_change_planner_or_same_task_plan_authority tests/integration/test_v08_companion_integration.py::test_t04_confirmation_replay_runs_one_tool_and_one_terminal_emotion
python -m mypy --strict src scripts/check_memory_api.py scripts/benchmark_memory.py
```

上述集合固定覆盖旧库迁移/恢复屏障、当前库重开与索引、旧 CLI/Facade 授权和幂等、单实例/关闭锁、记忆清理与迟到回执，以及人格/cue 不改变计划与权限。schema3、完整授权和 GUI expected_revision 是本轮明确变化，旧 schema2 夹具仍保持真实旧结构，不通过改 version 数字伪造迁移。

只有失败证据或尚未解决的边界表明需要时，才扩展到相关 workflow/memory/maintenance、容量或全仓回归；不把这些扩展混入本轮首轮最小批次。

CLI Qt-free、无密钥的只读浏览、单实例与恢复标记阻断需在不含 Qt 或不同测试依赖环境分别记录。真实模型表达实验仍按 HM08 暂缓决定，不作为本轮固定离线回归的前置条件。

## W1 物理桌面路径

1. 新配置：默认进入云连接设置，未配置可看本地记录；保存、生效与显式连接检查反馈不同，聊天没有每条模式选择或人格快捷入口。
2. 在日常和专项会话输入中文，候选确认不发送，Enter 发送、Shift+Enter 换行；切页/切会话/小窗展开后草稿和消息归属正确。
3. 使用明确的离线演示或受控测试 provider 产生写入/提醒计划：原聊天完成完整审核、受理、执行和结果；终态无确认选框，丢回执与部分效果不得自动重做。
4. 在设置各分区滚轮和打开下拉列表，页面正常滚动、选项不误改；键盘和点击仍可明确选择。
5. 单击/拖动/右键桌宠，所有大小与配置只在设置；小窗可输入，重新打开进日常聊天，复杂审核展开到原任务。
6. 调整60%–160%，跨不同 DPI 屏幕、边缘、负坐标和拔屏，检查画布/命中/小窗位置；穿透后通过托盘打开主窗口进入设置恢复。
7. 本地导入头像/主图/差分、预览后应用、重置、缺差分、坏图和超限；两个聊天窗口与角色同步，无后台抢焦点或突然切回另一角色。
8. 到期、系统通知关闭、免打扰、休眠和退出重开，核对应用未读、错过提醒、来源取消复核和通用通知；不承诺退出时准时。
9. 真实退出有草稿/未保存表单时可返回；关闭留托盘不丢草稿。进入维护会先提示未转移草稿，再等待生产与图片/小窗全部关闭；保存失败保留恢复入口，清理未完成不报成功。
10. 切换五个入口，仅聊天显示会话栏；日常聊天固定、专项会话独立且重开可见。查看工具插件的真实名称、权限与说明不会执行工具；检查暖色布局、长中文/路径、窄屏、空/错/忙状态与键盘焦点是否可读。

每项记录环境、实际操作、观察和证据位置；Windows 不能展示的通知或缺少设备标为环境限制，未做标为未执行，不并入通过。
