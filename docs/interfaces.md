# V0.1 同步接口契约

本文规定 Phase 1 的 DTO/Protocol；后续实现不能各自发明相似但不兼容的参数名。所有业务接口同步，时间为有时区 UTC `datetime`。跨工具/LLM/日志的 `Any` 值必须是 JSON 可序列化数据（对象键为字符串，拒绝 NaN/Infinity）。dataclass 为 frozen，但内部 Mapping 不自动冻结；注册工具时须复制参数 schema，`list_tools()` 返回与内部隔离的数据，不能允许调用方修改可信注册信息。

## Agent 与 Planner

```python
Agent.run(request: str, *, authorized_tools: Iterable[str] = ()) -> AgentResult
Agent.handle_request(request: str, *, authorized_tools: Iterable[str] = ()) -> AgentResult
Planner.plan(request: str, *, memories: Sequence[MemoryRecord] = (),
             tools: Sequence[ToolSpec] = ()) -> Plan
```

`handle_request` 与 `run` 行为相同。授权白名单是当前请求的用户显式授权，必须转换为 `frozenset`，模型不可修改。空白输入应在创建任务前抛出 `ValueError`；运行过程业务失败使用 `AgentResult(status=FAILED, error=...)` 并记录任务。日志/持久化基础设施故障不能静默伪报成功。

Core 的 Reflect 必须使用独立新请求：把原始目标和显式工具结果作为普通文本发送，`tools=()`，不附首轮 assistant.tool_calls 或 role=tool 消息。总结只生成用户反馈，不追加执行计划。这样首轮 planning → 工具执行 → 独立总结不依赖推理模型的 native 续接状态。

`Plan(reply: str = "", steps: tuple[ToolCall, ...] = ())` 顺序执行，无 DAG/异步。计划长度不能超过 `config.agent.max_tool_steps`。`AgentResult(task_id, status, response, tool_results=(), required_authorizations=(), error=None)` 返回可显示文本和完整已执行工具结果。Waiting 的 `required_authorizations` 为去重工具名，并且 `tool_results` 为空：整个计划先预检权限。

Waiting 授权方案示例：

```python
first = agent.run("写入 note.txt")
# first.status == TaskStatus.WAITING
# 展示 first.required_authorizations，用户显式同意后：
second = agent.run("写入 note.txt", authorized_tools=("write_text",))
# second.task_id != first.task_id，新的请求重新规划，不自动续跑旧任务。
```

不得实现虚假的 resume，也不得在 Waiting 之前执行计划中的低风险步骤。规划中的未知工具直接失败；缺少确认的已注册工具才进入 Waiting。除权限预检外，每次 execute 仍需再次权限检查。

## LLM

```python
LLMProvider.chat(messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] = ()) -> LLMResponse
LLMProvider.generate(prompt: str) -> str
```

`ChatMessage(role, content, tool_call_id=None, tool_calls=())`：role 为 system/user/assistant/tool；tool 消息须携带对应 call_id。assistant 的 `tool_calls` 携带此前模型提出的调用，便于工具结果回传。`LLMResponse(content="", tool_calls=(), provider="", model="")` 没有厂商 JSON 或内部推理字段。

上述工具历史字段只提供基础函数调用协议转换；当前 API 适配器不支持推理模型完整的 native 多轮续接。推理模型工具调用可能要求把响应中的 reasoning items 与工具结果一同回传；当前 DTO 不含 reasoning，Provider 丢弃这些输出，不记录/持久化，调用者不得仅重放 ToolCall 并假定所有模型均可续接。V0.1 Core 使用前述独立 Reflect 请求；未来扩展需另行设计 Provider 内不透明的内存续接契约。

实现拥有协议转换：离线 demo 必须明确标识；OpenAI 提供者使用 Responses API `/responses`，请求设置 `store: false`，模型来自配置，function 参数与输出转换为上述 DTO。工具 schema 来源于可信 `ToolSpec`；模型参数必须解析为 JSON 对象；响应中重复/缺失 call_id、未知输出或畸形工具参数需明确处理，不能直接执行任意字符串。网络、HTTP、响应格式错误抛出不泄漏凭据的 `LLMError`。不将 HTTP 原始错误正文直接塞入日志。

API 工具协议参考：[OpenAI 官方 Function calling](https://developers.openai.com/api/docs/guides/function-calling)。适配器保持标准库可实现（urllib.request + json），LLMProvider 不依赖具体 HTTP SDK。

## Tool Manager、Sandbox 与 Tool

```python
Tool.spec -> ToolSpec
Tool.execute(arguments: Mapping[str, Any], context: ToolContext) -> ToolResult
ToolManager.register(tool: Tool) -> None
ToolManager.list_tools() -> Sequence[ToolSpec]
ToolManager.check_permission(call: ToolCall, context: ToolContext) -> None
ToolManager.execute(call: ToolCall, context: ToolContext) -> ToolResult
Sandbox.check_permission(spec: ToolSpec, context: ToolContext) -> None
Sandbox.resolve_path(path: str, *, for_write: bool = False) -> Path
```

`ToolSpec(name, description, parameters, permission_level=AUTOMATIC)`：parameters 为 JSON Schema object，权限来自注册对象。`PermissionLevel` 是 IntEnum，AUTOMATIC=0、CONFIRM=1、HIGH_RISK=2。`ToolCall(name, arguments={}, call_id="")`：模型或直接调用方提供的请求，不带权限。`ToolContext(task_id, workspace_dir, authorized_tools=frozenset())`：调用方可信上下文，workspace_dir 必须与策略根一致，不能让调用者指定路径绕过。

`ToolResult(success, output=None, error=None, tool_name="", call_id="")`：manager 将工具名和 call_id 补全。成功时 error=None；失败时 success=False 且 error 为可显示信息。未知工具抛 `ToolNotFound`；缺授权抛 `PermissionDenied`；路径越界抛 `PathViolation`（三者定义在 sandbox/errors.py）。参数校验/执行失败返回失败 ToolResult，manager 需记录失败，不泄漏原始内部异常堆栈。拒绝重复注册，注册后不可由外部改写 schema/权限。

V0.1 参数校验实现的是明确的 JSON Schema 子集：所有节点须声明单个 `type`，支持 object/array/string/integer/number/boolean/null；公共关键字为 description/title/enum/default；object 支持 properties/required/additionalProperties（bool 或子 schema），array 支持 items/minItems/maxItems，string 支持 minLength/maxLength，integer/number 支持 minimum/maximum/exclusiveMinimum/exclusiveMaximum。不支持 `$ref`、联合 type、anyOf/oneOf、pattern、format 等；未知关键字在注册阶段拒绝，不能静默忽略。default 仅为描述，不自动填充参数；可选字段默认值由可信工具自身处理。对象的 additionalProperties 未指定时遵循允许额外字段，内置工具显式禁止；参数与输出只允许有限 JSON 值，数组使用 list，循环引用拒绝。

路径策略拒绝的 `PathViolation`（包括不匹配的 workspace context）必须从 manager.execute 向外抛，由 Core 将其记录为失败，不能转成 Waiting。普通文件不存在、覆盖被禁止、UTF-8/大小限制及工具执行错误返回失败 ToolResult。每次权限判断与执行结果都审计；日志自身写入失败向外传播，不归类成普通工具失败，也不继续执行未开始的后续步骤。

| 内置工具 | 参数契约 | output | 权限 |
| --- | --- | --- | --- |
| echo | `text: str` 必填，无其他参数 | 输入字符串 | AUTOMATIC |
| time | 空对象，无参数 | UTC ISO 8601 字符串 | AUTOMATIC |
| read_text | `path: str` 必填，相对工作区 | UTF-8 文件文本 | AUTOMATIC |
| write_text | `path: str`, `text: str` 必填；`overwrite: bool=False` 可选 | `{"path": 工作区相对路径, "bytes_written": UTF-8字节数}` | CONFIRM |

只允许可信内置工具，参数按具体工具校验，不运行 shell/eval。文件大小上限来自 `SandboxConfig.max_file_bytes`，按字节计算；所有文件为 UTF-8。read_text 读取前后均须保障大小边界。write_text 默认为不覆盖，嵌套父目录可以在通过路径策略后创建；不能先创建越界目录后才检查。权限预检不创建文件或目录。路径策略拒绝逃逸与目录目标；Path.resolve + 实际目录归属检查应兼容 Windows 盘符大小写。详见架构安全局限。

## Task Manager

```python
TaskManager.create(request: str) -> Task
TaskManager.get(task_id: str) -> Task | None
TaskManager.update_status(task_id: str, status: TaskStatus,
                          *, result: Any = None, error: str | None = None) -> Task
TaskManager.list_tasks() -> Sequence[Task]
```

`Task(id, request, status=CREATED, created_at=UTC now, updated_at=UTC now, result=None, error=None)`。create 生成唯一 id；get 未找到返回 None，update_status 未找到抛 KeyError，非法状态转换抛 ValueError。返回 frozen 快照，不能修改内部状态。列表按创建顺序。更新传入的 result/error 为新值，默认清空；若需保留需显式传入。

允许转换：Created → Planning/Cancelled/Failed；Planning → Running/Waiting/Failed/Cancelled；Running → Completed/Failed/Cancelled；Waiting → Cancelled。终态 Completed/Failed/Cancelled 不允许再转换。同态更新是幂等例外：返回原记录的隔离快照，不修改 result/error、updated_at，也不重复写状态审计；真实状态转换仍按传入 payload 更新。V0.1 不允许 Waiting → Running，因为恢复方案是提交新任务。底层管理器取消使用 update_status(task_id, CANCELLED)，正式 App/UI 使用 AgentCore.cancel_task(task_id)。Core 在阶段、工具和记忆写入边界观察取消，保留已经产生的副作用；不能抢占正在阻塞的同步 LLM/工具调用。单进程同步任务管理不承诺后台调度、并行运行。

## Memory Manager

```python
MemoryManager.save(record: MemoryRecord) -> MemoryRecord
MemoryManager.search(query: str, *, limit: int = 5, kind: MemoryKind | None = None) -> Sequence[MemoryRecord]
MemoryManager.delete(memory_id: str) -> bool
```

`MemoryRecord(content, kind=EPISODIC, id="", importance=0.5, confidence=1.0, created_at=UTC now, last_access_at=None, decay_rate=0.0, task_id=None)`；importance/confidence 为有限 [0,1] 数，decay_rate 为有限非负数，content 不允许空白。空 id 保存时生成；已有 id 替换同 id 记录。MemoryKind 为 working/episodic/semantic/goal/relationship。

V0.1 SQLite 文字搜索，非向量语义检索；query 为空返回最近记录；非空按文字包含匹配，SQL LIKE 元字符按普通文字处理；kind 为可选过滤，limit 必须正整数。匹配结果以 created_at 降序、id 为稳定次序返回，并更新 last_access_at。delete 返回是否存在且删除。SQLite 使用参数绑定并支持重新打开数据库后读取。不要因冻结 DTO 而保留活动 sqlite cursor 供外部消费。

## 配置与日志

`load_config(path: str | Path | None = None, *, environ: Mapping[str,str] | None = None) -> AppConfig`。配置与字段定义在 `config/__init__.py`；`ConfigError` 代表文件/格式/类型/必需值失败。environ 用于可测试的隔离环境，None 时读取 os.environ。读取不创建工作目录；Application 负责创建 storage 路径。

| 配置字段 | 环境变量 |
| --- | --- |
| llm.provider/model/base_url/api_key_env/timeout_seconds | DESKPAL_LLM_PROVIDER / DESKPAL_LLM_MODEL / DESKPAL_LLM_BASE_URL / DESKPAL_LLM_API_KEY_ENV / DESKPAL_LLM_TIMEOUT_SECONDS |
| sandbox.workspace_dir/max_file_bytes | DESKPAL_WORKSPACE_DIR / DESKPAL_MAX_FILE_BYTES |
| storage.data_dir | DESKPAL_DATA_DIR |
| agent.max_tool_steps | DESKPAL_MAX_TOOL_STEPS |

llm.api_key 不可写入 TOML，加载后只来自 llm.api_key_env 指向的环境变量。offline 模式不读取 API key。配置中数量/超时必须正值，整数配置拒绝 bool/浮点数。错误信息和 repr 不包含凭据。

```python
ExecutionLog.record(event: str, task_id: str | None = None,
                    data: Mapping[str, Any] | None = None) -> None
NullExecutionLog()
JsonlExecutionLog(path: str | Path)
```

JSONL 每行 `{"timestamp": UTC ISO8601, "event": str, "task_id": str|null, "data": object}`。事件建议 request_received、task_status_changed、plan_created、permission_checked、tool_started、tool_completed、tool_failed、memory_saved、request_completed、request_failed；不同模块复用同一个注入实例。data 为调用者提供的显式业务 JSON 数据，禁止 reasoning/API headers/raw API body。嵌套对象与数组按凭据键递归脱敏，涉及 api_key、authorization、password、secret、access_token、refresh_token、credential、token。写入错误向调用方传播；本地磁盘失败不得静默丢弃重要审计。线程锁仅覆盖同实例，不提供跨进程锁。

## 实现边界与文件分工

Phase 1 拥有上述 models/ports、config、execution_log 和工程文件。后续实现顺序 LLM → Tools/Sandbox → Tasks → Memory → Planner/Core/Application。建议具体实现名字分别 OfflineLLMProvider/OpenAILLMProvider、BuiltinToolManager/WorkspaceSandbox、InMemoryTaskManager、SQLiteMemoryManager、LLMPlanner/AgentCore；模块具体实现可补充内部 helper，但不修改共享 DTO/Protocol 语义。API 实现不得把供应商类型引入 Core；UI 与本地模型以后只能通过 Protocol 或 Application 注入扩展。

## 已实现构造入口与资源所有权

```python
OfflineLLMProvider(model: str = "offline-demo")
OpenAILLMProvider(config: LLMConfig | None = None, *, model=None, api_key=None,
                  base_url=None, timeout_seconds=None, transport=None)
WorkspaceSandbox(workspace_dir: str | Path, *, max_file_bytes: int = 1048576)
BuiltinToolManager(sandbox: WorkspaceSandbox, *, execution_log=None)
InMemoryTaskManager(*, execution_log=None)
SQLiteMemoryManager(database_path: str | Path)
LLMPlanner(llm: LLMProvider, *, max_tool_steps: int = 8)
AgentCore(*, llm, planner, tasks, memory, tools, workspace_dir,
          max_tool_steps: int = 8, execution_log=None, on_close=None)
create_agent(config: AppConfig | None = None) -> AgentCore
```

Provider 的可注入 transport 是可信测试/适配边界；生产调用优先通过配置和 Application，API key 只从环境变量加载。SQLiteMemoryManager 支持 context manager 和幂等 close，关闭后拒绝业务方法。AgentCore 拒绝并发/重入 run，运行中拒绝 close；close 只调用明确传入的 on_close，不自行寻找并关闭所有依赖。工厂拥有 SQLite，设置 `on_close=memory.close`，组装失败也关闭连接；外部注入资源由调用者负责。

```python
from deskpal.application import create_agent
from deskpal.config import load_config

with create_agent(load_config("config.toml")) as agent:
    response = agent.run("echo hello")
    task = agent.tasks.get(response.task_id)
    agent.run("记住：我喜欢简洁的回复")
    facts = agent.memory.search("简洁")
```

`agent.llm/planner/tasks/memory/tools` 是注入依赖，供单进程调用方检查状态；任务存储不持久化。`python -m deskpal` 与安装后的 `deskpal` 调用相同 CLI，支持 `--config PATH`、`--request TEXT`、可重复的 `--authorize TOOL`、`--json`、`--list-tools`。request 与 list-tools 互斥；authorize 仅允许单次 request，交互模式每次请求默认无额外授权。退出码 Completed=0、Waiting=2、失败/取消=1、中断=130。JSON 输出包括 AgentResult 全部字段，status 使用枚举值。

`AgentCore.cancel_task(task_id: str) -> Task` 是正式取消入口，合法转换前写入状态审计，再调用 TaskManager；重复取消遵循幂等状态语义，未知任务/不允许取消的终态分别抛 KeyError/ValueError。该入口允许独立取消 Waiting 任务，无需再运行请求。工厂 TaskManager 使用 NullExecutionLog，Core 拥有请求状态审计；直接使用底层 `agent.tasks.update_status` 不保证额外审计，主要供管理和检查。Core 观察到正在处理的任务取消时记录 request_cancelled；取消审计失败也向调用方明确反馈。取消不关闭 Agent 或 SQLite 资源，资源仍由 create_agent/on_close 生命周期管理。

## Core 记忆与审计数据

V0.1 的显式记忆语法为 `记住：内容`、`记住:内容`、`记住 内容`（前后空白忽略），最多存储内容前 2000 字符为 semantic；无分隔符的 `记住内容` 不触发记忆写入。成功工具请求额外保存最多 2000 字符的 episodic 请求/结果文本摘要，摘要不是保证完整的 JSON 文档。普通聊天不自动存储。Observe 查询当前请求的文字匹配，若空则取最近五条。Memory Evaluator 和向量检索尚未实现。

Core 事件包括 stage_started `{stage}`、request_received `{request, authorized_tools}`、plan_created `{reply, steps:[{name, arguments, call_id}]}`、memory_saved `{memory_id, kind}`、request_completed `{status, result}`、request_failed `{error, tool_results}`；任务转换事件使用 `{previous_status, status, result?, error?}`，工具结果使用 `{success, output, error, tool_name, call_id}`。只审计显式业务数据，不记录推理或 API 原始内容。

Core 会把日志/持久化故障转为 Failed 结果并尝试写入 Failed 任务状态，且追加说明二次审计/终态保存失败。工具与记忆的既有副作用不回滚；不可将失败结果解释为未执行任何操作。只有 Waiting 的权限预检保证没有执行任何工具。
