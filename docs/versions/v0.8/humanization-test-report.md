# 拟人化增量集中测试记录

执行日期：2026-10-22；归档：2026-10-23。状态：**本轮集中执行已收尾；HM01–HM07 已执行的离线用例及最终 strict 通过，另有两项既有符号链接权限缺项；HM08 真实体验未通过且两轮未执行，整体不予验收通过。** 用户已授权“开始测试”；Git 由用户提交。依据[HM01–HM08 最小清单](../../planning/v0.8/humanization-minimal-test-plan.md)，不把历史 V0.8 测试结果作为本次证明。

## 冻结与环境

工作区为未提交的 H0–H5 实现及 H6 材料；每次执行记录源码、测试、脚本的前后 SHA-256，检查执行期间没有变动。证据位于忽略目录 `data/validation/v08/humanization-20261022/`，`selection.json` 保存本次文件清单，独立 label 保存命令、时间、退出码、stdout/stderr 与 pytest XML。首败不覆盖。

主环境 `D:/anaconda/Anaconda3/envs/deskpal_env/python.exe`：Python 3.12.4。兼容环境 `data/validation/py311/python.exe`：Python 3.11.17。两个 probe 实测均为 SQLite 3.53.4、Qt 6.12.0、pytest 9.1.1、pytest-qt 4.5.0、PySide6-Essentials 6.12.0、mypy 2.4.0。

离线执行使用独立 cwd、临时库和录制 Provider，清除继承的应用及凭据环境变量，Qt 使用 offscreen。真实配置和个人数据库不参与离线验收。

## 执行范围

- HM01–HM05：新增 domain/application 用例及原 companion 数学/预算/恢复断言，公共遗忘受理使用真实 ApplicationService；不以私有 Core 调用代替。
- HM06：新增 Qt 竞态用例及有界 100 轮关闭检查；既有 GUI 回归检查共享桥接与生命周期。
- HM07：真实 schema 1 迁移夹具、受影响的任务/工具/Memory/授权/恢复/Planner/CLI/GUI/presentation 回归去重运行一次；strict 检查源码及三个脚本。Python 3.11 只运行领域、迁移、关键公共入口、CLI 和 presentation 子集。
- HM08：以 6 场景各 4 轮为真实模型评审材料；预定 23 轮通过 ResponseComposer，S1 第四轮通过真实 LLMPlanner 验证双候选歧义澄清，均使用模型网关，实际请求数和未完成轮次见下文。表达侧复用 companion/conversation；工具事实和规划工具规格均为显式合成夹具，没有执行器，也不替代 Application 验收。关闭可选记忆评估，单轮脚本最多 24 次真实请求、不自动重试；成功生成仍需人工评分。

排除冻结的 V0.6 实验执行/Windows 诊断、性能基准、长测、安装、物理 DPI/托盘及新增桌宠表现。已有 symlink 权限 skip 单列，不作为通过。

## 实际结果

### 首轮

`main-offline`：**1298 passed / 43 failed / 4 skipped / 3 errors**，1348 节点，pytest 428.98 秒；无超时，前后源码哈希一致。`strict-initial`：132 文件、1 个错误。全部首次日志和 XML 保留。

首次问题和处理：

| 类别 | 归因与处理 |
| --- | --- |
| Windows 文件操作、迁移备份、junction | 沙箱内 `os.link`/文件发布及 `mklink /J` 返回 WinError 5/拒绝访问，引起写入任务连锁失败；产品安全断言保持，使用获准的沙箱外固定定向命令复测 |
| 新旧测试协议 | 旧 mock/断言仍识别 Reflect、单段回复、`memory/forget` 或工作库 schema 1；适配 Composer 的精确 JSON/事实摘要、新公共遗忘端点和 schema 2，保留 presentation schema 1/七字段与授权/次数检查 |
| 合成 profile 种子 | 显式 profile 文本不符合既有严格语法；改用合法自述，保留真实检索排序 spy 和候选审核断言 |
| 旧遗忘故障点 | 旧 public wait 现在必须拒绝旧 privacy epoch；“deletion_job_committed”注入点位于新增外层事务内，进程退出会回滚，移至公共隐私受理事务提交之后；不放宽遗忘清除要求 |
| GUI 控制响应 | `_ready()` 新增隐私查询使用 `BEGIN IMMEDIATE`，遇外部 SQLite writer 时在进入取消入口之前阻塞；加入持锁/closed 检查的只读连接上下文，仅用于该查询，保留发送、Started 和发布的事务屏障 |
| outbox 启动恢复审查 | 原用例靠新任务触发消费，未覆盖无新任务的重启；同一 worker 首次 claim 前维护，有界分页最多 4096 项且可被关闭/Pending 中止；强化两窗口重启用例并加入 101 项跨页检查 |
| 类型边界 | 动态中立检索返回 Any；校验实际 `RetrievalBundle` 后返回，未使用 ignore 压制 |
| 执行器 | 隔离 cwd 后 PYTHONPATH 缺少仓库根导致 `scripts` 导入失败，已补根路径；外层控制台 GBK 无法打印 emoji，已固定 UTF-8。子进程原始输出、真实退出码、XML 和 receipt 均完整，未覆盖 |

### 定向复测

`main-retest-1`：**428 passed**，0 失败/错误/跳过，pytest 182.11 秒。`strict-retest-1`：**132 文件通过**，15.016 秒。两批无超时，前后源码哈希一致。

范围由 `retest-selection.json` 冻结：受影响共享模块回归的 24 个完整文件，及尚未被其覆盖的 19 个精确首败/错误节点；去重后执行，不默认重复整套配置参数矩阵。后续批次不与首批通过数直接相加。

`environment-skips` 在获准权限下复测首批 4 个跳过项：**2 passed / 2 skipped**。两个 junction 通过；剩余 `test_documents.py:255`、`test_sandbox.py:64` 不能创建符号链接（WinError 1314），列为环境未验证，不算通过。

### Python 3.11 有限兼容

`py311-compat`：**102 passed / 1 failed**，103 节点、无跳过，pytest 116.29 秒，无超时，前后源码哈希一致。唯一失败为 `test_hm03_local_tool_affect_and_body_stay_local_after_cloud_reopen`：本地失败工具的 affect mode 实际 neutral，预期 focused。主环境两轮该项通过，但不能代替兼容验证。

合成库取证确认唯一 receipt 为 `event_time_in_future`，不是配置尚未完成或状态被重置。任务终态时间比 companion 的稳定进程时钟领先至少 4.57ms；Python 3.11 在该 Windows 环境的 wall/monotonic 时钟分辨率均为 15.625ms，原来固定 1ms 的序列化容差不足。取证保存在 `py311-clock-forensics.json`，未读取个人数据库。

窄修补仅改 `companion/runtime.py` 和领域测试：默认系统时钟的容差由 `wall_resolution + 2 × monotonic_resolution + startup_bracket` 计算，保留 1ms 下限；bracket 超过两时钟分辨率之和、分辨率不可用或不符合应用校准限时仍使用严格 1ms。额外 monotonic 分辨率覆盖测量 bracket 本身的量化误差。注入任一时钟保持原取样次数和 1ms 边界。不改终态时间、重置 anchor 或隐私 epoch；事件计算时间仍夹到实际 delivery，不把容差加到 TTL/预算。新增 16 个确定性节点覆盖实测领先值、容差内外、明显未来、异常启动停顿、无效分辨率、过期和重复投递。最终定向复测单独记录，不覆盖兼容首败。

### 最终修补验证

| 证据 label | 范围 | 实际结果 | 执行器记录耗时 |
| --- | --- | --- | ---: |
| `clock-main` | 主环境：新领域、旧 companion runtime、新 Application、旧 companion integration 四个文件 | **91 passed**，无失败/错误/跳过，退出码 0 | 20.110 秒 |
| `clock-py311` | Python 3.11：同四个文件，含原兼容首败和新增时间边界 | **91 passed**，无失败/错误/跳过，退出码 0 | 19.688 秒 |
| `strict-final` | `src` 与 Memory 检查、Memory benchmark、humanization live 三个脚本 | **132 文件通过**，退出码 0 | 6.922 秒 |

三份收据各记录 247 个源码、测试、脚本的前后 SHA-256，集合及内容一致，均无超时。最终批次覆盖最后改动的 runtime 和 live 脚本；未再执行整套 59 文件或重复发送模型请求。Git whitespace 检查通过。离线首轮、428 项定向复测、兼容子集与最终两批 91 项存在重叠，不能相加成为新的总通过数。

HM01–HM07 本次已执行范围内无剩余失败，包含生产公共遗忘并发屏障、执行不变性、云用途与预算、可靠效果恢复、GUI 竞态、迁移及既有系统回归。两项符号链接权限缺项仍独立列为未验证；offscreen Qt、有限 Python 3.11 子集和 100 轮功能检查不等于物理桌面、所有平台或长时间稳定性验收。

### HM08 真实模型

配置的实际模型为 `deepseek-flash`，输出上限 2048 tokens/次、45 秒超时，关闭可选 Memory evaluation，无自动请求重试；脚本使用临时库与合成内容。未保存/打印密钥、端点、HTTP 正文中的 reasoning 或个人数据库。厂商 usage 未由现有 DTO 提供，不能声称实际 token 数。

| 证据 label | 实际请求 | 结果 |
| --- | ---: | --- |
| `live-initial` | 0 | 沙箱创建临时状态库 PermissionError，未发送请求 |
| `live-permitted` | 12 | S1、S2 完成；S3 第四轮合法正文保留、无效候选整体丢弃，`response_signals_invalid` 后停止 |
| `live-s3-diagnostic` | 4 | 只复测 S3，并记录有界原始模型正文；全部返回合法结构。未修改模型提示/校验，因此不能据此声称原失败已修复 |
| `live-s4-s6` | 2 | S4 第二轮模型直接返回普通正文，违反 JSON 契约；`response_invalid_envelope` 后停止，S5/S6 未在该批发送 |
| `live-s5-s6` | 8 | 单独完成尚未执行的 S5、S6；工具状态保留、未知不重做，清空后不恢复记号或编造共同历史 |

累计 **26 次尝试**（含 S3 的 4 次人工指定复测），低于 48 次总上限；覆盖 22 个不同场景轮次，S4 第三/四轮因前一轮失败未运行。没有通过反复采样覆盖首败。**HM08 当前未通过，也未完成全部 24 轮，不给出六场景平均分通过结论。**

主 Agent 人工逐份阅读回复、候选和退化标记后评分如下（1–5 分；S3 原样本与复测分开，不择优替换）：

| 样本 | 相关性 | 连贯性 | 风格符合 | 情境适当 | 事实谨慎 |
| --- | ---: | ---: | ---: | ---: | ---: |
| S1 原 4 轮 | 5 | 5 | 4 | 4 | 2 |
| S2 原 4 轮 | 5 | 5 | 5 | 4 | 4 |
| S3 原 4 轮（含退化） | 5 | 4 | 4 | 4 | 4 |
| S3 取证复测 4 轮 | 5 | 5 | 4 | 4 | 5 |
| S4 仅前 2 轮 | 未完成 | 未完成 | 未完成 | 未完成 | 未完成 |
| S5 4 轮 | 5 | 4 | 3 | 4 | 4 |
| S6 4 轮 | 4 | 5 | 4 | 4 | 3 |

评分为有限样本的工程判断：S1 未确认规则及 S6 自加故事设定拉低事实谨慎，S5 对简明字段的重复解释偏长；结构格式与安全检查仍独立判断，不能靠均分抵消失败。

目前已见的体验/语义问题：

- S1 第三轮把此前待确定的建议补写为“单次最多 5 本、书券仅本人且当天有效”等既定通知规则，未经用户或工具证实；历史助手建议仍可能被模型扩写成已定事实。
- S2 第二轮明确“这一轮”，模型却提出 session 范围的偏好候选。未确认，未变成生效偏好；候选本身的范围理解仍不准确。
- S3 初轮第二步请求曾被标为 `focused/explicit_report`，复测变为 tentative；显示情境标签语义存在采样差异。纠正后 cue 已清除，文本能回到写通知任务。
- 结构化返回不稳定：有一次坏候选、一次完全无 JSON。前者保留合法正文，后者被校验器拒收；这是安全降级的证据，同时也是实际体验未验收的原因。原坏候选首次未采原文，无法追溯具体非法字段；后续有界采集已补上，不反推根因。

后续应独立收敛模型输出契约与候选范围/草稿事实处理，再复测受影响的 S1–S4；不通过降低校验、自动修复请求或追加情绪关键词表解决。当前不把这 26 次评审推广成其他模型、反讽/否定准确率或稳定性认证。

2026-10-23 用户授权后的改进、迭代首败、原生 JSON 通道及新验证另见[HM08 改进记录](hm08-improvement-report.md)。本文件保留上一轮原始结论，不覆盖为后续通过结果。
