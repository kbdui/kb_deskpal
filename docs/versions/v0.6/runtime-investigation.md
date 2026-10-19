# V0.6 阶段0：runtime、profile 与持久写边界调查

日期：2026-10-17，Asia/Shanghai。状态：**08:40暂停后已恢复调查，阶段0未通过**。本文不改变V0.5候选版状态，也不声称完成V0.6A/B。当前恢复进度、分批测试结果和历史暂停点见[开发进度](development-status.md)；新的权限盲点与注册表实验提案见[静态审查](runtime-security-review.md)。

当前已完成双Python真实ordinary AC对照：DETACHED_PROCESS、明确五键环境、V2精确SEH合成fixture下，3.12.4与3.11.17的native/Python均能执行并回传结果。原初始化阻断已在该候选方式下解除；严格句柄策略未关闭。已测目录在文件DACL施加后拒写，但profile注册表仍可写，**阶段0明确未通过**。所有实际原型命令仍由主Agent集中执行。下文旧轮次/方案中的“未执行”“未知”仅描述相应历史时点，最新结果见下一节。

## 当前真实证据：canonical-r1

报告：`data/validation/v06/runtime-py312-canonical-r1/filesystem-report.json` 和对应 `runtime-py311-canonical-r1`。两轮各包含host正对照，以及默认/只读profile下的native/Python四个受限worker；全部exit0，四worker各1帧；最终profile_deleted。明确选择ordinary，LPAC未选且不作fallback。

默认profile的AC、Temp、INetCache、INetCookies允许8字节合成文件及WD/WO访问；文件DACL施加后的已测目录创建和WD/WO均被拒。注册表默认/文件只读两阶段的SET_VALUE、CREATE_SUB_KEY、WRITE_DAC、WRITE_OWNER请求均成功，专用随机子键创建并清理成功。文件DACL不能解决注册表容量风险；当前没有registry quota或拒写边界。

worker实际五键环境中，LOCALAPPDATA/TEMP/TMP出现系统追加的 `AC/Packages/<moniker>/AC` 嵌套映射。不能只按host传入路径推断实际存储位置；后续API路径定位不得向专用树外真实位置写canary。已测文件拒写不覆盖seeded文件、ADS、子对象继承、父DELETE_CHILD或所有缓存命名空间。Pythonctypes无效句柄异常保留未决；nativeV2精确捕获只补充该单canary观测，不等于完整继承表枚举。

下一步是独立的只读registry A/B/A对象绑定及有限资源/停止探针；registry绑定前不修改KEY DACL。单元/类型联合检查双Python各150项及16文件strict通过，但不替代上述实机边界或最终全量。

## 边界与实验对象

实现限定在 `src/deskpal/execution/runtime.py`、`scripts/probe_worker_filesystem.py` 和对应单元测试。Windows启动/Job/IPC由独立launcher提供；本模块不另起一套Win32启动。未安装依赖、下载模型、调用真实API或读取用户配置/密钥。

每轮仅新建项目专用runtime及随机 `deskpal.v06.<random>` profile；拒绝接管已有profile。ACL函数只接受本次host会话实际stage/创建并登记的对象，runtime重新核验manifest，profile重新核验原生查询路径；拒绝任意路径或伪造DTO作为所有权证明。真实profile/ACL实验经`exec_command require_escalated`执行。

复制runtime包含明确选择的解释器、CPython DLL、标准库源文件和必要native模块/运行库，不复制site-packages、Conda配置、仓库或模型。`python311._pth`/`python312._pth`仅列runtime根和`Lib`；启动使用`-I -S -B -X utf8`，当前环境为显式SystemRoot/WINDIR/TEMP/TMP/LOCALAPPDATA五键（历史四键失败对照保留）。manifest保存路径、大小和SHA-256，默认`acl_verified=false`、`isolated_startup_verified=false`；文件存在不代表隔离通过。

## 已观察结果

| 轮次 | 实际结果 | 边界 |
| --- | --- | --- |
| 双Python静态stage首轮 | 发现Conda将`_struct`编入CPython，未独立提供`_struct.pyd`；修正为允许builtin，`_ctypes.pyd`仍必需 | 静态stage不是执行证据 |
| Python3.12 native-r1 | 显式普通host启动正对照返回`0xC0000135`，未进入worker；专用profile成功删除 | 不算AppContainer拒绝或安全通过 |
| DLL依赖核对 | MSVC dumpbin实测两个CPython DLL均依赖`zlib.dll`，`_ctypes.pyd`依赖`ffi.dll`；stage补入明确本地DLL | 不是任意第三方包/完整Python分发兼容证明 |
| Python3.12 native-r2 | staged Python3.12.4普通host正对照exit0；sys.path仅runtime根/Lib，isolated=1、no_site=1、禁止pyc、site未加载，环境仅4个白名单key | 普通host仅为显式正对照，不作失败fallback |
| Python3.12 native-r2受限启动 | 默认profile/只读profile × AC/LPAC四次均在CreateProcess阶段返回winerror203，未执行worker；profile成功删除 | 尚无AC/LPAC实际文件/注册表拒绝证据，启动问题需调查 |

原始报告在Git忽略目录 `data/validation/v06/runtime-py312-native-r{1,2}/filesystem-report.json`；两轮均`stage0_passed=false`。native-r2原型曾将执行完成状态记作需要coverage审查，但四个受限启动均失败，不能计作完成隔离实验；脚本已调整为任何启动错误返回blocked和非零退出。

原生`GetAppContainerFolderPath`实测返回 `.../Packages/<本轮moniker>/AC`。因此只修改AC目录不够：当前探针严格校验`AC.parent.name == moniker`，仅覆盖该新建moniker根、AC/Temp、同级LocalState/LocalCache/RoamingState/TempState及AC缓存目录；绝不修改Packages父目录或其他应用profile。每个文件写探针最多创建8字节随机命名canary，并尝试删除；另仅请求WRITE_DAC/WRITE_OWNER检查权限，不改变worker自身ACL。

## ACL方案与尚未证明的风险

拟采用protected DACL：host/SYSTEM保留完整权限，package仅RX且显式拒绝文件写/追加/删除/改属性/WRITE_DAC/WRITE_OWNER，OWNER RIGHTS ACE保留READ_CONTROL以约束隐式owner WRITE_DAC。不降低系统目录完整性标签、不授予ALL APPLICATION PACKAGES、不放宽整个Conda/项目。方案必须由实际worker探针验证；生成SDDL不构成有效性证明。

注册表单独调用`GetAppContainerRegistryLocation`，仅尝试新随机专用子键的8字节值，并记录根key写/WRITE_DAC/WRITE_OWNER访问；不枚举或读取现有值。**文件DACL不代表注册表DACL生效。** canonical-r1已证实注册表可写；后续必须先可靠绑定本轮专用key，再使用KEY_*访问mask和独立安全描述符，保留正对照并确认OWNER RIGHTS，不能直接套文件mask。

任何worker可达持久命名空间若仍无界可写，阶段0即阻断。禁止把Job内存额度、目录轮询、退出后清理或host staging额度称为磁盘quota。有限canary覆盖不能证明已经枚举所有OS写入口；即便已知路径都拒绝，也须保留coverage审查和实际支持范围。

清理时必须先确认worker/Job已退出。若launcher报告cleanup_owner，先重试回收；仍无法确认时停止后续实验、保留profile/句柄所有权证据，禁止删除或复用profile。创建profile后发生异常也必须检查Delete HRESULT；失败明确记录可能残留，不伪报清理成功。

## 官方依据

Microsoft的[AppContainer启动说明](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)说明profile/TEMP及LPAC额外限制；本文仍要求本机实际验证，不将默认profile误作无写权。注册表定位使用[GetAppContainerRegistryLocation](https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-getappcontainerregistrylocation)。Python搜索路径按[3.12 `_pth`规则](https://docs.python.org/3.12/library/sys_path_init.html)设置；该规则只约束Python初始化，不阻止恶意worker主动调用OS。

## 尚待完成

CreateProcess winerror203已缩小到本机child环境缺LOCALAPPDATA的关联（下文恢复对照），尚不能据此声称完整启动已修复；当前主要阻断为`0xC0000142`初始化失败及LPAC token查询87。仍待双Python真实AC/LPAC执行到探针、实际profile/注册表/其他写命名空间拒绝与权限修改负测、独立标准用户/干净系统复核。阶段0保持未通过，自动能力保持关闭。

## 历史暂停检查点（以下为恢复前快照）

用户要求保存并暂停后，未启动任何新native对照或安装。LOCALAPPDATA单变量对照仅讨论方案；Windows launcher一侧已接受该显式诊断key，但本runtime脚本从未传递或执行五key实验。

- 当前修改文件仅：`src/deskpal/execution/runtime.py`、`scripts/probe_worker_filesystem.py`、`tests/unit/test_execution_runtime.py`、本文。生成的runtime/manifest/报告在Git忽略的`data/validation/v06/`；没有Git提交。
- Python3.12 runtime单元先后9、10项通过，后一次10项运行早于profile整体根绑定调整；Python3.11在当前runtime调整后10项通过。双Python对runtime及probe两个文件的strict检查均通过。不是全量回归、最终冻结构建或隔离验收证明。
- Python3.11仅完成静态stage及单元/strict，尚未运行staged解释器普通启动或AC/LPAC。Python3.12的普通启动正对照仅native-r2通过，所有AC/LPAC启动均winerror203。
- 本Agent所有exec/native session已结束。native-r1和native-r2的profile均Delete HRESULT成功，报告为`profile_deleted`；普通正对照进程均已返回，受限worker未跨过CreateProcess，没有已知本Agent遗留执行进程或profile。未创建注册表canary（受限worker未运行，host API返回失败）。
- native-r2的runtime保留本轮只读DACL，含实验host用户/SYSTEM/package，不含sandbox用户。因此暂停时读取其`runtime/runtime-manifest.json`实际返回Access denied。影响仅这个专用生成runtime树；项目源码/报告目录未被改ACL。未来读取/清理该树应通过拥有权限的host并按权限流程执行，不能给整个项目放宽ACL。native-r1在普通启动失败前未进行runtime DACL修改。

恢复时先读本报告及native-r2原始报告，确认Windows launcher当前接口/cleanup_owner协议。随后仅在重新获得继续工作的用户授权后，以新的专用root/profile比较四key与显式LOCALAPPDATA=原生AC目录；若仍失败，先native fixture缩小CreateProcess问题。不要复用已删除profile，不把winerror203当作拒绝文件/注册表越权的通过证据。当前脚本默认任何启动错误返回blocked；只有确认Job/进程回收后才删除profile，未知回收时保留并停止。

## 恢复后的启动对照与修补

用户恢复授权后，使用全新root `data/validation/v06/runtime-py312-resume-startup-r3/` 和新随机profile，以`--startup-only --native-fixture`执行一次真实对照；原始`filesystem-report.json`保留全部结果。startup-only不执行文件/注册表写canary。native fixture作为显式输入复制并纳入runtime manifest，而不是未记录的额外可执行文件。

| 同一profile/runtime内的对照 | native与Python3.12结果 |
| --- | --- |
| 普通AC，child四key | 均CreateProcess返回203 |
| 普通AC，child显式增加LOCALAPPDATA=原生AC路径 | 均通过创建及token/Job核验，随后exit `0xC0000142`、无frame/日志，未运行探针 |
| LPAC，child四key | 均CreateProcess返回203 |
| LPAC，child五key | 均在LPAC token查询报`token_size (winerror=87)`；未运行worker，交Windows adapter调查 |

上述profile成功删除，所有调用已收敛，没有持有未确认cleanup_owner。四key/五key改变了创建结果，但不足以独立推断所有根因。主Agent另行获得的caller absent/KnownFolder对照显示本机203随**child block**缺LOCALAPPDATA出现；本模块没有修改共享host环境或增加其他child变量。DLL初始化失败仍阻断实际权限探针。Python3.11尚无真实staged启动证据。

恢复审查发现并修补了三项未接入生产的原型问题：

- profile创建后立刻进入finally保护，再执行asdict/写首次报告；首次报告失败也清理。删除失败返回非零并明确可能残留，不能沿用此前待返回的成功码。纯mock测试覆盖首次写失败及同时删除失败，不执行OS安全操作。
- ACL所有权从dataclass值相等改为本会话对象identity；复制/replace出同值manifest/profile不能授权操作，防止旧对象跨新一轮同名对象使用。新增拒绝复制DTO测试。
- 注册表根请求拆为独立最小 `KEY_SET_VALUE`、`KEY_CREATE_SUB_KEY`、`WRITE_DAC`、`WRITE_OWNER`；不再用KEY_READ|KEY_WRITE组合拒绝冒充无写权。只有全新随机project子键才尝试8字节值，子键申请KEY_SET_VALUE，不申请多余READ/NOTIFY权限。尚未运行受限worker，因此没有注册表拒写结论。

probe另明确非零子进程退出或缺frame属于应用启动失败；即便CreateProcess成功也不能称探针已通过。runtime单元曾双Python各16项通过（2.68/2.54秒），随后移除一项仅检查源码文本的重复测试。Luna已以`runtime-safety-r1`复核当前冻结版本：双Python各15项行为回归通过、3文件strict通过，起止SHA一致。完整Windows启动、权限、磁盘与最终回归均未完成；这些单元/静态结果不计为隔离通过。

后续真实原型命令由主Agent集中通过`require_escalated`执行，避免多条权限队列并行；本模块继续负责runtime/registry/文件写边界，WindowsAgent负责`0xC0000142`及LPAC查询归因。不得为让启动通过而继承完整env或放宽全局目录/安全策略。阶段0仍为false。
