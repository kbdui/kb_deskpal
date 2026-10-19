# V0.6 暂停检查点

保存日期：2026-10-19 14:57 后（Asia/Shanghai）。用户要求保存进度、暂停，并汇报已完成与未完成部分。开发及测试已停止；重新收到明确“继续”前不要恢复。

## 当前结论

产品仍为 0.5.0rc1。V0.6 停在阶段0 Windows 隔离原型；阶段0未通过，禁止正式 A/B 整合。新增诊断、mock 通过、进程退出0均不代表完整隔离或 V0.6 交付。

暂停时子 Agent 因额度限制已报错结束，已对相关任务发出停止操作，没有重新唤醒。新聊天不得假定其仍在运行。Git 未 stage/commit/push/reset；保留所有已有及新增未提交文件。暂停保存期间仅只读核对及保存检查点，没有执行新测试、编译或原生实验。

## 已完成的阶段成果

- IPC framing、最小 runtime、AppContainer/Job launcher 及若干生命周期失败路径已有原型和定向测试；尚未接入生产 Core/Factory。
- 已有 Windows 启动、部分文件只读、Job 停止/输出预算、registry 身份观察等真实证据，限于各自已记录范围。注册表 Package SID deny 曾未挡写；User SID deny 挡住选定操作但宿主恢复失败。旧失败对象均保持封存，问题不能称为已修复。
- 新 MIC 路线只降低挂起 Worker 到 Untrusted RID0，宿主仍 Medium8192。挂起降级和真实 native smoke 已通过限定验收；未改生产默认启动器。
- 本轮 `raise-il` 和 `disable-no-write-up` 各一次真实 Windows 实验均完成：Worker 的 `OpenProcessToken(0x88)` 成功，但 `SetTokenInformation(25/27)` 均返回0、错误1314。C运行前后均为 RID0、policy1，独立宿主回读一致；各95个 host token native calls、42次查询、1次 Resume，原始关闭/回收及 profile 删除确认。**宿主观测的降级前 Worker 为4096；不能把这个值当作 C 运行时 before。** 仅支持选定自身令牌操作的抗恢复结论，不证明所有 token/impersonation 路径。
- 已修复诊断收集器的早期证据丢失：第一帧已报告弱化成功后，后续字节/第二帧/日志/poll/关闭错误不能清除失败结论；原始读证据及独立 token readback 保留。双Python定向66项及strict通过，纯typing import调整后相关生命周期复验通过。
- 新只读 root binding helper 已实现：只从本轮自有 helper 直接 DuplicateHandle 导出非继承 READ_CONTROL HKEY；Low AC/effective thread 与 helper Medium 身份分开核验；nonce/ACK、Revert、partial output、一次性关闭、stdout 保全、两个有序 seed 读取 phase。双Python各51项mock、双strict通过，首轮16项typing错误及修复前后证据保留。**尚无该 helper 的真实 Windows 根导出实验。**
- V5 native registry C fixture/mock 源码已完成父审冻结：Low正对照、Untrusted create/open-write/delete负对照；固定随机合成名称及8字节数据；每次 mutation 前真实 token/SD/LABEL 门控；80调用硬上限，frame8192/stream8196/nodes1024/container64/单帧。37个mock case及2个预算断言仅已写，尚未执行。
- 既有独立 inner Job128MiB 总量限制已有真实匹配事件10和清理证据；不能据此声称生产 outer Job256MiB 独立验证通过。旧内存 peak 字段是 resume 前快照，相关时序更正已保留。

## 本轮关键证据与源码

所有以下路径相对仓库根；原始 ignored data 也必须保留，不能只迁移 Git 跟踪文件。

| 项目 | 路径/状态 |
| --- | --- |
| 暂停时22项机器指纹与缺失状态 | `data/validation/v06/pause-2026-10-19-1457-inputs.json` |
| 令牌恢复诊断 | `scripts/diagnose_appcontainer_token_regain.py`，SHA `5efa603b3bd598eed48210fa2290d7bbed967af3144e9f62ef19df75bb46a4d4` |
| 两次真实结果、完整外层返回、父验收 | `data/validation/v06/appcontainer-token-regain-20261019/{raise-il,disable-no-write-up}-fixed-runner-r1/`；外层 chunks `8caa68`、`4a5b4f`，均exit0 |
| 第一次实验已删除 profile | `deskpal.v06.07ba16a8130c4369bdd820a715fd491e` |
| 第二次实验已删除 profile | `deskpal.v06.57432f19700c4e97a7c92a80189f107b` |
| helper 源码/测试 | `scripts/diagnose_registry_integrity_binding.py` SHA `262ae0b879fbb3e09ca44f12e48ec7b3bc25aaac9da4b0b7374e8d8f6afe9fff`；`tests/unit/test_registry_integrity_binding.py` SHA `e4211d8087058136d16c24d208a302ff0cc727f59e4070d590486237f5fa1456` |
| helper 51项及strict原始证据 | `data/validation/v06/registry-integrity-binding-20261019/checks-r1/`；14个完整工具返回，含首次strict失败；`parent-review.json`及`result-summary.json` |
| V5 C源码 | `tests/fixtures/windows/native_registry_integrity_probe_v5.c` SHA `3f39f093e791e6fbfd3dec45277b3c8fb93585e3abf4a224373be148d89dd6cc` |
| V5 mock源码 | `tests/fixtures/windows/native_registry_integrity_probe_v5_mock.c` SHA `b83da65f1212b265f510931970809661a3f9d8e44e4e522fa725cb114a80c95d` |
| V5构建准备与失败证据 | `data/validation/v06/native-registry-v5-build-20261019/prep-r1/` |
| 注册表MIC driver未完成草稿 | `scripts/diagnose_registry_integrity.py`：仅部分observer/assessor，无完整实验流程/入口；对应 `tests/unit/test_registry_integrity_diagnostic.py` 不存在。`EXE_PIN="UNBUILT"`，不得运行或当成可用实验 |

## V5 构建的准确状态

父审批准的首次固定构建器 SHA `aa29900f9612ccebb8bd086ea602cf40f5fd46450e9697642d1a4ffbd5ae866d` 在**预检**返回exit1（chunk `59c1db`）。`build-output-r1/build-record.json` 的 records 为空，cl/link 未启动，没有 V5 EXE，也没有 mock 执行结果。

原因是新 `userenv.lib` pin 转录少一个 `a`：错误63字符值与机器实算不符。正确实际值是 `3cf6e0708c9cdc0e7fa8511aca4e63e28beea46c08d6b72c0677ea8a2eaa5e72`。不能改写首次记录为通过。

已发出的下一步准备方案尚未落实：创建新 `build_fixed_r2.py`，从原始机器记录注入正确值；OUT改为`build-output-r2`，FINAL_DIR改为`data/v06-stage0/native-registry-fixture-v5/build-r2`，保留旧失败及空目录。暂停核对时 r2脚本不存在。先静态机器核对所有pin长度64且逐项匹配、父审新diff/命令，才可执行新构建；编译成功并审mock import table后，另获父审才能执行mock。真实 V5 fixture始终需要另外方案/源码/runner审查及工具批准。

草稿driver当前FIXTURE仍写成`native-registry-integrity-fixture-v5/fixture.exe`，与受控构建路径不一致；后续必须统一为实际受审receipt路径，不能提供候选路径fallback，不能先填造假的EXE hash。

## 尚未完成与下一次恢复顺序

1. **注册表核心阻断仍未解决。** 未证明同一轮中 Worker 真拒写、可信宿主核验与安全清理同时成立。优先完成 MIC 最小实验，不恢复旧 User SID/DACL 覆盖矩阵。
2. helper还有一项刚批准但**未实现**的补强：在 after_low/after_untrusted 两phase只读核验本轮派生的 `attempt_<nonce>` 和 `disposable_<nonce>` 不存在。不能仅凭创建API返回AccessDenied就假定无残留副作用。每名每phase一次RC open，仅接受FILE_NOT_FOUND2+空输出；成功存在先存证据；其它/partial输出unknown，必要单次close，不枚举/写/删。初始absence与负对照后新出现应由driver锁存边界失败。现有51项结论只覆盖未补强版本；改后再交Luna定向验证。
3. 完成V5构建、mock及helper补强后，补完整MIC driver与mock/strict。一个fresh profile，两独立worker（Low正对照、Untrusted负对照）、独立Session调用预算；helper只向host导出RC，Worker新开自身HKEY。Low不能伪称发生lowering。独立token回读、实际关闭/reap、两次canary/SD/namespace核验、一次DeleteProfile均需真实证据。失败/未知留现场不重试。父report与两phase各512KiB预算、exclusive新文件方案已批准但尚未实现。
4. 继续文件完整写区/seeded对象/ADS/reparse/父DELETE_CHILD、网络正负证据、CPU实际饱和限制、生产outer Job总内存、宿主崩溃/kill-on-close/泄漏、同构建长测等缺口。旧TCP timeout、UDP接受但未到达、bind/listen成功**不算网络拒绝通过**。
5. 干净标准用户/独立Windows或跨主机环境仍缺。用户暂无备用机/VM，不自行创建用户、安装VM或调整全局安全设置；本机限定结果不冒充独立验收。
6. 阶段0经真实证据及父安全验收通过后，才整合V0.6A：Executor/Supervisor/Policy/Broker、持久attempt/Uncertain、精确授权与撤销/遗忘屏障、工具迁移/GUI。再实现V0.6B默认关闭、本地time/echo/精确文件只读规则。最终生产回归、迁移、安装、长测和继承UI验收均未完成。

## 必须保留的协作与安全约定

- 所有常规单元/mock/strict/回归/真实隔离执行及证据采集原则上交6 Luna高；父Agent负责复杂归因、安全设计、关键修改审批和最终验收；6.1 Sol高可实现关键安全代码。父级不重复已充分验证的常规测试。
- 所有需批准的请求使用中文；真实安全实验先父审冻结，再走工具所需批准；不要用中断pending approval探测批准状态，不因等待而重复执行。
- 未经批准不改测试预期/跳失败/降低权限限制。配置可回读不等于隔离有效，测试通过不等于安全验收通过。
- 仅fresh合成profile/key/canary；不读`.env`/私人配置/DB/密钥，不云调用/下载模型/安装依赖，不改全局ACL/防火墙/loopback。
- 旧失败profile、两个leaf key及旧只读runtime全部封存，不打开/复用/删除/修ACL/递归扫描。历史封存清单及其它原始证据继续按旧检查点保存。
- 不运行旧V2 memory的16GiB分支，不提交Git，不覆盖用户修改。全部关键raw工具返回完整保留，不能从stdout推断外层exit。
