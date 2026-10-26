# 旧 GUI 实现退役开发记录

日期：2026-10-26。用户明确要求仅保留新版 GUI 实现；本轮仍为开发阶段。本记录不表示行为验收通过，pytest、mypy、GUI、项目导入、网络和真实配置/数据库均未执行。

## 删除和保留边界

已删除 `gui/conversation_panel.py`、`gui/conversation_state.py`、`gui/companion_panel.py`、`gui/viewmodels.py`，不保留旧页面的 shim、alias 或备用入口。旧临时聊天页的历史和会话选择改由 `ChatCoordinator` / `ChatView` 承担，旧请求跟踪代码由页面 `Channel` 和领域协调器承担。

`gui/interaction_state.py` 只保留设置页实际使用的 `InteractionSnapshot` 有界解码，不再携带旧会话 `TurnView` / `ConversationSnapshot`。`gui/companion_state.py` 的 `CompanionSnapshot`、`PRESETS`、`STYLE_AXES` 保留，供新设置与桌宠共享模拟状态 DTO；它们不属于旧页面。

`gui/persona_settings.py` 将有效人格设置组织为 `PersonaSettings`。保留启用开关、人格预设、六轴显式自定义、模拟情绪、短时模式、人格历史回退及重置。删除原隐藏偏好表单及其 `scope/verbosity/humor` 控件、保存按钮和偏好命令实现；表达偏好、云类别许可、身份和候选确认统一由 `settings.InteractionSettings` 负责，未重复创建后再隐藏。

## 新设置契约

- `PersonaSettings(bridge, parent=None)`：设置子区，所有读写经后台 bridge，独立有界单个在途请求。
- `set_available(bool)` / `on_state_changed(str)`：由 Host 传递生命周期；不可用时清理本区敏感展示和旧请求身份，关闭不发新请求。
- `poll()`：保留编辑的刷新；`_reload_inputs()`：显式放弃人格编辑并重新读取；`clear_sensitive()`：使旧代际回执失效并清空展示。
- `_config_dirty` / `_preset_dirty`：Host 退出和维护提示仍可消费；没有改选预设时不发送 `preset`，避免用同名预设覆盖已有自定义六轴。只有勾选本次自定义时才发送完整六轴。
- `conversation_target`：仅为短时 cue 捕获选定会话；偏好范围不再由此子区写入。
- 每个写操作先读取新鲜 `CompanionSnapshot.revision` 再 CAS 提交；迟到 preflight 不提交，命令回执仅触发刷新，不自动重发命令。保留回滚、存储不可用、代际/隐私屏障和状态单调性校验。

新版 `settings.py` 导入 `PersonaSettings` 和 `interaction_state.InteractionSnapshot`，不再调用旧 `preference_group.hide()`。设置页与宿主关闭、全局隐私清理的整合由本次 GUI 整合记录覆盖。

## 已准备、尚未执行的保障

`tests/gui/test_humanization_conversation_gui.py` 改用真实新版 `ChatView`、`ChatCoordinator`、`InteractionSettings` 和 `MainWindow`，保留五类保障：身份/许可编辑刷新与 CAS 冲突、两个设置区分别保存不丢另一分区编辑；两个会话乱序历史及候选不串线；候选必须显式确认并从记忆入口审核长期画像；`privacy_invalidated` 新 epoch 屏障阻断旧 DTO；全局遗忘清理 Pending/Completed 和关闭后旧回调不得恢复正文。旧隐藏偏好表单的 session/today/persistent、显式 False 值保障迁到 `InteractionSettings` 命令用例。

`tests/gui/test_v08_companion_gui.py` 改用 `PersonaSettings`，保留人格刷新 dirty、preflight CAS、cue/情绪重置/人格重置/全部重置/版本回退、有界请求、过期/重复回执、窗口隐私和桌宠状态映射保障；增加不改选预设不发送 preset、完整自定义六轴、cue 绑定选定会话的用例。前端已迁移的新版窗口场景保留。

仅允许通过 Python AST 解析和源码/引用搜索作静态开发核对，不代表上述测试通过。T1 获授权后执行这两个 GUI 文件及新版窗口/聊天整合清单；任何失败由源码归因后修补，不放宽旧保障。

实际静态核对：`interaction_state.py`、`persona_settings.py` 和上述两个 GUI 测试文件均完成 `ast.parse` 解析；四个退役模块均确认不存在；`src`、`tests`、`scripts` 的旧模块/类型/请求跟踪标识搜索无命中。该结果仅说明语法和引用清理，不代表行为验证。
