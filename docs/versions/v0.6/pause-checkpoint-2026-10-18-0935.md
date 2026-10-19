# V0.6 暂停检查点：2026-10-18 09:35

Asia/Shanghai。用户明确要求“保存进度，暂停工作”。当前全部子Agent已完成，无正在执行的原生实验；仅保存文档与文件指纹，停止新的实现、审查、测试和实验。**当前暂停，阶段0仍未通过，V0.6A/B未整合，产品仍0.5.0rc1。** 只有用户再次明确继续后才能恢复。Git未stage/commit/push，由用户自行提交。

本检查点是最新入口，优先于[本轮继续记录](resume-2026-10-18-after-0019.md)、[00:19检查点](pause-checkpoint-2026-10-18.md)和[原交接](../../handoffs/2026-10-17-v0.6.md)。历史记录与失败证据保留，不覆盖。

## 本轮已经完成

- ACL失败审计完整落盘before/plan/expected/actual及失败阶段；Session独立sticky evidence_unknown，有界原因列表不再丢失未知分类。报告区分直接句柄关闭、helper回收和仅owner方法返回，不把它们扩大为全部OS句柄关闭证明。
- fresh r2取得actual，精确回读唯一差异是六个原继承ACE清除0x10；随后将此转换明确放在Set前的输入计划，仅非critical原继承ACE允许清位，其余字节/相对顺序不变，转换后非canonical或critical+inherited在Set前拒绝，回读仍逐字节精确比较。
- fresh r3的五项exact DACL gate全部通过，但正确身份的新worker十项操作仍全部status0，包括root写类权限请求、通过RC父handle创建并写入，以及host seeded随机键SET/DELETE/WD/WO。因此**当前registry拒写方案明确失败**，不能以SD回读通过、mock通过或退出0替代拒写证据。r3脚本退出2，源码稳定，profile保留。
- 已新增最小只读SD端点观测：worker首次取得自己的RC根HKEY后、既有十项操作之后，从同一HKEY记录before/after；host在collect/context完成且结果已入report后、denial gate前，从登记的管理HKEY记录独立post-worker snapshot，失败路径也保留。未覆盖_apply.after，拒写要求不变。
- SD纯二进制解析与worker输出有界：复制SD≤65536 bytes、ACE≤32、累计raw ACE≤2048 bytes、frame≤16384 bytes；解析/查询/LocalFree等异常继续unknown并保留现场。不读取registry值，不枚举名称，不扩大权限。

## 当前冻结与检查

| 文件 | SHA256 |
| --- | --- |
| scripts/diagnose_worker_registry_acl.py | 6d5da8664d84d07a16425ff04eafce3b89240bc77db834c3d691ffbaceb6ece1 |
| tests/unit/test_worker_registry_acl_diagnostic.py | 3957c5f4ec24acbfcb0b225f565f2c99cdbbc945f6f307436895f1e1c0e2b583 |

`data/validation/v06/resume-20261018/acl-sd-r6/run-record.json`记录最新冻结版本的五测试合并检查：Python3.12.4与3.11.17各**293 passed**；两解释器下对应10文件strict均通过。四命令均未超时，14项输入逐命令起止SHA一致。日志/JUnit/命令/时间保留；旧r3 strict失败、r4审计、r5计划验证及全部native证据保持原样。测试均为mock/静态检查，未执行诊断main或native。

**最新SD端点实现尚未完成独立只读代码审查，也尚未执行fresh r4原生观测。** 独立Sol此前审查通过的是审计/计划版本，并认可最小观测方向，不能据此冒充已审查最新冻结实现。主Agent尚待复核解析边界、嵌入worker源码、LocalFree失败、host异常持久化和相应mock。

## 保留现场及不可扩大结论

以下profile和各自`registry-acl-native-rN-runtime/`、report/record/log全部保留，**禁止重新打开、删除、复用或修改ACL**：

| 轮次 | Profile | SID |
| --- | --- | --- |
| r1 | deskpal.v06.2730c3c209454a24ac9088af34cd76e6 | S-1-15-2-3000190650-978727491-2643708991-393094396-1502461118-1685071258-2529998708 |
| r2 | deskpal.v06.3956f1028f1f4a899ba947242c3afb27 | S-1-15-2-1490409598-949675408-1880839668-1050530837-3078614806-2270983871-3903502743 |
| r3 | deskpal.v06.96179503a0004909bab2b5d755493946 | S-1-15-2-2170386380-361984684-1982206656-3772551083-3988461422-3272150561-2996291488 |

r2/r3的resource_close_unconfirmed=false、evidence_unknown=true；helper已回收/streams关闭，记录的管理句柄直接关闭确认。Popen自有process handle和adapter逐句柄CloseHandle结果仍未独立核验，不称长测通过。

此前V3 memory只观察到匹配本次key/PID的进程限额消息9，未见Job-wide消息10；Job-wide、CPU饱和、长测和其它阶段0缺项继续未通过。旧V2的16GiB memory分支禁止执行。文件完整拒写、网络、独立普通用户/干净系统、停止/宿主崩溃等仍按原验收保留，不能进入Core/Factory或A/B整合。

## 用户明确继续后的入口

1. 读取本检查点，核对`data/validation/v06/resume-20261018/pause-inputs-0935.json`及适用指令/活动Agent；保留全部工作区和忽略目录证据。历史pause-inputs.json仅属于00:19旧版本。
2. 按既有Sol实现/审查、Luna固定检查分工，先完成最新SD实现的独立审查与主Agent复核。若修改源码，冻结新SHA并用新证据目录重新跑适当mock/strict，保留首次失败。
3. 审查和验证通过后，才由主Agent经工具权限流程在全新profile运行fresh r4。固定runner为`data/validation/v06/resume-20261018/run-native.py registry-acl r4`，已允许r4标签；r4尚未执行，不覆盖r1–r3，不触碰保留profile。外层timeout90秒，超时保留unknown不盲重试。
4. 比较_apply.after、worker before/after与host post原始SD，继续执行原十项拒写gate。端点快照不能定位启动/首次位置API/后续调用谁造成变化；首个RC获取本身是位置API调用。SD相同不能单独证明物理根对象身份；随机种子可访问仅支持namespace关联。不得先假定reset或以此降低拒写标准。

不读取.env/私人配置/真实数据库或密钥，不调用云API/下载模型/安装依赖，不改全局安全设置，不建立普通进程fallback。没有独立VM/备用机，不擅自建系统用户。
