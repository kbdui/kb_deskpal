# V0.7 开发计划：静态图片桌宠表现层

更新：2026-10-21（Asia/Shanghai）。状态：静态桌宠已实现，按用户要求带未决项[归档](../../versions/v0.7/delivery.md)，未全部验收通过。产品仍为 **0.5.0rc1**。进度与首败见[测试报告](../../versions/v0.7/test-report.md)，未执行诊断/扩展测试见[后续清单](../../versions/v0.7/deferred-tests.md)。下文阶段划分保留为开发追踪，不代表全部已通过或仍在自动执行。

用户已冻结方向：**V0.7 使用静态图片，不做Live2D；Live2D推迟到正式版后的更新。** 不再准备SDK/绑定/模型、建模工具、OpenGL探针或独立渲染进程。此前[Live2D开发提案](live2d-development-proposal-2026-10-19.md)作为历史保留；未执行条目是延期，不是已验收。

配套：[验收边界](acceptance-boundaries.md)、[环境评估](environment-assessment.md)。当前暂无商业计划；发行意向沿用此前暂无发行/出版的工作假设，不替用户签约或采购。

本轮新增[研究适配、表情差分与测试取舍](research-and-test-scope-2026-10-20.md)：采用报告中的状态优先级/同类合并/防闪烁/宿主可恢复输入原则；固定静态表情词表，不引入上游Core、Node/Electron、连续会话或人格算法。规划测试合并为T01–T08，原条目取舍可追踪，既有测试不删除/改预期。

## 1. 现有入口与复用

| 源码 | 已有能力 | 本版接入 |
| --- | --- | --- |
| src/deskpal/gui/main.py | 可选Qt、QApplication、唯一后台factory | 保留；不新增第二Agent或渲染进程 |
| src/deskpal/gui/host.py | DesktopHost拥有bridge/window/tray/notifications，Closed后退出Qt | 拥有PetController和宠物窗口；关闭汇合还需等待自有图片加载线程退出 |
| src/deskpal/gui/bridge.py | 后台命令/查询/控制分通道、背压、epoch与关闭 | 最小状态查询复用读通道，单个在途，不能挤占取消/退出 |
| src/deskpal/application/desktop.py | Qt-free公共DTO与任务摘要 | 增加无正文的表现快照；不依赖当前选中任务或系统通知开关 |
| src/deskpal/gui/preferences.py、tray.py、window.py | QSettings、恢复/退出、几何与DPR修正 | 独立桌宠开关/显示/位置/置顶/穿透偏好，不复用主窗口geometry |
| tests/gui/与既有GUI脚本 | 合成/offscreen、权限/隐私/慢后端/退出、原生屏幕检查 | 新增静态PNG与桌宠测试；新证据不覆盖旧版本/旧失败 |

立项时的主窗口文字占位不等于独立桌宠。现已实现实际PNG窗口、交互、状态与退出汇合；原生混合DPI和30分钟持续运行已执行，长测诊断失败及增长未完全归因，按最新用户收尾要求归档保留。

## 2. 最小实现方向

使用现有PySide6-Essentials的Qt Widgets/QPainter，在宿主GUI线程创建PetWindow；受控工作线程读取/解码QImage，GUI线程有界转换QPixmap并绘制。采用固定expression_id映射静态差分，推荐neutral/thinking/focused/pleased/alert五张；neutral必需，其余缺失回neutral并保留准确文字。不建立逐帧动画或情绪循环。

调用方向：唯一Application/Core → Qt-free最小快照 → 现有Bridge → PetController → PetWindow/静态图片适配。用户点击只发本地反馈或交DesktopHost导航；工具/授权仍走原有主窗口审查路径。

已实现模块按下列职责划分：

| 模块 | 职责 |
| --- | --- |
| presentation/models.py、state.py | Qt-free不可变状态DTO和固定展示映射，不依赖Application具体对象；过期/乱序/去重由GUI Controller处理 |
| gui/pet_controller.py | 窗口/加载/状态请求所有权、generation、开关/隐藏/关闭、偏好协调 |
| gui/pet_window.py | GUI线程透明窗口、QPainter/图像呈现、命中/拖动/DPR/工作区 |
| gui/pet_assets.py | 代码声明的固定expression_id/随包PNG映射、异步准备与有限缓存；不带用户正文或外部资源解析器 |
| 项目package资源目录 | 最终透明PNG和非敏感来源manifest，通过importlib.resources取得，wheel确实包含 |

不提前实现通用渲染框架/进程IPC。可保留少量静态呈现接口作为未来替换点，但不为延期的Live2D增加依赖或空实现。

## 3. 先冻结的契约

| 契约 | 要求 |
| --- | --- |
| PresentationSnapshot | 精确七字段schema_version/session_id/sequence/memory_epoch/status/task_id/revision；generation/privacy_epoch/request_id属bridge信封，新鲜度属GUI时钟，宿主生命周期走既有信号；不含Task/Plan/Memory正文/工具参数 |
| 状态来源 | 公共Qt-free应用快照；Ready不表示idle，通知流不是完整源；准确反映当前活动工作 |
| 查询/显示 | 单个在途，初始1秒异步查询且可合并；隐藏/禁用停止专用查询，恢复先取新快照，不补历史反馈 |
| 状态/表情映射 | 状态枚举保持不变；Planning→thinking、Running→focused、新Completed→pleased、Failed→alert，其余neutral+准确文字；不推测用户情绪 |
| 展示调度 | 状态文字即时更新；同类图合并，装饰延迟只用有限single-shot；关闭/Waiting/失败/失效立即抢占，定时器重核generation/revision/epoch，不补历史反馈 |
| 图片清单 | 代码固定词表→随包相对资源名/来源/hash/尺寸/用途；开发期记录来源，运行时不读外部manifest/任意路径/URL/脚本，未知id回neutral；小集合预备后切图不读盘 |
| 加载 | 最多一个在途和一个合并最新请求；QImageReader/句柄属工作线程；不可变完整QImage结果携generation；超时失效但仍跟踪worker |
| 绘制 | QPixmap/QPainter/Widgets留GUI线程；无FPS/呼吸/序列帧timer；缩放缓存按DPR/尺寸更新并有总预算 |
| 偏好 | 默认禁用；enabled与visible分开，独立逻辑位置/屏幕标识/尺寸/置顶/穿透；无正文/授权；坏值夹回或回退 |
| 生命周期 | Disabled/Loading/Visible/Hidden/Degraded/Stopping；关宠释放，隐藏可保留有限缓存；Host等待自有工作结束才退出Qt |

资源和性能候选值见验收边界；先初测再冻结，不在首次失败后改预算。公开状态投影不更改既有任务权威或执行语义。

## 4. 开发阶段与出口

| 阶段 | 工作 | 出口/证据 |
| --- | --- | --- |
| 0 静态方案/资源界限 | 明确Qt线程、图片预算/来源、状态API、默认交互、合成夹具；现有环境元数据复用 | 父审接口/所有权；无新增运行依赖；小型合成PNG探针和候选预算初测，非Live2D探针 |
| 1 状态垂直切片 | Qt-free快照/状态与expression映射，bridge窄读；过期/去重/epoch/背压、优先级/装饰防闪烁 | Luna定向单元/strict/离线集成；T02关键状态与抢占准确，无额外工具执行；纯时序用可控时钟不实等 |
| 2 窗口与异步图片 | PetController/PetWindow、实际PNG加载/降级、透明/拖动/点击、关闭/迟到结果 | Qt合成与原生PNG冒烟；慢解码时GUI响应，工作线程不遗失，无主线程IO/join |
| 3 主窗口/托盘/偏好 | 开关/隐藏/置顶/穿透及找回、逻辑几何/DPI/屏幕消失回退、退出汇合 | T04一次原生输入流程，T05代表DPI跨屏与几何单元；不做图片×DPI×Python组合，无托盘夹具与权限边界保留 |
| 4 定稿美术与整合 | 同一角色中性定稿后编辑差分；同画布/锚点，alpha/比例/来源检查，固定资源入包 | T01逐资产一次静态检查及原生切图；五张是推荐集合，neutral+准确标识最低可用，不等全套图才开发 |
| 5 最终验收 | 3.12一次受影响回归/strict、3.11一次同范围兼容；T07源码/wheel对照；T06两慢路径；T08一次30分钟运行含禁/启宠各5分钟空闲和20轮开关/隐藏/切图 | 原始证据/指纹/曲线与正常退出，父最终验收；不重复全仓/独立同类长测，异常先归因再按影响扩测 |

不再将自有Live2D画稿/建模/动作作为前置。美术流程为“角色方向→概念定稿→透明PNG→原生桌面检查→资源入包”，没有PSD拆件/网格绑定/SDK安装步骤。至少一张合格PNG加准确中性状态标识可完成本版，不因五套状态美术暂缺而停止工程工作。

差分由同一neutral定稿编辑眼眉嘴，保持画布、身体、缩放和锚点稳定；切图不resize窗口/改命中区。初始装饰参数thinking延迟250ms/最短停留500ms/完成最长1500ms，等待/失败/unknown/关闭不受其阻挡；这是待初测体验值，不是从报告复制的已验收常量。

用户已选择简洁可爱的小机器人，授权内置生图工具制作首版原创美术；五张同角色PNG及来源记录现已入包。没有读取API key、调用应用云模型、上传私人素材或下载推理模型；原生显示验收与结构检查分别记录。

## 5. 生命周期与桌面规则

PetController由DesktopHost在GUI线程拥有。图片准备异步、有预算；关宠/换代后迟到结果只释放，不更新窗口。重复开关不新建无限loader；旧任务未退出时拒绝/合并新加载。

隐藏停止专用状态轮询，不触发绘制循环；恢复刷新当前状态。关闭宠物释放图片/窗口/缓存与回调；应用关闭仍先阻止新后台工作，协作关闭Application，并等加载线程/Qt对象资源正确结束后退出事件循环。超时保留真实Closing、引用/所有权与诊断，不强杀Python线程或Core。

主窗口默认关窗退出、显式关窗留托盘和无托盘时的规则沿用V0.5；宠物关闭不意味着Application关闭。后台状态不抢焦点；显式打开主窗口才导航。拖动按Qt阈值区分，透明命中不采屏；穿透时托盘/主窗口仍能恢复。

保存逻辑坐标/屏幕标识和正常尺寸，当前DPR用于图像缩放与命中换算。屏幕/DPR/availableGeometry变化重新校验，允许负坐标，屏幕不在时夹回现有屏幕，不自动更改Windows缩放/电源/任务栏设置。

## 6. 分工和验证纪律

用户约定保持：**6 Luna高思考执行常规单元/mock/strict/回归/实机重复验证及证据采集；6.1 Sol high负责代码编写；父Agent负责架构/线程生命周期、复杂故障归因、关键修改审查与最终验收。** 共享文件指定owner，不并发改同文件，不唤醒旧V0.6任务。

测试使用明确项目解释器、独立合成数据/偏好和离线provider。测试夹具可自制小PNG验证预算/坏资源，最终角色图仍需原生验收。首次失败保留原始输出/退出码/现场，不改预期/跳失败/以重跑覆盖；崩溃/错误线程销毁/持续增长升级父审。

原立项文档检查只属历史证据；当前实现及新测试以versions/v0.7/test-report.md和独占v07证据目录为准。回归前静态审核测试收集/夹具，避免触发封存V0.6原生实验；报告实际运行范围，不删除/改skip凑全量通过。

测试按T01–T08执行：单元枚举/坏资源/几何边界只做一次有区分力的参数化覆盖；真实透明/穿透/混合DPI必须原生测。慢Core与慢loader是独立故障不可互相代替，加载中退出/迟到结果/权限/隐私不裁剪。最终受影响集合依据实际改动确定，优先复用tests/gui与tests/integration/test_v05_desktop.py等已有资产；不要为本版未改的OS锁/后端崩溃恢复再造实验。

资源循环由各项100轮合为最终同构建20轮逐周期采样，30分钟连续运行仍保留并内含空闲基准；有异常先存证父审，必要时另开100轮调查，不能靠扩次数等通过。原生125%/150%全矩阵、逐图片逐环境组合、实物断屏/休眠/RDP归扩展验证且明确未测；不是已有失败的豁免。新的显示/会话问题或改动扩大时补对应覆盖，不以“概率低”忽略已知故障。

## 7. 延期、剩余决策与交付

| 项目 | 当前处理 |
| --- | --- |
| Live2D/Cubism/绑定/模型/动作/许可/GL/独立进程 | 全部正式版后重新立项，未定具体版本/日期；本版不安装、不测试、不作为阻断；历史提案和失败/缺证据保留 |
| 角色方向 | 已按用户选择生成简洁可爱的小机器人五张PNG；来源/结构/棋盘视觉已查，剩余原生范围见测试报告 |
| 原生混合DPI设备 | 已识别实际100%/200%双屏；T05真实往返仍待完成，枚举设备及逻辑模拟不代替跨屏证据 |
| 实物断屏/休眠/RDP与完整缩放矩阵 | 扩展验证，不作为本版专门认证；需要支持或出现相关问题时补测，不擅自改系统设置 |
| 将来发行 | 当前非商业本机研发；发行前复核实际图片/Qt/闭包条件，不将非商业当通用许可豁免 |

实现后才写docs/versions/v0.7的interfaces/operations/test-report/delivery，并更新实际架构/依赖说明；不自动升版本或宣称V0.7交付。Git由用户提交，不stage/commit/push/reset/PR，不清理用户文件。

用户暂停时停止开发/测试并通知本轮子Agent，保存源码状态、指纹、证据/首败、未完成和准确恢复入口。恢复只处理V0.7自有资源，不访问V0.6封存对象。
