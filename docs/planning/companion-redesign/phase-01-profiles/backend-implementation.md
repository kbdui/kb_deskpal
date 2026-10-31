# P1 后端接口与数据清单

日期：2026-10-29。状态：开发实现已写入，测试待用户授权统一执行；本页不代表验收通过。

本次实施采用用户最后确认：旧版本角色数据库和受管图片直接清理，不迁移、不制作自动迁移快照；连接配置、API 凭据、外部工作区和模型缓存应用级共用。开发期间没有操作真实生产数据或调用真实模型。旧数据清理仅是首次初始化代码中的行为，尚未在真实目录执行。

## 数据根与归属

配置文件原有 `storage.data_dir` 现在是应用根，不随角色切换改写。`profiles.json` 原子保存版本、选择标识和 `{id,name}` 注册项；标识为随机 32 位十六进制 UUID，不使用显示名拼接路径。当前后台使用 `data_dir/profiles/<id>`，工厂只替换内存中配置的 `storage.data_dir`。

| 文件或资源 | 归属和处理 |
|---|---|
| `workflow.sqlite3` 及 WAL/SHM/journal | 档案独立；任务、执行账本、队列、会话、互动记录、通知收件箱与图片引用均随档案切换。 |
| `memory.sqlite3` 及 WAL/SHM/journal | 档案独立；现有记忆和用途标记不跨档案复制。 |
| `scheduling.sqlite3` 及 WAL/SHM/journal | 档案独立；日历、计时器、调度事件及身份。 |
| `companion.sqlite3` 及 WAL/SHM/journal | 档案独立；现有人格、状态、修订和回执。P1 不重写为 P2 角色卡。 |
| `assets/` | 档案独立的受管头像与桌宠图片，以及资源导入暂存；删除角色会清除该目录。备份复用既有引用校验和图片完整性清单。 |
| `execution.jsonl` 与档案内其他运行资料 | 档案独立；角色删除会清理这些数据。常规备份沿用既有格式，日志不入包；持久执行账本在 workflow 库内。 |
| 应用根 `profiles.json` | 最小注册资料和选择状态，改名不改变稳定 ID。 |
| `.profiles-initializing.json` | 首次初始化的 UUID 回执；中断后使用同一 UUID 重试，不把部分数据库登记为完成。 |
| `.profile-restore-request.json` | 尚未完成的导入请求，只含档案 ID、显示名、归档路径与摘要；失败可重试，或删除对应待完成档案后重新导入。 |
| 档案内 `.profile-creating`、`.profile-deleting` | 创建或删除中断屏障；带标记的档案不得正常打开。删除失败保留现场，可重试删除。 |
| `.restore-in-progress.json`、`.restore-stage/` | 沿用档案备份恢复的失败关闭屏障和有限暂存清单。 |
| 应用根与档案根 `.instance.lock` | 稳定 OS 锁文件；始终保留文件 inode，不能在清理时删除再创建。 |
| 配置、`.env`、系统凭据引用和 API 凭据 | 应用级共用，不复制到档案或明文备份，不进入操作回执。 |
| `sandbox.workspace_dir` | 应用级外部工作区；切换不复制、不删除，不撤销已经发生的文件操作。 |
| `memory.model_cache_dir` | 应用级模型缓存；切换复用原配置，不重新下载模型。 |

首次初始化先检查旧应用根恢复屏障，再在应用锁内预检固定旧角色路径：四库及其 sidecar、`assets/`、`execution.jsonl`。预检拒绝符号链接、Windows junction/reparse point、非普通文件及外部硬链接；与工作区/缓存路径有包含关系时拒绝删除。只有预检全部完成才清理。不会遍历整个应用根，也不清理 `profiles/`、`models/`、其他共享目录和锁文件。注册已经存在则不再次执行旧角色清理。

已注册完整档案要求四库全部存在，不能在打开时悄悄补一份空人格库。受支持旧格式备份若缺 companion 库，只在显式导入阶段建立空库，并通过 `companion_defaults_created` 回执说明。

## 操作接口

`deskpal.application.profiles.ProfileService(config_path=None, cancelled=None, provider_override=None)` 提供同步 `execute(operation, parameters) -> dict`。操作在 GUI 维护线程执行；服务不启动聊天模型、任务执行器或调度线程。`MaintenanceService` 同样支持下列七个操作。

| 操作 | 参数 | 成功结果 |
|---|---|---|
| `profile_list` | `{}` | `profiles: [{id,name,selected}], selected_id`。已有注册时只读；首次调用在没有后台占用时初始化默认档案。 |
| `profile_create` | `name` | `status: created, profile_id` 及列表；初始化独立四库和固定日常聊天，不复制旧角色数据，不自动选择。中断后同名待创建项可重试，也可删除。 |
| `profile_rename` | `profile_id, name` | `status: renamed`、稳定 ID 及列表。 |
| `profile_select` | `profile_id` | `status: selected` 及选择状态；只保存下次打开的选择，不表示后台已打开。 |
| `profile_delete` | `profile_id` | `status: deleted` 及列表；若删除当前选择，`selected_id: null`，必须重新选择。 |
| `profile_backup` | `profile_id, destination` | 既有完整备份 manifest、路径与摘要；界面调用前明确提示私人数据范围。 |
| `profile_restore` | `archive, name` | `status: restored, profile_id, startup_review_required` 及列表；导入新独立 UUID，不覆盖或自动选择已有档案。 |

未知操作/参数、无效名称或 ID 失败。维护边界使用固定错误码和消息，不把异常原文传到 GUI。新错误码为 `profile_not_found`、`profile_selection_required`；其他失败复用原有 busy、restore、archive 和路径安全分类。

导入先完整校验归档，再持久保存请求。中途失败后，该档案显示在列表中且恢复标记阻止打开。相同备份路径、摘要和名称可继续既有恢复；需要更换归档时先删除对应失败档案，删除会清除其请求。不支持直接覆盖正在使用的角色。归档已发布、注册尚未发布的崩溃窗口，通过逐文件摘要及格式校验重新确认后登记，不直接运行未校验目录。

旧 `backup_create` 现在解析当前选择档案；`setup_status` 的有效 data_dir、诊断与缓存维护也采用相同解析。底层 `backup_restore` 保留作为离线恢复至新目录的维护 API，但不会自动注册为角色，不得通过修改应用根来“采用”这个目录；角色入口应使用 `profile_restore`。

## 生命周期、锁与恢复

`create_application` 先取得原应用根锁，再解析所选档案，取得该档案锁，之后装配资源。任意当前档案后台仍持有应用根锁时，档案切换、创建、删除、维护及第二套生产后台均不能进入。退出使用既有 `ApplicationService.close`：队列、提醒泵、规则线程、调度线程和数据库全部停止后才依次释放档案锁与应用锁。关闭超时不释放锁，因此不会提前打开另一个角色。

GUI 在 Host 完整关闭后调用 `profile_select`；维护 worker 退出后才重新打开 Host。档案服务本身不强杀线程，也不在旧 Core 内替换数据库连接。已有 DesktopFacade 会话代次、Host 新实例和独立 Core 生命周期共同使晚到旧回执失效。

打开时任一必要数据库缺失或损坏都应失败，并释放本次已取得资源与锁，允许重新选择。companion 构造异常不再降级为只运行其他三个库。恢复屏障在旧应用根首次清理之前、以及目标档案打开之前均生效。

恢复后的任务继续复用 `ExecutionJournal.recover`：非终态且未有可靠完成证据的任务转审阅、队列转 Blocked；Started/Uncertain 尝试不自动重做。已提交成功步骤不再执行。档案切换和恢复不撤销外部事实；旧 Pending 不能证明备份之后没有发生外部操作。现有 ReminderInbox 启动时间边界将停用期间逾期事件记为 missed，避免集中补弹。本阶段定向验证覆盖范围见[验证记录](verification.md)，不代表真实桌面与全部恢复分支验收通过。

## 检查与验证状态

已进行源码审阅，包括应用工厂资源所有权、锁释放顺序、注册原子发布、恢复请求与删除屏障、目录有限预检和现有账本恢复路径。开发时一次初版导入因项目未安装报 `ModuleNotFoundError: deskpal`；后续获准测试通过显式源码路径与专用环境执行。最终 10 个本阶段改动源文件 strict 检查通过。

用户明确开始测试后，6 Luna 高执行 `tests/unit/profiles/test_profiles.py`、档案 Qt offscreen、受影响的归档／维护／会话／Host／锁／提醒回归，并补充跨档案业务生命周期集成用例，父 Agent 处理修补。最终去重 213 通过、1 跳过（平台不支持合成符号链接）。批次日志、首败与修补及矩阵见[验证记录](verification.md)。仅使用临时合成数据和离线替身；原生实机及真实模型未验证，不写“完整验收通过”。
