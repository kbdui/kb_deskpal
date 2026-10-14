# V0.1 阶段测试报告

## Phase 1：配置与执行日志

日期：2026-10-12。测试环境：Windows、Python 3.12.4、pytest 7.4.4。依据冻结的 `ARCHITECTURE.md`、`docs/versions/v0.1/interfaces.md` 和各包 DTO/Protocol；Python 3.11 尚未实际运行验证。

执行命令：

```powershell
python -m pytest tests/unit/test_config.py tests/unit/test_execution_log.py -q
```

结果：**59 passed in 0.66s**，无失败、无跳过。本轮未调用真实 API、未读取真实密钥或用户数据。所有配置与日志写入使用 pytest 临时目录；配置传入隔离的 environ。

| 文件 | 已验证行为 |
| --- | --- |
| `tests/unit/test_config.py` | env > TOML > 默认值；所有公开配置环境变量覆盖；路径以配置目录或当前目录解析；加载不创建数据目录；API模式必要配置；假密钥不进入repr/错误；offline不读取密钥变量；TOML拒绝凭据字段；数量与超时正值、有限数与类型边界；非法TOML、错误配置结构、缺失文件 |
| `tests/unit/test_execution_log.py` | 嵌套对象/数组/tuple凭据递归脱敏；保留中文、换行与非凭据业务数据；不修改调用方输入；UTC时间、有效UTF-8 JSONL与追加；同一日志实例80条/8线程记录完整且不丢失；非法事件与非JSON值拒绝；失败写入不产生部分记录；真实文件系统错误向调用方传播；NullExecutionLog |

已将架构阶段的 `tests/test_config_and_log.py` 拆分迁移到上述单元测试，避免同一场景重复维护。同步修正 `docs/versions/v0.1/test-plan.md`：依据各包models/ports；删除Memory get/metadata、Task过滤、Core start/stop和多轮模型重规划假设；明确单轮静态计划、Waiting重新提交、同步取消边界及无事务回滚。

未发现需要模块实现修复的缺陷。当前结果仅覆盖已交付配置与日志，不代表尚未交付的LLM、Tools、Tasks、Memory、Core或CLI已通过验收。日志测试不证明跨进程互斥，也不保证从普通文本中识别任意秘密；验收范围仍为凭据键脱敏。

## Phase 2.1：LLM Adapter

新增 `tests/unit/test_llm.py`，76项测试。HTTP响应全部由注入transport模拟，默认重定向策略通过替换build_opener、执行其HTTPRedirectHandler验证；没有真实网络请求。环境仍为Python 3.12.4、pytest 7.4.4。

| 行为 | 结果 |
| --- | --- |
| Responses出站POST、/responses路径、model、store:false、timeout、system/user输入、可信function schema | 通过 |
| assistant function_call与匹配function_call_output历史映射、普通generate结果 | 通过；此项为基础协议映射能力，不是推理模型native续接验证 |
| generate拒绝仅工具调用及文本混合工具调用，避免返回空字符串或部分文本 | 通过；主Agent审查修正后的公开接口回归 |
| message/output_text与function_call DTO解析、reasoning输出丢弃 | 通过 |
| incomplete/failed/error/refusal、未知类型、空输出、空白文本、缺失/重复call_id | 明确LLMError，通过 |
| 工具arguments必须字符串JSON对象；拒绝非对象、畸形、NaN/Infinity、重复键及嵌套重复键；响应JSON同样严格 | 通过 |
| 超时、URLError、HTTPError、注入传输异常的str(error)及格式化traceback异常链不含假密钥/原始HTTP正文 | 通过 |
| 3xx/4xx/5xx响应失败；不安全URL拒绝；默认transport不跟随重定向泄露凭据 | 通过 |
| 无效工具历史/schema在HTTP传输前失败 | 通过 |
| 离线echo/time/read/write、带空格路径、中文验收短语；未注册工具/非法命令不产生调用；明确demo标识 | 通过 |
| 基础历史反思、独立[REFLECT]+普通文本结果总结、旧工具结果不遮蔽后续新user | 通过 |

LLM阶段首次运行72项通过。架构复核指出offline旧工具历史遮蔽新user问题，LLM Agent随后修复；加入对应回归后73项通过，再加入独立文本Reflect检查。最终回归命令：

```powershell
python -m pytest -q
```

阶段回归曾通过133项；主Agent随后修正generate只接受无工具调用的文本响应，新增2项对应回归。最终累计结果：**135 passed in 0.67s**（基础设施59 + LLM76），无失败、无跳过。未发现新增待修复缺陷；没有更改LLM产品文件。

实际云端服务与真实推理模型未验证。V0.1不支持推理模型完整native多轮工具续接；Core必须独立文本Reflect，不重放首轮native历史。上述测试不能替代真实API兼容性认证。Tools、Tasks、Memory、Core、CLI阶段尚待交付。

## Phase 2.2：Tool Framework 与 Workspace Sandbox

新增 `tests/unit/test_tools.py`、`tests/unit/test_sandbox.py`，67项测试。所有注册与执行从公开导出的 `BuiltinToolManager`、`WorkspaceSandbox` 与 `register_builtin()` 接口验证，使用tmp_path内文件，不调用shell、不读用户文件。schema只验证已声明支持的子集，不要求完整JSON Schema实现。

| 行为 | 结果 |
| --- | --- |
| 显式内置注册/发现、未知工具与重复注册、tool_name/call_id补全、echo/time | 通过 |
| 必填/类型/未知参数、overwrite仅bool、integer/number拒bool、支持的array嵌套类型/数量/最低数值及enum | 通过 |
| 非JSON键、对象、NaN/Infinity参数拒绝，失败不执行工具 | 通过 |
| 注册来源与list_tools返回schema隔离，后续替换tool.spec不改变可信权限/schema | 通过 |
| Level 0/1/2权限，工具名白名单只在本次context生效；preflight和执行均检查，缺授权无文件/目录副作用 | 通过 |
| context workspace必须匹配；等价规范路径可匹配 | 通过 |
| UTF-8中文/emoji读写、按字节报告、空文本覆盖、默认不覆盖、显式覆盖 | 通过 |
| 12字节边界、超限写不损坏旧文件、缺失/非法编码/超限读失败；stat低报模拟文件增长后read仍有界 | 通过 |
| traversal/absolute/目录/Windows设备名/ADS、同前缀邻居、实际归属越界；拒绝路径不产生越界副作用 | 通过；归属越界额外使用模拟resolve结果验证，未声称代替真实symlink |
| 真实symlink越界读写 | 跳过：当前Windows创建symlink报OSError，winerror=1314，errno=22；未使用提权改变用户系统 |
| 执行/权限/参数失败审计、调用参数不受脱敏修改、错误无内部堆栈；注册与执行日志失败向外传播 | 通过；执行前审计失败不调用工具 |
| 原子overwrite替换失败保留旧字节并清理临时文件 | 通过 |

首轮3项失败：2项为测试假设错误（注册审计task_id可为None；故障logger也会在注册时失败），修正测试使其只约束请求事件并单独验证注册失败传播。1项揭示文档与实现的路径错误契约不一致：主Agent判定保留`PathViolation`向外抛，由Tools Agent修正工具内路径异常在失败审计后重抛；对应公开接口回归现已通过。Testing未修改产品文件。

阶段命令与结果：

```powershell
python -m pytest tests/unit/test_tools.py tests/unit/test_sandbox.py -q -rs
# 66 passed, 1 skipped in 1.16s
python -m pytest -q -rs
# 201 passed, 1 skipped in 1.84s
```

无待修复阻塞缺陷。symlink权限限制为明确未实测部分；测试不扩展为OS级隔离、跨进程资源限制或恶意插件安全保证。Tasks、Memory、Core及CLI仍待交付，原始阶段结果保留在上文。

## Phase 2.3：Task Manager

新增 `tests/unit/test_tasks.py`，33项测试，使用公开`InMemoryTaskManager(*, execution_log=None)`。按7个源状态分组，在每组内遍历目标状态验证全部49状态对，避免把重复转换逻辑展开成49个独立测试。依据主Agent澄清后的同态幂等语义，不要求后台抢占、并行执行或任务持久化。

| 行为 | 结果 |
| --- | --- |
| 七状态合法转换、非法转换保持原记录、终态不转移；同态返回隔离快照且不改timestamp/result/error、不重复审计 | 通过 |
| 唯一非空ID、创建顺序、UTC时间、get缺失返回None、update缺失抛KeyError、空白/非法请求不创建记录 | 通过 |
| 真实转换payload替换旧值，省略result/error清空；更新旧快照不被后续转换更改 | 通过 |
| 嵌套dict/list结果隔离caller输入、update返回、get、list及注入logger收到的审计data；frozen字段不可赋值 | 通过 |
| 拒绝非JSON对象、tuple、非字符串键、NaN/Infinity及嵌套无效值、循环；无效输入不改变原任务 | 通过 |
| bool/非TaskStatus值拒绝；Waiting不能恢复Running，可通过update_status取消 | 通过 |
| 创建与转换日志故障向外传播，尚未提交的任务/状态不出现；恢复logger后可正常转换 | 通过 |
| 审计task_id、状态序列、previous_status与当前状态一致 | 通过 |

初次针对性执行32项通过；根据已确认JSON子集增加tuple拒绝，并加强审计数据隔离/事件序列检查。最终回归：

```powershell
python -m pytest -q -rs
# 234 passed, 1 skipped in 1.85s
```

本阶段没有产品缺陷或待修复阻塞项。唯一跳过项仍为上一阶段真实symlink创建权限（WinError1314）；没有修改产品代码。Memory、Core、CLI待交付。

## Phase 2.4：SQLite Memory Manager

新增 `tests/unit/test_memory.py`，71项测试。使用已确认的`SQLiteMemoryManager(database_path: str | Path)`和公开save/search/delete/close/context接口；测试数据库在tmp_path，`:memory:`同时验证字符串与Path输入。不要求metadata、get、向量检索或LLM记忆评估。

| 行为 | 结果 |
| --- | --- |
| 五类MemoryKind、全部MemoryRecord字段往返、空ID生成有效UUID、已有ID替换不重复、frozen记录 | 通过 |
| SQLite关闭重开仍可查询，删除后重开不复现、缺失删除False | 通过 |
| 中文文字包含；`%`、`_`、反斜杠及组合按literal匹配；SQL注入式文本按普通文字检索、不匹配无关记录 | 通过 |
| created_at UTC降序、同时间id升序、kind/limit组合过滤与无匹配 | 通过 |
| search返回记录更新last_access_at，保留created_at；aware非UTC创建/访问时间归一化UTC且按实际时间排序 | 通过 |
| limit拒绝0/负数/bool/浮点/字符串；content拒绝空白或非字符串 | 通过 |
| importance/confidence有限[0,1]、decay_rate有限非负；拒bool/错误类型/nonfinite/越限值；允许合法边界 | 通过 |
| 创建/访问datetime拒绝naive与非法类型；非法kind与invalid replacement保持已有记录 | 通过 |
| close幂等、上下文异常仍关闭；closed save/search/delete/enter明确RuntimeError | 通过 |
| `:memory:`不创建字面文件、不同实例独立、正常CRUD | 通过 |

阶段与累计回归证据：

```powershell
python -m pytest tests/unit/test_memory.py -q
# 71 passed in 1.70s
python -m pytest -q -rs
# 305 passed, 1 skipped in 3.31s
```

本阶段未发现产品缺陷，无待修复阻塞项，未修改产品文件。累计唯一跳过仍为真实symlink的Windows权限限制；Core、Planner、Application和CLI尚未测试。环境保持Python3.12.4、pytest7.4.4；Python3.11和真实API未实际验证。

## Phase 3/4：Planner、Core、Application 与 CLI

新增`tests/unit/test_agent.py`、`tests/unit/test_planner.py`、`tests/integration/test_agent_flow.py`、`tests/test_cli.py`，最终66项。Core使用受控LLM/Planner/Memory与真实任务和工具验证边界；集成使用真实离线Provider及factory；CLI使用隔离子进程和console入口`main(argv)`，移除OpenAI/Deskpal/秘密环境变量后显式指定offline、临时workspace/data和UTF-8。

| 行为 | 结果 |
| --- | --- |
| 普通回复、run/handle_request一致、空白输入创建前拒绝、不保存全部chat、不调用无必要LLM反思 | 通过 |
| 相关记忆与可信工具元数据进入规划；一次有界规划；工具schema及结果参数隔离，非法/空输出、重复call_id与预算越限拒绝 | 通过 |
| 静态多步计划按序执行、工具结果完整传递；失败步骤停止后续步骤并保留已执行结果 | 通过 |
| 全计划预检、多个授权工具名去重、未知工具失败优先于Waiting；Waiting零工具执行/零文件副作用；授权重新提交产生新任务且不跨请求 | 通过 |
| 授权列表在请求入口复制为白名单，规划回调修改调用方list不会改变已捕获授权 | 通过 |
| 独立Reflect的system `[REFLECT]`与user普通文本JSON含目标及真实结果；tools为空，无role=tool/assistant.tool_calls历史 | 通过 |
| 空Plan/空Reflect失败；反思提出新工具视失败且不执行，不伪报完成 | 通过 |
| 明确`记住：/记住:/记住 空格`事实保存SEMANTIC；成功工具摘要保存EPISODIC；普通对话不全量存储 | 通过 |
| LLM/规划/记忆查询/保存错误转Failed；反思/记忆失败保留已执行工具结果；路径越界Failed而非Waiting | 通过 |
| rawtasks规划后/工具后取消在执行边界生效；首次事实save后取消阻止后续摘要save；Cancelled终态payload不被Core覆盖 | 通过 |
| 正式cancel_task Waiting取消、同态幂等、未知/终态/closed拒绝；audit失败不提交取消；执行中正式取消审计与request_cancelled结果完整 | 通过 |
| 观察Cancelled的审计故障返回可见error并保留Cancelled，不静默伪报正常；次生failure audit不覆盖原始LLM错误 | 通过 |
| Completed状态审计及request_completed审计两处失败均不提交成功任务 | 通过 |
| close/with资源所有权、回调只一次、closed拒绝、同线程重入/并发run及run中close拒绝且无额外任务 | 通过 |
| 真实离线中文验收→echo→Reflect→记忆/日志；write Waiting→授权write→read；factory关闭后记忆重开而任务不持久化 | 通过 |
| CLI `python -m deskpal` request/json/list-tools/config相对路径；Completed0/Failed1/Waiting2；重复authorize；交互EOF/quit且无隐含写权限 | 通过 |
| CLI request/list-tools互斥、authorize只随单次request，非法配置或参数明确失败且不创建执行工作目录 | 通过 |
| console目标main(argv)返回退出码与完整UTF-8 AgentResult JSON | 通过；pytest调用console目标函数，生成的deskpal.exe另由下文主Agent发布包验收 |

首批Core/Planner测试的2项预期修正：反思工具调用应Failed，授权去重场景必须使用不同非空call_id。两者均为测试契约假设而非产品缺陷。空输出、记忆取消边界及新增正式取消审计采用Lead/架构审查后的最终公开契约；测试Agent未修改产品文件。

针对性首轮59项通过，追加正式取消5项后64项通过，再补两处完成审计和授权快照边界。最终完整回归（包含Lead提取共用JSON snapshot后的实现）：

```powershell
python -m pytest -q -rs
# 371 passed, 1 skipped in 10.03s
```

阶段累计：基础设施59、LLM76、Tools/Sandbox66通过+1跳过、Tasks33、Memory71、Core/Planner/集成/CLI66，共**371通过、1跳过**。没有待修复阻塞缺陷。

未实际验证的部分：Python3.11运行、真实云端API与推理模型服务、Windows实际symlink（创建权限WinError1314）。推理模型native多轮续接、OS进程隔离、不可信插件、跨进程日志互斥与后台抢占不在V0.1保证范围。测试使用Python3.12.4/pytest7.4.4，不发送真实请求、不读取真实密钥、不改变用户数据库或Git提交。

## 最终发布包与独立复核证据

主Agent在最终代码上独立运行完整suite，结果为**371 passed, 1 skipped in 9.86s**；与测试Agent前述完整回归计数一致。主Agent另完成mypy严格检查（`--disallow-untyped-defs`，39个src文件）、Python3.11 AST语法检查及compileall，均通过。Python3.11语法检查不代表Python3.11解释器实跑，实际Python仍为3.12.4。

主Agent还以ignored脚本`data/package_validation/check_architecture.py`检查39个源码模块的显式依赖无环，并将每个模块放在fresh subprocess中逐个导入，全部通过。这是额外架构证据，区别于pytest在同一进程中的功能验证。

已构建`dist/deskpal-0.1.0-py3-none-any.whl`（43182字节），并以pip离线`--target data/package_validation/site-packages`安装。主Agent使用生成的`site-packages/bin/deskpal.exe`执行已安装包验证：cwd在`data/package_validation`，PYTHONPATH仅指向安装目录而非src。测试Agent读取验证脚本核对其断言及隔离导入路径，成功结果来自主Agent执行报告。

验证脚本`data/package_validation/verify_package.py`覆盖：

- 中文“帮我执行一个任务”返回Completed、退出0，实际echo结果为“离线演示任务已执行”。
- 未授权write返回Waiting、退出2且无文件；显式授权后创建新task并写入中文；read返回真实中文内容。
- 越界read返回Failed、退出1；显式事实保存成功。
- `deskpal.__file__`确实位于离线安装目标；关闭后SQLite重新打开仍有SEMANTIC事实和EPISODIC摘要。

该验证脚本与运行数据位于ignored目录，未作为测试源码或用户数据提交。至此源码测试、已安装wheel导入和生成console执行路径均有通过证据；无未修复阻塞缺陷。最终明确限制仍为真实Python3.11运行、真实API服务和实际symlink权限验证。
