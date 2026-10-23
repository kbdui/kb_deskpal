# HM08 改进实现与验证记录

日期：2026-10-23。状态：**本轮实现与确定性修补已收尾，离线复测通过；真实模型问题按用户要求记录并暂缓，HM08 未整体通过。** 依据[本轮实现与测试计划](../../planning/v0.8/hm08-improvement-plan.md)。[上一轮原始失败](humanization-test-report.md)保留，不覆盖、不择优替换。Git 由用户提交。

最终定向复测：主环境 **158 passed**、Python 3.11 子集 **67 passed**、strict **133 文件通过**；此前原生通道及受影响配置扩大回归为 628/225 项通过，各批重叠不累加。真实尝试共 **59 次**，没有自动重试或执行真实工具。Provider 无可用输出、表达约束偏差及缺少的场景验证见[暂缓项](hm08-deferred-issues.md)；停止追加请求，不宣称稳定或完整验收。本机原生格式试验覆盖已移除，恢复 prompt 默认，其他配置值不变。

## 实现与接口

- 新增 `response/protocol.py`：输出 JSON Schema、由同一定义生成的提示与局部结构校验；保留重复键、非有限数、深度/字节和工具调用拒绝。Composer 仍一次调用，未新增修复/重试。
- `ResponseDraft` 兼容增加默认空 `diagnostics`，由冻结 `ResponseDiagnostic(group, field, code)` 表示。正文、cue、偏好组独立处理，同组非法/重复/冲突整体失效；诊断只有主机固定字段和代码。Core 最多传播八项到既有 warnings，真实模型脚本记录同一信息；不默认记录生产原文。
- `review_preference()` 返回候选或无正文拒绝原因；原 `validate_preference()` 契约保留。有限明确命令限制范围和值，once 不产生保存候选；未覆盖的自然表达在明确范围与主体证据支持下仍可产生未确认候选，不要求所有措辞命中本地词表。profile 仍走原 Memory 审核。
- 偏好按证据所在分句绑定范围；同句第三人/引用/假设限制防止通过缩短 span 绕过。情境自述与任务指令区分，允许空 cue，纠正优先；主机有限规则不宣称证明否定、反讽等任意语义。
- `conversation.freeze()` 表达投影带用户引用、未确认助手 proposal、稳定 draft ID/version 及主机工具结果。工具执行记录不等于对文件正文内容的外部核验；自由文本不能自封 trusted。“采用上一稿”不会改写旧草稿的来源身份，明确补充信息仍保留在新的用户原话中。
- 表达投影的完整 JSON 元数据计入 16KiB，超限整轮省略；原中立规划投影、预算、hash 输入、来源闭包和用途限制保持。Composer 真实传递这些字段，新增测试覆盖生产入口。
- 审查修复同 key 复合范围的表达优先级：once 高于 session，高于 today；既有主机冻结 constraints 保留优先权，禁止项仍优先。

没有新增数据库 schema、运行依赖、后台模型请求、桌宠行为、工具权限或执行者。首轮保留原传输；提示模式再次失败后，才增加显式选择的原生 JSON 通道，能力验证和实际结果见下文。格式约束不代替语义、用途或授权检查。

## 验证安排与证据

新增测试文件：`tests/unit/test_hm08_response.py`、`test_hm08_signals.py`、`test_hm08_context.py`、`test_hm08_native_json.py`、`test_hm08_live_diagnostics.py` 和 `tests/integration/test_hm08_application.py`。分别验证协议组隔离、范围/来源、真实投影预算、原生通道/持久额度、错误脱敏和生产整合；旧测试适配协议 marker、assistant proposal 来源标签、有限策略和真实元数据预算，不放宽授权、事实和调用数断言。

证据目录为忽略的 `data/validation/v08/hm08-20261023/`。`selection.json` 冻结初始主环境十三文件、Python 3.11 五文件和 strict 范围；增加原生通道后按下文扩大相关配置检查。独立 label 保留 stdout/stderr、退出码、XML、时限及源码前后 SHA-256。测试使用临时合成数据和独立 cwd，不接触个人数据库；固定执行由 6 Luna 高思考承担。真实样本由主 Agent 逐项阅读，不以脚本生成成功代替验收。

### 首轮

- `main-initial` 与 `py311-initial` 均在收集阶段退出码 2，各 1 个 collection error、无超时，尚未执行行为断言。新 `test_runtime_once_wins_in_both_orders_then_session_remains` 错用 pytest 保留的参数名 `request`；修正为 `expression_request`，不改行为断言或业务代码。首败日志/XML 保留。
- `strict-initial`：**133 个文件通过**，退出码 0、无超时。
- 三批源码/测试/脚本的前后哈希一致；修复夹具前等待所有独立批次结束，避免执行中改动。

### 离线复测

`main-retest-1`：**335 passed**，pytest 19.08 秒；`py311-retest-1`：**141 passed**，pytest 2.93 秒。均退出码 0，无失败/错误/跳过、无超时，前后源码哈希一致。两个解释器的集合重叠，不能相加成为去重通过数。

主 Agent 静态审查要求补足纯范围前缀被逗号分段的正向证明（例如 `This session, please be brief.`、`今天，请不要开玩笑`）。Sol 确认合法情形已有 pending 机制支持，同时发现分隔符间混入句号/分号/换行时可能跨句继承，已补邻接核验及正反例测试，随后再做受影响复测。真实复测另增加 R1 两轮未确认草稿与选择性确认挑战，单独记录。

`main-final`：**203 passed**，pytest 18.19 秒；`py311-final`：**145 passed**，pytest 2.79 秒。`strict-final`：**133 文件通过**。三批退出码 0、无超时，前后源码哈希一致。该次只复测范围语义、表达协议及相关 Application/companion 入口，未重复不受影响的 Planner/provider/原始上下文单测；相应证据仍在 `main-retest-1`。通过数存在重叠，不累加。

## 真实模型首轮及继续改进的判断

仍使用实际配置的 `deepseek-flash`，2048 输出 tokens 上限、45 秒超时，无可选记忆评估和自动重试。`live-main` 六场景全部完成，24 次调用；`live-r1` 两轮全部完成，2 次调用。每轮一次请求，无降级/异常，源码前后哈希一致；累计 26 次新请求。`generated_pending_review` 仅表示材料生成，不能自动判为通过。

人工阅读确认：S2 本轮要求未再生成 session 候选，today 候选范围正确；S3 自述产生短期支持 cue，普通任务不产生自述标签，纠正后已清除；S4 朋友/小说/否定主体没有固化为用户状态，小说回复以合法 JSON 返回；S6 清空后未知，未再编造背景或共同经历。R1 两轮分别省略旧草稿中的数量/日期/券规则，并在用户只确认“五本”后只采用该数量，其他条件仍待定。

仍需改进：S1 通知改写把助手方案中的逾期不配对、预约超时转普通区、分区安排等写成既定规则，没有标明通知草案/待确认身份；S5 补充文字继续逐字段复述主机摘要，较长。S3 开头模板使用未提供的“同事”称呼，也提示文稿应保留未知条件。故没有仅凭结构全通过或平均分宣布完成，继续一轮有明确目标的修补。

下一轮将有限表达策略（详略、幽默、追问）作为主机生成 JSON 传入 Composer，减少仅依赖自然语言风格提示的歧义；文稿草案身份在正文中明确，保留用户新确认的具体信息。S5 改用 concise 预设验证简短模式，不与首轮 warm 设置混为同条件比较。另补真实 freeze→compose 的临界预算测试：投影新增的固定来源字段必须在 freeze 预算阶段已经计入，避免局部历史超过 16KiB。该修补不新增模型调用或改变规划/权限。

### 第二次修补与再次首败

`iteration2-main`：266 passed / 1 failed；`iteration2-py311`：171 passed；`iteration2-strict`：133 文件通过。主环境唯一失败是旧隐私断言禁止 payload 出现 `humor_allowed` 字段名；新字段是固定公开 preset 的 normal/false/false 策略，完整请求与修改本地私有设置前 baseline 精确相同的断言已经通过，`supportive` 也没有外发。保留原不干扰断言，改为精确检查有限策略的值，`iteration2-compat-retest` 原节点 **1 passed**。没有为修补测试修改用途许可。所有批次无超时、前后哈希一致。

随后 `live-iteration2` 在 S1 第一轮返回普通正文，固定诊断 `envelope / json_invalid`；累计发送 1 次后脚本停止，退出码 1，哈希一致。没有执行其余场景或重试。至此本轮累计 **27 次**请求，说明仅靠协议提示仍不能稳定满足格式，不能把前 26 次成功当作已解决格式问题。

据[OpenAI 官方文档](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)，Responses 支持 `text.format={"type":"json_object"}`；它约束 JSON 格式，不证明本地 schema 和事实语义，自定义端点也必须独立验证。下一步增加默认关闭的显式配置与受控可选 Provider 能力，仍复用 expression 根任务额度、发送/回包屏障和工具禁令；无自动格式回退或修复。新通道需要全场景验证，因此明确调整本轮总上限为 60，原 27 次全部计入；能力验证也计数，不隐藏为传输重试。

### 原生通道实现与离线验证

已实现 `LLMConfig.expression_output_format`（默认 prompt，显式 json_object）、可选 `NativeJSONProvider`、Provider 原生传输、Gateway 仅 expression 的共享额度路径、Composer 与真实评审 capture wrapper 的能力传递。普通 chat/generate、Planner 和 Memory evaluation 不附加新格式参数。额外生产集成测试使用真实 Provider 加注入 transport，核对只有 expression 的 HTTP payload 带原生格式，并且重复 wait 不再发送。

新增 `test_hm08_native_json.py` 覆盖配置、传输、用途、旧 Provider、持久额度重启、超时、隐私 guard、输入/工具/DTO 边界。最终扩大相关配置/Provider/Planner/Core/Application 回归：`native-main` **628 passed**（21.89 秒），`native-py311` **225 passed**（4.41 秒），`native-strict` **133 文件通过**。均退出码 0、无超时、前后源码哈希一致。与先前批次重叠，不累加通过数。实际端点验证另行记录。

`native-probe`：独立一次合成请求，端点回显 `json_object`，返回内容通过本地严格 envelope，退出码 0，5.188 秒，哈希一致。累计 28 次。此证据表明本次实际端点接受并确认该参数，不推广到其他端点/模型。

能力验证成功后，曾仅在本机忽略的 `config.toml` 添加 `llm.expression_output_format="json_object"`；更新脚本通过 TOML 对象比较确认其他配置值不变，没有输出或复制凭据。代码及示例配置默认仍为 prompt；其他安装应验证自己的端点后显式启用，应用重启后读取设置。随后使用产品原生能力链路评审 S1–S6 与 R1，实际未能全部完成，结果见下文；暂缓收尾时已移除本机试验覆盖。

`live-native-main` 在 S4 第四轮（第 16 次请求）由 Provider 抛 `LLMError`，5.438 秒即结束，未取得可解析回复；前 15 次合法，脚本立即停止、没有启动 R1，哈希一致。原记录仅保留异常类型，不能反推具体 HTTP/处理故障，也不能宣称已修复该外部错误。累计 44 次。随后仅给评审脚本增加主机固定错误消息白名单分类，不保存任意异常正文；人工明确指定复测 S4 并补未执行 S5/S6/R1，最多 14 次，不重新发送已完成的 S1–S3。

`live-native-recovery` 在 S4 第四轮再次由 Provider 抛 `LLMError`，前三轮合法，实际 4 次，累计 **48 次**。当时白名单仍不完整，仅能记录 `provider_error`；没有取得原始可解析正文，不能判断为截断、拒绝、工具输出、格式错误或网络问题。该次停止，没有运行 S5/S6/R1。

随后执行独立 `s4-diagnostic`：无请求重放 recovery 已记录的前三轮，只发送第四轮一次；结果合法 envelope、无 cue/候选/降级，源码哈希一致，累计 **49 次**。诊断原计划只收集本地固定错误原因，该次未复现故障，故没有具体归因。成功样本只补齐该轮内容审查，不证明前两次故障已修复，更不把不同时次拼接成一次完整通过。

评审脚本补全固定 `provider_failure_reason()` 分类，覆盖无可用输出、拒绝、不完整、未知内容/输出类型、坏 DTO、工具禁令及预算等。只对 `LLMError` 的单个原生字符串参数作精确白名单匹配，不调用任意异常的 `str()`，未知值始终为 `provider_error`。新增离线验证包含带合成凭据/正文的未知错误、相似前后缀、非 LLM 异常、多参数和禁止序列化异常；产品接收与重试规则没有改变。该分类不追溯改写旧证据。

`diagnostics-main` **41 passed**，`diagnostics-strict` **133 文件通过**，均退出码 0、无超时、前后源码哈希一致。随后 `live-native-remaining` 仅发送 3 次，在 S5 第三轮停止，固定原因 **`no_usable_output`**；不是可解析的空 `reply_text`，不能当作合法弃答。前两轮仍复述工具名及原始 output/error 文本，简明补充未达标。累计 **52 次**；S6/R1 未在该批执行。这个原因只定位本次失败，不能反推先前 S4 的错误。

### 工具补充契约调整

前述 S5 表明“必须再说一句”的接口会鼓励重复权威摘要，因此补上正常弃答能力。Composer 根据非空工具结果选择固定输入角色 `tool_supplement`，否则为 `direct_answer`；模型不能输出角色或自行选择接收规则。共享 schema/示例按主机角色生成，解析器新增默认关闭的 `allow_empty_reply`。只有工具补充的精确空字符串是合法弃答，直接回答、纯空白、坏结构/信号和 Provider 无输出仍按原规则拒绝或降级。

Core 对有效空补充直接展示原 `tool_summary`，不追加空“补充说明”、不误报生成失败，合法 cue 仍受原来源审核；真实工具输出和错误完整传入模型，不删除字段，也不截断生成文字。新增生产测试检查实际工具只执行一次、表达只调用一次、持久额度不刷新、重复 wait 不发送；空直接回答和 Provider 无输出的原回退单独验证。该改动不声称修复端点偶发无输出；后续真实场景仍独立判定。

`supplement-main` 首次 **230 passed / 5 failed**，`supplement-py311` **72 passed / 4 failed**，`supplement-strict` 133 文件中 1 项类型错误；均无超时、前后哈希一致，未继续网络请求。四项 response 失败涉及原提示契约：详略指导需恢复明确的 detailed 分支，另一个来源断言不应依赖句首大小写。第五项为真正兼容回归：旧非结构化空 Reflect 被误判为合法弃答，改变旧 Core 的失败结果。修补限定合法弃答只在结构化工具 envelope；纯文本依然拒绝空串，旧测试原失败断言保留。新 schema 示例补 `dict[str, Any]` 类型标注；复测另记，不覆盖首败。

### 最终离线复测

| 证据 label | 范围 | 结果 |
| --- | --- | --- |
| `supplement-retest-main` | 协议/上下文/生产 Application、Core 及脚本诊断五文件 | **158 passed**，退出码 0 |
| `supplement-retest-py311` | 新协议与生产 Application 两文件 | **67 passed**，退出码 0 |
| `supplement-retest-strict` | src 与三个既有检查脚本 | **133 文件通过**，退出码 0 |

均无超时、前后源码哈希一致；旧 `empty reflect` 失败断言原样通过。前批通过的原生 Gateway/配置、旧 Application 与 v08 integration 不重复；其结果仍保留在 `native-main`、`native-py311`、`supplement-main` 等批次。新诊断测试还以真实 Provider 加合成 transport 跑评审 main，证明合法空工具补充通过，而 Provider 无输出只尝试一次即失败。不同批次通过数重叠，不相加。

### 最后真实验证与人工判断

| 证据 label | 实际尝试 | 结果 |
| --- | ---: | --- |
| `live-supplement-s5` | 2 | 第一轮合法；第二轮 `no_usable_output`，停止该场景，第三/四轮未执行 |
| `live-final-s6` | 3 | 前两轮合法；清空上下文的第三轮 `no_usable_output`，第四轮未执行 |
| `live-final-r1` | 2 | 两轮完成；没有把未确认日期/券规则改为事实，仅采用用户确认的五本数量 |

三批均前后哈希一致，没有重试。失败类别来自 JSON **顶层** `failure_reason`，样本内只有异常类型；不能因样本字段缺失而漏报顶层分类。新增加 7 次，本轮最终累计 **59 次**（24 + 2 + 1 + 1 + 16 + 4 + 1 + 3 + 2 + 3 + 2），少于最后上限 62。剩余额度不再使用。

主 Agent 逐条阅读后的五维判断如下，仅列已完整生成的相应材料，不把跨版本或跨批次样本拼成一轮六场景通过：

| 材料 | 相关性 | 连贯性 | 风格符合 | 情境适当 | 事实谨慎 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `live-native-main` S1 四轮 | 5 | 5 | 4 | 4 | 4 |
| 同批 S2 四轮 | 5 | 5 | 5 | 4 | 5 |
| 同批 S3 四轮 | 5 | 5 | 4 | 4 | 5 |
| `live-final-r1` 两轮挑战 | 5 | 5 | 4 | 5 | 5 |

S4 没有一次原生完整场景成功；S5、S6 最后版本未完成，**不给完整场景分或总均分通过结论**。最新 S5 成功样本没有复制原英文字段名，但仍重复摘要含义，不能证明简明问题完全解决。最新 S6 首轮在 `followup_allowed=false` 下仍附“需要……直接说即可”的邀请，风格约束仍有偏差；第三轮在清空后的生成失败也不能算遗忘体验通过。R1 的草案身份与选择性确认符合预期，但第二轮附加说明偏多，不以局部成功抵消其他问题。

## 收尾与暂缓决定

用户随后明确要求难题先记录、搁置，因此停止新的真实请求及语义修补。已经实现的协议、范围/来源、有限策略、预算与工具补充能力保留；没有改用固定套话、放宽校验、截断输出或隐藏重试。Provider 无输出的具体结构尚未知，已准备的结构诊断脚本未执行（0 请求）。[HD01–HD03](hm08-deferred-issues.md)记录证据、未完成项与后续入口。

本机 `config.toml` 的试验性 json_object 覆盖已移除，恢复原 prompt 默认；`restore-format.json` 记录仅该字段变化，TOML 比较确认其余配置值不变，0 模型调用。原生能力仍可显式启用，但本轮不推荐把端点接受参数视为稳定性证明。原 prompt 模式也有已记录格式失败，恢复默认不等同宣称它通过。

本轮交付不关闭旧符号链接环境缺项、V0.7 未决问题，不恢复 V0.6，不新增桌宠表现，不调整版本号，也未执行 Git 提交。**实现与离线验证收尾；真实体验未验收，按用户要求暂缓。**

最终只读检查：十份变更 Markdown 的 204 个本地链接均存在，Git whitespace 和十个未跟踪源码/测试/文档的尾随空白检查通过；config 与验证证据仍被忽略、暂存区为空。当前 254 个 src/tests/scripts Python 文件的哈希与最后 strict、R1 证据完全一致，无缺失或变动；没有在运行的 Python/Pythonw 进程，结构诊断输出文件不存在。该检查没有执行 pytest、mypy、模型或诊断脚本。
