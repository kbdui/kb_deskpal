# V0.6 Windows 沙箱复用与替换评估

日期：2026-10-19。范围：源码、测试、许可证及微软官方安全语义调研。本文不是 Windows 安全验收记录。

后续状态：用户已要求并完成删除 sandboxrs-windows、wincage、WABOX 的下载源码及归档；下文是清理前的历史调研记录，不表示本地源码仍存在。用户随后允许评估另装 Docker／WSL2，新的路线建议见 [执行环境架构重评](execution-environment-architecture-review-2026-10-19.md)；本文“优先比较 MXC”的建议不再作为当前首选路线。原失败证据及微软源码缓存保留。

## 结论与决策

**应停止扩大自研底层机制的范围，优先比较微软 MXC 原生后端与现有启动器；目前不直接整体采用三个候选项目，也不宣称 MXC 已满足注册表核心要求。** 这是基于源码的技术路线建议，不是已经完成的迁移决定。

所审源码和测试均未建立本项目要求的完整证据链：同一轮合成对象中，Worker 确实不能写注册表，Host 独立核验实际对象，管理权限保持可用，权限安全恢复，进程及 profile 清理均得到确认。各项目的文件 ACL 撤销、profile 删除或 registry tracking 不能替代这条证据链。

现有代码也不应因为已经投入而自动保留。其 ctypes 资源所有权和部分失败路径存在明确补强点；如果受审、固定版本的 MXC 组件能通过同一套安全契约及真实 Windows 测试，应考虑替换启动、Job 和资源所有权组件。上层 IPC、策略、Broker、持久 attempt/Uncertain 和撤销语义仍由本项目掌握。

本轮没有修改产品代码、诊断代码、测试预期或权限策略；没有执行候选项目、编译、安装依赖或运行原生安全实验。只下载并阅读公共源码、只读核对现有记录并保存本报告。旧失败 profile/key/runtime 未访问。原开发目标仍暂停，阶段0仍未通过，禁止正式 A/B 整合。

## 证据范围、固定版本与许可证

| 对象 | 本轮审阅版本 | 实际许可证与依赖 | 成熟度及适用范围判断 |
| --- | --- | --- | --- |
| sandboxrs-windows | `81e14e5567fab1b88236e0b89c6741e8aa5f8f58`，Cargo 0.1.1 | 仓库有 MIT LICENSE；Rust 1.90，windows-sys 0.59、rappct 0.13.3、FlatBuffers 等 | 有原生攻击探针、Windows CI 和作者保存的评估结果；仍为早期库，部分关键 profile/ACL 实现委托给 rappct，不能只审本仓库就覆盖全部信任链 |
| wincage | `6f76360cab2d4ba503554474f264dc69932ed781`，0.1.0 | 仓库有 MIT LICENSE；Python >=3.10；另有 C++ sandbox_host.exe，构建涉及 MinGW/MSYS2 | 有真实 ACL、reparse、Job 测试脚本，但所审 CI 只编译 native/probe 及 py_compile；未见 CI 执行这些真实 Python 实验 |
| WABOX | `da1237c583c01c5e2dca8bb5c10bcdbc0bbbe73b`，0.1.0 | package.json 声明 MIT，但完整文件清单未发现 LICENSE/COPYING/NOTICE 正文；Node >=18；固定 @microsoft/mxc-sdk 0.7.0 | TypeScript 产品包装层；实际底层来自 MXC；集成测试以启动 smoke 为主，不能当作独立沙箱内核 |
| Microsoft MXC | 抽样固定 `4a941b0b913c39d91a9fb1ad1824c537a896eef5`；该快照 Node SDK 标为 0.8.0 | 实读 MIT LICENSE.md；原生 Rust/Windows API、SDK/CLI、按功能涉及额外系统契约 | 比三个包装项目更值得评估原生组件；仍有平台分层、实验契约与明确未完成清理路径。此快照不代表已证明最新，也不是 WABOX 固定的 0.7.0 |
| Microsoft SandboxSecurityTools | `8102bfc8dc3e244ec08e8204bd7d989e26a016e4` | 实读 MIT LICENSE；C++ Windows 示例 | 用于核对 AppContainer/LPAC API 用法；不是符合本项目生命周期、限额与失败保留契约的生产 Supervisor |

许可证判断仅覆盖实际读到的文件，不是完整的传递依赖许可证审计。移植 MIT 代码需保留相应版权及许可文本；WABOX 的 MIT 声明与缺失正文应在源码移植或分发前澄清，不能简单写成“未授权”，也不能假定所有依赖均已审完。

公共源码下载保存在 `data/validation/v06/windows-sandbox-review-20261019/`。sandboxrs 位于 `source-r1/`，其余成功下载位于 `source-r3/`；每个来源有清单/哈希。后者 ZIP comment 与固定 commit 对应。下载代码均未执行。API 限流失败的 `source-r2` 及下载失败记录保留，不算有效源码证据。

MXC 的 `filesystem_dacl.rs`、`run_dacl_tests.ps1` 及补充契约文件从已固定归档追加读取，记录于 `_additional-source-manifest.json`、`_extra-contract-manifest.json`。第一次补读错误查找根目录 Cargo.toml 留下空文件，清单已注明失败；分析使用真正的 `src/Cargo.toml`，不使用该空文件。

## 六项能力对照

| 维度 | 本项目当前原型 | sandboxrs-windows | wincage | WABOX / 所审 MXC 快照 |
| --- | --- | --- | --- | --- |
| Worker 创建、身份和权限 | 挂起创建，先入 Job，回读 Token/Job/子进程策略，再单次 Resume；capability0、句柄/环境白名单。已有 LPAC 开关，但其真实启动令牌查询曾报87，不能称为验收通过 | 自行实现 suspended + SECURITY_CAPABILITIES + HANDLE_LIST + Job 后 Resume；profile/ACL 依赖 rappct。未见等价的整套 Host 独立回读 | 原生 helper + cap0 + handle list + suspended/Job；AssignJob 特定失败会重建为 breakaway 启动，不能直接继承为本项目默认策略 | WABOX 调 SDK/CLI；MXC 根据 OS 能力选 BaseContainer/PSEC、过渡 CPIS 或 AC+DACL。默认 fallback 不代表本项目允许换安全契约 |
| 文件/网络及真实拒绝 | 有部分只读和停止证据；完整写区、seed/ADS/reparse/DELETE_CHILD 未闭环。旧网络超时/UDP未到达/bind成功不足以验收 | RW/RX package SID ACE，Hidden 不授权；baseline 尝试递归授权系统路径，未见对等撤销；测试有文件攻击，但未见实际网络/注册表拒绝覆盖 | package SID 文件 ACL，遍历跳过 reparse，带 pending ADS 标记；有 icacls/reparse 实验。cap0 不等于全面网络拒绝证据 | WABOX 映射 fs/outbound 配置。MXC 有 OS 分层与真实 curl 拒绝测试，但某脚本把任意 curl 失败当拒绝、后端不符可 SKIP；缺少本项目要求的明确数据面对照 |
| 注册表/Host/管理句柄/清理 | Package SID deny 未挡住十探针；User SID deny 挡住但 Host SetSecurityInfo5。RC|WD 实授0x60000已观测，根因未定。新 MIC 方案只有 Token 部分证据 | 未见匹配的 registry write-denial/Host-held-HKEY/恢复测试；profile delete 是 best effort | profile 设计为稳定复用，析构不删；文件 ACE 撤销不等于 registry 权限恢复；无匹配测试 | MXC 有 Host registry tracking，不能当作 Worker registry 只读。所审 file DACL 恢复不是 HKEY 恢复；BaseContainer 清理入口明确未完成；无本项目完整四联证据 |
| 超时/停止/子进程/CPU/内存 | 配置进程及 Job 总量各256MiB、CPU20%、active process1；有有界输出与回收 deadline。生产 outer Job 总内存、实际CPU饱和等尚未独立验收 | KILL_ON_JOB_CLOSE、进程内存、可选进程数；所审 Job 未设总内存/CPU。wait 才开始计时；终止后无限 wait，输出无界 | KILL_ON_CLOSE、CPU、进程内存；未见等价总内存/active1合同。父死亡时输出线程 join 可能阻塞 Job 析构；输出无界 | WABOX kill CLI 后立即拒绝 Promise，无“Job空”证据，stdout/stderr 累积无界。MXC 的 UiJobObject 有 terminate + 有界 ActiveProcesses0 核验，可借鉴；未据此认定全部资源限额满足 |
| 撤权/异常/崩溃/现场 | 已有失败锁存、隔离实验现场封存约定；尚未实现完整产品级 attempt/Uncertain、撤销/忘记屏障与所有崩溃矩阵 | grant 递归子项错误可被忽略；Drop 删除 profile 忽略错误。未见等价 durable 恢复事务 | pending 标记能提示中断，但 revoke 不恢复完整原状态；撤销默认关闭且异常可日志化后继续 cleanup | WABOX destroy 主要保存 session 状态/日志。MXC file DACL 有改前持久化、孤儿恢复、坏记录隔离；逐项失败仍可能仅为 warnings，需更严格适配；自动恢复不得碰本项目封存旧对象 |
| 版本/依赖/迁移 | Python ctypes 原型，产品仍0.5.0rc1；上层尚未整合，替换窗口尚在 | 引入 Rust+rappct/FlatBuffers，需重写限额、输出、回收、授权恢复和独立证据适配 | Python 外壳较近，但新增 C++ helper 分发/构建及事件协议；仍需修资源/cleanup 不确定性 | WABOX 再引入 Node 包装层而没有补足核心证明。直接评估 MXC 更合理，但需固定 OS/tier/CLI ABI、限制依赖与机器准备、确认许可证和退出语义 |

“未见”限于上述固定源码、测试和所列路径，不证明项目所有版本都没有该能力；同样不能将 README 的声明或作者结果转成我们的真实执行结论。

## 源码中影响采用决策的具体发现

### sandboxrs：攻击测试值得借鉴，整体替换会退化当前契约

- `appcontainer/compile.rs` 对显式路径授 package SID 的 FILE_ALL_ACCESS 或 RX，baseline 尝试授权 SystemRoot、ProgramFiles 等路径；递归过程中子项失败可被吞掉。注释所称 fail closed 不足以代替实际错误传播。不要在本机执行这类广域宿主 ACL 变更。
- `child.rs` 使用 Vec/read_to_end 收集输出，并有无限等待；timeout 从 wait 调用时起算。若直接替换，输出预算及从启动时计算的截止时间都需重做。
- `job.rs` 的进程内存限制不能冒充 Job 总内存限制；所审路径未找到 CPU hard cap。
- 有 ignore 的 Windows 原生测试并不等于作者未运行：Windows CI 明确执行 --ignored；安全评估 CI 也有标准用户实验。modern 路径允许 unsupported/继续失败，应分别解释。
- 作者保存的 Windows Server 2025 标准用户报告记录43 pass、1 unsupported，并列出兼容性问题；这是可追溯的作者材料，不是本轮重跑，更不是注册表核心验收。

可借鉴其 native 正对照、攻击后状态检查和 pass/escape/error/unsupported 分类；不得把 unsupported、探针未启动或 native control 本身失败算安全通过。

源码：[授权](https://github.com/TarunKurella/sandboxrs-windows/blob/81e14e5567fab1b88236e0b89c6741e8aa5f8f58/src/backend/appcontainer/compile.rs)、[等待与输出](https://github.com/TarunKurella/sandboxrs-windows/blob/81e14e5567fab1b88236e0b89c6741e8aa5f8f58/src/child.rs)、[Job](https://github.com/TarunKurella/sandboxrs-windows/blob/81e14e5567fab1b88236e0b89c6741e8aa5f8f58/src/job.rs)、[评估 CI](https://github.com/TarunKurella/sandboxrs-windows/blob/81e14e5567fab1b88236e0b89c6741e8aa5f8f58/.github/workflows/security-evals.yml)。

### wincage：helper 分离有价值，cleanup 名称不等于安全恢复

`container.cpp` 明确保持稳定 profile，文件 revoke 使用 REVOKE_ACCESS，而非恢复此前全部安全描述符；已有同 SID 授权与本次授权的区别需要特别处理。Python 默认不撤权，开启后部分异常也只记录日志。pending ADS 是中断提示，不能证明所有对象 ACL 已恢复，也不是不可替换的对象身份绑定。

`main.cpp` 约800行附近明确注明：父进程死亡时目标可能仍持管道写端，输出线程 join 会阻塞，尚未解决。由于 Job 的后续销毁依赖这一流程，不能仅凭 watchdog 和 KILL_ON_JOB_CLOSE 判定 Host 死亡回收可靠。

独立 native helper 可以隔离 FFI 崩溃、缩小 Python 的原生所有权范围，但它仍是同一用户的进程，不会自动取得独立管理安全主体。

源码：[容器/ACL](https://github.com/rymorrisj/wincage/blob/6f76360cab2d4ba503554474f264dc69932ed781/wincage/src/container.cpp)、[父死亡与 join](https://github.com/rymorrisj/wincage/blob/6f76360cab2d4ba503554474f264dc69932ed781/wincage/src/main.cpp#L800)、[Python 撤权](https://github.com/rymorrisj/wincage/blob/6f76360cab2d4ba503554474f264dc69932ed781/wincage/sandbox.py)、[CI](https://github.com/rymorrisj/wincage/blob/6f76360cab2d4ba503554474f264dc69932ed781/.github/workflows/ci.yml)。

### WABOX：应评估其上游，而不是多加一层包装

实际适配器启动 MXC CLI；外层 timeout 调 kill 后立即 reject，不能代表 Job 已空或权限恢复完成。destroy 保存日志/状态，不能替代执行撤销屏障。集成测试主要是 echo/Node 启动及 destroy 日志，没有证明恶意 Worker 的拒绝与 Host 恢复。

setup 脚本要求管理员并准备 C/D 盘和 null device，可选 SYSTEM 登录任务。这类机器准备超出本项目当前授权边界；不能为了试用而执行。其 MIT package 声明与仓库许可正文缺失也需澄清。

源码：[依赖与许可声明](https://github.com/Vinayak-RZ/WABOX/blob/da1237c583c01c5e2dca8bb5c10bcdbc0bbbe73b/package.json)、[原生适配](https://github.com/Vinayak-RZ/WABOX/blob/da1237c583c01c5e2dca8bb5c10bcdbc0bbbe73b/src/infrastructure/mxc-adapter.ts)、[集成测试](https://github.com/Vinayak-RZ/WABOX/blob/da1237c583c01c5e2dca8bb5c10bcdbc0bbbe73b/tests/integration/sandbox.test.ts)、[机器准备](https://github.com/Vinayak-RZ/WABOX/blob/da1237c583c01c5e2dca8bb5c10bcdbc0bbbe73b/scripts/setup-mxc-host.ps1)。

### Microsoft MXC：最值得认真评估的底层候选，仍有硬缺口

file DACL 管理器在 apply 前持久化对应 SID 的原有显式 ACE，使用路径锁、原子记录及 PID/创建时间等信息处理孤儿状态，恢复失败项保留。这比直接 grant/revoke 更适合借鉴。不过它面向 SE_FILE_OBJECT，并非注册表 HKEY；路径锁也不能等同于持有对象句柄及不可变文件身份。逐路径恢复失败可通过 warning 暴露，适配时必须读取剩余项/警告并给出严格失败或未知结果，不能只看外层 Result::Ok。

其测试含临时文件 ACL 往返、重叠授权和合成 dead PID 恢复，但后者不是完整真实 Host 崩溃实验。网络测试确实调用 curl，不只是配置断言；问题是任意非零退出可被记为拒绝，不能排除网络本身不可用等混淆。

所审 BaseContainer 的 `run_sandbox_cleanup` 明确是 stub：因未跟踪子进程而暂不执行 profile/BFS/tracking 清理。另一个 tracking cleanup helper 中的 best effort 删除与跟踪记录删除，也不是本项目的失败保留契约。这些结论限定于审阅快照和具体路径，不外推到 WABOX 所用0.7.0或未来版本。

按该快照版本文档，受支持下限为 Windows 11 build26100；T1 还依赖启用的 PSEC/CPIS 运行时契约，T2 BFS 默认关闭且有25H2挂起风险，T3 修改宿主文件 DACL。本机既有记录 build26200 不能据版本号就宣称具备 T1；本轮没有做实际能力探测。不能把高层 filesystem policy 在各 tier 上视为同样的原子性与安全强度。

源码：[DACL 管理器与测试](https://github.com/microsoft/mxc/blob/4a941b0b913c39d91a9fb1ad1824c537a896eef5/src/core/wxc_common/src/filesystem_dacl.rs)、[BaseContainer 清理 stub](https://github.com/microsoft/mxc/blob/4a941b0b913c39d91a9fb1ad1824c537a896eef5/src/backends/appcontainer/common/src/base_container_runner.rs#L417)、[Job 生命周期](https://github.com/microsoft/mxc/blob/4a941b0b913c39d91a9fb1ad1824c537a896eef5/src/backends/appcontainer/common/src/job_object.rs)、[OS/tier 条件](https://github.com/microsoft/mxc/blob/4a941b0b913c39d91a9fb1ad1824c537a896eef5/docs/process-container/os-version-support.md)、[网络测试配置](https://github.com/microsoft/mxc/blob/4a941b0b913c39d91a9fb1ad1824c537a896eef5/tests/configs/base_container_network_deny.json)。

## 对注册表矛盾的判断

1. **未找到已被候选源码/测试证实的现成解法。** Package SID 是区分应用的安全主体，但不是独立 Windows 用户；三个候选的普通 AC 路径均不能因此天然区分 Host 与 Worker 的 User SID deny。它们常见的设计是向 package SID 授予选定文件权限，而不是证明自身 profile registry hive 完全不可写。
2. **当前结果不宜简化为“Host 没有 WRITE_DAC”。** 已有证据包含保留管理 HKEY 的实际 GrantedAccess0x60000（READ_CONTROL|WRITE_DAC）；普通无子项 leaf 及 protected-first 变体仍复现5。继续只重复同一矩阵收益很低，但现有记录仍不足以定位 Windows 内部哪项检查失败。
3. **文件 ACL 日志恢复不能直接解决 HKEY 恢复。** Get/SetNamedSecurityInfo(SE_FILE_OBJECT)、profile 删除和 Host registry tracking 都不是本项目的独立根绑定、管理句柄权限与注册表还原证明。
4. **LPAC 不是尚未实现的新按钮。** 本项目已有开关/属性与 Token readback；失败87仍需处理。官方说明 LPAC 对公共资源要求更明确的能力，不能据此免测自己的 profile hive。候选中有 leastPrivilege 字段也不等于通过了本项目实验。
5. **MIC 仍是待验证的最小路径。** 已有真实证据仅支持降至 Untrusted 后选定两种自身 Token 提升尝试失败；未证明 registry namespace 不变、canary 未变、Host 安全恢复及清理闭环。不能提前判定成功，也无需立即放弃 AppContainer。
6. **独立可信管理主体是后备架构，不是本轮实施项。** 若所有同用户可部署方案均无法通过核心契约，再评估独立服务/账户或更强隔离；届时必须增加 IPC 授权、服务保护、安装/升级和崩溃恢复审计，而不是只换一个执行账户。

官方核对：[AppContainer 双主体及 LPAC](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)、[SetSecurityInfo 的对象类型与继承行为](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-setsecurityinfo)、[CreateRestrictedToken](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-createrestrictedtoken)。

SetSecurityInfo 改 DACL 与改 Owner 所需权限不同，继承传播也可能影响结果；错误5和根快照未变不能独立定位失败阶段。RegLoadAppKey 的安全模型基于 hive 文件，应只在确认实际采用该模型时分析，不能把这一特殊规则直接认定为当前根因。[RegLoadAppKey 官方说明](https://learn.microsoft.com/en-us/windows/win32/api/winreg/nf-winreg-regloadappkeyw)。

微软旧 Experimental_CreateProcessInSandbox 是实验契约，文档中的继承句柄限制意味着现有管道协议未必可以原样迁移；新 PSEC 也须按具体版本另审，不能混作同一 ABI。[官方接口约束](https://learn.microsoft.com/en-us/windows/win32/secauthz/createprocessinsandbox)。

SandboxSecurityTools 的 LaunchAppContainer 示例有可参考的 LPAC/API 用法，但其 CLI 会写真实 HKCU Console 设置，没有本项目完整的 Job/预算/恢复管线。因此只作语义参考，不运行或整体复制。[示例实际源码](https://github.com/microsoft/SandboxSecurityTools/blob/8102bfc8dc3e244ec08e8204bd7d989e26a016e4/LaunchAppContainer/LaunchAppContainer/LaunchAppContainer.cpp#L429)。

## 复用分类与建议路线

| 分类 | 建议 | 必须保留的限制 |
| --- | --- | --- |
| 可直接复用 | 固定版本源码和 MIT 许可文本作为审阅基线；采用已有 Windows API 的正式契约、错误含义及 Job accounting 字段；复用不含机器副作用的攻击用例思路/结果分类 | **没有一个已审沙箱组件可直接放进生产并宣布合格**；实际代码引入仍需固定依赖和本项目验证 |
| 值得局部移植 | MXC 的 apply 前持久化、原 ACE 状态、恢复失败保留、损坏记录隔离；有界等待 Job ActiveProcesses0；sandboxrs 的 native 对照/后状态检查；wincage 的独立 helper/watchdog 所有权划分 | 必须适配本项目一次性清理、失败锁存与封存约定，不导入开机自动扫描修复旧对象；路径恢复不替代对象身份，file DACL 不泛化到 registry |
| 借鉴而不直接采用 | WABOX 的会话/动作分层；wincage 的 pending 标记及稳定 profile 设计；微软示例的 LPAC 属性设置 | 会话日志不等于 durable attempt；稳定 profile 不适合默认每轮全新对象；示例的真实用户注册表修改不得跟随 |
| 应继续修补或由替换消除 | 现有 NativeWorker/PendingLaunchCleanup 忽略 CloseHandle 返回后标记已关闭；CreatePipe 输出所有权登记顺序；LPAC token 查询87；独立 binding 的 namespace 后验；未完成 MIC driver | 不盲目重试失败 numeric handle，不把 API 失败后的输出当空；换成 Rust/RAII 也必须定义关闭失败/未知证据，不假定语言自动解决安全合同 |
| 值得整体替换的组件 | 优先评估固定 MXC 原生启动/Job/资源所有权组件，外加薄 Rust/C ABI 或受控 CLI 适配；以同一验收契约与现有 ctypes 比较 | 不整体迁入 WABOX；不接受未声明 tier fallback；不缺失进程+总内存、CPU、active1、句柄白名单、身份回读、输出/时间预算及严格 cleanup 结果 |

这些类别不是为了维持现有实现：如果 MXC 适配后的 native 组件通过全部核心门槛且失败语义更清晰，应替换现有底层；若不满足，则移植经过验证的机制并修补现有最小后端。不要同时长期维护两个含大量手写 ACL 逻辑的生产后端。

推荐下一阶段按如下顺序决策，以下是计划，不是本轮已执行工作：

1. 冻结共同的安全合同与 backend 能力清单，把“已配置”“作者测试通过”“本机实际通过”“最终安全验收”分开记录。先确定支持 Windows 范围、实际可用 tier、句柄/IPC 兼容性、无管理员机器准备条件；不符合者不进入侵入性试验。
2. 对 MXC 固定快照做最小适配设计，保留现有预算与独立证据通路，评估替换 native owner 的工作量。先解决契约差异，再决定是否编译运行；不先执行 WABOX 的系统准备脚本，也不自动下载可执行依赖。
3. 以现有 fresh-profile MIC 最小实验作为注册表契约的共同基准。候选后端须使用相同的实际对象绑定与观察标准，不能改成无关普通子键就声称解决自身 hive 问题。只有同轮核心四项均通过，才值得扩大文件、网络和稳定性覆盖。
4. 常规构建/mock/strict/真实执行及证据采集继续交6 Luna；父 Agent 审安全方案、冲突证据、权限策略和最终验收。任何安全边界失效或无法解释的现象保留原始现场并升级，不改预期、不跳测试、不用更多重试冲淡失败。

## 采用候选后端前的真实 Windows 门槛

注册表门槛必须同时成立：全新随机合成 profile/数据；独立观察者绑定本轮真实根对象，管理 HKEY 不继承、不传 Worker；记录实际 token、Package SID、SD/label 与管理句柄 GrantedAccess；Low 正对照证明操作有效，受限 Worker 通过新开句柄执行实际 create/open-write/delete 等负对照；Host 分别核验8字节 canary、SD 和 attempt/disposable 名字的不存在性。不能只信 Worker 返回 AccessDenied。

停止后须确认根进程退出和 Job ActiveProcesses0，再验证权限恢复与单次 profile 清理。若方案改了权限，记录原描述符、每一步实际恢复结果与最终状态；若声称无需修改，仍须验证状态完整性和安全删除。失败/未知保持锁存并保留本轮现场，禁止自动重试删除，禁止读取或修复旧封存对象。管理权限失败阶段必须单独记录，不能以 profile 删除成功代替。

其余门槛包括：有本地原生正对照的 TCP/UDP/loopback 入出站测试，确定实际执行者；文件读写/seed/ADS/reparse/父 DELETE_CHILD；进程内存与 Job 总内存独立触发；受控 CPU 饱和；超时/停止/子进程/Host 崩溃/pipe flood/持管道子进程/关闭失败；同一固定构建的长测。SKIP、unsupported、无法启动、网络自身不可达与配置回读不能算拒绝通过。

干净标准用户/独立 Windows 的验收环境仍缺，不能把本机结果冒充独立验收，也不自行创建用户、服务或 VM。没有这些证据前，候选只能是候选，阶段0仍为未通过。

## 本项目依据与恢复入口

- 当前完成/未完成范围：`pause-checkpoint-2026-10-19-1457.md`。
- 核心真实证据及限制：`stage0-evidence.md`、`runtime-investigation.md`、`runtime-security-review.md`。
- 启动器对照：`src/deskpal/execution/windows/_win32.py`、`launcher.py`；本轮没有修改。
- 重新开发时先阅读本报告与暂停检查点。原检查点中的“优先继续 MIC”现在应结合此次后端比较先作路线选择，不应机械恢复旧测试矩阵。

