# Personal AI Agent Desktop Companion — V0.2

Windows 个人智能 Agent 桌宠项目的本地后端。V0.2 在 Agent Core、Planner、模型适配、任务、记忆与日志基础上，增加本地日历提醒、非阻塞计时器、文件搜索/分类/复制/移动/删除、需授权的截图，以及确认原计划的接口。Python 3.11+，后端以标准库为主，`.env` 读取使用 `python-dotenv`。

当前入口是命令行；Live2D、Qt UI、人格、情绪和高级自主行为属于后续版本。

## 安装与运行

在仓库根目录执行：

```powershell
python -m pip install -e ".[dev]"
# 截图可选依赖；不影响其他工具：
python -m pip install -e ".[dev,screenshot]"
deskpal --request "帮我执行一个任务"
deskpal --request "echo 你好" --json
deskpal --list-tools
```

也可使用 `python -m deskpal`。不安装项目时，在当前 PowerShell 中设置 `$env:PYTHONPATH = "src"`，再运行 `python -m deskpal --request "帮我执行一个任务"`。

默认 Provider 是明确标识的 `[离线演示]`，不需要密钥或网络。它采用固定规则演示普通回复和工具计划，不具有真实模型的自然语言理解能力。验收请求会创建任务、规划并实际执行 `echo` 工具，再返回结果。

## 基础工具与授权

| 工具 | 离线演示输入 | 权限 |
| --- | --- | --- |
| echo | `echo 文本` | 自动 |
| time | `time` | 自动 |
| read_text | `read note.txt` | 自动，仅工作区 |
| write_text | `write note.txt 文本` | 本次请求显式授权 |

```powershell
deskpal --request "write note.txt 你好"
# Waiting，未执行工具；同意后重新提交为新任务：
deskpal --request "write note.txt 你好" --authorize write_text
deskpal --request "read note.txt"
```

路径含空格时，离线命令中的路径使用双引号，例如 PowerShell 参数 `'read "my note.txt"'`。写入默认不覆盖；直接工具 API 可显式传入 `overwrite=True`。整个计划先检查授权，缺少任何授权时不会部分执行。

一次请求的 CLI 退出码：Completed 为 0，Failed 为 1，Waiting 为 2。授权不跨请求保存，模型不能自行提升权限。

## V0.2 工具与原计划确认

工厂默认注册 19 个工具：4 个基础工具、5 个 Calendar 工具、4 个 Timer 工具、5 个 Document 工具及截图工具。全量 schema 可用 `--list-tools --json` 查看。

| 功能 | 工具 | 权限 |
| --- | --- | --- |
| 日程 | calendar_create / calendar_update / calendar_delete | Level 1 |
| 日程查询与提醒记录 | calendar_list / calendar_events | Level 0 |
| 计时 | timer_create / timer_cancel | Level 1 |
| 计时查询与到期记录 | timer_list / timer_events | Level 0 |
| 文件检索与扩展名分类 | document_search / document_classify | Level 0 |
| 单文件复制 | document_copy | Level 1 |
| 单文件移动、删除 | document_move / document_delete | Level 2 |
| 本地 PNG 截图 | screenshot_capture | Level 1 |

`--tool` 使用确定的工具计划，也经过 Core、参数校验、权限、Sandbox 和日志。以下为 PowerShell 7 示例，不需要模型规划或真实 API：

```powershell
python -m deskpal --tool document_classify --json
python -m deskpal --tool timer_create --arguments '{"duration_seconds":60,"label":"休息"}' --authorize timer_create --json
python -m deskpal --tool timer_events --json
```

单次命令退出会停止调度线程；运行中的计时器和日程保存在 `data/scheduling.sqlite3`。重开后的首次轮询补记漏到期事件，退出期间没有系统通知。events 工具仅查询持久化事件，不消费；Calendar 和 Timer 各保存自己的 `after_id` 游标。创建任务 Completed 只表示登记成功，不表示倒计时已结束。定时器到期不自动执行任意工具或 shell。

交互模式可直接输入 `tool 工具名 JSON`。例如先运行 `python -m deskpal`，输入 `tool timer_create {"duration_seconds":60,"label":"休息"}`；系统展示具体 Waiting 计划与任务 ID，再输入 `/confirm 实际任务ID` 执行同一原计划。高风险操作使用 `/confirm-high-risk 实际任务ID`，取消使用 `/cancel 实际任务ID`。API 同样可用：

```python
first = agent.run("写入 note.txt")
confirmed = agent.confirm_task(first.task_id, authorized_tools=("write_text",))
```

确认不调用 Planner、不创建新任务，返回预览数据被修改也不能改变私有计划。移动/删除只接受 `high_risk_authorized_tools` 或单次 CLI 的 `--authorize-high-risk`；普通 `--authorize` 不能放行 Level 2。授权只在该任务内累积，不跨请求保存。关闭或重启后不能恢复内存 Waiting 计划；重新提交请求仍会创建新任务并重新规划。

文件只操作工作区内普通文件，不跟随符号链接/Junction，不递归删除。分类按扩展名，不读取内容或自动整理。复制/移动默认不覆盖；跨模块失败不承诺回滚。截图支持 1-based 物理屏幕和相对该屏幕的区域坐标，PNG 受文件字节与像素上限约束；只返回路径和尺寸，不自动上传图像。完整参数与时区规则见 [V0.2 接口](docs/versions/v0.2/interfaces.md)。

## 配置与 API 模式

`config.example.toml` 给出所有配置项。可复制为本地 `config.toml` 后通过 `--config config.toml` 加载。优先级是进程环境变量 > 配置目录的 `.env` > TOML > 默认值；相对路径以配置文件所在目录为准，未指定文件时以当前目录为准。读取 `.env` 不改变全局环境，也不对值进行变量插值。

真实模型模式须明确指定可访问的模型，密钥可从进程环境变量或本地 `.env` 提供。`api_key_env` 是变量名称，不是密钥值。DeepSeek 示例 `config.toml`：

```toml
[llm]
provider = "openai"
model = "deepseek-flash"
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"
```

将 `.env.example` 复制为 `.env`，仅在本地填写 `DEEPSEEK_API_KEY=` 后的值，然后运行：

```powershell
python -m deskpal --config config.toml --request "调用 time 工具查询当前 UTC 时间"
```

这里的 `openai` 表示兼容的 Responses 协议适配器，服务商由 `base_url` 决定。[DeepSeek 官方 Responses 支持](https://api-docs.deepseek.com/guides/responses_api/)

OpenAI 服务也可直接使用进程环境变量：

```powershell
$env:DESKPAL_LLM_PROVIDER = "openai"
$env:DESKPAL_LLM_MODEL = "<你账户可访问的模型名称>"
$env:OPENAI_API_KEY = "<你的 API 密钥>"
deskpal --request "查询当前时间"
```

适配器使用 [OpenAI Responses API 的工具调用协议](https://developers.openai.com/api/docs/guides/function-calling)，请求设置 `store: false`；Core 通过统一 `LLMProvider` 接口工作。默认 HTTP 传输拒绝重定向，服务地址须为 HTTPS，HTTP 仅允许本机回环。真实 API 模式会把请求、选中的记忆和必要结果发送给配置的服务。

CLI 不会自动切换或授权工具。模型端点、超时、工作区、数据目录和计划步数见 [基础接口](docs/versions/v0.1/interfaces.md)，新增调度/截图配置见 [V0.2 接口](docs/versions/v0.2/interfaces.md)。密钥禁止写入 TOML，真实配置、环境文件、数据库和日志默认不进入 Git。

## 结构与数据

```text
src/deskpal/
  agent/          请求编排与生命周期
  planner/        单轮有界工具计划
  llm/            模型协议与 Provider
  tasks/          七状态任务及隔离快照
  memory/         SQLite 基础记忆
  scheduling/     日程、计时与持久化到期事件
  tools/          工具注册、校验与派发
  sandbox/        可信工具权限和路径策略
  config/         TOML 与环境配置
  execution_log/  本地 JSONL 审计
  application/    依赖装配和 CLI
tests/            单元、接口与集成测试
docs/             当前开发与验收文档
doc/              原始设计和交接文档
```

默认文件工具根目录为 `workspace/`，数据目录为 `data/`。记忆持久化到 SQLite，日志使用 JSONL；任务状态只保存在当前进程内，重启不会恢复运行中的任务。文字查询提供基础检索，向量检索及自动遗忘留待扩展。

## 验证与开发文档

全部文档入口见 [文档导航](docs/README.md)。版本资料已按 `docs/versions/v0.1`、`v0.2` 归档；下一阶段的 [V0.3 环境评估](docs/planning/v0.3/environment-assessment.md)、[开发计划](docs/planning/v0.3/development-plan.md) 与 [验收边界](docs/planning/v0.3/acceptance-boundaries.md) 为规划草案。

```powershell
python -m pytest -q -rs
python -m mypy src --ignore-missing-imports --disallow-untyped-defs
```

默认测试使用临时数据、离线 Provider 和模拟 HTTP，不访问真实密钥或网络。当前验收见 [V0.2 测试报告](docs/versions/v0.2/test-report.md) 和 [V0.2 交付说明](docs/versions/v0.2/delivery.md)；V0.1 历史证据见 [原始测试报告](docs/versions/v0.1/test-report.md) 和 [补测报告](docs/versions/v0.1/followup-test-report.md)。

真实 API 测试须显式启用，最多 3 次请求，每次限制 128 个输出 token 并关闭推理。仅用于测试进程加载 `.env`：

```powershell
$env:DESKPAL_RUN_LIVE_API_TESTS = "1"
$env:DESKPAL_LLM_MODEL = "deepseek-flash"
$env:DESKPAL_LLM_BASE_URL = "https://api.deepseek.com"
python -c "from dotenv import load_dotenv; load_dotenv(override=False, interpolate=False); import pytest; raise SystemExit(pytest.main(['tests/integration/test_live_api.py', '-q', '--tb=no']))"
Remove-Item Env:DESKPAL_RUN_LIVE_API_TESTS
```

强制 echo 的真实用例验证协议与本地执行，不宣称完整自主规划或所有模型能力已获认证。

- [总体架构](ARCHITECTURE.md)
- [V0.1 开发计划与角色分工](docs/versions/v0.1/development-plan.md)
- [V0.1 基础接口契约](docs/versions/v0.1/interfaces.md)
- [扩展与开发约定](docs/guides/development.md)
- [V0.1 测试计划](docs/versions/v0.1/test-plan.md)
- [V0.2 环境准备评估](docs/versions/v0.2/environment-assessment.md)
- [V0.2 开发计划](docs/versions/v0.2/development-plan.md)
- [V0.2 接口契约](docs/versions/v0.2/interfaces.md)

## 当前边界

Sandbox 是可信内置工具的权限与路径策略，未提供操作系统进程隔离、强制超时或不可信插件防护。工具执行失败不会回滚已经完成的其他步骤。

Reflect 使用独立请求总结显式工具结果，不继续执行总结阶段提出的新工具。推理模型完整的原生多轮工具续接尚未实现。真实 API 服务验证与离线测试分别报告。日志按凭据键脱敏，不记录内部推理；普通文本中的秘密仍应由调用方避免写入。
