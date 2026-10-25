# GUI 与桌宠实现

GUI 是可选 PySide6 Widgets 前端。CLI 和领域模块不依赖 Qt；没有 GUI 依赖仍可运行后端。

下一聊天的协作约定见[2026-10-24 工作方式交接](../handoffs/2026-10-24-gui-refactor.md)，只说明 Agent 管理等工作方式，不提供页面重构设计。

## 文件与所有权

| 文件 | 职责 |
| --- | --- |
| [main.py](../../src/deskpal/gui/main.py) | 参数解析、Qt 初始化、默认配置路径与离线覆盖 |
| [launcher.py](../../src/deskpal/gui/launcher.py) | `DesktopLauncher` 切换生产 Host 和独立维护，等待旧阶段实际结束 |
| [host.py](../../src/deskpal/gui/host.py) | 主窗口、桥接、托盘、通知、桌宠的生命周期总协调 |
| [bridge.py](../../src/deskpal/gui/bridge.py) | `ApplicationBridge` 后台初始化、命令/查询/控制通道、回执和关闭 |
| [window.py](../../src/deskpal/gui/window.py)、[views.py](../../src/deskpal/gui/views.py) | 主窗口与任务/记忆/目标等视图及交互 |
| [viewmodels.py](../../src/deskpal/gui/viewmodels.py) | 有界、版本化视图状态 |
| [conversation_panel.py](../../src/deskpal/gui/conversation_panel.py)、[conversation_state.py](../../src/deskpal/gui/conversation_state.py) | 会话、互动许可、候选与隐私回执展示 |
| [companion_panel.py](../../src/deskpal/gui/companion_panel.py)、[companion_state.py](../../src/deskpal/gui/companion_state.py) | 本地人格、偏好与模拟情绪设置 |
| [preferences.py](../../src/deskpal/gui/preferences.py) | QSettings 中的窗口、显示器、托盘、通知和桌宠偏好 |
| [tray.py](../../src/deskpal/gui/tray.py)、[notifications.py](../../src/deskpal/gui/notifications.py) | 托盘动作、通知基线/去重/静默/点击复查 |
| [maintenance.py](../../src/deskpal/gui/maintenance.py) | 独立维护窗口及其后台操作 |
| [pet_controller.py](../../src/deskpal/gui/pet_controller.py)、[pet_window.py](../../src/deskpal/gui/pet_window.py)、[pet_assets.py](../../src/deskpal/gui/pet_assets.py) | 元数据驱动表现、透明窗口和有界图片准备 |

## 后台边界

调用方向为 `Qt Widget → ApplicationBridge → DesktopFacade → ApplicationService`。主线程只做展示、局部状态和 Qt 对象操作，不调用 Core、SQL 或同步等待服务。

bridge 将命令、查询和控制分成有界后台通道；取消/诊断/遗忘控制不能排在一个慢普通请求之后。当前 `MAX_PENDING=32`，参数上限 128 KiB、回执上限 4 MiB；这些是边界保护，不能靠无限队列修复压力问题。

`DesktopFacade.execute(operation, parameters)` 是 Qt-free 的操作分发表，输出 JSON 风格深复制 DTO。查询可以并发，但只有生产 dispatcher 执行 Core。添加操作时同时检查 bridge 的 READ/CONTROL 分类，避免阻塞或错误顺序。

桥接 request ID、页面 generation、记录 revision 和 privacy epoch 共同决定回调是否可用。隐私操作先清空界面正文/授权，迟到的旧 epoch 结果必须丢弃。收到回执不能直接把请求参数当作已持久状态，应按回执或重新查询更新页面。

## 桌宠表现层

`presentation/` 提供严格元数据 DTO 和固定状态映射，不传任务正文或参数。PetController 经既有 bridge 查询，处理新鲜度与代际；角色点击只请求打开主窗口，不拥有 Core 或授权能力。

图片在有界后台单 worker 准备 QImage，GUI 线程创建 QPixmap、QPainter 和透明窗口。关闭必须等待图片 worker 真正退出。静态素材位于 `gui/assets/pet/`，`provenance.json` 记录来源；资源打包声明在 `pyproject.toml`。

默认禁用时不加载素材或启动专用轮询。隐藏保留有限缓存，禁用释放窗口/缓存。任务关键状态文字优先，表情不能将失败显示为成功；过期/故障必须显示未知并提供有限诊断。

## 通知和关闭

通知默认关闭，开启时建立当前基线，不补历史；投递前再次检查状态、TTL 和来源。QSettings 保存去重 ID/revision/channel、游标等，不保存私人正文、密钥或授权。通知点击只导航并重读。

Host 负责等待后台 Closed、图片线程退出和宠物窗口销毁；Launcher 收到生产阶段实际结束后才打开维护。维护窗口也需等待其 worker 完成才能重开应用。关闭超时不能先释放实例锁或强退 Qt；保留 Closing 让用户查看诊断、等待或重试。

验证时区分 Qt offscreen、原生窗口脚本和用户实际物理交互；前两者不能推定真实托盘、通知、混合 DPI 和休眠全部通过。详见[验证指南](development-and-testing.md)。
