# V0.6 阶段0证据与剩余门槛

**2026-10-18 09:35最新：按用户要求暂停，入口见[暂停检查点](pause-checkpoint-2026-10-18-0935.md)。fresh ACL r3的精确DACL gate通过，但正确身份的负worker十项全status0，registry拒写明确失败，三个失败profile保留。最新只读SD端点观测实现双Python各293项mock及10文件strict通过；最新实现独立审查与fresh r4尚未执行。此前V3消息9、Job-wide未知和其它阶段0缺项保留；下文为较早快照，不以旧计数替代当前检查或实际拒写证据。**

日期：2026-10-17。状态：**未通过，候选后端未接入产品**。本表按验收条件组织证据，避免把历史诊断或测试计数当作版本交付。原始产物均在Git忽略的 `data/validation/v06/`，进度见[开发状态](development-status.md)，标准见[验收边界](../../planning/v0.6/acceptance-boundaries.md)。

## 已有证据

| 边界 | 当前观测 | 证据与限制 |
| --- | --- | --- |
| 启动方式 | DETACHED候选下，V2 native与staged Python3.11.17/3.12.4可执行并回传 | `runtime-py{311,312}-canonical-r1/filesystem-report.json`；ordinary AC明确选择，LPAC未测试通过 |
| 身份与限制配置 | 每个受限run记录实际AC/package SID、capabilities=0、child policy=1、Job成员、active limit1、256MiB进程/Job commit、CPU cap2000 | 查询配置不是全部限制已触发；不抵独立标准用户/干净系统验证 |
| runtime入口 | 两个版本sys.path仅专用runtime/Lib，isolated=1、no_site=1、不加载site、禁止pyc；五键环境 | 只验证选定最小分发，不承诺任意Python依赖可运行；manifest安全标记不自动改为true |
| 文件初步拒写 | 默认profile有正对照；文件DACL后已选目录的新建与目录WD/WO被拒 | 尚缺seeded文件改写/追加/截断/ADS/属性、删除/重命名、继承/父DELETE_CHILD、SD回读、API实际位置与生产句柄竞态 |
| 注册表 | 默认和文件只读阶段均可创建合成子键，SET_VALUE/CREATE_SUB_KEY/WD/WO请求成功 | **明确阻断项**；文件DACL、Job内存和事后清理均不提供registry容量限制 |
| registry只读关联 | A/B/A actual token、唯一包ACE、无交叉SID及稳定回切一致，helper/worker/profile清理确认 | `registry-aba-r1.json`；仅支持下一freshprofile重做门控取得候选HKEY。旧bindingfalse不改；critical allow须保留，尚无KEY拒写证据 |
| 有限资源行为 | smoke、deadline、child拒绝、日志限额、非法帧均按预期观测，退出后Job active0/process signaled | `limits-native-r1.json`；memory未跑、CPU饱和未证、child原因未分离，首轮host句柄+6未归因，不能将外层cleanup计时0说成0秒停止 |
| loopback网络 | 双Python host四API正对照成功并收包；AC TCP超时，UDP API成功未收包，bind/listen成功 | `network-py{311,312}-r1/network-report.json`；未得10013/drop归因，保持未决；不证明LAN/外联/入站隔离或流量越界 |
| 环境与句柄 | 实际worker仍只有五键；native单个excluded数值句柄记录精确STATUS_INVALID_HANDLE | 环境路径被系统追加映射；不等于所有可达缓存已覆盖。配置allowlist/单个canary不等于完整句柄身份清单；Python异常保留未决 |
| 清理 | 两轮报告均profile_deleted；无清理失败记录 | worker context退出调用Job终止/等待，是代码语义；报告缺逐worker独立停止时延及长测，不补推断 |
| 编码协议 | 有界严格JSON framing，拒绝重复键/非有限数/越界/截断；编码前累计字节预算 | 数据格式检查尚不是session、invocation、attempt、generation和业务状态认证 |
| 固定检查 | 双Python各150项定向测试、16文件strict通过 | `canonical-backend-unit-r1-*`；不是V0.6最终全量。native前后17输入及V2 exe全部SHA匹配，见`canonical-native-inputs-{before,after}.json` |

## 接下来按依赖推进

1. **注册表对象可靠绑定**：只读A/B/A定位已取得上述关联证据，下一freshprofile仍须重做verified token/API/包SID/owner/DACL/control门控、外层登记实际session-owned HKEY并保留helper/Job/profile所有权；不复用旧报告对象。无法可靠绑定则不调用修改SD API。
2. **独立registry拒写边界**：只有绑定成立后才设计/审查KEY DACL，保留默认正对照、seeded后代、只读根handle创建路径及关闭旧可写handle后的负对照。无强制总量约束时必须拒绝持久写。
3. **扩展持久存储覆盖**：先读取选定Windows路径API的结果，再分类专用树内/外；树外不写canary、不读取真实内容。补文件既有对象/ADS/继承/父删除权和SD回读；每个不支持或未知结果单列，不能计作拒绝成功。
4. **真实资源与停止**：有限合成spin/memory/child/flood/invalid-frame，分别判断deadline、commit分配拒绝、子进程限制、日志/协议限额和确认回收；CPU cap还需计时/Job accounting，不能只查配置。
5. **网络及宿主隔离**：已完成双Python本地出站对照，但尚未形成明确拒绝/匹配drop证据；后续listener入站、loopback、可测LAN/外联分别记录。无正对照或无实际路径仅为未决；不新增loopback exemption或全局防火墙规则。私人配置/数据库/模型与进程访问只用合成canary验证。
6. **平台与生命周期**：双Python、独立标准用户/干净环境、宿主异常退出/IPC断开、真实停止分布、最终同构建长测。测试专用进程树必须与生产禁止子进程策略区分，不通过修改生产限制制造通过。

以上是阶段0的实施依赖，不删减A/B、持久授权、broker提交事实、隐私屏障或继承的V0.5门槛。所有可达持久命名空间尚未拒写或有硬总量限制前，不进入A整合/B自动规则。阶段成果可记录，产品版本保持0.5.0rc1。

## 证据记录约定

新增真实实验使用新报告名和新profile，记录命令、OS/Python、合成产物与实现指纹、实际错误、清理结果；保留首次失败。API返回、精确异常、路径不存在、启动失败、配额拒绝和未知结果分别表示。退出0只说明诊断脚本完成，不授权工具、不证明业务成功或阶段0通过。

测试Agent执行固定检查与机械提取；权限实验、结果归因和门槛判定由主Agent或Sol审查。所有实际profile/ACL/native实验由主Agent集中通过工具权限流程执行，不读取`.env`、私人配置/数据，不调用云API；Git提交由用户处理。
