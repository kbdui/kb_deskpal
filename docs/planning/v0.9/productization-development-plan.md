# V0.9 产品化功能开发计划

日期：2026-10-23。用户明确将隔离执行、细粒度授权及有限无人值守执行移到正式版之后，本轮补齐首次配置/模型管理和跨库备份恢复。**原开发阶段只开发和准备测试，不运行测试、mypy、GUI、下载、云请求或真实数据维护操作。** 用户随后已启动集中验证，结果见[测试记录](../../versions/v0.9/test-report.md)。Git 由用户提交，产品版本暂不调整。

## 范围与交付

1. 首次配置向导：离线演示或兼容云模型、地址与模型名、凭据来源、工作区/数据/模型路径；明确生效来源与环境覆盖。可修复损坏配置，但需明确确认、CAS 和保留旧文件，保存不等于已连接模型。
2. 凭据：现有环境变量优先，Windows 可选系统凭据库，TOML 仅保存非秘密引用。向导新密钥只在内存中传递，不落日志/配置/备份；不支持平台继续使用环境变量。显式 `environ` 的配置加载保持隔离，不读系统凭据库。
3. 模型缓存管理：固定已支持的 BGE 中文语义检索模型，状态、显式离线验证、明确许可后的下载准备，以及带版本确认的自有缓存清理。模型不是本地聊天 LLM；缺依赖只给安装引导，不自动安装。已有非托管目录可验证，不任意清空。
4. 四库备份：关闭后台后独占 data_dir，协调 workflow/memory/scheduling/companion 的 SQLite 快照，单文件 manifest/hash/schema 校验与无覆盖发布。工作区原文件、配置/密钥、模型缓存、历史日志不在备份范围，界面须明确展示。
5. 恢复：检查备份后恢复至用户选择的新目录，保留原目录；确认旧备份可能恢复后来删除的数据。中断留下持久标记，阻止初始化半成品，允许相同备份显式续办；完整校验后才解除标记。恢复不产生工具授权、不重算旧计划、不自动重放工具。
6. GUI/CLI 维护入口与固定故障引导。主窗口进入维护先走既有完整退出；维护操作在单个有界后台通道执行，运行中关窗等待资源真正退出。首次缺配置直接进入向导，保存后可启动应用；恢复目录须用户明确采用并保存配置。

## 文件组织与分工

| 责任 | 文件 | 所有者 |
| --- | --- | --- |
| 配置/凭据/缓存实现 | `maintenance/{setup,cache,errors}.py`、`credentials.py`、`config/__init__.py`、`memory/{embedding,cache_lock}.py` | 6.1 Sol 高思考 |
| 备份/恢复/启动屏障 | `maintenance/archive*.py`、`application/factory.py` 中屏障、新合成测试 | 6.1 Sol 高思考 |
| GUI 入口/维护窗口/生命周期 | `gui/{launcher,maintenance*,main,host,window,bridge}.py`、新 GUI 合成夹具 | 6.1 Sol 高思考 |
| 架构/接口/整合 | `maintenance/{service,cli,__init__}.py`、原 CLI 路由、文档和跨模块审查 | 主 Agent |

不新增第二 Core、后台云循环或自动工具权限。缓存读者持共享 OS 租约，下载/清理持独占维护租约；模型初始化失败释放租约，跨实例共享缓存不能在使用中被清理。数据库使用既有 InstanceLock，与 GUI/CLI 争用同一数据目录；本地维护不启动生产工厂来读取设置。

## 接口约定

`MaintenanceService(config_path, cancelled=None, provider_override=None).execute(operation, parameters) -> dict` 为 Qt-free 维护入口，固定参数、有限 JSON DTO，异常统一固定 code/message/next_steps，不回显任意异常或请求。可选 `provider_override="offline"` 只影响本次启动校验，不保存文件。操作为 `setup_status/setup_save/setup_repair`、`cache_status/cache_verify/cache_prepare/cache_clear`、`backup_create/backup_inspect/backup_restore`、`diagnostics`。

`SetupService` 用文件 SHA-256 revision，缺文件为 `missing`；save/repair 先验证、锁定和复核，再保存。配置有效性、凭据存在、模型文件存在和真实模型可用是不同状态，不能互相冒充。系统凭据新引用写入后若配置发布失败，尽力清理新引用，旧引用不自动删除。

备份 manifest format_version=1，精确标识四个已知数据库是否存在、字节/hash/schema，限制文件数及总量，拒绝 ZIP 路径穿越、重复名称、链接、未知附件或不受支持的 schema。SHA-256 证明完整性，不证明备份来自可信作者。恢复标记不接受任意外部路径或清理指令；目标已有不匹配文件时拒绝续办，不覆盖用户新修改。

默认 GUI/维护配置路径优先已有工作目录 `config.toml`，否则 Windows 使用 `%LOCALAPPDATA%/Deskpal/config.toml`，其他平台使用用户配置目录。显式 `--config` 优先。普通 CLI 在存在该默认配置时采用同一配置；没有默认配置时保留原默认离线行为，不自动写入文件。

## 开发与验收分离

本轮允许阅读和静态源码审查、编写合成测试及最小测试清单，不执行任何验证命令或试运行功能。完成后归档实际接口、已实现项、静态审查中发现的调整和未验证限制，再提交最小测试清单，由用户另行启动测试。

V0.6 的封存对象和安全实验不恢复；V0.7 资源归因、HM08 真实模型暂缓项不因本轮开发被改为通过。Live2D、语音、云同步、自由插件和周期性云规划不加入本轮。
