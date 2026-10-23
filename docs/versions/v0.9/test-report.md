# 产品化补齐：集中测试记录

日期：2026-10-23。状态：**本轮集中自动化完成，首败已修补并复测；主环境去重 1,289 passed / 2 skipped / 0 failed，Python 3.11 兼容 169 passed / 1 skipped。** 原生 Windows 锁、专用随机假凭据和 strict 通过；真实模型下载/实际 CPU 推理、物理桌面交互及两个 symlink 创建用例未验收。依据[最小 P01–P08](../../planning/v0.9/productization-minimal-test-plan.md)，不宣称整个版本或发布验收通过，Git 由用户提交。

## 范围与证据

本轮先执行新配置/凭据/缓存/归档恢复和维护 GUI 用例，再执行受影响的既有回归与 strict。测试使用临时配置、合成四库、录制模型/SDK 和 Qt offscreen；不读写私人数据库或既有系统凭据，不发送云请求。不恢复 V0.6 实验、HM08 真实模型或 V0.7 暂缓长测。

固定执行由 6 Luna 高思考 Agent 承担；故障归因与修补由主 Agent/6.1 Sol 高思考 Agent 负责。证据位于忽略目录 `data/validation/v09/productization-20261023/`，每个 label 独立保存命令、时间、退出码、stdout/stderr、XML 和源码前后 SHA-256。首败保留，复测不覆盖。

原生跨进程锁、专用随机假凭据的 WinCred 调用与 fake 测试分别记载。未执行的真实模型、物理桌面或平台验证不得从合成通过中推定。

## 初始批次

主环境：Python 3.12.4、SQLite 3.53.4、Qt 6.12.0、pytest 9.1.1、pytest-qt 4.5.0、mypy 2.4.0。所有初始批次源码前后哈希一致。

| label | 范围 | 结果 | 秒 |
| --- | --- | --- | --- |
| `main-probe` | 解释器与依赖版本 | exit 0 | 1.594 |
| `main-initial` | 7 个新后端测试文件 | 155 passed / 1 skipped / 5 failed，exit 1 | 57.407 |
| `gui-initial` | 维护 GUI | 25 passed，exit 0 | 6.969 |
| `strict-initial` | `src` 与 3 个既有检查脚本 | 145 文件，4 项类型错误，exit 1 | 16.703 |

初始失败：配置保留测试对未知 TOML 字段的相邻位置断言失败；4 个缓存流程在文件清单阶段触发 `st_nlink != 1` 安全拒绝。类型错误涉及缓存锁 fd 推断、归档流类型和 GUI 联合类型/元组长度推断。具体归因及修复后结果在下方补记，初始证据不覆盖。

归档符号链接测试因当前平台不允许创建测试链接而跳过；此项不计为链接攻击验证通过。

## 修补与复测

文件元数据 probe（`file-stat-probe-sandbox`）在专用临时目录创建普通文件和硬链接。Windows `DirEntry.stat(..., follow_symlinks=False)` 两种情况均返回 `st_nlink=0/st_ino=0`，而 `os.stat`、`Path.lstat` 和 `os.fstat` 正确显示普通文件链接数 1、硬链接 2。缓存清单改用真实路径元数据，保留 `nlink == 1` 和 reparse 拒绝；新增资产/manifest 的实际硬链接拒绝测试。没有通过放宽链接规则来绕过失败。

配置保留测试改为逐项原文保留、完整 TOML 语义等值，并继续要求恢复副本与原始磁盘字节一致；初轮过严要求插入新字段后仍保持未知字段与下一表头相邻，属于测试错误。

`main-retest`：162 passed / 1 skipped / 1 failed（exit 1，52.750 秒）。缓存 4 项和新增硬链接 2 项通过；四库完整 WAL 内容对照通过。剩余配置用例抵达恢复副本断言后，暴露测试夹具在 Windows `write_text` 下 CRLF 与期望 LF 字节不一致，随后按实际磁盘原始字节修正夹具，最终通过见下表。符号链接创建限制仍单独记录。

`native-lock-initial`：3 passed / 1 failed（exit 1，27.250 秒）。进程退出释放及应用/维护双向互斥通过；第二进程共享 reader 被 `msvcrt` 锁误拒为 Busy，属于产品缺陷。随后改用真正支持 Windows 共享/独占的 OS 锁，最终通过见下表；首批失败不能当作共享读取证据。

`strict-retest`：145 文件通过（exit 0，14.594 秒）。

`regression-core`：779 passed / 1 skipped（exit 0，123.734 秒），涵盖 CLI、配置/LLM、记忆/检索、实例、任务/工具/授权/计划和七字段模型。跳过的是 `test_sandbox.py` 的真实 symlink 创建（WinError 1314），没有按通过计算。

`regression-integration`：stdout/XML 为 190 passed、pytest 用时 298.18 秒，但整个子进程超过 runner 的 300 秒上限，receipt 为 exit 124 / timed_out，实际 301.250 秒，**此批不能记为通过**。XML 显示 `test_agent_flow.py` 的 5 项约 172.72 秒，其余 185 项约 120 秒；后续按该文件与剩余文件拆分为两个有界批次，要求实际退出码 0。

`regression-gui`：144 passed / 6 failed（exit 1，89.625 秒）。失败均为 `test_instance_processes.py` 在 canonical/case/junction 路径、GUI 为 contender 时期待旧异常类名 `InstanceLockError`，而实际收到固定启动故障分类 `busy`。更新回执契约断言，保留实际子进程所有权、失败关闭不释放 owner、正常关闭和重新打开的检查。

`native-vault`：专用新随机目标在沙箱 write 阶段失败（exit 1，4.750 秒），清理检查通过；不输出目标或合成值。通过工具 `require_escalated` 获准后，以新 label `native-vault-escalated` 重跑，1 passed（exit 0，3.719 秒），中文/非 BMP 合成值写读删及最终不存在检查通过。此次只操作新随机 `Deskpal/LLM/<uuid>` 目标，不枚举或读取已有目标、配置或私人数据；首跑权限限制与获准后的原生证据分别保存，fake vault 不代替原生结论。

Windows 缓存锁最终改用 `LockFileEx/UnlockFileEx`：共享读/独占写均立即失败返回，原生宽度句柄、完整 `OVERLAPPED` 和 binding 状态由同进程 registry 持有至最后读者释放。保留 Unix `flock`；Windows 可锁 EOF 之外的固定区间，无须初始化字节，避免读者争写。获取/释放异常均回收 fd 和引用登记；两项新增故障测试确认 fd 真正关闭、错误不带底层私有正文、之后仍可重新取得独占。

## 最终复测批次

各 pytest label 都使用独立 cwd、`--basetemp` 和 JUnit XML；回执包含实际绝对命令、解释器、开始结束时间、timeout、退出码及全部 Python 源码前后 SHA-256。下表仅写实际结束的批次，未完成项不先标通过。

| label | 范围 | 结果 | runner 秒 |
| --- | --- | --- | --- |
| `native-lock-final` | 真跨进程共享/独占、最后引用、正常/异常进程退出、应用/维护双向锁及跨 data 共 cache | 4 passed，exit 0 | 38.047 |
| `product-final` | 原 7 个后端文件＋四库非空 WAL 内容/排除项 | 165 passed / 1 skipped，exit 0 | 50.781 |
| `strict-final` | `src`、memory API/benchmark/HM live 检查脚本 | 145 文件无类型错误，exit 0 | 16.890 |
| `gui-final` | 维护 GUI＋真 GUI/CLI 实例竞争（canonical/case/junction） | 34 passed，exit 0 | 93.781 |
| `embedding-final` | 既有 cache-only/CPU/归一化及检索回归 | 39 passed，exit 0 | 4.453 |
| `integration-agent` | 原 Agent 组合/任务/工具/授权/记忆持久化 | 5 passed，exit 0 | 5.969 |
| `integration-rest` | 其余 17 个恢复/隐私/生命周期/人格/七字段集成文件 | 185 passed，exit 0 | 217.797 |
| `compat-probe` | Python 3.11.17、SQLite 3.53.4、Qt 6.12.0；pytest/pytest-qt/mypy 同主环境 | exit 0 | 2.797 |
| `compat-product` | Python 3.11 同 8 个后端文件 | 165 passed / 1 skipped，exit 0 | 57.219 |
| `compat-native` | Python 3.11 真共享/独占及应用/维护锁 | 4 passed，exit 0 | 35.422 |

所有上表回执 `workspace_unchanged=true`，未使用放宽断言或跳过失效流程的方式取得通过。兼容批唯一 warning 为主动构造恶意重复 ZIP 名时 Python 3.11 `zipfile` 的 `Duplicate name: manifest.json`，拒绝该归档的断言通过。

主环境机械汇总按 JUnit `classname + name` 去重，后续成功覆盖先前失败，Python 3.11 另列：后端 166（含 1 skip）＋core 780（含 1 skip）＋integration 190＋GUI 150＋native 5＝1,291 唯一用例，其中 1,289 passed、2 skipped、0 failed。`gui-final` 的 34 项与前批重叠，39 项 embedding 与 core 重叠，不重复加总。strict 不计入 pytest 数量。去重脚本及结果为证据目录内 `summarize_junit.py` 和 `junit-dedup-summary.json`。

## P01–P08 覆盖与复现

主解释器为 `D:/anaconda/Anaconda3/envs/deskpal_env/python.exe`；兼容解释器为仓库 `data/validation/py311/python.exe`。执行器剥离继承的 `DESKPAL_*`、`PYTEST_*` 与密钥/token/password 等变量，再明确设置 `PYTHONPATH`、UTF-8、Qt offscreen 和关闭 live API 开关；每批 cwd 独立，不加载真实配置或 `.env`。

执行形式为 `python data/validation/v09/productization-20261023/runner.py LABEL --python INTERPRETER --kind pytest --timeout SECONDS TARGETS...`；mypy 使用 `--kind mypy`。实际绝对命令与完整目标、环境版本、时限、stdout/stderr/XML 均保存在相应 `LABEL.json` 和同名文件中，首败不覆盖。最终目标清单另见同目录 `selection.json`。WinCred 仅专用节点显式设置 `PRODUCT_NATIVE_VAULT_TESTS=1`，其他批次不设置。

| 清单 | 本轮实际证据 |
| --- | --- |
| P01 | setup/CAS/env/路径/修复/原始字节副本；主、兼容后端和维护 GUI |
| P02 | fake vault、配置与 CLI 隐藏输入/秘密回执；获准后真实 Windows 随机合成目标写读删 |
| P03 | fake SDK 的许可/取消/stage/manifest/hash/篡改/部分清理重试；真实硬链接拒绝；真实下载及推理另列未验证 |
| P04 | 主与兼容环境真跨进程 reader/writer/退出/应用与维护锁；原 GUI/CLI canonical/case/junction 竞争；初始化/解锁失败释放 |
| P05 | 四库非空业务记录；仅复制主文件证明关键记录确实只在已提交 WAL；备份恢复逐库完整 SQL dump 一致，含预算/来源/人格/删除工作单/墓碑/身份；排除诱饵文件 |
| P06 | ZIP 名称/重复/大小写/链接/额外附件/hash/schema/体积/ZIP64/截断拒绝，原目录无覆盖 |
| P07 | 标记/stage/逐库发布/清标记故障及原归档续办；变化/缺失/未知文件拒绝；生产四库构造前屏障；NeedsReview/Uncertain/授权/隐私/预算保留回归 |
| P08 | Qt offscreen 维护/启动/关闭/迟到回调/未保存输入/密钥清空、真实 GUI/CLI 子进程互斥、Qt-free CLI、旧工具/任务/记忆/授权/恢复/七字段及 strict |

## 最终边界

- 本轮覆盖的自动化与原生系统合成检查没有剩余失败。两项真实 symlink 创建（归档父路径和既有 sandbox）受 Windows 未授予特权限制而跳过；ZIP 中伪造 symlink、实际硬链接拒绝和 junction 实例别名的通过不替代这两项。
- 未经本轮实际验证的内容：真实 BGE 下载/托管准备后的实际 CPU 推理，以及首次向导/维护窗口的物理桌面点击、显示与系统交互。缓存端到端仅验证 fake SDK 和真实本地文件/锁；GUI 使用真实 Qt 事件循环但为 offscreen。不能据此宣称模型或实桌面完整验收。
- 主环境最初集成批 exit 124 保留；拆分后 5＋185 项均真实 exit 0，不把截获的 `190 passed` 单独当作进程成功。WinCred 沙箱首败也保留，原生成功来自获准后的独立批次。
- 没有发送云请求、下载模型、读取已有系统凭据或操作私人四库；专用合成 WinCred 目标清理检查通过。未恢复 V0.6、V0.7 暂缓长测或 HM08 真实模型问题，也未进行打包/发布/升级/长期稳定性验收。
- 工作树保留供用户审查，未 stage/commit/push。产品版本仍为 `0.5.0rc1`。
