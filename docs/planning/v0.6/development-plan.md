# V0.6 开发计划：隔离执行与有限自动行为

日期：2026-10-16。状态：**设计提案，尚未开工实现**。依据当前0.5.0rc1源码；[环境评估](environment-assessment.md)与[验收边界](acceptance-boundaries.md)共同构成开工基线。V0.5未结项继续保留。本计划不修改已有历史证据或将新功能计为完成。

## 版本范围与顺序

先完成 **V0.6A：独立执行、能力授权、实际停止与恢复**，再完成 **V0.6B：默认关闭的本地确定性自动规则**。这是同一版本的两个验收门槛；A可单独评审，不以A代替完整V0.6。首版只接入内置能力，公开不可信插件生态、任意Python/shell执行、浏览器自动化、后台云自主规划、Live2D和人格情绪不在范围内。

保留 Application→Agent Core→Planner/Task Manager→Tool Manager→Sandbox→Tools 的方向。跨进程执行属于 Sandbox/执行adapter，GUI和规则引擎均不直接启动工具，不产生第二个Core。

```text
GUI / CLI / LocalAutomationController
                |
        ApplicationService（唯一owner）
                |
        Agent Core + 持久Task/Plan/Attempt
                |
     ToolManager / CapabilityPolicy / ToolExecutor
                |
       ExecutionSupervisor（独立控制通道）
         |                       |
  Windows WorkerLauncher    CapabilityBroker（可信）
         |                       |
  AppContainer + Job       句柄锚定文件/日程/显式桌面代理
         |
  无密钥、无业务库、默认无网络的工具worker
```

LLM Adapter、Memory Manager仍由宿主组合，不能把API key或整个Application序列化进worker。worker默认只得到当前调用的必要输入；它没有任意workspace/data目录访问权。可信broker是安全边界的一部分，需要单独审查和故障处理。

## 关键设计决定

### 执行与能力分工

新增接口先于实现，暂定 `ToolExecutor.execute(invocation)`、`ExecutionSupervisor.cancel(invocation_id)`、`CapabilityBroker.invoke(operation, payload)`、`CapabilityPolicy.issue/authorize/revoke()`。返回结构化执行结果，区分启动拒绝、已知业务失败、已停止且无副作用、已知提交和结果未知；不能全部压成 `ToolResult(False)`。现有Tool协议保留业务语义，由adapter迁移；协议版本与权限规则版本分别记录。

| 能力类别 | 放置与权限 |
| --- | --- |
| echo/time、纯文本/分类处理 | 独立worker，处理有界输入；生产环境默认禁止再启动子进程 |
| read/write/copy/move/delete/search | worker只请求已批准的精确操作或处理快照；真实文件访问/提交由文件broker控制，禁止任意路径open代理 |
| calendar/timer、目标/记忆相关状态 | 通过宿主有界命令接口，持久连接不跨进程；现有19工具须逐项列出执行类别、风险、限额和回归证据 |
| 截图 | 人工确认的专用桌面代理，限定屏幕/区域/输出位置/字节预算；不让worker获得一般桌面权限，不自动截图 |
| 云LLM/embedding | 不在受限工具runtime。自动规则首版完全禁止这些链路；普通请求沿用用途资格与超时限制 |

截图或其他可能阻塞的代理不能只在宿主主控制线程同步调用。阶段0判定是否放入独立可信helper并纳入Job/回收；任何仍无法强停的代理必须标为协作停止，不得作为“已隔离可强停工具”登记，自动规则也不得使用。文件提交短临界区与SQLite事务单独核验。

### 宿主持有精细授权

引入持久Policy/Grant与短期InvocationLease，记录签发主体/来源、规则或任务、plan hash、call_id、tool contract、参数摘要、操作种类、资源身份、workspace身份、依赖revision、用途、policy revision、session epoch、有效期和预算。读取和写入、源与目标、覆盖与新建分别授权；授权不是单一工具名集合。worker持有的ID只用于相关性，不能凭伪造字段升级权限。

一次人工确认派生当前原计划逐调用租约；长期规则授权来自专门的界面确认，不从普通确认、Proposal接受、LLM文字或 `PermissionLevel.AUTOMATIC` 自动推导。过期、撤销、暂停、策略/计划/资源/依赖变化都使旧租约失效。重启保持规则定义但**默认暂停并清空执行租约**，本版需人工重新启用，避免持久授权的隐式扩张。

旧v1 immutable plan保持原始JSON/hash；不就地改写历史计划。旧授权不转换为新长期Grant，旧未完成任务沿用NeedsReview/Uncertain，生成新执行资格须显式审核。低层 `create_agent` 测试/开发入口和生产 `create_application` 都要标明执行模式；生产安全模式不得经旧同步入口绕过隔离。

### IPC与启动

阶段0首选AppContainer、评估LPAC和受控句柄管道；后续冻结唯一默认实现。挂起启动→设置并查询Job限制→核验token/继承句柄→允许处理请求。若任一关键步骤失败，关闭句柄并回收进程，拒绝执行；无普通子进程自动fallback。

协议采用长度受限的严格JSON DTO，不使用pickle、任意对象反序列化、动态import或worker提供的可执行路径。包括protocol version、session/invocation/attempt/call ID、generation和单调序列；校验类型、深度、重复字段、总字节和当前状态。channel绑定真实进程/实例，不只信任消息自报PID或ID。过期/重复/乱序/越权消息拒绝且有界记录。broker按宿主持久计划取参数，不接受worker自由指定SQL/路径/URL/模块/命令。

stdout/stderr与控制消息隔离、限额排空；禁止把日志当协议。worker环境白名单、只读runtime、无写权或有强制总量约束的专用临时区、可继承句柄白名单均列入启动清单。检查AppContainer profile/TEMP/缓存等全部可达持久写区；任何无界写入口都阻断阶段0，不能只记录为已知限制或依靠事后清理。broker staging限额不替代此条件。AppContainer公共可读资源应明确列出；runtime/profile不得跨任务残留用户正文，异常退出清理失败需遮蔽并阻止复用。

### 文件与提交边界

文件broker绑定受控根和已打开句柄/volume/file ID，逐组件拒绝reparse/junction；hardlink多链接、ADS、设备路径、UNC/远程盘和不支持的文件系统默认拒绝。已有工作区若不满足V0.6安全模式条件，显示迁移/选择受控目录的要求，不放宽ACL。路径解析只是输入验证，不充当最终提交授权。

读取采用被授权文件的有界快照；已审核内容版本用快照hash等证据绑定，file ID不代表内容未变。实际送入worker的快照必须匹配版本，覆盖同ID原地改写及读取期间变化；不可证明一致时拒绝并重新审核。写入在broker专属临时区准备，并在提交时重新核验目标身份、覆盖前提、租约、取消/遗忘epoch及预算。rename/replace与hardlink竞态需实测；不能把“最终路径字符串相同”当作对象未变。数据用途资格随快照传递，结果不得隐式进入云反思或持久旁路日志。

### 停止、撤销与副作用事实

控制操作通过独立supervisor入口，不排在Core等待队列后。取消先持久化请求、提升epoch并关闭后续broker提交入口，再请求合作退出，超时终止Job并等待全部进程实际退出。全局暂停/规则撤销先阻止新领取，再使活动租约失效并进入同一停止流程。

broker提交点与撤销事务需定义可复现的先后顺序。撤销先提交时禁止新副作用；若某原子操作已进入提交临界区，等待其结果并记录已提交/未知，不能承诺撤销回滚。**报告“执行已停止”须同时证明worker回收和broker无未决操作**；仍有broker I/O或同步宿主调用未返回时维持Stopping/Closing。Stopping可作为执行会话状态，避免凭空把Task提前置Cancelled。

不新增副作用前证明无效果的取消可成为Cancelled；成功提交后的取消保留已发生事实，缺乏可靠提交凭据进入Uncertain，已知失败按Failed。不能因进程退出码0、worker自报成功或结果帧丢失而猜测提交结果。已有副作用不因kill自动回滚；不保证跨文件系统/SQLite/外部服务exactly-once。

此版hard-stop保证针对迁移且验收过的工具执行进程/代理。宿主同步云请求、embedding和部分存储I/O若仍协作关闭，界面必须说明；终止客户端等待不证明服务商停止生成/计费。若要扩大保证，单独引入可信provider/helper边界并重新验收，不和无密钥worker混为一体。

SQLite事务只负责预算预留和证据，不持写事务等待长OS操作；短控制锁/epoch负责broker提交开始与撤销的先后顺序。操作系统副作用和SQLite落账之间仍有崩溃窗口，必须保留Uncertain。

### 持久化、遗忘与审计

在现有WorkflowStore增加policy/grant/automation_rule/invocation等表，与attempt、唯一run key、预算预占在可覆盖范围内使用同一事务。attempt在发给worker前持久化；broker操作记录受理/提交/结果，宿主负责最终对账。崩溃点覆盖dispatch前后、broker提交前后、结果落账前后；不重发不确定操作。

跨Memory/Scheduling数据库沿用删除工作单和保守恢复，不能宣称跨库原子。当前活动任务时memory_command可能Busy：若继续保留，应明确“遗忘未提交”；不得显示已遗忘。若为取消后遗忘开放并发路径，先建立持久屏障，撤销相关租约、停止持有快照的worker、清理broker staging/视图/结果，再完成删除；迟到消息按epoch丢弃。无法清理时保持Pending/屏障和可重试，不恢复旧正文。

审计记录policy/grant/invocation/attempt、拒绝类别、预算变化、提交凭据和进程回收事实；默认不写正文/密钥。确需结果正文的既有账本须继续受来源/用途/删除索引约束，不能新增无人管理的IPC原始日志、崩溃dump或长期staging副本。

## V0.6B：有限自动规则

第一版采用确定性本地规则→固定受约束计划→唯一任务队列→同一执行/授权链，**不调用LLM planner、Reflect、记忆候选LLM或后台embedding下载**。普通Core的结果反思会进入LLM，因此需要明确的local deterministic路径和数据用途校验，不能仅靠关闭某个通知开关。

初始allowlist只包含time、echo，以及显式授权的精确workspace文件只读快照；不自动枚举目录，不自动写入、复制、移动、删除、截图、建立外联或修改目标完成状态。文件读取授权有独立用途、大小和精确目标范围；内容变化是否接受由规则声明，默认绑定已审核版本，变化转Waiting重新审查。目录元数据扫描留待另行评估。

用户先dry-run看具体能力、目标、输出去向、调用频率、次数/期限/预算，再显式启用；dry-run不执行真实读写或云调用，只做策略匹配和拟执行计划展示。默认关闭、重启暂停；全局暂停和逐规则撤销均可从GUI/CLI操作。Proposal接受仍只建任务，不能变成自动授权。

调度使用持久唯一键 `(rule_id, revision, due_window)`、事务预算预占和单实例领取；忙碌时只保留有界待办，过期窗口跳过，休眠/重启不集中补跑。崩溃后已开始但未对账的run暂停待审，失败也消耗已预占次数，不用自动重试绕过预算。时间回拨不重置已用配额，资格同时核验宿主UTC/单调时钟与session有效性。

## 开发切片与完成条件

| 阶段 | 交付 | 退出条件 |
| --- | --- | --- |
| 0：威胁模型/原型 | Win32 adapter、专用runtime与ACL、IPC/Job/token/网络拒绝实验、19工具迁移表、ADR与限额冻结 | 标准用户双Python真进程证明；启动/隔离失败关闭；不满足则停在原型不开放自动能力 |
| 1：接口与迁移 | Executor/Supervisor/Policy/Broker DTO，schema v2、历史v1兼容、配置/错误码/预算协议 | 接口与迁移审查通过；旧数据无静默授权/丢失 |
| 2：A执行骨架 | 先迁echo/time→只读文件→受控文件提交/日程→显式桌面代理；生命周期和资源限额 | 每类真进程/权限/停止证据通过；未迁能力明确禁用或可信协作模式，不冒充隔离 |
| 3：A系统整合 | Core/Task/账本/GUI确认与停止、遗忘失效、崩溃恢复和安装 | A全部P0/P1通过，独立架构/安全审查后才进入B |
| 4：B自动规则 | dry-run、规则Grant、预算/去重/暂停/撤销/重启暂停、CLI/GUI管理 | 无后台云、越权或过期补跑；同一broker与事实链 |
| 5：最终验收 | 双Python全量/strict/安装、Windows对抗矩阵、同源码长测、操作/诊断/迁移文档 | A+B及继承必需项通过，无未处理阻断项；否则继续rc并列明缺项 |

## 子Agent分工与合并纪律

主Agent冻结接口、持有跨模块变更、审核schema/安全和合并；共享工作区按文件划区，依赖变更由主Agent统筹，不让多个Agent同时改Core/Factory/Store。

| 角色 | 建议模型 | 文件/职责边界 |
| --- | --- | --- |
| Windows执行Agent | 6.1 Sol，高思考 | `execution/windows/`、launcher/runtime/Job/IPC，负责真实系统证据 |
| 授权与Broker Agent | 6.1 Sol，高思考 | `security/`、`execution/broker/`，策略/提交/撤销/TOCTOU，不改GUI |
| 持久化与恢复Agent | 6.1 Sol，高思考 | Workflow schema/attempt/invocation/deletion迁移，配合主Agent接Core |
| 自动规则/界面Agent | 6.1 Sol，高思考 | A冻结后实现 `automation/` 与GUI/CLI管理，不能直接越过App调用工具 |
| 测试执行Agent | 6 Luna，高思考 | 简单机械、低风险、预期明确的固定回归/strict/包与日志检查；不独自负责隔离安全结论 |
| 独立审查Agent | 6.1 Sol，高思考 | 只读审查权限、进程所有权、恢复、隐私/删除与验收证据 |

实际并发按可用槽位排期；原型、威胁模型和异常归因由主Agent/高思考审查Agent主导，安全对抗测试不因“都是测试”一律交给Luna。每项返回修改文件、设计、接口、测试、潜在问题；审查通过后按依赖顺序整合。保持用户自行Git提交的约定。
