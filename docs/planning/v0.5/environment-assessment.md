# V0.5 环境、工具与依赖评估

评估日期：2026-10-16。基于当前 **0.4.0** 源码和 V0.4 交付记录；状态为**待开发提案**。本轮只检查源码、环境元数据和官方资料，不安装依赖、不修改运行配置、不读取密钥或私人数据库、不调用云 API。当前工作区已有 V0.4 改动，保留原状，由用户自行提交 Git。

结论：继续使用 Windows x64 / Python 3.12 的 deskpal_env，增加可选 Qt Widgets 界面层与 GUI 测试依赖即可启动开发。主要工作量在异步应用接口、状态一致性、准确授权和生命周期，不在补充渲染 SDK。Live2D 属 V0.7，当前无需提前安装。

## 本机检查

通过指定解释器的 importlib.metadata、platform、sqlite3 和当前 shell 的 Get-Command 检查；未导入/启动 Qt。原始无密钥记录在忽略目录 data/validation/v05/environment-py312.json 和 environment-py311.json。

| 项目 | 已观察到的状态 | V0.5 处理 |
| --- | --- | --- |
| 系统 | 主环境报告 Windows 11、build 26200、AMD64、64 位 | 本机开发与交互验收目标；不据此认证所有 Windows 版本 |
| 主环境 | D:\\anaconda\\Anaconda3\\envs\\deskpal_env\\python.exe，Python 3.12.4；deskpal 安装元数据 0.4.0 | 继续使用，安装 GUI extra 后重新验证 |
| 最低版本环境 | data/validation/py311/python.exe，Python 3.11.17；deskpal 安装元数据仍为 0.1.0 | 开工先刷新可编辑安装并核验导入路径；旧元数据不等于当前源码是 0.1.0 |
| 基础依赖 | 两环境 SQLite 3.53.4、python-dotenv 1.2.4、tzdata 2026.5 | 沿用既有配置与持久化，不引入 ORM/消息服务器 |
| 测试 | 两环境 pytest 9.1.1、mypy 2.4.0 | 保留后端双 Python 回归，补 GUI 测试 |
| Qt | 两环境均无 PySide6、Essentials、Addons、shiboken6 | 需要补装并验证 wheel、插件、字体和窗口 |
| GUI 测试 | 两环境均无 pytest-qt | 开发依赖中增加 |
| 其他候选 | 两环境均无 qasync、pywin32、PyInstaller | 当前不是前置条件 |
| 工具 PATH | 可找到 Git、Conda、CMake；未找到 cl、ninja、pyside6-designer/uic/rcc、pyinstaller | PATH 未找到不代表机器全局未安装；预编译 Qt wheel 开发不要求新增 C++ 工具链 |

Python 3.11 对同一宿主的 platform 字符串显示 Windows-10-10.0.26200，主环境显示 Windows-11-10.0.26200；这是解释器报告差异，不作为两台 Windows 实机证据。现有 GUI 包缺失，所以本轮没有 GUI 启动、托盘、通知或 DPI 通过结论。

## 技术路线与最小依赖

建议采用 **PySide6 / Qt Widgets**，使用 QtCore、QtGui、QtWidgets、QtTest；不引入 Qt WebEngine、Electron 或本地 HTTP 服务。Qt 官方提供的 wheel 包含对应 Qt 二进制，无需另装一整套 Qt SDK。[Qt 安装说明](https://doc.qt.io/qtforpython-6/gettingstarted.html)

| 分组（拟新增） | 建议依赖/工具 | 用途与边界 |
| --- | --- | --- |
| gui | PySide6-Essentials | 已包含 QtCore/QtGui/QtWidgets/QtTest；shiboken6 随解析进入闭包。包名是 Essentials，Python 导入仍为 PySide6 |
| gui-dev | pytest-qt，加现有 dev/gui | qtbot、信号等待、窗口和线程交互测试；显式选择 pyside6 绑定 |
| GUI 设计工具 | pyside6-designer/uic/rcc，按选定 wheel 核查 | 代码布局或 .ui 均可；不是另装 Qt Creator 的理由，不阻塞基础界面开发 |
| deploy（后续可选） | PyInstaller 或 pyside6-deploy 二选一做试验 | V0.5 必交源码/wheel GUI 安装入口；便携目录包可选，正式安装器/自动升级留 Beta |
| 通知/托盘 | QSystemTrayIcon + 自己的 NotificationPort | 先用 Qt 能力，无需先引入 pywin32、winotify 等第二套通知库 |
| 设置/图标 | QSettings；项目自有静态图标与缺失回退 | 只存 GUI 偏好；不存 API 密钥、工具授权或个人正文 |

Essentials 的模块范围由 [Qt 项目发布页](https://pypi.org/project/PySide6-Essentials/)确认；完整 PySide6 元包会引入 Essentials、Addons 等依赖，只有实际需要 Addons 模块时才扩大范围。[Qt 包说明](https://doc.qt.io/qtforpython-6/package_details.html)；[pytest-qt 发布说明](https://pypi.org/project/pytest-qt/)

截至评估日，PyPI 的 PySide6 发布为 **6.12.0（2026-10-08）**，提供 Windows AMD64 abi3 wheel，声明 Python >=3.10,<3.16；pytest-qt 为 **4.5.0**。可作为首轮兼容性探针候选，**尚未在本仓库安装、解析或冻结**，不能把“有 wheel”当作与当前 pytest/embedding/Conda 组合兼容的证明。[PySide6 发布文件](https://pypi.org/project/PySide6/)、[pytest-qt 发布文件](https://pypi.org/project/pytest-qt/)

如 6.12.0 的 DLL、类型检查或测试插件存在兼容问题，在记录原因后选择受支持的修复版并重新锁定；不同时安装 PyQt 和 PySide，不混用 Conda Qt 与 pip Qt。需要打包时再评估选定工具及模块许可，pyside6-deploy 是工具命令，不能用是否存在同名 Python distribution 判断安装状态。[Qt 部署说明](https://doc.qt.io/qtforpython-6/deployment/index.html)、[PyInstaller Windows 要求](https://pyinstaller.org/en/stable/requirements.html)

当前不需要 qasync（后端为同步 worker 模式）、FastAPI、Redis、额外云 SDK、向量数据库、GPU/CUDA、Live2D/Cubism 或新的模型。截图和 FastEmbed 仍是原有可选能力，GUI 必须在未安装它们、无模型缓存、离线 provider 下启动。

## requirements 与版本约束

依赖唯一声明仍是 pyproject.toml。开工后增加 gui/gui-dev extras，再建立 requirements-gui.txt（引用 -e .[gui]）和 requirements-gui-dev.txt（引用对应开发 extras）；不手抄两套版本范围。**本轮不创建这些尚不可安装的入口，也不改 pyproject。**

保留现有 44 项 Windows Python 3.12 后端约束；GUI 安装验证后形成 GUI 约束闭包（例如 constraints/windows-py312-gui.txt，复用基础约束并固定 Qt/shiboken/pytest-qt 及实际传递依赖）。Python 3.11 单独解析和验证，不能直接声称 3.12 文件通用。Qt 相关包须配套版本；清单不包含整个 Conda 环境、模型缓存或私人配置。

进入实现后的准备顺序：

1. 记录 V0.4 基线及工作区指纹，核验两个 Python 的导入路径，刷新 3.11 安装元数据。
2. 在 pyproject 声明 GUI extra，安装首个候选闭包，保存解析结果，执行 pip check。
3. 分别验证 QtCore/Gui/Widgets/Test 导入、QApplication 创建/正常退出、平台插件和一项 qtbot 信号测试；GUI 不得成为 CLI 导入的强制依赖。
4. 验证真实 Windows 窗口、托盘、系统消息支持；记录实际系统能力与抑制设置，不把 OS 未显示消息直接当作业务失败。
5. 冻结已验证版本和测试入口，再开始各页面并行开发。

## 本地测试资源与目前不足

| 资源 | 要求 |
| --- | --- |
| 合成数据 | 使用独立临时 data_dir、workspace、配置、fake provider/tool，不接触用户现有任务/记忆 |
| 实机桌面 | 需要交互登录的 Windows 会话，检查 tray 显示/菜单/恢复、通知禁用与回退 |
| 屏幕 | 单屏可先验证缩放与屏幕范围恢复；不同 DPI 双屏、断开显示器需实际设备，缺少时记录未测，不用 mock 代替 |
| 电源/会话 | 休眠恢复、锁屏后恢复时检查 TTL、去重与窗口位置；不自动修改系统电源设置 |
| 性能 | 使用现有 Windows 进程指标采样方式，补 GUI heartbeat、冷暖启动与最终同构建长测 |
| 云 API | 默认不需要；确定性慢/失败 fake 足够验证 GUI 接入。若后续要真实 DeepSeek 验收，重新申请限定次数/内容授权 |

V0.4 既有双 Python 1101 passed/4 skipped 和 77 文件 strict 通过是历史后端基线，本轮未重跑。其 30 分钟 soak 是开发树证据，最终树仅另做 5 分钟补测；V0.5 必须对最终同一构建补做完整 GUI+后端长测，不继承为已通过。[原测试报告](../../versions/v0.4/test-report.md)

下一步见[开发计划](development-plan.md)与[验收边界](acceptance-boundaries.md)。
