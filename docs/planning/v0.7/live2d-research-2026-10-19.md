> 历史方案快照（2026-10-19）：用户已决定 V0.7 改用静态图片，Live2D 推迟到正式版后的更新。下文不是当前开发范围、依赖或验收门槛；未完成的 Live2D 项不得标作通过。当前范围见 [V0.7 验收边界](acceptance-boundaries.md)、[开发计划](development-plan.md)和[环境评估](environment-assessment.md)。

# V0.7 环境、工具与依赖评估

评估日期：2026-10-19（Asia/Shanghai）。基于当前 **0.5.0rc1** 源码、V0.5 实际接口与 V0.6 最新暂停检查点。状态：**公开源码/许可初审及本机元数据核查完成，渲染路线尚未冻结，未安装或运行 Live2D**。关联：[验收边界](live2d-acceptance-proposal-2026-10-19.md)、[开发计划](live2d-development-proposal-2026-10-19.md)。

用户补充（2026-10-19）：项目暂无商业使用计划，询问初版美术资源由 Agent 调用生图模型制作的可行性。原话“暂无商业使用与应用初版打算”的后一部分暂按“暂无应用发行/出版计划”理解，发行意向未作为已确认法律事实；当前不开展发行、采购或签约。研发使用许可仍需核查，产品再分发条件留作未来发行前门槛，不以尚未办理商业发行手续阻断合法的本机开发验收。

## 1. 结论和证据等级

现有 Qt 宿主、后台桥接与关闭所有权可继续使用；不需要为 V0.7 更换 Core、Worker 或权限策略。建议优先做 **独立 PySide6 渲染进程 + 维护中的 Native Python 绑定** 探针，首候选为 `EasyLive2D/live2d-py 1.0.0`。这只是验证顺序，尚非生产选型批准。

当前两个明确的项目解释器已经有 Qt OpenGL 模块；新增绑定、SDK、模型、合法分发闭包和实际 GL/生命周期证据仍缺。现在能推进无模型的状态 DTO、控制/降级/生命周期；不能宣称真实 Live2D 已完成。

| 等级 | 本轮达到的范围 | 尚不能推导的结论 |
| --- | --- | --- |
| 开发时可用性 | 读到绑定构建/声明、PySide6 示例和部分 C++ wrapper；有上游 Windows wheel 元数据 | 未安装、未导入扩展、未链接/构建，不能说本机可运行 |
| 本机环境 | 指定解释器版本/包元数据、模块可发现、发行包文件归属 | 没有 QApplication/GL context 探针，DLL 可加载与 GPU 驱动兼容未验证 |
| 合法随产品分发 | 识别各组件许可与分发条件、模型独立授权、可扩展应用风险 | 未取得 SDK 包中实际 RedistributableFiles/NOTICE，未选正式模型，未确认发行许可分类 |
| 本项目验收 | 仅形成待执行矩阵和候选预算 | 没有 V0.7 模型/动作/稳定性/实机通过证据，V0.5 结果仅是基线 |

## 2. 本机只读取证

6 Luna 高思考执行，父 Agent 审查。原始命令/退出码/输出见 [2026-10-19 环境取证](evidence/environment-2026-10-19.txt)。只访问下表明确解释器，不枚举封存 V0.6 runtime/profile/key；未读取 `.env`、私人配置/数据库或调用云 API。

| 项目 | 主环境 | 最低版本兼容环境 |
| --- | --- | --- |
| 解释器 | `D:\anaconda\Anaconda3\envs\deskpal_env\python.exe` | `E:\no_game\git\kb_deskpal\kb_deskpal\data\validation\py311\python.exe` |
| Python/架构 | 3.12.4，MSC v.1929，64-bit AMD64 | 3.11.17，MSC v.1942，64-bit AMD64 |
| 系统字符串 | Windows-11-10.0.26200-SP0 | Windows-10-10.0.26200-SP0 |
| Qt发行包 | PySide6-Essentials 6.12.0、shiboken6 6.12.0 | 同左 |
| 测试/静态工具 | pytest 9.1.1、pytest-qt 4.5.0、mypy 2.4.0 | 同左 |
| GL模块元数据 | `PySide6.QtOpenGL` / `QtOpenGLWidgets` 有 spec；Essentials.files 含对应 .pyd/.pyi | 同左 |
| Live2D/PyOpenGL | `live2d-py`、`PyOpenGL` 未安装，`live2d`/`OpenGL` 无 spec | 同左 |

同一机器的 platform 字符串不同不是两台 Windows 的证据。发行包 `PySide6` 元包未安装，但 `PySide6` 导入命名空间由 Essentials 提供；不要因此误报 Qt 缺失，也不要预先增加 Addons/完整元包。find_spec 子模块会解析父包，这不等于已导入 GL 扩展、验证 DLL 或创建了窗口。

当前 shell 默认 `D:\anaconda\Anaconda3\python.exe` 是另一个 Python 3.12.4：pytest7.4.4/mypy1.10.0，无 Qt/pytest-qt。后续必须显式选项目解释器，不能在默认 base 下看到 skip 就当 GUI 已通过。首次元数据命令因 PowerShell 引号导致 SyntaxError，证据文件保留重现原文及修正后的实际返回；它不是产品失败或产品测试结果，也不是把重现记录冒充首轮完整工具返回。

没有探测显卡/驱动/OpenGL profile/远程会话、真实缩放/屏幕设备，也没有重新验证 CMake/MSVC/Ninja/打包工具。V0.6 材料提及已用过原生工具链，不能因此声称选定 Cubism 版本已构建。后续只核查明确工具路径或 PATH，不打开旧实验 runtime。

工作区初次 Git 只读查询遇到 dubious ownership；后续仅使用每条命令的 `git -c safe.directory=E:/no_game/git/kb_deskpal/kb_deskpal ...` 读取状态，没有改全局 Git 配置。初始 short 状态为空；不 stage/commit/push/reset/创建 PR。

## 3. 候选路线比较

| 路线 | 可复用部分与主要代价 | 本轮结论 |
| --- | --- | --- |
| live2d-py + 独立 Qt 子进程 | 复用 Native SDK wrapper、模型/动作/命中API；父宿主与扩展崩溃/GIL/长加载分离；新增窄 IPC/进程收敛职责 | **首选验证**，不等于绑定/许可/运行已通过；不自写渲染底层 |
| live2d-py 同进程 QOpenGLWidget | 已有上游 PySide6 示例，接线较少 | 仅对照探针；原生崩溃会损坏宿主、同步加载会堵宿主，未满足故障边界，不直接生产接入 |
| 官方 Cubism Native SDK 小 helper | 官方 C++ Framework/渲染后端可复用；Python只保留薄IPC | 后备路线；需维护构建、窗口/OpenGL与分发闭包，不从零实现 Cubism；当前未构建 |
| Cubism Web + Qt WebEngine | 官方 Web SDK 可作其它后端候选 | 首版暂不采用：新增 WebEngine/JS bridge/本地内容策略与部署资源；没有证据表明比 Native 路线更符合本项目 |

独立进程只是表现故障边界；仍以用户身份运行，不能称 OS 隔离、不能当 V0.6 Worker，不接受不可信代码。官方 [Native Samples](https://github.com/Live2D/CubismNativeSamples) 是渲染/应用示例和 Framework 参考，Core 仍需官方 SDK；不用其“可编译示例”替代项目 PySide6 兼容性验证。[Core 获取说明](https://docs.live2d.com/en/cubism-sdk-manual/cubism-core/)

## 4. live2d-py 的实际核查与缺口

仅阅读公开文本，未下载/安装 wheel、sdist、SDK 或角色素材。上游页面存在不同缓存时间，动态 main 的文件不保证属于同一次构建；本轮不把它们组合成“已审定完整版本”。

| 证据 | 实际观察 | 对项目的影响 |
| --- | --- | --- |
| [GitHub releases](https://github.com/EasyLive2D/live2d-py/releases) | v1.0.0，发布页2026-09-30，关联 commit `35f686412470ea25718fa9c76ee4f1809f6a8506`；统一为 `live2d.Model`，移除旧 `live2d.v3/v2cpp` 模块 | 不能照抄早期 v3 教程；后续审同一tag/commit |
| [PyPI 1.0.0 文件](https://pypi.org/project/live2d-py/1.0.0/#files) | Python>=3.11；有 `live2d_py-1.0.0-cp311-abi3-win_amd64.whl`，发布页 SHA256 `5489e08b77c0b97584bc5346b58d6877db23a9ba94c83665a450ed8a5d6d6c8d` | ABI标签支持候选3.11/3.12 x64，不承诺32-bit/Windows ARM64；这是索引值，尚未下载实算或校验包内二进制 |
| [固定 commit setup.py](https://raw.githubusercontent.com/EasyLive2D/live2d-py/35f686412470ea25718fa9c76ee4f1809f6a8506/setup.py) | stable ABI构建选项；依赖 numpy/pyopengl/pillow；缺 SDK 时构建路径自动下载并解压 SDK，build/install 命令有自定义逻辑 | 普通源码安装可能额外联网；不得静默运行。先审批准的wheel/闭包或使用明确合法的本地SDK，禁止隐式sdist回退 |
| [main pyproject](https://raw.githubusercontent.com/EasyLive2D/live2d-py/main/pyproject.toml)、[CMakeLists](https://raw.githubusercontent.com/EasyLive2D/live2d-py/main/CMakeLists.txt)、[wrapper配置](https://raw.githubusercontent.com/EasyLive2D/live2d-py/main/cmake/Wrapper.cmake) | Python>=3.11、numpy/PyOpenGL/Pillow；顶层CMake最低3.26并进入Wrapper/Live2D子目录 | 只在源码构建路线需要C++闭包；实际SDK版本/编译器/runtime/DLL匹配须另核验，不因有CMake即通过 |
| [main Python入口](https://raw.githubusercontent.com/EasyLive2D/live2d-py/main/package/live2d/__init__.py) | 声明1.0.0、`__csm_version__="5-r.5"`，init进入扩展，导入有banner输出 | 5-r.5只是所读绑定目标，不声称官方最新；协议stdout需避免被导入日志污染；实际DLL与声明是否相符待查 |
| [PySide6示例](https://raw.githubusercontent.com/EasyLive2D/live2d-py/main/examples/main_pyside6.py) | QOpenGLWidget.initializeGL里glInit/建Model/同步LoadModelJson；paintGL更新绘制；120FPS timer、primaryScreen DPR和glReadPixels命中，退出后dispose | 能说明接入方式，不能原样生产复用：高帧率、跨屏DPR、上下文current、阻塞加载和释放顺序需重做项目适配 |
| [main stub](https://raw.githubusercontent.com/EasyLive2D/live2d-py/main/package/live2d/_live2d.pyi) | 声明 LoadModelJson(create_renderer)、Create/DestroyRenderer、glRelease/dispose 等接口 | 声明不是线程安全/异步/析构实现证明；必须对选定二进制验证 |
| [C++ PyModel 源码页面](https://github.com/EasyLive2D/live2d-py/blob/main/Wrapper/V3/PyModel.cpp) | 可读页面含 `new Model`/Dealloc `delete self->model` 与直接LoadModelJson；所读方法命名/签名与1.0入口/stub有差异 | 这是不同时间缓存的局部源码证据；未证明统一wrapper的GIL释放、GPU清理/异常传播/回调引用寿命，不据此冻结1.0生命周期 |
| [根 LICENSE](https://raw.githubusercontent.com/EasyLive2D/live2d-py/main/LICENSE) | wrapper作者代码 MIT，要求保留许可/版权 | 不能将SDK/Core/样例/模型统一贴成MIT |

资料获取限制：GitHub commit API 返回 Internal Error；v1.0.0 多个原文件/目录及固定commit的 Python入口返回 Cache miss；部分 raw C++ 文件404或空响应；examples目录一次重定向至 `EasyLive2D/relive2d`。本轮不跟随重定向改选另一项目、不声称读完上游全部实现。固定commit的 setup.py 成功读取，动态 main 的少量文件/示例与部分C++页面成功读取；仍缺**同一固定版本**的统一扩展初始化、Model/纹理/动作析构、GIL/线程约束、异常路径、构建CI/测试和wheel内容核对。这些缺项阻止正式绑定冻结，但不阻止状态层开发。

阶段0需取得该版本小范围源码并记录内容hash，检查：DLL来自何处/怎么链接，LoadModelJson是否释放GIL/创建GL、资源失败是否会assert/崩溃、动作回调何线程/怎样断开、model析构是否调用GL、dispose与glRelease顺序及多次调用、context重建与Python晚期析构。若必须长期维护大补丁才能满足项目条件，应改选现成可维护路线；不能只根据README或演示视频通过。

## 5. Qt/OpenGL 适配约束

Qt 官方文档要求在合适的 current context 中初始化/清理GL资源；QOpenGLWidget会管理其framebuffer，不能假设默认FBO永远为0。线程方案、context销毁和显式清理需按文档实现，不能用Python GC代替确定的释放时点。透明顶层窗口与“透过GL widget看到下面其它Qt子控件”是不同场景，后者有合成限制。[QOpenGLWidget官方说明](https://doc.qt.io/qt-6.12/qopenglwidget.html)

本项目拟让宿主保持Widgets事件循环，另一个渲染进程有自己的QApplication/QOpenGLWidget；所有GL仍在该进程的Qt GUI线程。后台只预检/读取纯数据，不在没有线程所有权证明时转移模型/context。阶段0记录实际GL版本/profile、alpha、FBO绑定、premultiplied alpha/纹理混合和DPR后，才冻结SurfaceFormat；不能凭示例选择一个全局profile影响现有主窗口。

Qt高DPI的逻辑坐标不等于GL像素尺寸；Windows混合缩放的屏幕布局可能有间隙，应使用实际screen/availableGeometry，不能把屏幕边界拼成一张连续画布。命中/viewport按当前窗口DPR换算，保存位置不用反复乘DPR。[Qt High DPI](https://doc.qt.io/qt-6/highdpi.html)

待实测：本机与RDP可用的OpenGL/驱动、透明黑底/边缘、窗口焦点/输入穿透、多屏切换、context重建、SDK与Qt共享GL状态、帧时和回收。Qt模块存在并不证明这些项目通过。offscreen适合纯UI逻辑，不能充当真实硬件渲染验收。

## 6. 许可与分发清单

本节是工程准入清单，**没有代用户签约、购买、确认发行主体或申请出版许可**。具体发行包必须按实际版本文件与官方条件核对，依赖作者的免责声明不取代官方条款。

当前按非商业、本机研发组织工作，不预先采购商业发行许可。开发试用与应用发行是不同条件；未来分享应用/模型/演示材料时，按实际行为重核条款。“非商业”本身不是所有 SDK、模型、素材限制的通用豁免。[官方许可区分](https://www.live2d.com/en/sdk/license/)

| 组件/资源 | 来源和许可 | 开发/测试处理 | 产品分发条件与当前缺项 |
| --- | --- | --- | --- |
| Python wrapper | live2d-py LICENSE，MIT | 可审源码，采用时保留通知 | 核对固定版和wheel内第三方内容；MIT仅覆盖有权授权部分 |
| Cubism Native Framework/Samples | [官方Framework LICENSE](https://github.com/Live2D/CubismNativeFramework/blob/develop/LICENSE.md)、[Live2D Open Software Agreement](https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html) | 按对应协议使用；不是泛称MIT/Apache | 记录组件/修改/通知及实际SDK对应版本，不将develop视为冻结版 |
| Cubism Core | 官方SDK包，[Proprietary Agreement](https://www.live2d.com/eula/live2d-proprietary-software-license-agreement_en.html) | 需接受适用协议再取得合法包；本轮未下载 | 核对实际包的RedistributableFiles及协议条件；不能单独镜像整套SDK/Core，也不能笼统认定所有Core都不可随应用分发 |
| 应用出版 | [官方SDK发行许可](https://www.live2d.com/en/sdk/license/) | 开发试用与发行分开判断 | 个人/小规模豁免有条件，Expandable Application例外；本应用若允许无限导入模型，存在该分类风险（工程推断），发行前核实，不承诺“免费开源就免许可” |
| 官方样例模型/动作/纹理 | [样例入口](https://www.live2d.com/en/learn/sample/)、[Free Material Agreement](https://www.live2d.com/eula/live2d-free-material-license-agreement_en.html)、[逐模型使用条件](https://www.live2d.com/learn/sample/model-terms/) | 只能在相应条款允许的用途内选测试模型 | 每个模型可能有额外限制，需记录名称/版本/版权/改编和再分发范围；公开下载不等于产品附带授权 |
| 自有/委托/第三方模型 | 创作者原始授权与素材依赖 | 用户提供或合法取得后登记；不从聚合模型仓库默认取材 | 需同时覆盖图像、模型、动作、纹理、演示/截图和再分发；未选正式素材，不入包 |
| PySide6/Qt | [Qt for Python许可清单](https://doc.qt.io/qtforpython-6/licenses.html)，依实际模块商业或开源许可路线 | 沿用现有Essentials，不因新功能改变许可路线 | 对实际DLL/插件/NOTICE列闭包，满足所选许可义务；打包不意味着许可工作自动完成 |
| PyOpenGL/numpy/Pillow/打包工具 | 依各固定发行包LICENSE/NOTICE | 上游声明有此依赖，不预先安装全部可选演示库 | 在下载固定包后逐一核查其第三方许可/二进制，尚未完成，不提前宣称整个闭包可分发 |

Core条款包含特定可再分发代码的条件与发行许可例外，不能将上游setup.py中“禁止分发”的概括当成最终法律结论。这里保留待核查的实际文件清单，不凭网站下载按钮绕过许可，也不替用户接受SDK下载协议。

阶段0的资源manifest至少记录：来源URL/获取日期、SDK/绑定/model版本与SHA256、平台架构/链接方式、许可证原件/通知路径、用途（内部测试/用户自备/可随产品分发）、动作映射、资源闭包与尺寸上限、许可判定依据/未解决项。用户自备素材不自动免除应用SDK发行条件。首版暂不提供任意模型平台，不代表自动排除了Expandable分类。

## 7. 依赖管理和准备顺序

现有 `pyproject.toml` 仍是唯一直接依赖声明：基础python-dotenv/tzdata，gui为 `PySide6-Essentials>=6.12,<6.13`，gui-dev为pytest-qt。CLI不得因V0.7导入Qt/Live2D。保留现有constraints和requirements，不用整份Conda freeze替代项目依赖闭包。[现有依赖约定](../../guides/dependencies.md)

| 拟议分组/工具 | 用途 | 本轮状态 |
| --- | --- | --- |
| live2d 或 pet extra（待命名冻结） | 经验证的绑定和实际直接使用的图形库；包含所需gui依赖 | 不新增空extra/不可用安装命令；不改pyproject |
| requirements-pet / pet-dev（待选型） | 只引用pyproject extra，复用dev/gui-dev | 未创建，不维护第二套手写版本 |
| Python3.11/3.12的pet约束 | 固定验证后的Qt/绑定/传递闭包及下载hash | 尚未解析/安装/pip check；不把py312-gui约束视为Live2D锁 |
| CMake≥3.26、MSVC、Windows SDK | 仅源码构建/Native helper需要；需核对Core库ABI/CRT | 未重查，不作为已满足条件 |
| PyInstaller/pyside6-deploy | 按后续交付形态择一验证DLL/Qt插件/资源收集 | V0.7先保证源码/wheel路径和子进程入口；正式安装器留Beta，不预装两个工具 |
| 资源/性能工具 | 优先复用Windows只读进程指标与Qt heartbeat；GPU指标可用性单列 | 不为此默认加psutil/驱动/服务；需要新增工具时说明用途再走批准 |

实施准备顺序：

1. 在新的V0.7专用取证/临时合成目录冻结源码/解释器/依赖指纹，不用V0.6环境与对象。
2. 获取并审定固定源码/二进制文件清单与许可，明确正式模型或内部测试模型用途；下载/安装遵守工具批准要求，所有请求中文。不运行上游setup自动下载，不允许无wheel时静默走sdist。
3. 批准后在明确项目/专用环境安装固定闭包，保存解析/下载hash/安装stdout/exit与pip check；核对导入路径，避免Conda Qt与pip Qt混用。安装不等于验收。
4. 仅用新合成配置做Native子进程Qt/GL/模型/动作/清理探针，验证3.11与3.12；记录首次失败。通过后冻结环境/模型/数值预算。
5. 再接入生产表现接口；运行关闭桌宠/缺依赖/CLI安装对照和受影响GUI回归。同构建原生长测完成后审发行闭包。

不需要新增推理模型、CUDA、云SDK、人格微调、Docker/WSL迁移、系统账户、驱动或服务。V0.6架构重评允许另行考虑Docker/WSL的背景，不是本轮安装/迁移授权。

## 8. 现有测试资产与验收资源缺口

Luna只读盘点 `tests/gui/`：已有桥接背压/关闭、窗口授权与隐私缓存、任务/记忆/通知、慢后端和进程互斥等测试；fixture使用offscreen并在Qt缺失时importorskip。未运行这些测试，定义数量不作为通过结论。当前没有真实Live2D模型/动作、GL上下文重建/回收或宠物交互/性能验收资产。

[V0.5实际报告](../../versions/v0.5/test-report.md)记载的双Python各1203 passed/4 skipped、100%/200%原生双屏194项和1800秒长测是历史结果；本轮没有复跑，也不能覆盖新模型/渲染进程。V0.5完整缩放矩阵/休眠等仍未完成，首次停滞记录必须保留。

| 缺项 | 当前状态 | 可继续的工作 |
| --- | --- | --- |
| 固定绑定全链源码/二进制审核 | 同版本释放/GIL/CI、wheel内DLL与许可未核完 | 纯DTO/reducer/IPC/controller设计与mock |
| 官方SDK合法包/再分发清单 | 未为V0.7获取/核验 | 依赖缺失诊断、静态降级和窗口壳 |
| 模型/动作与用途授权 | 未选定、不下载聚合仓库素材 | 本地资源manifest/缺文件/超量/路径校验设计 |
| 原生GL及GPU指标 | 未建立上下文、未测驱动和GPU资源 | 现有Qt元数据足够启动无模型UI开发；不当成GL可运行 |
| 实物显示/睡眠/RDP | 历史双屏不代表当前设备全矩阵；可用性待对应验收协调 | 几何逻辑/合成屏幕变化；原生项与人工项分列 |
| 最终构建长期运行 | 尚无新渲染构建 | 编写基准/观测方案，阈值初测后冻结 |

本轮没有产品功能变更或产品测试通过结论。环境命令的引号失败与公开资料获取失败均已保留说明；后续证据须按日期/场景/轮次独占保存，不覆写首败。是否完成Live2D、是否可分发、是否完成V0.7只能由对应真实证据分别判断。

## 9. 初版美术资源的生图路线（用户补充）

可由 Agent 使用内置生图工具承担角色概念稿、透明背景静态立绘、表情参考、降级图片及有需要的位图装饰。先确定同一角色的造型/比例/配色，再围绕定稿编辑，减少每张重新生成导致的角色漂移。已有代码/矢量图标仍按原生资产方式维护，不为简单控件增加位图库。

| 制作阶段 | 可以承担的工作 | 完成边界 |
| --- | --- | --- |
| 概念与静态资源 | 原创角色方案、透明PNG、表情参考、定向修图；检查alpha、边缘、形体和一致性 | 合格后可用于静态降级/界面展示；不是Live2D接入证据 |
| Live2D画稿准备 | 制订拆件清单，尝试分件/补全遮挡区域和统一画布对齐，记录参考与生成来源 | 生图输出通常为栅格图片，不能承诺直接得到可靠的分层PSD；拆件质量和连续变形需另验 |
| 建模/动作/导出 | 另行评估可用Cubism Editor/工具链、网格、变形器、参数、物理与动作的制作方式 | 本轮没有已验证的自动建模/绑定能力；生图工具不会直接交付可用`.moc3`/`.model3.json`运行包 |
| 真模型验收 | 将实际导出模型接入冻结渲染路线，执行动作/命中/资源/回收测试 | 继续满足L02与原生验收，静态图或序列帧不能顶替 |

Live2D官方制作说明将插画拆为眼睫、眼球、轮廓等部件，并要求补绘运动时会露出的区域；这正是“生图立绘”与“可变形模型”之间需要独立投入的工作。[官方拆件说明](https://docs.live2d.com/en/cubism-editor-manual/divide-the-material/)

资源记录包含提示词/生成日期/工具、参考输入及其使用依据、原图与定稿hash、编辑历史和用途。优先原创、不使用未经授权的现成角色/参考素材；不把AI生成等同于排他版权、第三方无权利或所有用途均已获许可。当前不据此承诺商业发行资格。

实际制作采用内置image_gen路径，不读取项目API key，不接入应用的LLM调用链，不下载本地推理模型。允许制作本项目美术不等于允许上传私人照片/配置/数据；使用外部参考前核查其来源。选定项目资产落入工作区的版本化资源目录，记录生成来源；角色方向尚未确定，本轮没有调用生图或生成资产。真实Live2D技术探针可使用合法测试模型独立推进，不必等待自有角色建模完成。
