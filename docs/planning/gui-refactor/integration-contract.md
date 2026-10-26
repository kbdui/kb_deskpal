# 开发接入约定

2026-10-25。D1–D5 开发中的共享约定，尚在整合，不能作为测试通过证据。实际实现状态见[开发记录](development-status.md)。

## 所有权

迁移由工作库维护单一 `SCHEMA_VERSION=3` 入口，执行会话、素材和提醒 DDL。`application/service.py` 与 `desktop.py` 先由会话任务修改，任务审核/提醒路由在其释放后串行接入。模型任务独占 config/llm/factory 和维护/launcher。素材任务独占 assets 与 archives；Qt 图片解码位于 GUI 适配层。

## 持久会话接口

| 操作 | 参数 |
| --- | --- |
| conversation_ensure_default | 可选 command_id |
| conversation_list | cursor=None、limit=50、archived=False |
| conversation_create | title、command_id |
| conversation_update | id、expected_revision、command_id，title/archived |
| conversation_timeline | id，before/after、limit=50、known_revision |
| conversation_clear / conversation_delete | id、expected_revision、command_id |
| submit | request、command_id、conversation_id、expected_context_epoch；兼容 expected_conversation_epoch |

摘要字段：`id/conversation_id`、`kind`、`title`、`revision`、`context_epoch/epoch`、创建/更新时间、归档/删除时间。默认固定“日常聊天”，kind=default；专项 kind=project。`context_epoch` 与兼容 `epoch` 表达同一会话上下文版本，和全局隐私 epoch 不混用。

时间线条目：`id`、`conversation_id`、`sequence`、`kind`、`task_id`、`revision`、`body`、`use_policy`、`created_at`、`redacted_at`，任务卡可带有限 `task` 投影。顶层返回 conversation/items、next_cursor/has_more 和隐私/启动标识。普通消息不强制绘制任务卡；同一 task 的卡片与表达按稳定 ID 更新。

ApplicationService.submit 保留返回 Task 的 CLI 契约，DesktopFacade 的受理 DTO 扩充 message_id/card_id/conversation_id。界面受理占位通过 command_id 对账，不以当前选中页面决定归属。

## 模型与维护

`LLMConfig` 提供 `is_cloud`、`ready`、`credential_source`；`execution_location` 区分 cloud/local，`provider=offline` 明确为兼容离线演示。config 缺少云模型或凭据时允许设置与本地数据访问，submit 前阻断，不产生伪模型回复。

通用 settings 只含安全元数据：llm_ready/configured、execution_location、protocol、credential_mode/source、request_use_default 等。密钥交独立维护敏感通道，普通 GUI command tracker、业务回执及日志不接收秘密。

MainWindow 保持 `maintenance_requested` 信号供 Launcher 串行关闭 Host 后进入维护；Host.finished 必须晚于 bridge Closed 和所有图片/小窗资源释放。缺配置的首次流程可浏览，实际发送由 ready 状态决定。

## 素材服务

`AssetService(store, data_dir, decoder=None)`；槽位固定为 user_avatar、character_avatar、pet_main、pet_thinking、pet_focused、pet_pleased、pet_alert。

服务提供 snapshot、prepare、preview、apply、reset、discard、collect。prepare 返回一次性导入 token，apply 使用 expected_revision/generation；preview 为受限尺寸 PNG bytes，仅通过专用图片通道。Qt-free 业务在无 decoder 时返回明确不可用，不强行导入 GUI。图片缓存不由普通 JSON DTO 承载。

## 提醒投影存储

schema3 预留 desktop_reminder_cursor、desktop_reminder_inbox、desktop_resource_link。游标按 source 与来源库身份/世代复核；inbox 去重键为 source/store_id/generation/event_id；资源关联来自已知工具结果，不从模型文本推测。清理闭包要遮蔽提醒 label 和相关时间线正文，后续重新查询也不能将它们从调度库复活。

任务 review、确认、提醒与目录的实际接口已记录于 [D2 开发交付](d2-development-status.md)，尚未执行产品测试。旧 raw task 查询保留兼容，新界面消费 task_review/task_updates；confirm 明确携带 expected_revision，权限集合固定为 ordinary_tools/high_risk_tools。confirmation_revision 是本进程单调队列版本，同 task_revision 的迟到 none 不得覆盖 accepted。提醒统一使用 reminder_feed/detail/ack，metadata 路由与专用 asset_preview 图片通道分开。
