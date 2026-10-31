# P1 验证记录

状态：**隔离机制验证完成，未完成原生实机验收**。指定定向用例按 JUnit `(classname, name)` 去重后共 214 个：213 通过、1 跳过。最终有效源码批次无失败。原生桌面、云模型、资源释放可见效果等未验证项见下表；因此不标记 P1 全部验收通过。

## 当前覆盖矩阵

| 项目 | 隔离机制覆盖 | 实机覆盖 |
|---|---|---|
| P1-01 首次清理与重复初始化 | **覆盖**：合成旧库/sidecar/受管资源清理、UUID 默认档案建立、重复初始化不清理新档案、共享 cache/workspace/config 保留。 | 未验证。 |
| P1-02 新档案隔离 | **覆盖**：真实 Application 工厂与临时四库中分别写入记忆、专项会话目录、默认日常聊天 turn、日程和现有人格 preset；A→B 与 B→A 重开后逐项检查 A/B 标记和实际档案路径。学习闭环属于后续阶段，未在此声称覆盖。 | 未验证。 |
| P1-03 A→B→A | **覆盖**：关闭应用后台，通过档案选择切换并实际重建工厂，往返重开后验证各自业务数据。GUI/Host 屏障另有 offscreen 测试。 | 未验证。 |
| P1-04 延迟调用切换 | **覆盖机制**：Event 阻塞的离线 `Planner.plan_context` 持续占用后台；`close(0.05)` 未完成时档案锁阻止切换 B；释放 Event 后关闭完成，任务仅存在 A。没有调用云模型或验证真实模型网络延迟。 | 未验证。 |
| P1-05 损坏档案打开 | **覆盖**：损坏/缺失 store 拒绝启动并释放锁，保留其他档案可用。 | 未验证。 |
| P1-06 改名 | **覆盖**：改名维持稳定 profile ID、数据路径与记录。 | 未验证。 |
| P1-07 固定会话 | **覆盖**：档案初始化固定日常会话并检查正常查询/清理路径。 | 未验证。 |
| P1-08 删除运行档案 | **覆盖机制**：删除流程、共享数据保留、应用/档案锁及 GUI 关闭门槛有定向用例；未验证真实资源释放效果。 | 未验证。 |
| P1-09 备份恢复 | **覆盖**：合成四库备份、导入为新档案、归档清单与资源验证，以及既有归档安全回归。 | 未验证。 |
| P1-10 中途恢复失败 | **覆盖**：注入发布阶段中断、恢复标记阻止打开、同一请求重试恢复；首次初始化中断后同一 UUID 继续创建，重复进入不清除已完成的新档案数据。 | 未验证。 |
| P1-11 停用期间提醒到期 | **覆盖机制**：合成 FakeClock 下 A 的计时器在关闭期间到期；B 无该计时器；重开 A 后记录为 `missed`，无通知/补弹事件。既有提醒 inbox 逾期基线和 scheduling 回归也通过。 | 未验证。 |
| P1-12 实例锁与维护互斥 | **覆盖机制**：同应用根重复后台、档案选择/创建竞争、进程锁和维护生命周期定向回归通过。 | 未验证。 |

## 最终执行批次

使用 `D:\anaconda\Anaconda3\envs\deskpal_env\python.exe`。pytest 每批均从唯一 `data/validation/phase01-.../work` 运行，使用同批独立 `pytest-tmp`、JUnit、stdout、stderr 和 exit code 文件。显式 `PYTHONPATH=<repo>/src`，GUI 设为 `QT_QPA_PLATFORM=offscreen`，移除当前子进程环境中的 `DESKPAL_*` 和常见模型 API key。使用离线配置与测试临时数据库，调用真实 Application 工厂及后端；未启动原生桌面、未调用真实模型、未读取或修改真实用户配置/数据库。文件系统硬链接受沙箱限制的批次获系统审批后，只在合成临时目录受限提权执行。

| 批次 | 用例 | 结果 | 产物目录（含完整日志与 JUnit） |
|---|---|---:|---|
| 后端档案 | `tests/unit/profiles/test_profiles.py` | 11 passed | `data/validation/phase01-elevated-20261029-d600f30878c14092ab61c90bbc7734a5/artifacts/` |
| 档案 GUI | `tests/gui/settings/test_profiles.py` | 6 passed | `data/validation/phase01-gui-20261029-2616b6f3fc2c4397a8d38cd119290596/artifacts/` |
| GUI 维护与 Host | `tests/gui/settings/test_maintenance.py`, `tests/gui/desktop/test_host.py` | 36 passed | `data/validation/phase01-gui-regression-20261029-e0aac252c5d0462587c71ec891d406a5/artifacts/` |
| 产品/外观归档 | `tests/unit/maintenance/test_product_archives.py`, `tests/unit/maintenance/test_appearance_archives.py` | 52 passed, 1 skipped, 1 fixture warning | `data/validation/phase01-archives-20261029-903679130f064e52b12a9da6620f8707/artifacts/` |
| 持久会话 | `tests/integration/application/test_persistent_conversations.py` | 6 passed | `data/validation/phase01-conversations-20261029-1156eda0766c413184f63a72edf4860e/artifacts/` |
| 实例/桌宠 Host 生命周期 | `tests/gui/desktop/test_instance_processes.py`, `tests/gui/pet/test_pet_host.py` | 10 passed | `data/validation/phase01-lifecycle-20261029-5192e1fc537e45668fa198c39c3ea89a/artifacts/` |
| 提醒与调度 | `test_new_consumer_baseline_restart_missed_and_stream_generation_block`, `tests/unit/tools/test_scheduling.py` | 43 passed | `data/validation/phase01-reminders-20261029-4e05c2e1fff44611a07e139a3f670901/artifacts/` |
| 跨档案业务与生命周期补充 | `tests/integration/storage/test_profile_lifecycle.py`：业务数据 A/B 隔离及工厂重开、阻塞 Planner 关闭栅栏、非激活档案计时器 missed、首次初始化中断 UUID 重试 | 4 passed | `data/validation/phase01-lifecycle-final5-20261029-51b324e5e7534959beedf5c065d7603e/artifacts/` |
| 修补后受影响复测 | 后端档案、档案 GUI、GUI 维护 | 44 passed | `data/validation/phase01-retest-20261029-5e015032fb3b44aea647f4f39905033d/artifacts/` |
| 维护备份/恢复复测 | `tests/gui/settings/test_maintenance.py`, `tests/unit/maintenance/test_product_maintenance_service.py` | 72 passed | `data/validation/phase01-maintenance-final-20261029-66371785352841058bf5f441e774d472/artifacts/` |

最终唯一用例数通过所有最终及修补后复测 JUnit 文件按 `(classname, name)` 去重计算：214 个唯一用例，213 passed、1 skipped；另有 71 次重复通过观测，不重复累计。新增跨档案生命周期用例4项均为唯一项。唯一跳过项为 `tests.unit.test_product_archives.test_symlink_parent_and_source_target_overlap_are_rejected`，原因是当前平台不允许创建合成 symlink。归档用例发出一条由 `test_hostile_zip_structure_and_hashes[duplicate]` 生成重复 `manifest.json` 的预期 fixture 警告，不影响退出码 0。

## 有界静态检查

- 对 10 个 Phase 01 改动范围文件执行 `mypy --strict --explicit-package-bases`。修补前发现 6 项类型错误，父代理修正后另有 2 项路径类型错误；最终复查退出码 0：`Success: no issues found in 10 source files`。
- 最终 mypy 日志：`data/validation/phase01-mypy-final2-20261029-8d686514c086434aadcfdb799a41d45f/artifacts/`。
- 首轮 strict 的 6 项和后续 2 项错误均发生于 Phase 01 改动文件，已修复；不把这些静态错误混入 pytest 失败统计。第一次 mypy 命令的重复模块发现错误是命令行路径配置问题，使用绝对 `MYPYPATH` 与 `--explicit-package-bases` 后已完成检查。

## 修补记录与环境诊断

- 父代理修正了 `application/profiles.py` 的 profile ID 校验和无用回执类型、`maintenance/service.py` 的当前/备份路径类型、`gui/profiles.py` 的 Qt `actions` 名称冲突与返回值类型，并同步 GUI 测试引用。其后相关档案和维护批次通过。
- 新增的跨档案集成测试初期有两项测试构造问题并保留了失败日志：P1-10 用了已经初始化的通用 fixture，后来改为未初始化临时根；P1-02 用 `conversation_open()` 创建的专项会话去检查默认会话，后来改为通过 `conversation_ensure_default()` 写入并核对默认日常聊天，同时保留带 marker 的专项会话目录断言。以上均为测试设定错误，最终四个跨档案场景全通过，不是产品缺陷。
- 沙箱首轮档案测试出现 9 passed、2 failed，均被归并为 `archive_io_error`。只在诊断进程内临时替换归档错误包装以捕获异常后，确认原始异常为 `PermissionError(errno=13, winerror=5)`，位于原子硬链接发布；同目录 `os.link` 独立探针亦被拒。没有修改发布逻辑或断言。随后受限提权的合成档案批次 11/11 通过。
- 两个早期批次因 pytest `--basetemp` 位置和 PowerShell argv 传递问题以 exit code 4 结束，没有收集测试。另有一个较早 GUI 批次 11 passed、30 pytest-qt setup errors（系统 Temp 不可写），以及一次无退出汇总的启动重试；均作为历史工具/environment 记录，不计入最终唯一用例数。
- 系统 Python 有 SQLAlchemy 但缺 GUI 依赖；`deskpal_env` 有 pytest、PySide6、pytest-qt，可运行获准的隔离测试。没有安装依赖。

## 口径与未验证边界

最新用户决定为：旧版角色数据不迁移、不留自动快照；首次初始化清除旧四库和受管资源并创建 UUID 默认档案；连接配置/凭据引用、workspace、模型缓存保留，重复初始化不得删除新版数据。此口径已同步至 `acceptance.md` 和开发记录，覆盖旧 P1-01 保留迁移标准。

offscreen Qt 测试只证明机制，不能代替真实桌面操作。Application 工厂与后端服务确实在独立临时根中运行，但没有启动原生桌面窗口或真实模型。P1-02 覆盖记忆、会话目录/聊天、日程和现有人格配置，不代表 P4 学习闭环；全部原生窗口/托盘/资源释放可视行为仍未验证。全仓测试未执行。没有真实数据清理或真实模型调用。
