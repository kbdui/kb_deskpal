> 历史存档：保留当时的设计、失败与验收边界；不代表当前实现或新的执行授权。当前入口见 [文档导航](../../../README.md)。

# 聊天桌宠重构源码接口

2026-10-27，D1–D5 开发整合与实机反馈修补版。当前源码和接口由本次重构演进，实际范围见[开发记录](../../gui-refactor/development-status.md)，首轮自动化见[测试记录](../../gui-refactor/test-report.md)，本轮修补与实机范围见[10月27日记录](../../gui-refactor/2026-10-27-repair-report.md)，设计依据见[设计总览](../../gui-refactor/README.md)。历史 V0.5/V0.7/V0.9 的测试报告不覆盖本次代码。

## 应用与存储

工作库 schema 为 3，继续使用 workflow/memory/scheduling/companion 四个业务库。新目录、时间线、素材元数据和提醒投影都在 workflow；受控 PNG 存在 `data_dir/assets/`。旧版本 1/2 迁移前创建备份，未知版本拒绝；无完整持久档案的旧聊天不自动合成为新消息。

Task/Submission、turn_ref、用户消息与任务卡关联由同一提交事务写入。时间线消息使用稳定身份更新，Waiting 和终态共用条目；默认会话名称、身份、禁止归档/删除由服务与 SQL 约束保护。热上下文最多四会话、八轮及既有字节/引用预算，缓存淘汰不删除存档。

`context_epoch`/兼容 `epoch` 是会话上下文版本；目录 revision、timeline_revision、Task revision、进程 confirmation_revision、工作库隐私 epoch、GUI 隐私 epoch 与 Facade session_id 各自独立。界面不可用其中一个数字替代全部新鲜度判断。

## DesktopFacade 操作

| 操作 | 主要参数及结果 |
| --- | --- |
| conversation_ensure_default / list | 确保固定入口；目录 cursor、limit≤100、archived |
| conversation_create / update | title、command_id；更新带 id、expected_revision、title/archived |
| conversation_timeline | id、before/after、limit≤100、known_revision；返回 conversation/items、分页和重置标记 |
| conversation_item | id、item_id、字符 offset/limit；长正文分段 |
| conversation_task_location | id、task_id；按稳定卡片身份查 before 游标，定位旧任务不扫描整段历史 |
| conversation_clear / delete | id、expected_revision、command_id；返回清理工作单和真实影响范围 |
| conversation_cleanup_status | job_id；Pending/Completed，不用按钮点击代替完成事实 |
| submit | request、command_id、conversation_id、expected_context_epoch（兼容 expected_conversation_epoch）；受理 DTO 带消息和卡片身份 |
| task_review | task_id；可信步骤、完整参数、required/missing/granted、available_actions、confirmation_state/revision |
| tasks | offset、limit≤100；互斥 status（合法状态或all）/statuses（1–9个唯一合法状态字符串），在数据库分页前过滤 |
| task_updates | task_ids≤100、known_revisions；有界当前元数据 |
| confirm | task_id、plan_hash、expected_revision、ordinary_tools、high_risk_tools、command_id；非阻塞受理 |
| command_status | command_id；核查两类持久回执的受理和关联对象，不恢复 grant |
| reminder_feed / detail / ack | cursor/limit/unread；id；id/command_id。已读不修改真实计时资源 |
| schedule_items | source=all/timer/calendar、offset、limit≤100、status；查看当前及未来资源，不消费到期事件 |
| tools_catalog / tool_detail / plugins_catalog | 实际注册表、可信中文元数据、版本、可用性与只读扩展能力 |
| appearance_snapshot | `{profiles:[...]}`，七个独立槽位，不含图片正文 |
| asset_import_prepare / appearance_apply | path/slot/generation → token；slot/token/expected_revision/generation/display_mode/crop |
| appearance_reset / asset_import_discard / asset_collect | 槽位 CAS 重置、取消暂存、有界无引用资源清理 |

完整 D1 细节见[会话记录](../../gui-refactor/d1-development-status.md)，素材见[素材契约](../../gui-refactor/assets-development.md)，配置见[模型连接契约](../../gui-refactor/model-connection-development.md)。CLI 原始同步 Task/confirm 方法保留；普通 GUI 使用结构化 review，技术 JSON 只做折叠只读诊断。

## 图形层共享与二进制通道

`gui.chat.ChatCoordinator(bridge,parent=None)` 是主窗口和桌宠小窗的共同领域展示状态；`ChatView(coordinator,compact=False,parent=None)` 独立持有当前会话选择、历史页游标和阅读位置。旧记录读取经同一协调器通道返回当前视图，不替换另一个窗口的最新页；草稿与任务审核事实共享。`show_default()`、`set_conversation(id,task_id=None)`、`focus_input()` 和 `conversation_id` 为 Host 消费接口。`expand_requested(conversation_id,task_id)` 请求展开同一位置，不能重新 submit。

MainWindow 保留 `chat_coordinator/chat_view/open_conversation`、`close_requested/privacy_cleared/maintenance_requested` 和 Host 关闭协议。五入口导航只改变页面可见性；会话列表仅属于聊天页。草稿和审核选择按版本共享，清空/删除先提升 GUI 隐私屏障，旧图片、通知和消息回执不得复现正文。

10月27日修补：`ChatView.set_selector_visible(bool)` 控制整行会话切换，窄窗带明确标签，compact始终保留；Ready进入/恢复刷新当前页，初始化模型引导按导航版本丢弃过期回包。隐私清理后当前页可发起新代际读取，旧命令/旧查询仍不能恢复展示。

`PagedView` 保留原 `detail: QTreeWidget` 和完整显示预算，默认显示正文、中文元信息与依据，完整原字段由用户展开。Memory/Catalog/Arrangements 的查询各操作最多一条在途加一条最新待重读，写命令不合并。任务页只显示列表与完整审核卡，目标页表单分配明确宽度。任务分组“待我确认”为Waiting/NeedsReview，“进行中”为Created/Planning/Running；服务与存储参数为 `statuses: tuple[TaskStatus,...] | None`。

全局 WheelGuard 必须让 QScrollBar 使用原生事件路径；QAbstractScrollArea 会把 viewport 滚轮转给自身滚动条，不能再次反送 viewport。防误改仅用于设置选择/数值/滑条等控件，展开选择列表仍只滚动列表。

同日可读性补充：`gui.display_text.friendly_time(value,now=None,timezone=None)` 与 `compact_path(value,workspace='')` 是仅供明确字段使用的Qt-free显示助手，不修改命令值或任意聊天正文。TaskCard默认中文步骤与有界参数文档，原参数、结果和技术计划折叠，完整值有显式查看/复制入口；原hash、权限及4096项/16层门禁不变。Memory编辑显式展开；Goals保留 `forms` 公开对象，阅读/编辑改为右侧页签。设置内容最大960px，普通按钮自然宽；维护摘要与精确回执分开，密钥仍不进入两者。

`response.templates.tool_summary` 对内置结构化结果使用中文事实，不改变原tool_results存档或执行结果资格；unknown/失败/部分效果与未取消/未删除保留区别。OfflineProvider只依据可信EXPRESSION系统协议处理内部封装，普通用户JSON不重写。最终回归与原生边界见[可读性记录](../../../reports/implementation-log.md#全界面可读性与比例调整)。

后续聊天结果增量：表达角色改为非空 `tool_answer`，Core直接发布模型答案，工具事实仍独立存档；`parse_envelope` 不再接受空补充。失败降级用 `tool_response_fallback`，单步详情用 `tool_result_text`；`legacy_chat_reply` 只在旧正文、持久response与按账本生成的旧摘要精确匹配时提供显示投影，不改存档。共享协调器新增 `tool_results_requested(str)` 和 `reviews_cleared()`；MainWindow拥有右侧 `QDockWidget` 与有界 `ToolResultsPanel`，负责选中任务的轮询pin及清屏；compact TaskCard不显示详情入口，授权范围门禁不变。说明与未测试状态见[本轮简记](../../../reports/implementation-log.md#全界面可读性与比例调整)。

T1 修补后，聊天滚动/卡片定位使用 QObject context 及会话、分页、重绘代际，控件销毁后不再运行旧回调；不可用状态不新建或改写草稿，Ready但模型未配置仍可编辑。用户/角色消息头像从同一个图片投影更新，两窗同步，无配置快捷入口。任务卡自有重绘方法为 `render_review`，不覆盖 QWidget 原生 `render`。

GoalsPage 将 goals/tasks/schedule_items/milestones/bindings 五个读取流各限制为一个在途加一个最新待重读；旧结果不覆盖新父目标或分页，成功写入后必须重新读。写操作不合并或自动重发，Channel上限仍为12。无会话任务导航显式切换到安排的任务子标签，卡片可见后使用正常轮询更新。

普通 bridge 保持有界 JSON 命令/查询/控制三通道。`send_image('asset_preview', parameters, generation=...)` 走独立有界图片通道；`image_reply` 包含 request_id/operation/generation/privacy_epoch/ok 和 `image:{png:bytes,width,height,asset_id,token}`。bytes 不经过 JSON/base64、普通命令缓存或 QSettings。初始化后台通过 `ApplicationService.configure_asset_decoder(QtAssetDecoder())` 一次注入；`app.asset_preview` 仅供此通道调用，CLI 默认无解码器仍可导入 Qt-free 业务模块。

PNG 准备在后台，QPixmap 与绘图仅主线程；Host 统一等待后端、图片线程、桌宠和小窗销毁。角色画布不激活，小窗独立可聚焦；设置中的缩放必须同时改变画布、命中区域、位置和缓存键。Pet 元数据 presentation 仍不包含私人聊天正文。

## 模型、提醒与维护边界

`LLMConfig.is_cloud/ready/credential_source` 代替 provider 名推断执行位置。新默认云，未配置使用明确不可用的 Provider，submit 前阻断；`offline` 仅固定演示，真实本地只接受回环且不使用环境代理/重定向，不回退云。当前仅 Responses 协议，embedding 配置独立。

秘密只进 MaintenanceBridge 的 `send_sensitive` 专用瞬时字段，配置保存 keep/replace/remove 与 CAS、凭据清理意图同现有维护锁序整合；回执只有配置/来源/待生效状态。连接测试显式固定公共文本，无自动探测。Launcher 串行拥有生产 Host 和维护窗口，实例锁未释放前不得开始维护。

ReminderPump 独立于主动提议、UI 和系统通知开关。事件源身份/世代/锚点与当前 snapshot 复核，inbox 与 cursor 同事务去重；可信创建结果登记资源关联。隐私清理保留非正文墓碑，阻断调度 label 重新进入聊天。系统通知复用 audit feed、基线、静默和投递前复核；退出期间到期归错过提醒，不声称后台常驻准时通知。

新备份为格式 2，四库与当前托管图片一致保存、校验和恢复；兼容格式 1。离线备份在持有实例锁后调用文件级 `recover_imports`，仅恢复受控临时文件/精确双硬链接对，不构造数据库迁移。仍不导出系统密钥、模型缓存、配置或工作区文件。
