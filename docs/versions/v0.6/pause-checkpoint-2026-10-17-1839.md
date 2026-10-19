# V0.6 暂停检查点：2026-10-17 18:39

Asia/Shanghai。用户要求保存进度并暂停；主Agent已中断三个子Agent，没有启动新的测试、编译或权限实验，仅保存交接。**阶段0未通过，V0.6A/B未整合；产品仍0.5.0rc1。** Git未stage/commit/push，由用户自行提交。

本检查点优先于此前08:40暂停快照和恢复过程。完整背景见[开发进度](development-status.md)、[证据矩阵](stage0-evidence.md)、[验收边界](../../planning/v0.6/acceptance-boundaries.md)。

## 已完成且已检查

- 双Python真实ordinary AC runtime：`runtime-py{311,312}-canonical-r1/filesystem-report.json`，host正对照及默认/文件只读profile下native/Python均能运行。文件DACL后已选目录拒写，但registry仍可写；不能据此通过持久写边界。
- PI所有权修补：`_create_worker`在create调用finally登记PI句柄，避免诊断包装填PI后抛错时漏关。Luna纯mock双Python各100 passed，6文件strict通过；`pi-ownership-r1/`保留首轮失败与最终记录。
- `diagnose_worker_limits.py`：mock双Python各16 passed、两文件strict通过。实际`limits-native-r1.json`五项有界行为按预期、退出后Job active0/process signaled、profile_deleted、source_unchanged。首smoke宿主句柄净增6未归因，后四项0；不能宣称零泄漏/零秒停止。CPU饱和、memory、独立child限制、宿主崩溃及停止分布未通过。
- `diagnose_worker_registry.py`：mock各29 passed、两文件strict通过。真实`registry-aba-r1.json`只读A/B/A的token/包ACE/SD关联一致，helper Revert/关闭/退出均确认，挂起worker已回收、两profile_deleted、源码稳定。旧`binding_verified=false`保留；仅支持下一freshprofile重做核验后取得候选session-owned HKEY。
- `diagnose_worker_network.py`：mock各46 passed、两文件strict通过。`network-py{311,312}-r1/network-report.json`均host正对照四操作成功并收两canary，受限TCP超时、UDP API成功但host未收包、bind/listen成功。退出/清理和源码稳定；不将超时/无收包计权限拒绝，也不将API成功计流量越界。LAN/外联/IPv6/入站可达未过。
- 安全内存V3源码与builder：V3仅smoke/bounded-memory，最多32×16MiB=512MiB commit，先释放所有成功块再emit；V2仍保留且其16GiB memory分支禁止运行。builder已支持version3，并将三处reparse检查改为lstat以识别junction；V1/V2默认/产物未覆盖。Luna在暂停前已完成r2双Python各20 passed、builder/test两文件strict通过，输入SHA稳定。记录 `native-memory-v3-unit-r1/r2-run-record.json`。**V3尚未真实编译，没有exe SHA/pin，不可运行memory实测。**

上述原始证据均在Git忽略的 `data/validation/v06/`；分批定向检查不是最终全量或V0.6发布验收。源码新旧指纹不同的轮次不得互相替代。

## 未验证草稿与未落盘工作

`scripts/diagnose_worker_registry_acl.py` 已有约37KiB草稿，尚未冻结、主审未完成、没有对应测试文件、没有pytest/strict或真实ACL实验。不要执行。它尝试helper只持verified AC token取得RC|WD根key、Revert后ready，外层只对自身新helper拉取非继承管理HKEY，ACK/退出确认后外层绑定、seed/apply；实际worker不继承管理key。原critical allow ACE必须逐字节保留，新精确package deny前置，OWNER RIGHTS按canonical顺序放原显式项后/继承项前。必须继续审查部分成功的token/helper/pipe/HKEY/Job所有权、SD回读、默认/拒写实际worker对照、REG_CREATED_NEW_KEY及清理失败；不能从旧报告或裸PID/HKEY/SID/路径授权。

`scripts/diagnose_worker_memory.py`及其测试在暂停时**尚未落盘**。已讨论的IOCP设计不是实现：原process/job各256MiB、active1/CPU/AC限制保持；在adapter完成_verify、所有权登记后的resume hook关联host-only port，通过SetJob7、有限NEW_PROCESS(key/PID)、IsProcessInJob、QueryJob3唯一PID及class9限额作绑定链。QueryJob7不在官方支持列表。硬限额消息不保证送达；匹配消息9仅per-process事件、10才job-wide事件；未收到保持未知，不用VirtualAlloc错误/峰值替代。清理需先真实回收worker/Job，再有限drain、关闭observer/port，最后删profile。

网络入站补测只有设计：receiver以端口0绑定后必须在socket存活期间发布ready，host bounded发送，再记录accept/recv/canary。现public collect退出后才交付帧，不可猜端口或扫描用户socket表；后续另审有界诊断pump或通知接口，不能让callback阻塞破坏deadline。冻结的network脚本不含该入站实验。

## 恢复顺序

1. 用户明确继续后，先读本检查点、核对源文件SHA和活动工具状态；不要把未审ACL草稿当作已通过实现。Luna仅负责简单固定检查，权限设计/原生实验由主Agent或Sol审查，实际命令仍集中由主Agent执行。
2. 完成ACL草稿的独立审查、纯mock与双Pythonstrict；全部管理句柄/新profile的绑定和清理闭合后，才经工具权限流程运行freshprofile正负对照。原包ACE flags0x23含CRITICAL_ACE_FLAG，不靠删掉该项拒写；必须回读真实DACL。未覆盖系统预建/受保护后代或其它可达namespace仍是阻断项。
3. V3 builder当前检查已通过，可由主Agent构建**新** `data/v06-stage0/native-memory-fixture-v3/`（先确认不存在），命令 `python -B scripts/probe_windows_launch.py --build-native-fixture data/v06-stage0/native-memory-fixture-v3 --fixture-version 3`。记录receipt、日志、源码/编译器/SDK样本/exe SHA，审查pin后才接新memory诊断脚本；编译成功不是受限执行通过。
4. 继续文件既有对象/ADS/继承/父DELETE_CHILD/SD回读/API真实缓存位置、网络入站及可获得的跨主机路径、真实资源和生命周期证据；不重跑已收敛旧loader失败矩阵。新影响执行路径的修改需要对应新证据。
5. 所有可达持久写区拒写或有硬总量约束前，阶段0不通过，不整合A/B自动能力。用户已确认暂无独立Windows11 VM/备用机，先本机开发；独立普通用户/干净系统及LAN/外联所需环境继续列未过，不新建系统用户/安装VM或修改全局网络设置。

## 指纹与清理状态

13个关键文件SHA保存在 `data/validation/v06/pause-2026-10-17-1839-inputs.json`。其中ACL草稿SHA只是暂停快照，绝非审查或运行批准：

- `_win32.py`：`6162abd320cf29b7f2184fea287fe07e213038fe0b63f1849739a527f91bc421`
- builder：`a426179a3a595c2045554c0ada2ae1220157fb9788b51b1dbbe64595a8c6fdf4`
- V3 C：`bc34c6ae69ca10bd20cd3ddeb2f1502b85114b009f3d71bab529a6cf6913cbbc`
- ACL草稿：`e7d60eaa0f2befbd15cdec63cc6fe8044549817134b39b5c14988858dfb1a884`

主Agent本轮启动的limits/registry/network实际调用均已返回，报告确认其专用profile已删除，无待处理cleanup_owner记录；三个子Agent状态已中断。本次不读取`.env`/私人配置/数据库，不调用云API或安装依赖，不更改全局ACL/防火墙/exemption。早期readonly生成runtime及其ACL保留原状，不递归删除/放宽整个仓库或Conda权限。
