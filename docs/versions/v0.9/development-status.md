# 产品化补齐：开发记录

日期：2026-10-23。状态：**源码整合和集中自动化修补复测完成；主环境 1,289 passed / 2 skipped / 0 failed，兼容环境 169 passed / 1 skipped，strict 145 文件通过。** 真实模型/物理桌面与符号链接权限边界见[测试记录](test-report.md)，不宣称完整版本验收。产品版本仍为 `0.5.0rc1`，Git 由用户提交。

## 本轮范围

按用户本轮决定，隔离 worker/强制停止、细粒度授权与有限无人值守执行移至正式版之后。这里实现原缺项中的后两项：首次配置/模型管理/故障指引，以及协调四库备份恢复。开发依据见[计划](../../planning/v0.9/productization-development-plan.md)，实际契约见[接口](interfaces.md)，使用流程见[操作说明](operations.md)。

| 能力 | 已写入源码的行为 | 主要文件 |
| --- | --- | --- |
| 首次配置 | 缺配置进入向导；展示文件值与生效来源；修订冲突拒绝保存；保留未编辑配置；明确确认后重建坏配置并保留原文副本 | `maintenance/setup.py`、`gui/maintenance.py` |
| 凭据 | 环境变量优先，可选 Windows 系统凭据；TOML 只保存引用；显式测试环境不读系统凭据；保存不发云请求 | `credentials.py`、`config/__init__.py` |
| 模型缓存 | 有界状态清单、CPU 离线验证、明确许可下载、资产 manifest 与哈希、限定自有缓存清理、使用中拒绝维护 | `maintenance/cache.py`、`memory/cache_lock.py`、`memory/embedding.py` |
| 失败指引 | 固定错误分类与下一步提示，不回显任意 SDK/系统异常、请求参数或密钥 | `maintenance/errors.py`、`maintenance/service.py` |
| 协调备份 | 关闭应用后持同目录实例锁，用 SQLite backup 收集 WAL 中已提交数据，四库归档与完整性/schema 校验，无覆盖发布 | `maintenance/archives.py` |
| 可续办恢复 | 新目录恢复、显式回退确认、持久标记、严格匹配续办、恢复完成前阻止生产初始化 | `maintenance/archives.py`、`application/factory.py` |
| GUI/CLI 入口 | Qt-free 维护命令；GUI 独立后台维护；主应用、桌宠和维护线程依次完整退出后才能切换 | `maintenance/{service,cli}.py`、`gui/{launcher,maintenance,main,host,bridge,window}.py`、`application/cli.py` |

主 Agent 负责方案、接口和整合；配置/缓存、归档恢复、GUI 分由三个 6.1 Sol 高思考 Agent 实现。新增关键代码没有全部由主 Agent 独自承担。

## 静态审查后的调整

- 将恢复入口放在独立维护阶段，不能与运行中的 Core 并行写四库；主窗口的维护请求必须等待 Host 完整结束，不能只隐藏窗口。
- 将配置文件有效、有效配置可尝试启动、已有凭据、缓存元数据曾验证、本次离线模型验证分别表示。统一复用纯 URL 策略，避免向导接受外部 HTTP 地址后运行时才拒绝。
- 普通保存按原文修改并比较完整 TOML 语义，复杂写法无法证明保留时拒绝。明确修复使用重建语义，可处理语法正确但内容不合法的旧文件；界面提前说明其他设置会重置、原文副本可能含误填秘密。
- 缓存租约从 provider 初始化持续到 close；同进程多个读者共用 OS 锁引用计数。清理先资产与子目录、后 manifest，删除失败后允许相同 revision 重试；出现未知或变更文件则停止。成功保留空缓存根目录。
- 恢复时当前目录只作为所有权检查，损坏、Pending 或尚未创建不阻止恢复到新的独立目录；配置已指向未完成恢复目录时，可显式续办且只锁一次。
- 恢复选择区分“父目录＋新目录名”和“已有未完成目录”。恢复完成只提供填入配置，仍需明确保存，不自动换库或启动。
- 有未保存配置/密钥输入时禁用启动，避免启动前复查静默覆盖编辑；GUI 本次 `--offline` 从启动器传递到维护校验，保留原文件 provider。
- 超时值统一要求正数且有限；旧 TOML 的 `nan/inf`、零、负数或超大整数不能污染 JSON 状态回执，向导仍能取得修订并明确修复。

集中测试后又修正两处产品实现：Windows 文件清单使用真实 `os.stat` 链接数，避免目录条目占位元数据误拒普通文件；Windows 缓存租约采用 `LockFileEx` 真共享读/独占写，替换不能满足跨进程共享的 CRT 路径。测试同时修正配置原文相邻位置/CRLF 字节夹具和启动固定 `busy` 分类断言，未放宽实际安全规则。

## 测试交接

原开发交接时只阅读源码、审查差异及编写测试，未执行验证。用户随后授权“开始测试”，已集中执行合成自动化、strict、Qt offscreen、Windows 原生跨进程锁和专用随机合成 WinCred 目标；首败、修补与复测见[测试记录](test-report.md)。没有运行云请求、真实模型下载或用户数据备份恢复；历史开发阶段的未测试说明不代替当前证据。

夹具包括 `tests/unit/test_product_setup.py`、`test_product_credentials.py`、`test_model_cache_management.py`、`test_product_archives.py`、`test_product_maintenance_service.py`、`test_product_maintenance_cli.py`、`test_product_native_windows.py`，以及 `tests/integration/test_product_restore.py`、`test_product_backup_content.py`、`tests/gui/test_product_maintenance_gui.py`。统一验收按[最小 P01–P08](../../planning/v0.9/productization-minimal-test-plan.md)进行，不因开发分工重复运行全仓或真实模型。

## 当前边界

- 四库归档包含私人记录，未加密、未签名；哈希只用于完整性。旧归档可能恢复之后已遗忘的数据，不是隐私删除完成证明。
- 不包含工作区原文件、配置、凭据、模型资产、日志或操作系统设置；恢复并不恢复外部工具已产生的副作用。完整机器迁移需要分别准备这些资产。
- 已知旧 schema 先按原样恢复，正常启动由既有迁移和任务恢复流程处理。未完成任务仍须复核；不继承运行授权、不自动重放未知步骤或追加云请求。
- 只管理固定中文语义检索模型；没有增加本地聊天模型下载器、任意模型目录删除、自动 pip 安装或聊天服务自动探测。同步 SDK 中的下载/验证仍只能协作停止，窗口需等待其退出。
- Windows 专用随机凭据、跨进程锁、文件硬链接发布、Qt offscreen 生命周期与合成故障恢复已有本轮证据；不推广为真实模型下载、物理桌面交互或全部平台验收。两个真实 symlink 创建用例受 Windows 特权限制，单独保留。当前归档限制单库 512 MiB、总数据库 1 GiB；模型清单最多 4096 条、总资产 4 GiB。
- 发布安装、升级包与稳定性验收仍需后续工作。V0.7 资源诊断和 [HM08 真实模型暂缓项](../v0.8/hm08-deferred-issues.md)继续保留；本次开发没有将其改为通过。
