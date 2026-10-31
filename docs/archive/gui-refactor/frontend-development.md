> 历史存档：保留当时的设计、失败与验收边界；不代表当前实现或新的执行授权。当前入口见 [文档导航](../../README.md)。

# 前端开发整合记录

2026-10-26。本记录覆盖接手前已保存的 D1–D5 前端成果与最终最小修补；当前仍为开发阶段，不是验收报告。共享后端字段见[接入约定](integration-contract.md)和[D2 接口](d2-development-status.md)。

## 实际组成与调用接口

- `MainWindow` 提供聊天、安排、记忆、工具插件、设置五入口。设置中心装配新的 `PersonaSettings` 和 `InteractionSnapshot`，不导入已删除的旧 GUI 类。维护继续发 `maintenance_requested`，由 Launcher/Host 串行释放生产资源后进入维护。
- 主窗与桌宠小窗消费同一 `ChatCoordinator`：会话摘要、最新时间线、草稿、任务审核、授权选择及命令回执共享。每个 `ChatView` 独立持有选中会话、滚动位置、历史页、历史游标和读取 generation；一窗翻页或返回最新不会改写另一窗的历史页。最新快照最多缓存四个会话，单个历史页最多 50 项；历史导航使用 `conversation_task_location` 的索引位置，再读取有界时间线页。
- `Channel.send(operation, parameters, callback, mutation=False)` 跟踪 request ID、generation、privacy epoch 和回调，最多 12 个在途请求；命令回执独立于页面选择。`invalidate()` 显式使同 epoch 的旧请求失效。Closed 拒绝新请求及迟到回调；Closing 仅保留清理、取消和诊断控制入口。StartupFailed 仍可读取无业务正文的启动诊断。
- `submit` 使用 `conversation_id`、`expected_context_epoch`、`command_id`；回执按原 command ID 与草稿 revision 对账。归档会话的输入框只读，首次提交与原命令重试均有 Coordinator 门禁；原草稿和未核实命令继续保留。
- 任务卡消费 `task_review/task_updates`，确认明确发送 task ID、原 plan hash、expected revision、ordinary/high-risk 工具集合。L0-only 的 NeedsReview 也有显式“确认并继续原计划”入口。accepted 与 confirmation revision 共同防止迟到的旧审核重新出现确认按钮；不可完整展示参数时禁确认。
- 审核缓存及待取队列各最多 128 项，审核读取最多四个并发。淘汰同时移除对应 grants/accepted 状态，保护正在显示的卡、当前时间线卡、已选授权和待核实命令；容量被全部保护项占用时明确拒绝新读取。提交与任务命令回执分别最多 32 项。
- 草稿最多 128 份、总 UTF-8 512 KiB，单份最多 20,000 字符/64,000 bytes。拒绝编辑时立即将可见输入恢复原草稿，并禁用该输入视图发送；不会显示被拒的新文本而发送后台旧草稿。
- 记忆页新建表单没有可编辑 `use_policy`。读取安全 settings 的布尔 `request_use_default` 后只读继承为 `local_only/cloud_allowed`；元数据未返回或类型无效时禁止创建。更正继续携带当前记录 revision。

## 生命周期与本轮补齐

- `ChatCoordinator.clear_sensitive`、互动设置清屏和主窗全局清屏会 invalidate 相应 Channel。历史页以 view generation 拒绝过期返回；互动设置仅在 Coordinator Ready 时刷新。关闭后的同 epoch 回调不能重新填入正文或设置。
- `CleanupBlocked` 使用主窗独立的 2 秒 `cleanup_status` 定时器，隐藏窗口或切换页面不会暂停；由 Bridge 的 Ready 状态终止轮询，Closing/Closed/StartupFailed 立即停止。此通道与正常可见页面轮询分开。
- 本轮进一步补齐 Channel 对 Bridge 当前生命周期的直接检查，覆盖 queued lifecycle slot 尚未处理时先到达的旧回调；保留 StartupFailed diagnostics。
- TaskCard 的计划参数和执行结果属于原始载荷，所有叶子值保留字面显示及全文复制，包括名为 `status/use_policy` 的嵌套字段；技术树中对应载荷子树也按原值展示。真实审核状态等语义枚举仍可翻译。
- 本轮进一步补齐归档输入框只读以及原 command ID 重试入口的归档检查。

## 开发验证与待执行清单

本轮仅执行指定前端源文件及两份测试文件的 AST 语法解析和跟踪文件 diff 空白核对；未导入运行产品模块。`tests/gui/test_chat_refactor.py` 与 `tests/gui/test_window.py` 已准备共享草稿、独立历史页、关闭迟到回调、原值审核、L0 确认、缓存预算、归档重试、记忆策略门禁及全局清理轮询等用例。

实际结果：7 个 Python 文件 AST 解析退出码 0；本范围 `git diff --check` 退出码 0，仅提示既有 LF/CRLF 转换。指定五个前端实现文件没有已删除旧 GUI 模块的导入引用。

**pytest、mypy、GUI、真实模型与网络验证均未执行。** 未读取真实配置、密钥或数据库，未 stage/commit。开发语法检查不等于行为测试通过。测试授权后先运行上述两份 GUI 用例，再按[最小测试清单](development-test-plan.md)集中验证。

仍需实机确认：Windows 多屏/DPI 与字体缩放、中文 IME、滚轮透传、桌宠小窗与主窗实际焦点/位置、长历史翻页与异步清屏时序、全部缓存占满时的交互提示、图片导入裁剪与高分辨率预览、Host 最终关闭及维护切换的资源释放。静态源码已接入相应门禁，但没有本轮实机通过证据。

## T1 后续状态

上述“未执行”保留为开发交付时的事实。用户随后已启动T1，实际首败、修补与最终复测见[测试记录](test-report.md)。前端补齐QObject回调生命周期、关闭后草稿门禁、空选择详情清屏、Goals读取合并、无会话任务导航和双窗消息头像；TaskCard重绘更名为 `render_review`，保留Qt原生render。B2最终两组共200项通过，实机交互仍按用户决定未验证，后续按[手工检查表](manual-checklist.md)自行测试。
