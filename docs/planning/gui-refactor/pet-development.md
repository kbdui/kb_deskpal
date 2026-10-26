# D4 桌宠、小窗与宿主开发记录

2026-10-25。开发实现及源码审查；没有运行 pytest、mypy、GUI、真实模型、网络或其他产品测试。AST 解析仅确认语法，不是功能验收。

## 实际接口与所有权

- `GuiPreferences.pet_scale` / `set_pet_scale(float) -> bool` 使用 `pet/scale`，范围 0.6–1.6，旧值缺省 1.0，拒绝 bool、非有限值和越界。大小控件由统一设置页提供；画布和托盘没有配置控件。
- `PetWindow.set_scale` 同时改变逻辑画布、状态标签位置/宽度、居中绘制、fallback 椭圆、稳定主图透明 mask 和屏幕约束。文字高度保持 36 逻辑像素。拖动使用 `QApplication.startDragDistance`；右键独立发 `context_requested(QPoint)`，不会触发左键；滚轮不缩放。
- 窗口移动、QWindow.screenChanged、QScreen availableGeometry/逻辑 DPI 变化和 Qt 支持时的 DevicePixelRatioChange 触发适配；角色和小窗均约束到 availableGeometry，支持负屏幕坐标。物理桌面行为尚待验证。
- `PetController.hide_temporarily()` 只设置 `_session_hidden`；不写 pet_enabled/pet_visible。`restore_visibility()`、再次 `set_pet_visible(True)` 和托盘打开主窗口可恢复临时隐藏。禁用则释放画布、图片缓存和小窗；重新启用会恢复会话临时可见状态。
- `PetChatWindow(coordinator, parent=None)` 使用 `ChatView(coordinator, compact=True)`；`open_for(anchor)` 每次进入日常聊天并主动聚焦；允许通过共享组件手动切专项。Esc、关闭和失去窗口激活隐藏小窗。草稿/审核选择始终由同一 ChatCoordinator 持有，隐藏由 ChatView 注销可见会话轮询；没有第二 Agent、聊天缓存或旧 ConversationPanel。
- Host 拥有小窗和四项右键菜单：聊一聊、打开主窗口、收起小窗、暂时隐藏桌宠。展开只调用 MainWindow.open_conversation(conversation_id, task_id)，不 submit。托盘只有打开主窗口/退出。
- Host 在禁用/关闭/维护时销毁小窗；保存 retired 引用到 QObject.destroyed 后的下一轮 Qt 事件循环，确保子控件销毁也已结束。finished 仍要求 bridge Closed、PetController 旧图片 worker 及旧画布销毁完成、小窗销毁屏障全部满足。

## 素材与有界资源

桌宠订阅 `appearance_snapshot` 的 profiles，固定使用 pet_main/pet_thinking/pet_focused/pet_pleased/pet_alert。差分缺少自定义 asset 时采用当前自定义主图及其 crop，不能回到另一内置角色。已有 package loader 的固定资源路径、有界线程、取消/晚到结果及真实结束所有权保留。

自定义预览只向 `ApplicationBridge.send_image('asset_preview', {asset_id,max_edge}, generation)` 请求；消费 image_reply 的受限 PNG bytes，不读源路径、SQL 或受控素材文件。后台已完成校验/归一，GUI 创建 QImage/QPixmap 并按 crop/contain 展示。主图决定稳定 alpha mask，差分不会改变命中区域。替换素材/尺寸/DPI 使用独立 generation，旧回执不能覆盖当前图。

图片源（包含内置 prepared images）和预览源合计最多 16 MiB；渲染 QPixmap LRU 最多 16 MiB，总缓存上限 32 MiB。键含 asset ID/revision、profile revision、表情、scale、实际屏幕 DPR、crop/mode。物理预览单边最多 1152；超过采样上限时限制图像精度并保持逻辑尺寸，避免高 DPI 把画布越缩越小。没有每屏/每次换图永久增长的缓存。

## 通知

NotificationController 在原 task/proposal 链增加 kind=reminder、status=due/missed，identity 仍为 kind/entity_id/revision（occurrence revision）。沿用 quiet hours、baseline、持久 ledger、隐私清理和投递前 notification_validate；只显示通用更新数量，点击后 navigate('reminder', id) 重新取详情。不会自动打开小窗或抢页面。

Host 每次启动以 None cursor 建立当前后台基线，不套用可能属于另一备份的 QSettings 旧游标；游标越界/读取失败后重新建基线，避免恢复后旧范围重放。inbox 的独立 pump、source/ack/TTL 校验由 D2 后端负责，系统通知开关不控制应用 inbox。没有新增第二通知通道或退出后的准时承诺。

## 源码审查与未执行用例

已审查当前 preferences/tray/pet_window/pet_controller/pet_chat/host/notifications 实际源码和 bridge、ChatView、MainWindow、settings 的调用方，完成 11 份 Python 文件的 AST 语法解析。没有执行导入或运行应用。

新增但未运行 `tests/gui/test_pet_refactor.py`：完整画布/mask 缩放、偏好拒绝、临时隐藏无持久写入且停止读、当前自定义主图 crop 回退、迟到旧图拒绝、32 MiB 缓存、共享草稿/默认重开无重复 submit、提醒 occurrence revalidation/通用摘要。

同步但未运行旧 V07/V08 测试 fake 的 image_reply/send_image 公共接口和只读操作白名单（增加 appearance_snapshot）；旧单在途、disabled 不工作、无 submit/工具调用、元数据空参数、晚到回执和 worker 生命周期断言保留。旧 hidden 回执用 request_id 精确定位 presentation，避免把新增外观读误认作状态回执。

后续 T1 最小候选（需用户另行授权）：`python -m pytest tests/gui/test_pet_refactor.py tests/gui/test_v07_pet_window.py tests/gui/test_v07_pet_controller.py tests/gui/test_v07_pet_assets.py tests/gui/test_v07_pet_host.py`；V08 涉主窗口旧结构的部分由前端整体迁移后再确定回归范围。真实 Windows IME、外部点击/焦点、托盘投递、原生透明穿透、混合 DPI/拔屏和维护切换必须单列实机证据，本次均未通过验收。

## 隐藏回执屏障补充

主 Agent 源码审查指出：旧 image_reply 在隐藏后仍可能进入 render，再由缺图路径续发预览。已在 `_visibility` 的 Hidden 分支提前增加图片 generation、清空 image/appearance pending；在 `_request_image`、`_poll_appearance` 以及两类回执处理同时检查持久可见偏好、会话临时隐藏和原生窗口可见状态。隐藏时保留有限缓存；恢复显示后强制重新读取外观元数据。后台已经开始的有界 worker 允许正常结束，其晚到结果不能继续创建窗口图片工作。

补充未执行的参数化用例 `test_hidden_pet_drops_late_images_and_profiles_without_new_queries`，分别覆盖持久隐藏和临时隐藏、切换时不新建 query、迟到图片/metadata 不续发 query、已有 source cache 保留以及重新显示读取新 metadata。修补后的两个 Python 文件仅做 AST 解析，未运行测试或应用导入。
