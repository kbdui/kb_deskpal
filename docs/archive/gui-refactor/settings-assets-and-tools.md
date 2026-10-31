> 历史存档：保留当时的设计、失败与验收边界；不代表当前实现或新的执行授权。当前入口见 [文档导航](../../README.md)。

# 云模型、密钥、形象素材与工具插件扩展

设计日期：2026-10-25，第二轮需求修订。本文为专项设计基线；后续 D1–D5 开发范围见[开发记录](development-status.md)，集中产品验收未执行。交互示意与原生产品分别记录。

关联：[产品设计](product-design.md)、[技术方案](technical-plan.md)、[实施与验收](implementation-and-acceptance.md)。用户的本轮要求优先于首轮“聊天设置快捷入口”“桌宠右键快捷设置”和“逐条选择本地/云”的方案。

完整入口见[设计总览](README.md)，首次配置、保存切换和失败状态见[页面与状态细则](page-and-state-spec.md)。D0 先按用户要求完成文档，后续已获授权开发。

## 1. 界面边界与导航修复

一级导航为 **聊天 / 安排 / 记忆 / 工具插件 / 设置**，默认会话显示名为 **日常聊天**。角色产品定位仍是个人陪伴 Agent，但界面使用“聊天”描述用户操作。

会话侧栏仅挂载在聊天路由下；其他路由使用“一级导航 + 主内容”两列，不能留下 190 像素空位。主窗/小窗共用会话状态，因此隐藏侧栏不清掉目录、草稿或选中会话。窄屏下聊天提供自己的会话下拉，其他页不展示该下拉。

设置之外删除：头像编辑/人格入口、模型切换、本次云用途选择、表达偏好保存控件、素材选择、工具启停和配置快捷按钮。桌宠右键/托盘仅保留窗口动作；大小、置顶、穿透、通知策略等只能在设置中修改。工具执行的本次授权、记忆内容更正/遗忘、任务取消和会话切换属于业务操作，仍在对应页面进行。

首次启动缺配置可直接展示设置内的模型配置区；用户进入聊天后只见说明和不可发送状态，不出现另一份配置表单。配置变更后回到聊天无需重复问“本地还是云”。

## 2. 云默认与真实本地模式

### 现状依据

- [config](../../../src/deskpal/config/__init__.py) 当前 provider 仅有 `offline/openai`，默认 `offline`，`openai` 要求模型名和密钥。
- [factory](../../../src/deskpal/application/factory.py) 多处以 `provider != offline` 判断 cloud，包括记忆检索和 companion；把 URL 改成本机地址并不足以完成真正的本地模式。
- [url_policy](../../../src/deskpal/llm/url_policy.py) 已支持 HTTPS 或 loopback HTTP，并拒绝 URL 内凭据、query/fragment；继续复用而非降低校验。

### 配置语义

| 字段（拟定） | 含义与默认 |
| --- | --- |
| execution_location | `cloud` 默认；用户在设置中选择 `local` |
| adapter / protocol | 实际 API 协议，与运行位置分离；初期复用兼容适配器的已验证能力 |
| endpoint、model | 云/本地各自配置，切换可保留两份配置但只激活一份 |
| credential_mode | 新建云配置默认 `system_vault`；显式支持 `environment`；无需鉴权的本地服务可为 `none` |
| credential_ref / env_name | 非秘密的凭据来源索引，与具体连接配置绑定 |
| request_use_default | 新建云配置普通新消息默认允许云处理；仅在设置管理，不由聊天控件每次选择 |
| revision、active_revision | 已保存版本与运行时实际版本分开，避免“保存了但未生效”无提示 |

“离线演示”是高级诊断模式，保留原 `offline` 兼容，不称“本地模型”。本地模式首期只支持用户已经部署好的本机 loopback 推理端点，不承诺自动安装/运行模型；局域网远程服务不冒充本机。确认协议兼容后才标记可用，不把能建立 TCP 连接当作模型可对话。

这里的云/本地默认针对聊天、规划与表达使用的生成模型。记忆检索的 embedding 配置和已有本地模型缓存是独立能力，保留原值；不能把“默认云聊天”实现为同时切换云向量服务或上传现有记忆索引。相关高级配置仍在设置内，普通聊天不需要理解两类模型的部署区别。

本地连接不得回退到云；拒绝跳转到非本机地址，明确处理环境代理，避免 `local_only` 经代理外发。主机名/实际连接策略一起验证，不能只相信用户提供的 `local=true`。云使用 HTTPS，发送密钥前校验目标；不将旧连接的密钥无提示地转发到用户改过的新主机。

更新 factory、LLM 调用网关、MemoryRuntime、companion、planning/expression 的所有 cloud 判断：以经验证的连接配置能力为准，而非协议名。密钥对于云连接是否必需由可信适配器能力定义，不用虚构占位密钥绕过本地模式校验。

### 云默认不覆盖来源约束

新安装在设置中默认选云，用户保存云配置后，后续普通新消息无需逐条确认去向。保存页清楚说明新输入将由所配置服务处理；自定义人格、长期记忆、历史、模拟情绪仍按各自类别和来源许可筛选。

原数据的 `local_only`、明确撤销、删除屏障不变，切换执行位置不能批量改写它们。命令受理时冻结连接 revision 和用途，切换模型/用途的请求不能在队列中悄悄改去另一服务。需要切换时沿用停止新受理、等待安全关闭、重启生效的生命周期。

旧用户显式 cloud/local 保持选择；旧 `offline` 配置显示“离线演示”，提供设置中的显式迁移，不能凭默认值把现有离线会话送云。默认值同时更新首次配置与生产配置装配的契约，不能只有 UI 选中了云、后端却仍按 offline 启动。

## 3. API 密钥在设置中输入并交给后台

### 复用已有能力

现有 [SetupService.save](../../../src/deskpal/maintenance/setup.py) 已接受 `credential_secret`，在配置 CAS 和实例锁内写入 [WindowsCredentialStore](../../../src/deskpal/credentials.py)，TOML 只写随机 `credential_ref`，配置文件原子替换。[MaintenanceBridge](../../../src/deskpal/gui/maintenance.py) 已有后台通道、错误规范化和完成后清理参数的处理。

因此不另造明文 `.env` 写入、前端存密钥或临时 HTTP 密钥服务。重构的是统一设置入口、可信传递通道、生效语义和异常恢复，而不是推翻已有凭据库实现。

### 用户流程

1. 在“设置 → 模型与连接”填写服务地址、模型名、API 密钥。密码框默认掩码，允许短暂查看本次输入；已保存密钥不回填，只显示“已配置 / 未配置 / 外部环境提供”。
2. **保存配置**只做本地校验与保存，不自动联网、不消耗模型额度。**测试连接**单独明确操作，告知会向当前服务发一个固定、无私人上下文的最小请求，并可能产生少量费用；不使用真实聊天作探测。
3. 若生产应用运行，设置先申请安全进入配置阶段。禁止直接调用会获取同 data_dir 实例锁的 SetupService 与生产实例竞争。界面保持设置表单，等待旧 Host 完整停止后交给独立维护 worker。
4. 密钥通过一次性、只驻内存的敏感命令字段传递给该 worker。普通业务通道不得通过参数 hash、命令 ledger 或自动重试缓存保留秘密；旧 `RequestTracker` 已在本轮退役，新的通用 Channel 同样不能接收凭据载荷。
5. 后台校验非空、大小/控制字符、配置版本、地址及凭据来源；写 OS 凭据库，原子发布只包含引用的配置文件。响应仅含配置 revision、configured/source、requires_restart 和安全错误码。
6. 前端收到保存回执后清空密码输入，再明确“已保存，正在重新连接”或“已保存，等待重启”。实际新 Application 使用新配置成功后才显示“已生效”。失败仍可留在设置修正，不丢聊天存档。

为了避免输入在停机等待期间悬挂，Launcher 可在同进程内转移一次敏感表单所有权；不用磁盘中转。取消、超时、提交结束、离开本次编辑流程均清理控件与请求引用。Python/Qt 不能保证堆中每份字符串都被物理覆写，不能宣传“内存绝对清零”；重点是减少复制、短寿命和绝不落日志/文件。

### 凭据来源与覆盖

现有实现环境变量优先，因此不能保存新密钥后仅显示“已生效”，实际仍使用环境里的旧值。新连接配置显式记录 `credential_mode`，GUI 默认系统凭据库；高级设置可选择环境变量模式。环境级显式配置覆盖仍应显示其优先级和受控字段，不擅自修改用户环境。

旧配置迁移记录当前实际生效来源，不能静默改变外部环境控制。状态 DTO 同时显示 saved_source/effective_source，存在覆盖或未重启时显示差异，不暴露密钥内容或片段。

密钥替换先生成新 reference，配置原子切换成功后才考虑清理旧 reference；失败删本次新写的孤立 reference，保留旧配置可用。移除密钥先明确后续连接不可用，配置引用变更成功后再删除旧凭据；清理失败记录待清理引用，不宣称操作全部完成。不同 endpoint/profile 的凭据不会自动互用。

凭据库不可用时返回明确失败，不能偷偷降级为明文保存。设置可让用户改用已有外部环境变量；不同平台未来通过 `CredentialStore` Protocol 实现安全后端，不让业务逻辑直接依赖 Windows API。

### 拟定接口

| 接口 | 边界 |
| --- | --- |
| model_settings_snapshot | 配置值、revision、active_revision、effective_location、configured、credential_source；永远无 secret |
| model_settings_save | 独立维护通道；非秘密配置 + expected_revision + replace/keep/remove 意图；新 secret 单独敏感载荷 |
| credential_status | 仅存在性、来源、是否被覆盖；不提供 read-secret 给 GUI |
| model_connection_check | 显式触发；固定最小无上下文请求、有超时、无自动重试；地址和鉴权错误归类脱敏 |
| runtime_configuration_status | 保存/关闭/重开/失败阶段和实际生效 revision |

保留现有 `setup_*` 方法作为实现适配或兼容入口，最终名字在实际接口文档中冻结。普通 ApplicationBridge/LLM prompt 不得收到密钥内容，审计仅记操作种类与结果。若用户误把密钥粘贴到普通聊天，不因此被程序当作配置输入。

## 4. 头像与桌宠静态素材

### 功能与界面

设置 → 桌宠与外观提供独立槽位：用户头像、角色聊天头像、桌宠主形象；可选思考/专注/愉悦/提示差分。主形象必需，差分非必需；缺省差分回退到当前自定义主形象，非内置另一角色。

流程为 **选择本地文件 → 解码校验 → 预览与显示方式 → 保存应用 → 后端回执**。头像提供方形/圆形显示预览，桌宠保持宽高比，可选择显示区域；此处是产品功能设计，不在本轮编辑用户上传的图片。PNG 支持透明；JPEG 可用但不会自动抠图。恢复默认按槽位进行，不连带抹掉用户其他配置。

### 导入与存储

- 新增 Qt-free 的 AssetService/Repository/Decoder Protocol；文件 IO、解码和缩放放有界后台 worker。GUI 可注入既有 Qt 图像适配器，领域、CLI 导入不依赖 Qt；不强制为 CLI 初始化图片组件。
- 原始文件以实际内容识别，仅首期 PNG/JPEG，拒绝多帧/动画、SVG、伪装后缀、空图和不支持格式。初始限制单文件 5 MiB、单边 2048、单图像素不超过 4,194,304；整个表情组归一后像素/字节预算与现有 8M/20MiB 有界策略对齐。
- 解码前验证尺寸和字节、解码后再验证；后台受限输入计数，不先无界读入。归一到受支持 RGBA 画布、清理 EXIF 等元数据再编码 PNG，不改写原文件。桌宠透明图的命中区不能为空；全透明形象明确拒绝或要求恢复默认，不制造无法点击的角色。
- 源路径只是一次导入输入，复制后不持续引用该路径。托管路径如 `data_dir/assets/<opaque_asset_id>.png`，ID/文件名由主机生成；拒绝路径穿越，不允许模型提供外部路径充当新素材。
- 在本轮 workflow schema 新增 `asset` 与 `appearance_profile` 元数据，包含槽位、媒体类型、字节/像素、hash、revision 与 asset ID，不把大图塞进 SQLite/普通 JSON DTO/QSettings。
- 有界暂存 → 完整校验与重编码 → 原子发布不可变文件 → 同库事务更新 profile CAS。事务失败保留旧引用，未引用文件由有界清理任务回收；崩溃后不能把半张图片发布为当前形象。磁盘不足/解码失败不更换现用图片。
- 双窗口/多次导入使用 generation + expected_revision，只允许最新成功回执激活。图片缓存键含 asset revision、状态、DPR 和缩放档，旧引用释放后回收；不能每次换图都永久保留缓存。
- 用户形象来源记录为 user_import，不覆盖内置 `provenance.json`，不伪造授权或作者信息。

素材工厂与实际 UI 之间只传受控 asset ID/导入令牌及有界预览，不用普通 bridge 的 128 KiB 参数上限传整张 base64 大图。产品是本机 Qt 桌面，文件选择路径可经专用本地导入服务读取；不存在必须“上传云端”才能换头像的要求。

### 拟定接口

| 接口 | 语义 |
| --- | --- |
| appearance_snapshot | 槽位、当前 asset ID、revision、是否内置和有限图片元数据 |
| asset_import_prepare | 用户选定路径/受控读取句柄、目标槽位；返回临时 import token 和校验结果，不立即替换 |
| asset_preview | token/asset ID、有限尺寸；专用图片传输/本地句柄，不回任意文件路径 |
| appearance_apply | slot、import token、expected_revision、显示区域；成功后返回新 profile |
| appearance_reset | slot、expected_revision；恢复内置，不影响其他槽位 |
| asset_import_discard / asset_collect | 丢弃本次暂存、清理无引用素材；不删除正在使用的资源 |

主窗头像、小窗头像、桌宠分别订阅相应槽位。更新桌宠时重建真实尺寸/稳定透明命中遮罩，使用主形象基准避免表情切换导致鼠标命中闪烁。

### 素材备份与删除

原有维护只备份四个 SQLite，单把 asset ID 放库会导致恢复后图片丢失。因此本轮必须演进备份格式：新 manifest 版本包含四库及受控 `assets/` 文件条目/hash/尺寸上限，保持读取旧 format 1；旧备份没有素材则使用内置默认并提示，不能启动失败。

生产停止/一致快照屏障覆盖 profile 和素材引用；恢复先校验所有引用及文件，再通过现有恢复标记逐步发布，全部就绪后允许启动。拒绝归档路径穿越、重复条目、解压超限和可执行内容。仍不备份 API 密钥，跨机器恢复要求重新配置凭据。

“恢复默认”只更换当前引用；无引用素材在清理周期回收。需要彻底删除自定义形象时，设置提供删除所有托管副本的结果回执，同时说明既有备份副本不被自动修改。图片不参与云聊天/长期记忆，除非未来新增明确、独立授权的视觉功能。

## 5. 工具插件目录与扩展接口

### 现状与首期交付

当前 [BuiltinToolManager.list_tools](../../../src/deskpal/tools/manager.py) 返回注册 `ToolSpec` 的防共享副本，[ToolSpec](../../../src/deskpal/tools/models.py) 已有 name、description、parameters、permission_level、contract_version。首期从此权威注册表生成目录，而不是 UI 硬编码“已安装”列表。

现有 19 项能力：时间/调试（time、echo），文件（read_text、write_text、document_search/classify/copy/move/delete），计时器（timer_create/list/cancel/events），日程（calendar_create/list/update/delete/events），截图（screenshot_capture）。目录可按能力包组织，不能将分组数误报为插件安装数。

每项显示中文名称、说明、分组、来源（内置）、工具契约版本、权限、可用性/原因；参数用人类可读字段表，原 JSON schema 只在技术详情。截图后端依赖缺失、平台不支持、缺配置等要显示实际状态，不把“注册成功”一概等同“现在能执行”。目录查询不能调用模型、执行工具或主动联网检测。

工具插件页只读查看、搜索、过滤和展开详情。启用/停用、连接、安装、卸载、凭据和工具选项集中设置 → 工具与插件。首期没有后端支持的安装操作不放假按钮；只提供真实能力目录和扩展协议。

### 分层接口

| 类型 / 接口 | 建议字段 / 作用 |
| --- | --- |
| ToolCatalogEntry | stable ID、display_name、category、summary、plugin_id、origin、contract_version、permission_level、availability、reason_code、parameter_fields、result_fields |
| PluginDescriptor | plugin_id、display_name、origin（builtin/未来 connector/package）、version（可空）、tool_ids、capabilities、lifecycle_status |
| tools_catalog | q、category、origin、cursor、limit、known_catalog_revision；有界列表 |
| tool_detail | stable ID、catalog revision；当前权威详情与声明参数 |
| plugins_catalog | 已识别的能力提供方；内置 package 也有稳定身份，不伪造市场条目 |
| plugin_configuration_schema | 可信宿主提供的配置字段描述/是否 secret/可用操作，设置页按能力渲染 |
| plugin_configure / set_enabled | 后续或本轮实际实现了才宣告 capability；CAS、command_id、影响范围、重启要求 |

展示元数据可由宿主的 `ToolPresentationRegistry` 按稳定工具名补充中文标签；未知工具使用安全名称和通用结构化字段回退，不能漏掉原参数或降低权限。扩展提供方声明属于输入，必须由宿主注册/校验后才能进入目录；UI 不能凭 manifest 直接导入任意 Python。

目录 revision 变化时刷新页面。停用/变更/卸载影响正在等待的计划时，执行边界再次核对当前注册与契约并将计划置为需复核；不能因为用户已经看到旧目录而执行被停用能力。启用不授予任何工具权限，配置凭据也不是执行授权。

当前仍只支持可信工具；第三方执行隔离按原 V0.6 暂缓决定保持范围，本轮为未来扩展提供数据/界面入口，不宣称已具备任意插件安全运行能力。

## 6. 实施落点与本轮验收增量

- `config/factory/llm/maintenance`：默认云、真实 local 语义、按位置做用途判断、配置未就绪生命周期、凭据来源与生效回执。
- `gui/settings/launcher/maintenance`：唯一配置 UI、敏感命令传递、模型/连接检查、素材导入预览、分区保存。
- `workflow/migrations + assets service + pet loader/controller`：素材元数据、原子发布、资源限额、头像与形象同步。
- `maintenance/archives`：带静态素材的新 manifest 与旧四库备份兼容，密钥始终排除。
- `tools/application.desktop/gui catalog`：真实注册表投影、可读详情、可用状态、分页与插件 capability 扩展。
- `gui/navigation`：聊天专属会话栏、非聊天页面内容扩展、移除分散设置。

新增验收 G16–G21 见[实施与验收](implementation-and-acceptance.md)。生产实现仍按开发/测试分阶段推进；演示中的密钥框不可输入真实密钥，图片选择仅做本地临时预览，不声称已提交给产品后台。
