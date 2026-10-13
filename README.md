# Personal AI Agent Desktop Companion — V0.1

Windows 个人智能 Agent 桌宠项目的基础后端。V0.1 提供 Agent Core、Planner、模型适配、七状态任务管理、SQLite 基础记忆、工具框架、权限与路径策略、配置及执行日志。Python 3.11+，运行依赖使用标准库。

当前入口是命令行；Live2D、Qt UI、人格、情绪和高级自主行为属于后续版本。

## 安装与运行

在仓库根目录执行：

```powershell
python -m pip install -e ".[dev]"
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

## 配置与 API 模式

`config.example.toml` 给出所有配置项。可复制为本地 `config.toml` 后通过 `--config config.toml` 加载。优先级是环境变量 > TOML > 默认值；相对路径以配置文件所在目录为准，未指定文件时以当前目录为准。

真实模型模式须明确指定可访问的模型，并从环境变量提供 API 密钥：

```powershell
$env:DESKPAL_LLM_PROVIDER = "openai"
$env:DESKPAL_LLM_MODEL = "<你账户可访问的模型名称>"
$env:OPENAI_API_KEY = "<你的 API 密钥>"
deskpal --request "查询当前时间"
```

适配器使用 [OpenAI Responses API 的工具调用协议](https://developers.openai.com/api/docs/guides/function-calling)，请求设置 `store: false`；Core 通过统一 `LLMProvider` 接口工作。默认 HTTP 传输拒绝重定向，服务地址须为 HTTPS，HTTP 仅允许本机回环。真实 API 模式会把请求、选中的记忆和必要结果发送给配置的服务。

CLI 不会自动切换或授权工具。模型端点、超时、工作区、数据目录和计划步数都可配置，完整字段见 [接口契约](docs/interfaces.md)。密钥禁止写入 TOML，真实配置、环境文件、数据库和日志默认不进入 Git。

## 结构与数据

```text
src/deskpal/
  agent/          请求编排与生命周期
  planner/        单轮有界工具计划
  llm/            模型协议与 Provider
  tasks/          七状态任务及隔离快照
  memory/         SQLite 基础记忆
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

```powershell
python -m pytest -q -rs
python -m mypy src --ignore-missing-imports --disallow-untyped-defs
```

测试使用临时数据、离线 Provider 和模拟 HTTP，不需要真实 API 密钥。实际执行环境、阶段结果和未实测部分见 [测试报告](docs/TEST_REPORT.md)。

- [总体架构](ARCHITECTURE.md)
- [V0.1 开发计划与角色分工](docs/V0.1_PLAN.md)
- [接口契约](docs/interfaces.md)
- [扩展与开发约定](docs/DEVELOPMENT.md)
- [测试计划](docs/TEST_PLAN.md)

## 当前边界

Sandbox 是可信内置工具的权限与路径策略，未提供操作系统进程隔离、强制超时或不可信插件防护。工具执行失败不会回滚已经完成的其他步骤。

Reflect 使用独立请求总结显式工具结果，不继续执行总结阶段提出的新工具。推理模型完整的原生多轮工具续接尚未实现。真实 API 服务验证与离线测试分别报告。日志按凭据键脱敏，不记录内部推理；普通文本中的秘密仍应由调用方避免写入。
