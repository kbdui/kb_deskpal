# V0.7 静态桌宠测试与未完成验收

更新：2026-10-21（Asia/Shanghai）。产品0.5.0rc1；**按用户要求带未决项归档，未完成全部验收**，见[归档记录](delivery.md)。本文件只汇总本版真实新证据，不将V0.5基线或V0.6实验算作通过。代码由6.1 Sol high编写，常规执行由6 Luna high负责，父Agent审查与验收。

## 已执行

证据根目录：[implementation-20261020](../../../data/validation/v07/implementation-20261020/)。各目录含原命令、日志和适用的指纹；不同批次存在重叠，不相加成独立覆盖数。

| 批次 | 实际结论 | 边界 |
| --- | --- | --- |
| backend-r1 | 原状态接口47通过 | 后来索引优化后由r2覆盖 |
| backend-r2 | 108通过 | 状态投影、持久工作流、schema1附加索引与原排序对照 |
| gui-r1 | 新桌宠11通过 | 合成Qt资源/状态/加载/关闭，不代替实际PNG和原生输入 |
| regression312-r1 | 158通过，82.77秒 | 既有GUI与受影响应用/隐私/实例回归 |
| compat311-r1 | 277通过，81.17秒 | Python3.11同范围兼容，含前述新旧用例 |
| strict-r2 | 正式命令101源文件无问题 | Qt实例类型已缩窄，不用ignore掩盖 |
| strict-probes-r3、r4 | 含src及四probe，103源文件无问题 | r4外围指纹脚本路径拼接错误，不能声称r4有有效前后hash；运行输出保留，未为补证重复执行mypy |
| projection-r2 | 10k/100k合成历史语义一致、索引命中 | 查询/并发取消最大低于5ms；10万历史首次索引约795ms、重开约5.7ms；不是任意损坏库的硬上界 |
| asset-r3 | 五PNG结构通过，10张诊断预览写出，源图hash不变 | 真实1254×1254；合计3,291,181字节、7,862,580像素，Qt与独立PNG解码逐像素一致；结构不等于原生视觉 |
| offscreen-smoke-r2 | 沙箱外12秒实际PNG整机完成，normalClosing=true | 心跳p95 79ms/max125ms，6条控制回执最大63ms，额外关闭约219ms |
| offscreen-slow-core-r1 | 独立4秒慢Core，运行与回收完成 | 心跳p95 63ms/max125ms，控制最大62ms，额外关闭约281ms |
| offscreen-slow-loader-r1 | 独立4秒慢loader，超时降级和回收完成 | **心跳p95 125ms超过100ms候选目标**（max141ms），控制最大79ms；不得因exit0标全部指标通过 |
| native-input-smoke-r2 | 修复辅助窗口生命周期后5.844秒完整正常关闭 | Host/宠物及两个bench窗口均释放，normalClosing=true；无人操作的短测不认证原生点击 |
| native-input-operator-r1 | 508.328秒，exit0、normalClosing=true | 70条事件、508次采样；透明角落点击到底窗一次、角色点击导航一次。没有足够操作者记录认证其余交互或跨屏 |
| 20261021/native-smoke-r1 | Windows QPA真实PNG，12.453秒，normalClosing=true | 心跳173样本p95 125ms/max468ms，控制7样本最大94ms；GUI内资源采样最高172ms，响应目标尚未通过 |
| 20261021/offscreen-slow-loader-r1 | 独立4秒loader，12.281秒，normalClosing=true | 心跳177样本p95 125ms/max188ms，控制6样本最大110ms；GUI内资源采样最高141ms，响应目标尚未通过 |
| 20261021/strict-added-helper-r1 | src及五探针strict通过（104文件）；新增分发helper另检通过 | 后续资源采样探针修改仍需定向strict，不把此前检查算作最终探针已测 |
| 20261021/wheel-offline-r1、wheel-install-r2、distribution-cli-r1 | 离线wheel构建、含中文空格target安装及资源/无Qt CLI探针通过 | 五PNG与provenance的source/wheel/installed哈希一致；安装GUI短测另验 |
| 20261021/strict-final-probe-r2 | 最终异步采样/精简checkpoint探针strict通过 | 原r1检查前一probe版本，保留但不代替r2 |
| 20261021/installed-native-smoke-r1 | 安装target原生12.453秒、normalClosing=true，无probe失败 | source_package确为中文空格target；心跳195样本p95 63ms/max141ms，控制7样本最大47ms；诊断采样线程真实结束 |
| 20261021/offscreen-slow-loader-final-r1 | 独立4秒loader路径12.312秒、normalClosing=true，无probe失败 | 心跳193样本p95 63ms/max125ms，控制6样本最大47ms；超时降级与资源结束，保持原50ms Coarse及100/500ms门槛 |
| 20261021/native-input-operator-r3 | 600.828秒，exit0、normalClosing=true、failures=[] | 原生拖动不导航；整窗穿透body到底窗及取消后body单击正常导航；focused/pleased/alert预览的几何/neutral命中区域一致。neutral/thinking、位置恢复和DPI另补，不因本轮exit0全项通过 |
| 20261021/native-input-operator-r4b | 600.844秒，exit0、normalClosing=true、failures=[]，额外关闭672ms | 进入后桌面工具因Esc停止，没有新的受控输入验收；最终报告观察到未归因的原生事件，receiver_clicks/navigation_requests均0。Host/bridge/pet及两个辅助窗口正常释放，不认证待补的交互项 |
| 20261021/native-input-operator-r5 | 安装版300.875秒，exit0、normalClosing=true、failures=[] | neutral/thinking真实预览可用且无fallback；拖动从[580,146]到[620,186]，导航/底窗点击均0；hide/show及禁用释放后重启均恢复同位置。恢复权威查询后Unknown→Idle，控制台保持active、宠物不active；不认证跨屏 |
| 20261021/native-input-operator-r6 | 安装版301.141秒，exit0、normalClosing=true、failures=[] | 操作只移动了测试面板，没有宠物输入，navigation/receiver均0；不计跨屏通过。工具模块绑定错误和Esc停止记录保留 |
| 20261021/native-input-operator-r7 | 安装版281.453秒，exit0、normalClosing=true、failures=[]，额外关闭609ms | 用户真实拖动主屏→右侧200%→主屏；右屏DPR2时禁用释放再启用恢复原位置，回主屏DPR1；固定240×284逻辑尺寸及neutral命中区域不变，navigation/receiver均0。右屏外观正常由用户人工确认，工具截图未覆盖右屏 |
| 20261021/final-soak-installed-r1 | 安装版1800.406秒，Qt exit0、normalClosing=true；**helper exit1，三条诊断失败保留** | 3轮预热、20轮组合/120控制完成，源码/PNG/probe前后指纹一致；心跳28,714样本p95 63/max157ms，控制p95 62/max109ms，额外关闭282ms、采样worker结束。禁/启CPU各300.281/300.547秒、8.2735%/8.6873%单核，增量0.4138个百分点；资源曲线与诊断失败归因中，不标T08整体通过 |

父已查看五张棋盘背景预览：角色完整、真alpha、同画布；差分改脸但生成并非逐像素锁定身体。轮廓alpha≥128两两IoU约0.988–0.997；运行时固定neutral命中区域，仍需原生切图观察确认不跳动或裁切。原始图未后处理/覆盖，提示词与来源在随包provenance.json。

## 首败与归因保留

| 证据 | 首次结果与处理 |
| --- | --- |
| environment-r1 | 元数据包装首个NameError及修正均保留；不算产品测试 |
| projection-r1 | 诊断捕获预期ValueError时误用外层嵌套事务，触发现有poison保护；只修probe的事务采样，r2复验，未改store保护 |
| strict-r1 | QApplication.instance stub返回QCoreApplication；生产代码以isinstance缩窄修复，r2复验 |
| strict-probes-r1/r2 | 探针类型及检查范围问题；没有放宽测试/预算，修正类型和包含src后复验 |
| asset-r2 | 显式b'PNG'匹配stub却不被本机binding接受；只修诊断写图为按.png后缀推断，asset-r3复验 |
| offscreen-smoke-r1 | 12秒无Ready、无循环，报告资源关闭完成；退出清理sandbox AppData临时目录WinError5。首败保留；后按工具流程在沙箱外执行r2，不据此认定权限是唯一原因 |
| native-input-smoke-r1 | Host/bridge/宠物已Closed及settled，但两个辅助窗口无条件ignore closeEvent，阻止Qt退出。只修probe两窗所有权与正常关闭记录，未改Host/Core；原会话Ctrl+C结束、两HWND消失，r2复验。后续11次human/unknown点击不计受控验证 |
| strict-probes-r4/r5外围记录 | PowerShell路径/数组包装导致部分pre/post hash无效，原缺陷保留，未重复mypy补造前证；r5补有效post-only104文件快照与说明，产品源对r3基线未变 |
| 20261021/wheel-install-r1 | 外围PowerShell参数合并错误发生在pip执行前，原wrapper-failure保留；r2修正包装后离线安装成功，非产品失败 |
| 20261021/native-input-operator-r2 | Esc导致桌面工具停止；用户重新授权后同一子轮仍被工具阻止，未继续调用。300.75秒后Host及辅助窗口正常释放，但外层exit1：运行期间父安排修check_v07_pet.py，输入probe记录该依赖指纹从72f369…到0db5ff…。这轮不能作为同构建验收通过；无有效拖动/点击记录，保留原始中断及指纹失败，后续在全部冻结的新子轮补缺项 |
| 20261021/native-input-operator-r4 | 包装提前创建helper要求独占新建的probe-output，进入Qt前FileExistsError、exit1；原目录保留，另用r4b补尚未验证项，没有修改helper的目录校验或产品 |
| 20261021/native-input-operator-r5工具绑定 | 起初误用仅浏览器面的cua_repl.getState，apps=[]；猜测listWindows报not a function。父指出应按computer-use技能使用node_repl + @oai/sky，改回正确插件后实际选中同一PID39644窗口完成操作；无新进程替换、无替代输入注入，不将错误工具面的空列表当窗口不存在 |
| 20261021/strict-preview-trace-r1 | 新增被动时序诊断helper的strict首败：invoke调用类型及参数类型共5错，102源文件；未运行该helper，不改原probe或验收断言，由Sol修正类型后另批复验 |
| 20261021/final-soak-installed-r1 | helper exit1：test_only_preview_paint_unobserved:13、:16及test_only_five_fixed_asset_paint_coverage_incomplete。前两条均有Paint事件且painted回调已执行，但回调时pixmap identity不再匹配，驻留250/266ms；现有记录不足以区分覆盖发生在Paint前/后或回调迟到。五个非None ID实际均有，第三条因集合额外含None而不等，不是缺少某一张图。保留原报告和全部断言，新增被动时序诊断定因，不以新结果覆盖原失败 |

asset-r3最初30秒时记录的是尚未完成观察；同一原进程随后自然完成（UTC09:41:10、exit0），不是再次运行。后续以完整退出记录为准，不把早期空日志误写成崩溃。

native-input-smoke-r1曾出现Luna“空events”观察与父当前43407字节/64条实际文件不符。已用同一LiteralPath重新核对并保存observer-reconciliation.txt，未发现不同cwd；早期读取时点未记录，不猜测shadow原因。原始文件保留，不能把Get-Process在沙箱内看不到进程当沙箱外已退出证明。

native-input-operator-r1已正常结束：2026-10-21恢复后重新读取原exit/result/events/summary，不能把中断前的“进行中”继续当作存活状态。受控透明角落点击（pet-local4,4、mask=false）到达底窗，opaque点击触发一次导航。导航后主窗口激活符合“显式打开主窗口”的既定交互，不是后台状态抢焦点；首次误判说明和原事件均保留，后台更新的焦点约束仍单独验证。未完成的拖动/穿透/切图/DPI行为不提前记通过。

r3过程中桌面工具再报告physical Escape stop，遵守工具停止要求，用户新消息重新授权后才在新子轮恢复。seq79–82是父要求的“取消穿透后正常body单击”，一次导航符合既定交互；操作者首次将它称为“重要异常”的消息保留并更正，不是拖动误导航或放宽预期。有效拖动另有seq17–23的spontaneous鼠标及Move事件，导航未增；穿透body到底窗见seq35，不能把未知/未命中动作计入受控通过。

响应归因：保留原50ms默认Coarse timer与100/500ms阈值，新增有界raw heartbeat、tick正文及Toolhelp采样耗时，不通过调timer类型或放宽预算消除首败。[Qt官方定时器说明](https://doc.qt.io/qt-6/qtimer.html)指出默认Coarse且忙碌时所有类型均可能延迟；[Windows计时说明](https://learn.microsoft.com/en-us/windows/win32/api/timeapi/nf-timeapi-timebeginperiod)说明不可见窗口的分辨率限制。这些仅支持待查假设，**不能替代本机数据归因**，也未调用timeBeginPeriod或修改系统设置。

2026-10-21两批原始计时进一步显示：探针在GUI线程调用Toolhelp资源采样，一次约78–172ms；tick正文p95分别79/110ms、最大172/141ms，与采样耗时同量级。同步测量本身已形成可观测的GUI占用，不能直接归因产品loader；调度等其他因素仍可能存在。父已要求仅修探针为单个非Qt后台采样线程、有界最新样本、真实采样时间/年龄与完整结束记录，保持原计时器/阈值/长测时长。旧首败保留，新方法短测和长测另立批次，不将两种测量方法视为完全相同实验。

探针修复另经父审：将每次调用动态建立的ctypes Structure/POINTER与DLL绑定固定为一次初始化，避免ctypes按类型缓存指针而产生测量自身的内存增长。单worker每秒取样，GUI非阻塞读取最新值；稳定窗口只计阶段后的新鲜独立样本，CPU用真实采样时间跨度，Qt循环结束后有界等待该诊断worker并记录最终采样。五图循环用明确test-only缓存预览和Paint观察补足，不伪造权威状态或确认工具；这些诊断改动未修改生产源码。

最终probe为5f23b888c922e4aa4e67fad53581f259f9afb3f6e5c10bc27a36e94e64a9c988。运行中仅阶段/新增错误写固定大小checkpoint，逐秒samples.jsonl及resource-worker.jsonl继续保留；Qt退出后is_final_report=true的完整report含所有样本/计时/指纹。避免增长的完整JSON在GUI线程反复序列化形成第二种测量干扰。新短测p95均63ms，支持原测量存在实质干扰且修复后满足目标；不声称已证明全部延迟只有一个原因。原超标证据和方法差异均保留。

最终长测判读保持原进程预算：以新鲜预热基线逐条比较20轮回收点，并核对其后禁宠区间的唯一新鲜worker样本。计划disable后2秒的cycle_settled记录本身不证明OS资源已连续稳定5秒；不可用后续低值替换原记录。CPU分别用禁/启两段实际采样跨度（各至少300秒）复算，不扣估计采样开销。探针的原始计时/样本数组随时长增长，且循环与时间相关；这说明归因局限，不能据此扣除推算内存或放宽20MiB门槛。最终关闭后的线程/内存下降也不能代替运行中逐轮回稳。即使满足预算，结论仅限本轮30分钟观测，不声称证明无泄漏。

逐轮后续采样可选连续至少5秒作为回稳的辅助观察窗口；这不是验收边界新增门槛，更不要求每秒句柄完全相等。warmup/idle中固定signature稳定5秒是探针阶段推进机制，不移植成全部cycles的额外要求。保留小幅波动范围和原2秒快照，按既有预算与完整趋势判断；线程低于基线时同时核对bridge仍Ready及工作所有权，不能把异常退出误判为回收成功。

2026-10-21最新桌面控制停止由用户明确确认按下Esc，按工具要求停止该轮接管。没有新的恢复授权时，只继续不接管输入的测试；不将工具中断记为产品故障，也不将无人操作的存活采样记为原生交互通过。

r4b早期checkpoint为native_input_observed=false，完整收尾报告为true、event_count=2093；存在spontaneous鼠标事件但操作者及意图未经归因，两个明确控制计数均0。以最终报告为准，不把早期观察继续写成全程无输入，也不将这些事件补认作受控拖动、DPI或位置恢复证据。

input probe用于原生输入/几何观察，自身仍每秒同步取得进程资源；其运行存活和关闭证据可按范围使用，计时不得代替已修正异步采样的check_v07_pet.py所承担的T06/T08响应/资源门槛。

final-soak-installed-r1运行中elapsed1226.031秒出现Unknown/Degraded/cache0，父要求保留前后行后复核：1224/1225为Disabled、loader=false、settled=true；1226启用且loader_busy=true、settled=false；1227已Visible/Completed、loader=false、cache4,608,000；1228按控制流程Hidden。Controller的_enable先设置Loading，随后_visibility在缓存尚空时将lifecycle置Degraded，所以该字段也覆盖初次加载过渡，并非只表示已发生故障。此处在2秒超时前实际加载完成，与该源码路径一致；不以“后来恢复”单独作解释。采样没有diagnostic/bridge_ready/generation字段，不能凭这一组行认证所有错误诊断路径，最终加载/预览及失败列表仍另核。

T07的无Qt证据使用base Python的隔离模式，从明确安装target执行`deskpal --help`：PySide6不可发现、Qt模块未加载、无写文件/数据库事件、全新cwd仍空，exit0。该base现有python-dotenv=0.21.0、tzdata=2023.3，其中dotenv低于项目声明>=1.0；未为测试安装新依赖。因此这只是当前无Qt环境的入口/导入边界证据，不认证完整CLI业务或符合声明的干净环境安装。安装GUI另用具备项目依赖的主解释器检验。

r7跨屏证据：右屏首次DPR2记录seq181；用户松开后seq294位置[2447,142,240,284]，DISPLAY1/DPR2。禁用后缓存为0且窗口销毁，重新启用在同一位置/DPR2加载完成。用户确认“已拖回左屏，右屏显示正常”；返回主屏首次DPR1记录seq422，最终松开后seq728位置[1136,269,240,284]、DELL P2419H/DPR1。两端mask均224区域、bounds=[8,41,224,235]，宠物不active，控制台active，无导航或底窗点击。右屏外观是人工观察，命中仅认证实际发生的拖动；不扩展为每个透明边缘在所有DPI均受控点击认证。

最终长测的逐轮资源复算见[resource-analysis.md](../../../data/validation/v07/implementation-20261021/final-soak-installed-r1/resource-analysis.md)：预热基线private=77,836,288字节、句柄1304、OS线程22；20轮原始回收点均有匹配的新鲜worker样本，禁用区间private最大增量14.098MiB、句柄最大+5，回收点线程18–20且bridge仍Ready。启宠private最大增量22.402MiB；运行最大RSS156.441MiB、记录峰值RSS168.988MiB。均未超对应数值预算，但禁宠空闲、启宠空闲、循环阶段的private斜率分别为+0.4794/+0.2789/+0.4289MiB每分钟，不能据此直接宣布无持续增长。探针保留的原始数组会增长，尚需实际分配归属诊断；不扣估计内存、不用正常退出替代运行中归因。1787条worker序列连续、无错误；GUI最新槽出现19次重复消费和6条未消费worker样本，原始worker记录仍完整，匹配资源字段无矛盾。

## 核心门槛进度

| 门槛 | 当前结论 | 仍需证据 |
| --- | --- | --- |
| T01 真实图片与降级 | 已测；五张结构/棋盘视觉、定向失败路径及五种原生预览分别有证据 | r3覆盖focused/pleased/alert，r5 seq17/21补neutral/thinking，均实际可用；原生预览不冒充对应Core业务状态，T08诊断identity竞态仍独立保留 |
| T02 状态与调度 | 已测通过 | 最终持续运行不代替已有状态/授权测试 |
| T03 加载与退出 | 已测通过 | 最终持续运行的真实资源汇合见T08 |
| T04 原生交互 | 已有受控透明点击、拖动不导航、全窗穿透及取消恢复，r5补同屏位置恢复和后台更新焦点 | r5 seq83→85为Unknown→Idle、控制台active/宠物不active；只认证本次观察区间。跨屏位置/命中见T05 |
| T05 DPI/多屏 | 逻辑几何及r7实际1x/2x双屏往返、右屏释放/重启位置恢复已测 | 用户确认右屏外观正常；原生拖动命中、固定逻辑尺寸/区域和回主屏DPR1有记录，扩展环境仍未验证 |
| T06 慢路径响应 | 独立慢Core、慢loader及修测量后的响应达标 | 最终持续运行见T08 |
| T07 回归/安装 | 受影响双Python回归、strict、资源wheel/target一致、无Qt CLI帮助及安装GUI已测 | 无Qt帮助不认证完整CLI或干净依赖安装；禁宠资源对照合入T08 |
| T08 资源/持续运行 | 30分钟同构建运行完成，但helper exit1，未整体通过，按用户要求归档 | 响应/CPU/回收数值预算达标；父已完成[内存简析](memory-review-2026-10-21.md)，未排除全部泄漏。两个预览identity失败未定因，用户明确不做时序诊断；后续条目见[清单](deferred-tests.md) |

缺环境的扩展DPI/拔屏/休眠/RDP项目按验收边界标未验证，不冒充通过。发现持续增长、错误线程销毁、挂起或证据矛盾必须父审归因，不能重复到通过。V0.6阶段0和既有V0.5未结项维持原状态。

## 2026-10-21 用户收尾决定

用户明确不做表情切换时序诊断，要求简要分析内存并处理必要修复后先归档。父复核现有资源曲线和源码，确认探针有增长的记录保留路径，但没有实际heap/native证据支持把全部增长归给探针；未定位到明确需修的生产缺陷，因此不作推测性修改。原probe/断言/首次失败与安装构建保持原样。本次未再运行160秒时序或分配快照诊断，也未重跑长测。

新增trace helper在strict首败后仅完成类型修正，修后未复检；resource_retention helper为未审未测草稿。两者列入后续清单，不作为验收证据。归档结束本轮主动开发与测试，不把搁置诊断改成通过。
