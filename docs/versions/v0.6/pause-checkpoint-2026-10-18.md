# V0.6 暂停检查点：2026-10-18 00:19

**后续状态：00:19后曾恢复，用户现已再次要求暂停。本文保留00:19历史快照；最新入口见[09:35暂停检查点](pause-checkpoint-2026-10-18-0935.md)，恢复过程见[继续记录](resume-2026-10-18-after-0019.md)。**

Asia/Shanghai。用户明确要求“保存进度，停止工作”。主Agent已中断两名Sol，Luna固定检查已结束；停止新的实现、测试、编译和真实实验，仅保存本检查点与指纹。**当前暂停，阶段0未通过，V0.6A/B未整合，产品仍0.5.0rc1。** Git未stage/commit/push，由用户自行提交。

本检查点优先于[本轮恢复记录](resume-2026-10-18.md)、[原交接](../../handoffs/2026-10-17-v0.6.md)和2026-10-17的暂停状态。只有用户再次明确继续后才能恢复开发或测试。

## 本轮完成的工作

- 接手核对2026-10-17的13个关键SHA全部一致，原未提交文件和历史证据保留，未找到适用AGENTS.md。
- 在新目录`data/v06-stage0/native-memory-fixture-v3/`实际编译V3成功，MSVC14.44.35207、SDK10.0.26100.0、`/W4 /WX /MT`；源码、builder与选定工具链样本稳定。exe SHA256为`613d1d91e6f2115e067e2a16421d477e13f4e3ca3ace3485610c03b570903cb2`。未覆盖V1/V2，旧V2的16GiB memory分支仍禁止执行。
- 新增`scripts/diagnose_worker_memory.py`与对应纯mock测试。保持原256MiB process/job、active1、CPU及AC限制，在已核验身份/Job之后的resume hook建立IOCP绑定，区分消息9/10，缺消息保持unknown；先reap再drain/关闭observer与port。未改变公共launcher/runtime接口。
- ACL初版补齐RC|WD管理HKEY、`-I -S` helper导入、fresh session绑定、partial句柄登记、Revert失败退出、双流输出预算、critical ACE字节与canonical顺序、精确SD回读、失败保留profile。之后真实首轮暴露了失败审计缺口，当前又有未验证修改，见下文。
- 两名Sol交叉只读审查，实际实验始终由主Agent经工具权限流程执行；未读取真实配置/密钥/用户数据库，未调用云API/下载模型/安装依赖，未改全局ACL/防火墙/loopback exemption。

## 固定检查证据

原始路径均位于`data/validation/v06/resume-20261018/`，各批次数量不相加冒充最终全量。

| 范围 | 已完成与边界 |
| --- | --- |
| V3 builder | 双Python各20 passed、脚本/测试strict通过，`builder-unit-r1/`；首次未设MYPYPATH的import-untyped失败保留，按既有`MYPYPATH=src`配置复验通过，无源码修改 |
| ACL初版 | 双Python各58 passed、脚本/测试strict通过，`registry-acl-unit-r2/`；对应脚本SHA为`7afe9e89938bd6818d414edd4da57261e4222117d1bdb1da4e176d56e476adf1`，测试SHA为`4778db47ab27e5c1206157b055245de04352d4e3bef373fa236ec8b747a2ebc1`。r1的超长pytest参数ID/测试模块引用错误保留，只修测试写法。**此结果不覆盖当前新审计草稿** |
| memory诊断 | 双Python各64 passed、脚本/测试strict通过，`memory-unit-r2/`；脚本SHA为`b5343ecec3a646e6be2f8636ed9b9014b887a896363f406fa1ae1fd3b737dace`，测试为`3ea23b801e52b86197e651b8e62a64309ea36ef05232624c3f7b72be99b82eee`。r1两条测试类型失败保留，r2仅修测试函数返回和Api直接引用 |

所有固定检查均有外层timeout、新basetemp、日志/JUnit、起止源码SHA，未重跑旧产品全量。暂停前计划的五测试组合回归尚未启动。

## 真实实验与现场

### Memory：取得进程限额事件，Job-wide仍未通过

`memory-native-r1.json`及同名`-record.json`、`.log`：build26200、host token未提升，smoke与bounded-memory均exit0；本次Job关联链通过。bounded run成功分配15×16MiB=240MiB，下一块失败、原始错误1455，所有成功块在emit前释放。匹配本次key/PID的消息6→9→7→4支持“观察到本次进程内存限额事件”；没有消息10，Job-wide保持unknown，不凭1455或峰值推断原因。

两run退出后Job active0/process signaled，bounded drain完成，observer/port关闭，profile_deleted，起止SHA稳定。另一Sol只读审查确认归因。process/job同为256MiB、active1，尚无独立Job-wide enforcement、CPU饱和、逐个adapter句柄关闭或长测通过。

### Registry：首轮回读失败，profile保留

`registry-acl-native-r1.json`及同名`-record.json`、`.log`：verified primary/thread token、session-owned同根HKEY拉取、helper ACK/Revert/关闭回执和退出确认；默认实际worker十项正对照全status0。SetSecurityInfo后精确回读gate失败`acl_readback_exact_gate_failed`，**负worker未启动**，脚本退出2，源码稳定。不能宣称registry拒写成立。

首版`_apply`把实际回读snapshot保存在局部变量，gate失败后没有写入报告，故当前无法判断是ACE字节、顺序、ACL revision、owner或protected位的差异。不得猜原因或放宽gate以通过。

保留profile：`deskpal.v06.2730c3c209454a24ac9088af34cd76e6`，SID为`S-1-15-2-3000190650-978727491-2643708991-393094396-1502461118-1685071258-2529998708`。报告cleanup为`withheld_unknown_helper_worker_key_or_binding`，**不重新打开、删除、复用或改其ACL**；相应`registry-acl-native-r1-runtime/`只读生成树、旧报告及日志继续保留。不得把该profile写成已清理。主Agent的两条native调用均已返回，无正在执行的native命令；首版报告未逐资源记录close，不能补称所有OS句柄已验证关闭。

## 暂停时未完成草稿

`scripts/diagnose_worker_registry_acl.py`的后续修改仅为增强失败审计：BoundRoot登记before/plan/expected/actual、Set调用状态和失败步骤；Session新增有界unknown原因与资源关闭记录；experiment finally保存这些数据。原exact gate和sticky unknown/禁删策略保持。

**该草稿尚未冻结、未执行pytest/strict/真实实验；对应新增审计测试尚未落盘。** 当前测试仍是上一批58项的版本，不能拿其通过结果为新草稿背书。暂停SHA仅是快照，不是执行批准。

独立审查尚待闭合两点：

1. generic worker的`owner.close()`正常返回不能表述成逐个OS句柄闭合；底层NativeWorker/PendingLaunchCleanup尚未逐个检查CloseHandle返回，Popen自有process handle依赖对象生命周期释放，需明确报告口径，不改共享接口来凑当前结果。
2. `evidence_unknown`若仅从有预算的unknown_reasons列表推导，resource原因先填满后可能丢失后续evidence分类；建议独立sticky evidence flag，再补预算截断测试。固定实验目前对象数低于预算，但此点尚未修复/复验。

没有执行后续fresh r2/r3 native实验，没有触碰旧profile。原文件/网络/停止/宿主崩溃/独立普通用户/干净系统等门槛仍按原交接保留，暂无VM或备用机；不新建系统用户或改变全局设置。

## 再次获用户明确继续后的入口

1. 先核对`data/validation/v06/resume-20261018/pause-inputs.json`的14个SHA，检查适用指令与活动Agent，不覆盖工作区。该JSON记录暂停时间00:19:32+08:00。
2. 由Sol补完ACL审计草稿与上述审查项、真实失败路径mock，冻结后交Luna双Python纯mock/strict。可组合ACL、readonly-registry、memory、limits、V3 builder五测试检查共享导入/fixture交互；需新basetemp、日志/JUnit、外层timeout、起止SHA，保留所有首次失败。
3. 主Agent检查通过后，才经工具权限流程在**新profile**进行有记录的有限对照，保存actual与expected精确差异；旧profile禁用。未定位差异前不更改exact gate、critical ACE或验收标准。
4. 阶段0的所有可达持久写区拒写/有硬总量限制、网络/资源/生命周期等门槛达标并独立审查后，才进入A整合/B自动规则。当前仍不满足。

暂停关键SHA：ACL新草稿`6dc773d04af256827de34cfc008e5add5413c8c103e4c84356c9eef2da86e6cf`；ACL测试仍`4778db47ab27e5c1206157b055245de04352d4e3bef373fa236ec8b747a2ebc1`；memory与builder等详见暂停JSON。未提交不代表源码或忽略目录中的证据不存在。
