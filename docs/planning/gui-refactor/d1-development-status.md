# D1 持久会话与隐私基础开发记录

日期：2026-10-25。开发实现已落源码；本记录不代表产品验收。用户尚未授权本轮测试，未执行 pytest、mypy、GUI、真实模型或其他产品测试。

## 已实现边界

- 工作库版本为 3。识别旧版本 1/2，先 online backup 到新的 `.schema-N-UUID.backup` 文件，再在同一初始化事务完成 DDL、默认会话与版本更新。未知版本仍拒绝；不改写旧计划，不从旧 Task 合成聊天历史。schema 3 同时执行 `assets.schema.ASSET_SCHEMA`，并集中创建 D2 使用的提醒 inbox/cursor/resource link 表。
- `conversation/repository.py` 是持久目录、时间线和发布来源索引的适配器；`ConversationRuntime` 在生产仅缓存最多四个热上下文。1800 秒失效或热缓存淘汰不删目录和消息；新轮上下文从已发布、未清理、来源仍有效的持久记录重建，仍最多八轮并沿用原字节、引用、规划预算。历史任务不会触发工具重放。
- 默认会话固定为“日常聊天”，唯一索引及 SQL trigger 保护其身份、标题、归档与删除。专项会话支持创建、目录分页、CAS 改名/归档；默认会话可清空内容。持久目录限 10000 项、消息记录限 100000 项，超限明确拒绝新写入，保留现有数据。
- `SubmissionDispatcher.submit` 现有外层事务包住 Task/Submission、`turn_ref`、三类时间线占位及会话 receipt。轮次与 UI sequence 分开，由 SQLite 分配。失败整体回滚；重试不能修改原会话归属。
- 用户消息、任务卡和助手消息用 `task_id:kind` 固定身份。任务状态事务更新助手当前阶段表达与卡片版本；Waiting 到 Completed 原地替换同一助手条目。需要审核、含可信工具步骤或执行账本的任务才显示卡片；普通文本聊天保留关联但 `visible=false`。Waiting 表达可浏览，尚未进入后续轮次上下文，以免尚在变化的表达成为稳定来源。
- 清空/删除先提交会话 context epoch 撤销，再清理正文、来源闭包内的任务/计划/结果/上下文/效果副本和提醒显示副本。未开始工作协作取消，运行中工作返回 Pending，持久工作单追踪原会话及派生会话的运行任务；迟到模型结果和任务完成都不能重新写回已清内容。独立长期记忆保留。
- 遗忘长期记忆沿用既有跨库 deletion job，新增时间线来源索引参与闭包。会话派生标题保守擦除；提醒 inbox 保留 `redacted` 墓碑，资源链接保留 source/store/generation 到 task ID 的身份映射，只清 conversation ID，避免之后到期被重新识别为未关联提醒。
- 历史上下文的外部来源校验分三段：工作库锁内复制有界候选；锁外访问独立 Memory 等来源；重新加短锁检查 privacy/context epoch、当前来源和 Task/消息版本。全局隐私屏障使旧执行 admission 失效，但未受影响历史可在新任务中重新准入。

## 实际桌面操作与 DTO

`ApplicationService` 与 `DesktopFacade` 提供以下同名操作：

| 操作 | 参数 |
| --- | --- |
| `conversation_ensure_default` | `command_id=None` |
| `conversation_list` | `cursor=None, limit=50, archived=False` |
| `conversation_create` | `title, command_id` |
| `conversation_update` | `id, expected_revision, command_id, title=None, archived=None` |
| `conversation_timeline` | `id, before=None, after=None, limit=50, known_revision=None` |
| `conversation_item` | `id, item_id, offset=0, limit=32768`，字符 offset 的长正文分段 |
| `conversation_clear/delete` | `id, expected_revision, command_id` |
| `conversation_cleanup_status` | `job_id` |

摘要含 `id/conversation_id`、`kind/title`、目录 `revision`、`context_epoch/epoch`（同值）、`timeline_revision`、时间与归档/删除状态，以及 `schema_version/privacy_epoch/session_id`。Facade 使用本次启动的 session ID；新进程不会把 receipt 当作执行 grant。

时间线每页默认 50、最大 100，返回 `conversation/items/next_cursor/has_more`。item 有稳定 ID、sequence、kind、task ID、revision、body、用途、时间、redacted_at、当前 Task 元数据和 `visible`。单条正文在分页 DTO 最多 32768 UTF-8 字节，整页正文最多 2 MiB，超限设置 `body_truncated/body_bytes`，使用详情分段继续读取。清理后条目保留空正文 tombstone。`known_revision` 对应 `timeline_revision`；使用 after 查询时发现版本变化则返回当前最近页并设置 `reset_required`，调用方按稳定 item ID 合并。

`submit` 仍返回 Task，保留 CLI 调用；新增 `expected_context_epoch` 并兼容 `expected_conversation_epoch`，同时提供两者时须同值。Facade 的受理回执额外包含 `conversation_id/message_id/card_id/context_epoch/epoch`。未提供 `use_policy` 时使用当前 `llm.request_use_default`，显式用途不被覆盖；未配置模型连接在受理前阻断。云/本地判断统一使用 `config.llm.is_cloud`。

兼容 `conversation_open/close/snapshot`：open 新建持久专项会话，close 仅释放热缓存与临时 companion scope，snapshot 返回有界上下文摘要；close 不删除存档。旧 schema 2 的临时会话身份不导入目录，恢复审阅计划时按无历史的单任务表达处理，保留原 `turn_ref`。

`ApplicationService.assets` 持有 Qt-free `AssetService`，`configure_asset_decoder` 供 GUI 初始化 worker 一次性注入 decoder。素材 metadata 与图片通道路由由 D2/GUI 后继整合，独立于本记录。

## 源码审查与后续测试

已完成 13 个修改/新增 Python 文件的 `ast.parse` 语法解析，以及事务、默认保护、时间线固定身份、跨库锁外校验与隐私墓碑的源码审查。语法解析不执行应用，不证明行为通过；未执行 pytest/mypy/GUI 或真实模型。

新增但未执行：[持久会话集成用例](../../../tests/integration/test_persistent_conversations.py)，覆盖旧库备份迁移和默认保护、超过四会话/八轮后的持久浏览与上下文预算、dispatcher 原子绑定/重试/回滚、Waiting 到 Completed 原地更新、清理后迟到结果阻断和未受影响历史重新准入。T1 获授权后建议先执行 `python -m pytest tests/integration/test_persistent_conversations.py`，再按整合后的最小清单补授权/遗忘并发与 GUI 场景。

剩余边界：同步工具已经开始的外部效果无法撤回；历史日志、备份、导出和外部云副本不随本地清空被擦除。旧迁移回归中固定期望 schema 2 的断言需在测试准备阶段更新为 schema 3，不能把原有版本 2 的测试结论扩大到本次开发。
