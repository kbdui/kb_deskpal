# V0.6 开发进度与交接

阶段：**阶段0未通过，2026-10-18 09:35（Asia/Shanghai）按用户要求暂停**。最新入口为[09:35暂停检查点](pause-checkpoint-2026-10-18-0935.md)：fresh ACL r3精确DACL通过但真实拒写失败；新增SD端点实现双Python各293项mock与10文件strict通过，最新实现独立审查及fresh r4尚未执行。[继续记录](resume-2026-10-18-after-0019.md)、[00:19检查点](pause-checkpoint-2026-10-18.md)及下文为历史过程。V0.6A/B尚未交付，产品仍0.5.0rc1，Core/Factory没有切换执行后端。Git仍由用户自行提交；用户再次明确继续前不恢复开发或测试。

## 08:40暂停后的恢复进度

主Agent已读取暂停检查点并重新分派子Agent：6 Luna修复并复核loader清理测试的4条类型错误；6.1 Sol审查下一项Windows启动对照。没有重跑旧baseline，不修改私人配置或安装依赖。

USERPROFILE与APPDATA单变量对照已由主Agent分别执行：`loader-user-profile-r1.json`、`loader-roaming-appdata-r1.json`。两次均保留原7个加载/卸载/退出事件、exit0xC0000142/0帧，source_unchanged=true，worker_reap=confirmed且profile_deleted。这些单变量没有修复初始化失败，不能据此排除多个变量交互；当前停止继续猜键，转查控制台创建方式。阶段0仍未通过。

loader测试4条类型错误已由6 Luna最小修复：fake改用call_events记录顺序，从launcher直接导入LaunchRejected，不扩大产品导出。双Python各4 passed、脚本/测试2文件strict均通过，起止SHA一致（`loader-cleanup-r2-run-record.json`及对应日志/JUnit）；r1失败仍保留。随后继续独立诊断console-mode，并根据下文实际证据选择候选adapter默认。

console-mode诊断已实现。首轮mock揭示显式no_window与默认值相同会绕过argparse组互斥检查，已改为默认None、解析后归一化，未修改测试断言。双Python各22 passed、脚本/测试strict通过，起止SHA一致（`loader-console-unit-r2-*`）；首轮19 passed/3 failed日志保留。

主Agent执行 `loader-detached-r1.json`：仅将NO_WINDOW换DETACHED，实际flags=0x0008040E，完整AC/Job/child/句柄约束保留。进程加载apphelp.dll、到初始breakpoint，此后0xC0000008发生first/second chance并退出，0帧；与原0xC0000142不同，但尚未成功完成native smoke。source_unchanged=true、worker_reap=confirmed、profile_deleted。下一项拟在相同DETACHED下移除debug位，核对异常是否由调试环境和合成无效句柄负测组合触发；不吞掉异常，不据此宣称句柄安全或阶段0通过。

无debug对照已实现并由6 Luna复核，双Python各44 passed及脚本/测试strict通过（`loader-plain-unit-r1-final-*`，最初临时目录/合成SID测试夹具失败记录保留）。主Agent随后执行 `loader-detached-no-debug-r1.json`：actual flags=0x8040C、debug_enabled=false，仍exit0xC0000008/0帧，源码稳定、回收确认、profile_deleted。因此“仅由debug连接造成”没有得到支持；下一步只读查询实际StrictHandleCheck策略，区分已预期的无效句柄负测与其他执行故障，不能直接忽略异常继续验收。

StrictHandleCheck元数据补丁经双Python各49 passed及脚本/测试strict通过（`loader-policy-unit-r1-recheck-*`）。真实 `loader-strict-handle-r1.json` 在挂起恢复前查询policy3成功：raw_flags=3、RaiseExceptionOnInvalidHandleReference=true、HandleExceptionsPermanentlyEnabled=true、reserved=0；进程仍0xC0000008/0帧并已确认回收/删除profile，源码起止SHA稳定。该结果支持“合成无效句柄负测与严格策略冲突”的假设，但尚未精确定位异常调用。下一步仅制作新版本fixture，在GetHandleInformation(excluded)这一条调用周围用SEH捕获预期0xC0000008并记录；其他异常继续搜索，不关闭严格策略、不让launcher吞异常，旧fixture与哈希不覆盖。

V2 fixture源码 `tests/fixtures/windows/native_probe_v2.c` 已完成并由主AgentMSVC14.44/SDK26100实际构建成功（/W4 /WX /MT，build.log无警告），旧V1未覆盖。V2只在该负测API周围精确捕获STATUS_INVALID_HANDLE，独立记录excluded_handle_exception，不编造Win32返回错误。源码SHA `226c38d7e9798735ac144b1ef0a209a3b92d8805e06e075b49646fb7aecdb5cb`，exe SHA `08423adb75615e3609e268409be844053bf2e14af1d4d250917006c0ba7ab4e1`；构建receipt在 `data/v06-stage0/native-fixture-v2/`。loader仅允许固定v1/v2路径与已审查hash，主Agent审查pin后交目标mock/strict，再进行下述smoke。

V2目标mock双Python各62 passed，loader/builder/测试3文件strict通过（`loader-v2-unit-r1-*`）。真实 `loader-v2-detached-r1.json` 首次完成native主体：exit0、1帧，excluded_handle_valid=false、excluded_handle_exception=0xC0000008、returned error=0（该路径API没有正常返回错误）、operation_error=0；严格policy raw3仍启用，源码稳定、回收确认、profile_deleted。这只证明指定合成程序在该候选方式下运行，以及一个数值句柄canary未有效，不能替代完整继承表/身份/文件/注册表/网络/资源证据。

基于上述结果，Windows实验adapter已选DETACHED作为默认（不是新增可降权模式）；诊断脚本保留显式历史NO_WINDOW对照。runtime候选环境改为必需显式local_appdata的五键白名单；文件系统探针移除已知四键失败循环并可明确选择ordinary/LPAC范围，绝不自动fallback。Python ctypes负测若抛OSError会保留为未决观测而非当作拒绝通过。两条实现已冻结，正在进行联合定向mock/strict；Core/Factory和产品版本仍未整合或改变。

## 双 Python 真实运行与权限对照（2026-10-17 14:04 后）

冻结候选适配器后，6 Luna完成五份测试的联合检查：Python3.12/3.11各150 passed，16个显式文件的strict均通过；17个输入文件起止SHA一致，记录为 `canonical-backend-unit-r1-*`。这不是最终全量回归。

主Agent分别执行 `runtime-py312-canonical-r1` 与 `runtime-py311-canonical-r1`：明确只选ordinary AC，不把未选LPAC算通过或作为fallback；各自的host正对照exit0，默认/只读profile下native和Python四个worker均exit0、各1帧。实际Python分别3.12.4/3.11.17，隔离路径、不加载site、不写pyc、五键环境均有报告。两轮profile均报告 `profile_deleted`。脚本中每个worker退出context前执行Job关闭并确认回收；这项代码语义与报告的profile删除事实分开记录，尚无独立逐worker停止时延数据。

默认profile下AC/Temp/INetCache/INetCookies合成文件可写；施加文件DACL后，已测目录的创建及WD/WO请求被拒。**注册表仍能创建合成子键，且SET_VALUE/CREATE_SUB_KEY/WD/WO请求均成功**，因此当前后端明确未满足持久写入边界。文件少量canary拒绝不抵seeded文件、ADS、继承/父DELETE_CHILD及所有实际缓存位置的覆盖。

另外，worker实际观察到LOCALAPPDATA/TEMP被系统追加映射为原生AC目录下的 `Packages/<moniker>/AC` 等嵌套路径；已记录为需要API路径定位的缺口，不根据宿主传入值假定实际缓存位置。Python句柄canary的ctypes OSError仍记未决；native V2记录精确STATUS_INVALID_HANDLE，但不代表完整句柄表证明。

下一步由Sol分别准备独立只读registry A/B/A绑定诊断、有限Job/停止/输出探针，主Agent审查并集中执行；Luna只做固定检查与证据核对。registry定位未可靠绑定前不施加KEY DACL。Core/Factory、产品版本和自动规则均未变，阶段0仍未通过。

## 后续所有权修补与有限探针准备（2026-10-17 18:00 后）

新增独立 `diagnose_worker_registry.py` / `diagnose_worker_limits.py`，仍为阶段0诊断，未接Core。前者只读定位两个新profile的A/B/A token与registry SD，不读值/枚举/写入/改SD；后者固定有限smoke/spin/child/flood/invalid-frame，保留只读Job/process观察副本以取得退出后证据。旧V2 memory最多16GiB分配不适合日常host，明确不运行；另版有限探针及CPU饱和证据仍待补，不能以配置查询抵通过。两脚本新增清理/报告防护经下述固定检查后，分别完成实际实验。

主Agent修补 `_create_worker` 的PI所有权缺口：若私有诊断包装在真实CreateProcess填入PI后抛错，旧代码可能回收进程却漏关process/thread句柄；现在create调用的finally在任何后续关闭/转移之前登记非零PI句柄，不在较晚的except重复补登记。6 Luna新增纯fake API测试验证原异常保留、先回收后关闭、首次回收失败保留cleanup_owner及重试、assign/verify/resume失败时每句柄只关一次。双Python三份定向测试各100 passed，6显式文件strict通过，输入SHA稳定；证据 `pi-ownership-r1/`，首次失败保留。此后真实实验须记录新源码指纹，不能把旧canonical报告当修补后的native回归。

### 有限诊断的实际结果

6 Luna完成新limits双Python各16项、registry各29项、network各46项mock，三组各两文件strict均通过、输入SHA稳定。首次mock和测试类型错误保留；registry/network仅修测试对导入属性及dict不变性的类型表达，未扩大实现exports或放松断言。证据分别 `limits-unit-r1/`、`registry-unit-r1/`、`network-unit-r1/`。这些测试不能代替实际Windows边界。

- `limits-native-r1.json`：Python3.12下smoke成功；spin触发deadline；child创建返回367；flood触发log_budget；非法帧触发frame_budget。每项关闭后Job active=0、process wait=0；profile_deleted、source_unchanged。首smoke宿主句柄净增6，后四项0，原因未明，不声称零泄漏。collect异常内已执行关闭，外层cleanup_elapsed=0不能解读为零秒停止。未运行memory，CPU20%饱和/独立child限制归因/宿主崩溃与最后Job句柄关闭/停止分布仍未过。
- `registry-aba-r1.json`：两个新profile A/B/A三次token与SD只读观察完整，A回切hash稳定、B不同，每处唯一匹配其本轮package SID，无交叉测试SID、无未解析ACE；helper均Revert确认、已退出、无close错误，挂起worker已回收、两profile_deleted、源码稳定。脚本binding_verified=false保留。主Agent与Sol结合current-profile API契约，认可在**下一新profile重新执行同一核验链**后，把当场实际HKEY作为候选实验绑定；不从旧报告/SID/裸数值/推导路径授予能力，也不宣布生产通用绑定已通过。
- registry包allow ACE flags0x23含本机SDK标注的CRITICAL_ACE_FLAG，后续拒写设计必须保留原字节，不能假定移除此项。拟前置精确package deny并按canonical次序加入OWNER RIGHTS，实际回读和正负对照尚未执行。
- `network-py312-r1/network-report.json` 与3.11同构报告：仅127.0.0.1、每send最多8字节。host正对照四API操作成功且两canary均收到，中间drain为0；受限TCP1秒超时，UDP发送API成功8字节但host未收到，TCP bind/listen及UDP bind成功。两轮worker/Job退出确认、profile_deleted、源码稳定。**没有权限拒绝错误或匹配WFP drop事件，不把超时/未收包改称拒绝通过，也不把API成功称为流量越界**；LAN/外联/IPv6/入站可达未测。

用户已确认暂无独立Windows11 VM/备用机，先完成本机开发。独立普通用户/干净系统门槛继续未通过，不将同机未提升token算作替代。下一步新registry拒写实验及绝对上限512MiB的V3产物仍在准备，阶段0未通过。

## 历史暂停检查点（2026-10-17 08:40）

以下仅为08:40暂停时快照，当前状态以上方恢复进度为准。

用户发出暂停指令后，没有新增实现或测试；仅接收已运行调用的结果并保存交接。所有子Agent已停止，没有活动测试session。最后一次原生对照 `data/validation/v06/loader-system-drive-r1.json` 已返回：worker_reap=confirmed、profile_deleted、source_unchanged=true，无待处理cleanup_owner。诊断进程退出0仅表示采集完成，阶段0仍false。

本轮新增/修改均保留工作区，未stage/commit/push，未安装依赖，未调用云API或读取私人配置/.env/数据库。

### 最新结果及缺项

- transport预算修补：双Python各37 passed，定向strict通过；runtime安全修补：各15 passed，3文件strict通过；Windows入口清理/启动事实修补：各28 passed，6文件strict通过。对应证据 `protocol-budget-r2-*`、`runtime-safety-r1-*`、`windows-safety-r2-*`，各自起止SHA稳定；它们是分批定向检查，不是V0.6最终全量。
- startup诊断清理mock：双Python各3 passed、2文件strict通过。loader诊断清理mock：双Python各4 passed，但2文件strict均失败，保留 `loader-cleanup-r1-*` 原始结果，暂停后未修复/补跑。错误均在新测试：第19/23行fake的events属性与基类list[dict]类型冲突，第60/73行通过诊断模块访问未显式导出的LaunchRejected；恢复后应改独立call_events属性并从launcher直接导入异常，不能扩大产品export来迎合测试。
- `diagnose_worker_loader.py` 实现本身在作者双Python定向9文件strict中通过；加入新测试的strict失败不能据此抹去。当前脚本SHA `260e71dc602d89214682cc2032c95eb6a877c5e319204d6817b62359e8822e81`；新测试SHA `ebab57ce9c454576e2105efd112cfb22e3d071698c4dd79be5696e930daa217a`。
- SYSTEMDRIVE单变量已实际执行，仍为同样7个加载/卸载/退出事件、exit0xC0000142、0帧，不能解释或修复初始化失败。报告包含Python3.12.4、Windows build26200、fixture及9个依赖源码起止SHA。未提升权限token不抵独立普通用户/干净系统验收。
- USERPROFILE、APPDATA单变量选项已写好，**未执行**；DETACHED_PROCESS对照只有设计意见，**未实现/未执行**。LPAC class46返回87原因/支持契约尚未完成本机调查；文件/注册表/网络/资源负测及双Python真实worker启动仍未通过。

### 恢复入口

1. 先读本检查点和[runtime调查](runtime-investigation.md)，确认当前没有未收敛实验；不要重跑已完成旧baseline或把历史快照当现状。
2. 将loader新测试的4条类型错误修正后，委派6 Luna执行对应双Pythonmock/strict；保留首轮失败日志。暂停指令下本次故意未做这一步。
3. 主Agent审查后，经工具明确权限流程运行新profile的USERPROFILE/APPDATA有限单变量对照；例如 `python -B scripts/diagnose_worker_loader.py --bootstrap-key user_profile --report data/validation/v06/loader-user-profile-r1.json`。不要继承全量环境或扩大产品环境白名单。若仍无解释，再实现仅诊断用的CREATE_NO_WINDOW→DETACHED_PROCESS对照，保留全部AC/Job/child/句柄限制，不放宽全局desktop ACL。
4. 解决初始化后，才运行双Python实际只读runtime/profile、注册表、其他持久写命名空间及网络/停止/资源探针，继续独立安全审查。任一未知/无界可写位置仍阻断阶段0；未过前不开发A持久授权整合/B自动规则。

受限原生程序主函数尚未执行，不能凭CreateProcess成功、Job配置查询或mock通过声称隔离成立。所有真实原型命令集中由主Agent执行；测试Agent只负责简单、机械、低风险、结果明确的固定检查。

## 进入基线

开工前产品源码+pyproject指纹为 `7b679308f34da7b8d89a77aee07b78416567a8c56844f5a3871f816df29c8bac`。此后新增execution实验模块，因此新源码不能直接借用V0.5同指纹的长测作为最终V0.6证据。历史状态保留在V0.5文档；没有把V0.5未结项变为通过。

计划和发布标准以[开发计划](../../planning/v0.6/development-plan.md)、[环境评估](../../planning/v0.6/environment-assessment.md)、[验收边界](../../planning/v0.6/acceptance-boundaries.md)为准。

## 本轮分工

- 主Agent：阶段0接口、严格JSON framing、工具迁移表、整合审查和门槛判断。
- 6.1 Sol高思考Windows Agent：ctypes-only挂起启动、AppContainer/LPAC属性、显式继承管道、Job限制查询和进程回收；`execution/windows/`及启动探针。
- 6.1 Sol高思考runtime Agent：专用双Python runtime、最小ACL、profile/TEMP/registry实际写入调查；`execution/runtime.py`及文件系统探针。
- 6 Luna高思考测试Agent：固定命令的双Python旧回归基线，随后协议/静态检查与证据机械核对。权限实验设计与归因不交给机械执行Agent。

## 当前边界

恢复后由6 Luna执行三份新增测试：双Python各66 passed、0 skipped，pytest各2.11秒。但12个输入文件的起止SHA中3个变化，标为非最终快照；3.12定向strict8文件通过，3.11捕获探针参数列表tuple长度推断不匹配，交回原作者修正。冻结前不把该批次当最终检查。

WinError203的native对照已收敛到子进程环境：同一新profile、同一静态CRT fixture下，子进程缺少LOCALAPPDATA时CreateProcess返回203；显式加入该键后真实创建成功，但恢复线程后以0xC0000142（DLL初始化失败）退出，零控制帧。调用者该键有无不改变结果；子进程取原生AC目录或KnownFolder LocalAppData基目录都仍初始化失败。因此不能把“调用者缺变量”或“路径取值错误”写成已确认根因。原始native-caller-contrast-r1/r2.json均记录host_token_elevated=false和profile_deleted；未提升权限不等于已完成独立普通用户/干净系统验收。脚本退出0只表示对照完成，stage0_passed始终false。

上述成功创建分支已取得实际AC token、package SID、零capabilities、child policy及Job/CPU设置查询证据；它们不证明fixture主函数、限制负测、磁盘/网络边界或停止指标通过。runtime-py312-resume-startup-r3得到相同AC结果；LPAC在token_size返回87，仍须核对API/平台支持。r3已profile_deleted。当前主阻断是受限进程初始化；所有实际原型命令统一由主Agent经明确工具权限流程执行，子Agent准备代码/审查，避免嵌套审批队列滞留。

独立 `diagnose_worker_loader.py` 已在未提升权限token下运行一次（loader-debug-r1.json）：保留全部AC/Job限制，只添加DEBUG_ONLY_THIS_PROCESS；事件为image、ntdll、kernel32、KernelBase加载、两次卸载及0xC0000142退出，未见初始breakpoint/异常/调试字符串，0帧。宿主当前WindowStation/Desktop名称为WinSta0/Default；仅名称不证明访问权限正确。Job回收确认、profile_deleted。诊断退出0只表示事件采集完成，不能定位具体失败DLL/底层拒绝原因或作为阶段0通过。

新增host编码预算修补：JSON渲染前累计字节预算并预检字符串字符长度，防单项合法但合计超大/控制字符转义膨胀造成先分配大JSON再拒绝。6 Luna复核双Python各37 passed（0.26/0.22秒）、定向strict2文件通过，协议与测试SHA在各自检查窗口稳定。首轮新测试因pytest将巨型参数自动放入Windows环境变量而setup/teardown失败，已仅改短参数ID，数据/断言未变；失败日志保留，r2记录另存。

独立审查指出的runtime探针修补已由原作者落盘：profile创建后立即建立finally保护（报告首次写失败也清理）、删除失败非零退出、registry SET_VALUE/CREATE_SUB_KEY/WRITE_DAC/WRITE_OWNER最小权限分测；session登记对象改为身份相等而非数据相等。Windows启动探针同类清理路径也已修补，并明确非零退出/缺native标记帧属于应用启动失败。当前尚不能据代码/单元结果判定实际Windows隔离通过。

该runtime修补由6 Luna复核：双Python各15 passed（2.54/2.33秒），runtime/探针/测试3文件strict均通过；各检查窗口起止SHA一致，证据runtime-safety-r1-*。这批是行为单元与类型检查，不是实际拒写证据。

新增独立scripts/diagnose_worker_startup.py只用于已知fixture的启动对照，不是产品后端；其报告首次写失败、profile删除失败、进程回收未确认三种失败路径由6 Luna纯mock验证，双Python各3 passed，脚本/测试定向strict双环境2文件通过。真实对照只操作新建项目profile和合成程序，不读取私人内容，不继承全量环境。

固定旧回归基线由6 Luna完成：Python3.12为1203 passed/4 skipped（149.13秒），3.11为1203 passed/4 skipped（148.87秒），退出码均0，外层600秒未超时。明确排除本轮新增原型测试，因此不是V0.6最终全量。新增protocol测试双环境各35 passed（0.24/0.18秒），定向strict在补充测试输入列表注解后双环境2文件通过。原始日志/JUnit在 `data/validation/v06/`，首次类型失败保留，未降低断言。

Windows Agent已用本机MSVC14.44.35207与SDK10.0.26100.0编译链接静态CRT合成fixture。该证据只解决native构建可用性，尚未证明其受限执行通过。runtime首轮实际实验在普通positive-control启动即出现缺DLL状态，profile已删除，未计为AC/LPAC隔离通过；完整原因和后续结果由runtime调查记录。

Windows启动、runtime和写入限制仍须真机证据。AppContainer默认profile可写是明确待验证点，LPAC不自动等于磁盘有界；用8字节合成canary验证允许/拒绝，不做磁盘耗尽。任一worker可达持久位置无强制总量限制且不能拒写时，阶段0不通过。

JSON framing模块只验证受限数据，不授予能力或证明IPC身份；session/invocation及业务状态校验属于后续supervisor契约。原型未接Core/GUI，自动规则保持未实现，不静默替换生产工具执行。

不读取私人配置/.env/用户数据库，不调用云API，不修改全局防火墙或放宽整个Conda/仓库ACL。项目专用AppContainer profile/ACL和安全探针若需越过工作区，由工具明确请求执行权限；失败保留原始错误，不换工具绕过。

## 下一步

1. 完成两条原型并对齐Launcher/runtime/framing接口。
2. 核对旧功能回归和新协议边界，进行Windows真实受限启动、权限/写入及停止探针。
3. 独立审查结果，冻结唯一后端、runtime manifest和实际限额；阶段0通过后才开发持久能力/精细授权与A整合。

本记录是进度，不是测试通过或正式交付声明。后续结果与失败原因在本目录追加；原始日志/运行时产物放Git忽略的 `data/validation/v06/`。

## 历史暂停检查点与当时恢复顺序

以下为暂停时快照，已恢复调查；其中“未执行”“尚未运行”等只描述当时状态。当前证据与后续步骤以上方“当前边界”和“下一步”为准，不据此重复旧实验。

用户明确要求暂停后，已停止新的测试、编辑实现和LOCALAPPDATA诊断；主Agent仅保存交接。所有子Agent报告工作已停止，没有活动构建/测试/native worker。两轮项目专用AppContainer profile均删除，未接入Core、未新增自动规则、未安装依赖、未提交或推送Git。

### 已保存实现

- `src/deskpal/execution/protocol.py`：4字节网络序长度+严格UTF-8 JSON，有界帧/流/深度/节点/字符串/消息数，拒绝重复字段、非有限数、越界整数、截断和EOF后输入；仅数据验证，无业务授权。
- `src/deskpal/execution/windows/{__init__,launcher,_win32}.py`：ctypes挂起启动、AppContainer/LPAC属性、Job/token/child policy核验、独立控制输出/日志和deadline回收；未确认回收时保留cleanup_owner，不假称closed。包入口 `execution/__init__.py` 同步新增。
- `src/deskpal/execution/runtime.py`：专用最小runtime及manifest、隔离搜索路径/白名单环境、仅本会话登记对象的ACL provision、全新profile创建/删除与失败清理记录。
- `scripts/probe_windows_launch.py`：静态CRT原生合成fixture的构建及启动/超时/内存/子进程/淹没/非法帧探针；`scripts/probe_worker_filesystem.py`：8字节文件/专用注册表canary及明确普通host正对照。
- 三份新增测试：`test_execution_protocol.py`、`test_execution_runtime.py`、`test_windows_execution_api.py`；工具迁移表19项已由AST核对一致（AUTOMATIC9、CONFIRM8、HIGH_RISK2）。

### 已有检查与未复核变更

1. 双Python旧基线各1203 passed/4 skipped；协议各35 passed；协议与其测试定向strict双环境2文件通过。详细日志见前文。
2. runtime单元：3.11当前10 passed；3.12最后10 passed发生在profile整体根绑定调整前，须恢复后重跑当前版本。runtime/probe双Python定向strict2文件通过，不是最终全src检查。
3. Windows adapter/probe strict4文件通过发生在最后加入显式LOCALAPPDATA诊断白名单key之前；该单行变化尚未复核。API单测21个参数展开用例尚未运行，不将存在测试文件记为通过。
4. native fixture真实编译链接通过，`data/v06-stage0/native-fixture-v1/fixture.exe`，SHA256 `4d92fe675dd1476eeb04ae7f2b948516aa695229fb827af6f5267f09161f836a`；未因此宣称受限执行成功。

### 实际阻断与保留产物

Python3.12 runtime补齐明确的zlib/ffi依赖后普通host正对照已exit0；默认/只读profile×AC/LPAC四次都在CreateProcessW返回 **winerror203**，从未运行受限worker。3.11仅静态stage及单元/strict，尚无staged解释器实际启动。所有文件/注册表拒写、未列入句柄负测、实际Job限制及停止证据仍未取得，**阶段0未通过**。

`GetAppContainerFolderPath`实际返回profile的AC目录，因此代码已收紧为校验新moniker的整个package root，而不是仅AC子树。新方案还没有取得受限执行证据。profile注册表及其他持久写命名空间仍未知，不能降低既定磁盘门槛。

原始 `runtime-py312-native-r{1,2}/filesystem-report.json` 保留失败及profile_deleted。native-r2的生成runtime仍保留本轮只读DACL，实验host/SYSTEM/package可访问，但sandbox用户读其manifest会Access denied；只影响这棵专用生成树，源码及外层报告未改ACL。后续读取/清理需经有权限的host及相应工具权限流程，不给整个项目/Conda放宽ACL。细节见[runtime调查](runtime-investigation.md)。

恢复时：先读此记录与调查、确认无遗留进程/profile；在新的专用root/profile诊断203（先native最小对照，再按需要做4key与显式LOCALAPPDATA=原生AC目录的单变量实验，五key实验本次未执行）；随后复核当前Windows/API/runtime测试和strict，再做双Python实际受限启动及权限/磁盘/网络/停止验证。任何cleanup_owner未确认回收时，保留所有权并停止后续实验，不删除或复用profile。阶段0通过前不进入持久授权/A整合/B自动行为。
