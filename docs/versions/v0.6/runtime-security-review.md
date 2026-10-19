# V0.6 阶段0：runtime/profile 权限静态审查

日期：2026-10-17，Asia/Shanghai。状态：**静态设计审查；阶段0未通过；本文方案均未执行**。

审查对象为[验收边界](../../planning/v0.6/acceptance-boundaries.md)、[runtime 调查](runtime-investigation.md)、`src/deskpal/execution/runtime.py` 和 `scripts/probe_worker_filesystem.py`。本次只创建本文，没有修改实现、现有文档或测试，没有执行 native、profile、ACL、网络、安装或 git 操作，没有读取真实配置、密钥或数据库。启动问题由 Windows adapter 调查继续处理；初始化故障消失或转移不代表权限探针主体已经完成。

## 当前文件 DACL 的判断

`readonly_sddl` 的 protected DACL、包 SID 明确拒写、宿主/SYSTEM 保留完整权限及 OWNER RIGHTS 方案，适合作为待验证提案：

- 拒绝 mask `0x000d0156` 包含 FILE_WRITE_DATA/FILE_ADD_FILE、FILE_APPEND_DATA/FILE_ADD_SUBDIRECTORY、FILE_WRITE_EA、FILE_DELETE_CHILD、FILE_WRITE_ATTRIBUTES、DELETE、WRITE_DAC、WRITE_OWNER。
- 包 SID 的 `0x001200a9` 为文件/目录读与执行所需权限，没有授予写权限；拒绝项没有误含 SYNCHRONIZE。
- `S-1-3-4` 的 OWNER RIGHTS ACE 仅允许 READ_CONTROL。微软明确说明，该 SID 的 ACE 会使系统忽略 owner 隐式 READ_CONTROL 和 WRITE_DAC；这不是仅凭注释作出的假设。宿主的显式完整权限仍保留。[官方依据：Owner Rights](https://learn.microsoft.com/en-us/windows-server/identity/ad-ds/manage/understand-special-identities-groups)
- AppContainer 的用户/组与包/能力两部分授权取交集；不能因为同用户宿主 ACE 为完整权限，就直接断定 worker 也有完整权限，反之也不能省略真实 token 的 owner/WRITE_DAC 负测。[官方依据：Launch an AppContainer](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)

当前代码覆盖本轮新 profile 的整个 moniker 根及已有后代，比只修改原生 AC 目录完整；仍不能据此证明所有持久缓存都位于该树内。文件 DACL 对注册表没有约束。SDDL 生成、SetNamedSecurityInfo 成功或 profile 最终删除，都不是 worker 权限边界已经通过的证据。

文件权限由对象 SD 控制；新对象默认继承父目录，rename/move 不会重新分配默认 SD。因此仅验证父目录或仅测新建文件，不足以证明已有对象安全。[官方依据：File security and access rights](https://learn.microsoft.com/en-us/windows/win32/fileio/file-security-and-access-rights)

## 现有探针盲点

1. `file_probe` 只在指定目录中创建最多 8 字节新文件，并打开目录请求 WRITE_DAC/WRITE_OWNER。尚未测试 host seeded 既有文件的改写、追加、截断、ADS、EA/属性、文件自身 WD/WO，以及 rename/delete。
2. 尚未覆盖子目录/子文件的实际继承、protected 子对象例外、封 ACL 后由 host 创建的新对象，以及父目录 FILE_DELETE_CHILD 对对象删除/替换的影响。删除检查可能涉及包含目录权限，不能只看目标 DELETE 位。[官方依据：IRP_MJ_CREATE 的特殊访问检查](https://learn.microsoft.com/en-us/windows-hardware/drivers/ifs/checking-for-other-special-cases--on-irp-mj-create)
3. ACL 应用后缺少实际 owner、protected 控制位、DACL/ACE 及 seeded 后代对象的回读核验。对文件树的 lstat 后再按名称修改 ACL 也不是生产所需的句柄身份/竞态证明；目前只有在 worker 尚未运行、可信宿主持有新对象期间才有受控实验前提。
4. 注册表分别请求 KEY_SET_VALUE、KEY_CREATE_SUB_KEY、WD、WO 是正确改进，但根 KEY_SET_VALUE 成功仅记录 handle 权限，没有真实根值写对照；子键只在请求 CREATE_SUB_KEY 成功后才尝试。尚未测试从只读根 handle 发起创建的路径。微软明确说明 RegCreateKeyEx 的创建授权检查父 key 的 SD，而不是 handle 打开时的访问 mask；KEY_READ handle 仍可能用于创建。[官方依据：RegCreateKeyExW](https://learn.microsoft.com/en-us/windows/win32/api/winreg/nf-winreg-regcreatekeyexw)
5. 手工拼接 AC/Temp、INetCache、INetCookies 和几个同级目录，不等于实际 API 返回的全部位置。后续需记录实际 worker 的 GetTempPath/SHGetKnownFolderPath 等已选 API 返回值，避免使用 CREATE 类 flag；落在本轮专用树外的结果先只做最小写权限打开检查，不能向真实用户位置写 canary。[官方依据：SHGetKnownFolderPath](https://learn.microsoft.com/en-us/windows/win32/api/shlobj_core/nf-shlobj_core-shgetknownfolderpath)
6. 尚无实际受限主体完成这些探针的证据。API 不支持、路径不存在、服务不可达、启动未到主体和实际 ACCESS_DENIED 必须分别记录。默认权限正对照与施加后负对照应使用同类对象/API，不能把“控制本来不可达”当隔离成功。

## 官方契约与 registry 定位缺口

已知官方契约如下：

- CreateAppContainerProfile 成功表示为当前用户创建了 per-app profile，其中包含文件夹和 registry storage。已有 profile 返回 ERROR_ALREADY_EXISTS，本项目继续拒绝采用该对象。[CreateAppContainerProfile](https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-createappcontainerprofile)
- GetAppContainerRegistryLocation 接受 desiredAccess，返回 current profile 的 registry storage HKEY；文档说明以当前用户为范围，访问另一用户需 impersonate。该 API 没有接受指定包 SID 的参数。[GetAppContainerRegistryLocation](https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-getappcontainerregistrylocation)
- GetTokenInformation 可核验 TokenUser、TokenIsAppContainer、TokenAppContainerSid、capabilities、IL 等实际 token 信息。[TOKEN_INFORMATION_CLASS](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ne-winnt-token_information_class)
- GetSecurityInfo 可对 handle 指定的 registry key 读取 owner/DACL；读取这些部分需 READ_CONTROL，返回的 SD 用 LocalFree 释放。[GetSecurityInfo](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-getsecurityinfo)

由“本轮新 profile + 对应 AC token + host thread impersonation + GetAppContainerRegistryLocation”取得专用 root，是基于上述 API 的**待证组合设计**。官方页面没有完整规定 impersonated AC/LPAC 的包选择细节、root 固定路径/owner/DACL 结构、该 API 的 WOW64 view 选择或 store 缺失时是否有初始化副作用。不得把这些未载明内容写成官方保证，也不能把普通 AC 的结果外推为零能力 LPAC 保证。

因此第一轮只能只读定位。调用成功、owner 等于宿主、DACL hash 不同，或出现新包 SID 中的任一单项，都不足以单独证明返回对象就是本轮专用 root；后代对象也可能继承相同 ACE。施加任何 ACL 之前，必须把 verified thread token、API 的 current-profile 契约和返回对象实际 security metadata 共同核对，并通过仅涉及两个全新 profile 的 A/B 差异检查排除错误绑定。如果无法可靠绑定，就停止在定位证据，不调用 SetSecurityInfo。不得扫描其他用户 HKU、Storage 路径，硬编码/推导旧 key 路径，接管已有 profile，或通过 take-ownership 解决失败。

## 最小只读定位实验提案

这不是本次已经执行的测试。实际命令仍由主 Agent 集中通过 require_escalated 执行。

1. 外层可信 host 创建、登记并持有两个全新的随机 profile A/B；任一 create 返回 already-exists 即拒绝该对象。外层 host 持有 profile 清理责任、launcher 的进程/Job 所有权以及 helper 生命周期控制，不把唯一清理责任交给即将 impersonate 的 helper。
2. 通过已有 launcher 为每个 profile 创建并保持 suspended 的 AC 进程，先核验 token/Job，绝不 resume。这条实验只为取得实际 token，不依赖 worker DLL/main 初始化，也不能作为实际 worker 行为验收。
3. 对 primary token 仅请求 TOKEN_QUERY | TOKEN_DUPLICATE。若显式 DuplicateTokenEx，创建 SecurityImpersonation / TokenImpersonation，目标权限仅 TOKEN_QUERY | TOKEN_IMPERSONATE，handle 不继承；重复核验包 SID 等信息。[DuplicateTokenEx](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-duplicatetokenex)
4. 每轮使用专用同步 helper。成功设置 impersonation 后，回读 thread token，确认实际 TokenAppContainerSid 等于该轮 profile SID，用户身份符合预期；然后 GetAppContainerRegistryLocation 仅请求 READ_CONTROL。SetThreadToken/Impersonate 失败立即结束该 helper，不继续 API 请求。[ImpersonateLoggedOnUser](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-impersonateloggedonuser)
5. 仅读取返回 key 的 owner、DACL 和控制位，记录是否存在与本轮包 SID 一致的实际 ACE；不枚举/读取已有值，不创建 key，不改 SD。按 A、B、A 的顺序比较 token 与对象 metadata 的对应关系；结果相同或无法解释时记为绑定未明，不能因此升级为 ACL 实验。metadata 关系仅为本机实验佐证，不补写官方未声明的普遍契约。
6. 每次 impersonation 后必须 RevertToSelf 并核验返回值。失败时原 helper 必须退出，不再执行宿主 cleanup。微软明确要求在 RevertToSelf 失败时关闭进程。[RevertToSelf](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-reverttoself)

外层 host 必须能检测 helper 超时/失败并结束、等待 helper，继而回收其持有的 suspended worker/Job。单个 helper 既独占 profile/Job 又在 Revert 失败时 os._exit，会丢失清理责任；该泄漏路径不是可交付方案。需要外层 host 的资源登记和失败报告在启动 helper 之前已经建立，且 helper 不继承额外宿主句柄。

## 后续独立 KEY DACL 候选（尚未批准为有效边界）

只有只读定位已经可靠绑定到本轮专用 root、相应实际实验获准后，才考虑使用该 root 的已绑定 handle 施加独立 registry DACL。候选 SDDL 为：

```text
D:P(D;CI;0x000d0026;;;{package_sid})(A;CI;0x000f003f;;;SY)(A;CI;0x000f003f;;;{host_sid})(A;CI;0x00020019;;;{package_sid})(A;CI;RC;;;S-1-3-4)
```

`0x000d0026` 拒绝 KEY_SET_VALUE、KEY_CREATE_SUB_KEY、KEY_CREATE_LINK、DELETE、WRITE_DAC、WRITE_OWNER；`0x000f003f` 为 KEY_ALL_ACCESS，`0x00020019` 为 KEY_READ。使用 CI 继承，不照搬文件 OICI/RX/FA mask；registry 不支持 SYNCHRONIZE。实际是否需要全部 KEY_READ 位仍需按最小运行需求冻结。[Registry Key Security and Access Rights](https://learn.microsoft.com/en-us/windows/win32/sysinfo/registry-key-security-and-access-rights)

接口宜与文件 ACL 分开，例如 session-owned registry binding 对象与独立的 readonly_registry_sddl/apply_registry_dacl；拒绝裸 HKEY 数值、调用者路径或反序列化 DTO 自行授权。绑定对象保存本轮 profile identity、实际 token/SD 证据与生命周期，不把“同值对象”当所有权凭证。

施加应使用 SetSecurityInfo(handle, SE_REGISTRY_KEY, DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION)，随后回读 owner、protected 位和实际 ACE。使用明确所需访问权的 handle，避免 MAXIMUM_ALLOWED handle 导致子对象传播差异。已有 protected 子键及后代 SD 仍需在本轮专用树内单独核验，不能仅凭 root 继承位宣布整个树被约束。[SetSecurityInfo](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-setsecurityinfo)

后续正负对照仅操作本轮唯一随机 synthetic 子键/值：默认 DACL 的受限 token 正对照有限创建；拒绝 REG_OPENED_EXISTING_KEY；施加后重新打开最小 handle，分别测 SET_VALUE、CREATE_SUB_KEY、DELETE、WD、WO，补充只读根 handle 的创建尝试与 seeded 子键的继承。旧的可写 handle 不能带入负对照；host 的管理 handle 绝不继承给 worker。实际 canary 每处最多 8 字节且有总次数上限，不以扩大写入测试 quota。

## 清理、最小下一步与验收边界

外层 host 先确认 helper 已退出/不再 impersonate，停止并等待所有 worker/Job 回收，关闭所有 registry/file/token 等相关 handle，然后以原宿主用户身份删除本轮新 profile。Revert 失败时由外层 host 执行该顺序，原 helper 不继续 cleanup。删除 API 的 HRESULT 必须检查；失败保留 profile 状态未决并可重试，不复用该 profile、不伪报成功。微软说明删除前应关闭 storage handle，失败时 profile 状态不确定。[DeleteAppContainerProfile](https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-deleteappcontainerprofile)

最小下一步仍为主 Agent 确认真实主体能够启动，再集中执行上述独立只读 A/B 定位；当前不实现或运行 registry ACL。随后按顺序补齐可靠绑定、独立 KEY DACL 正负对照、seeded 文件/ADS/继承/父 DELETE_CHILD，以及实际 API 缓存位置。token impersonation 结果只能补充 ACL 证据，不能替代实际 worker、双 Python、标准用户及干净环境验证。

本次未运行任何测试；文中所有候选和实验都保持未验证。真实风险包括未知或无界持久写入口、缓存/服务重定向、继承例外、已有写句柄和普通 AC 的公共资源权限。发现任一未知/无界可写持久命名空间，或无法确认清理/对象绑定，即维持阶段0未通过。有限 canary、Job 内存限制、轮询大小及事后清理不构成磁盘 quota，不降低现有验收门槛。
