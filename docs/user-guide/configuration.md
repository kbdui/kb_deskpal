# 配置与模型连接

优先通过 GUI“配置与数据维护”调整启动设置。进入维护前应用会关闭后台和桌宠；维护结束后重新启动应用。手工编辑配置也需要完整退出再启动。

## 配置从哪里读取

显式指定 `--config 路径` 时读取该文件。未指定时优先当前目录已有 `config.toml`，否则选择用户配置目录；Windows 为 `%LOCALAPPDATA%/Deskpal/config.toml`。GUI 缺文件进入向导；普通 CLI 没有默认文件时沿用离线默认值，不自动保存配置。

配置优先级为：**进程环境变量 > 配置目录的 `.env` > TOML > 内置默认值**。相对目录以配置文件所在目录解析。维护页区分文件值和生效值；修改 TOML 后仍未生效，通常需要检查环境覆盖。

完整字段见[配置样例](../../config.example.toml)。手工创建时复制为个人 `config.toml`，不要覆盖已有配置。样例和内置默认值不包含模型权重，也不会自动开启语义检索。

## 当前工作区的 BGE 配置

本工作区已下载并验证 `BAAI/bge-small-zh-v1.5`，模型位于 `data/models/bge-small-zh-v1.5`；仓库根目录的本地 `config.toml` 已生效：

```toml
[memory]
embedding_provider = "fastembed"
model_cache_dir = "data/models/bge-small-zh-v1.5"
```

这是当前本地配置，不改变下表中的内置默认值。相对路径以该配置文件所在目录解析；复制配置到其他位置后需重新核对模型路径。用已具备 embedding 依赖的 `deskpal_env` 启动，当前缓存无需再次下载；完整退出应用后，可执行[数据维护](maintenance.md)中的离线校验。

模型权重、实际 `config.toml` 和恢复副本不随 Git 提交。新克隆或另一台机器需自行准备依赖与模型，并在实际使用的配置中启用。BGE 仅用于本地语义检索，聊天 provider 仍由 `[llm]` 决定。

## 离线演示与云模型

离线配置：

```toml
[llm]
provider = "offline"
```

离线 provider 识别 `echo 内容`、`time`、`read 路径`、`write 路径 内容` 等固定命令，不是本地聊天模型。语义检索模型也不能替代聊天模型。

连接模型服务时，将下列设置合并到现有 `[llm]` 表中，替换服务地址和模型名：

```toml
[llm]
provider = "openai"
model = "替换为服务支持的模型名"
base_url = "https://你的服务地址/v1"
api_key_env = "OPENAI_API_KEY"
timeout_seconds = 30.0
expression_output_format = "prompt"
```

`openai` 是本项目兼容 Responses 协议的 adapter 名称。服务必须兼容该协议，不能仅凭“支持 OpenAI 格式”推定可用。外部服务地址使用 HTTPS，本机回环服务可用 HTTP；地址填 API 根路径，adapter 负责请求 `/responses`。保存配置不会联网验证凭据或模型。

`expression_output_format = "json_object"` 仅用于明确支持原生 JSON 的表达请求；默认保留 `prompt`。修改后仍需验证具体服务，离线测试不证明真实模型效果。

## 保存密钥

可选择以下任一方式：

- 进程环境：在启动应用的终端设置 `$env:OPENAI_API_KEY = '替换为密钥'`。
- 配置目录 `.env`：写入 `OPENAI_API_KEY=替换为密钥`。
- Windows 向导密码框：保存至当前用户系统凭据库，TOML 仅保存 `credential_ref` 引用。

环境或 `.env` 中的非空密钥优先于系统凭据。若使用其他变量名，令 `api_key_env` 与之相同；仓库 `.env.example` 的示例名是 `DEEPSEEK_API_KEY`，不会自动改成 `OPENAI_API_KEY`。不要把密钥写入 TOML、聊天或 Git。

清空向导密码输入框不会删除已存凭据；旧引用也不会自动删除。系统凭据库不可用时，改用环境变量。配置保存会保留 `.recovery-*` 原文副本；损坏配置的明确修复会重建支持的设置，其他旧设置重置，原文副本需单独保管。

## 目录和常用设置

| 配置项 | 默认值 | 用途 |
| --- | --- | --- |
| `sandbox.workspace_dir` | `workspace` | 文件工具允许操作的根目录 |
| `sandbox.max_file_bytes` | `1048576` | 文件工具大小限制，1 MiB |
| `storage.data_dir` | `data` | 四个 SQLite 库及执行日志 |
| `memory.mode` | `explicit_only` | 只显式记忆；`candidate_review` 启用候选审核 |
| `memory.embedding_provider` | `disabled` | 可选 `fastembed`，用于本地语义检索 |
| `memory.model_cache_dir` | `data/models/fastembed` | 检索模型缓存 |
| `agent.max_tool_steps` | `8` | 单计划工具步骤上限 |
| `workflow.queue_capacity` | `100` | 持久请求队列容量 |
| `proactive.enabled` | `false` | 本地规则提议开关 |
| `proactive.timezone` | `Asia/Shanghai` | 提议静默时段及当天偏好时间边界 |

常用环境覆盖包括 `DESKPAL_LLM_PROVIDER`、`DESKPAL_LLM_MODEL`、`DESKPAL_LLM_BASE_URL`、`DESKPAL_DATA_DIR`、`DESKPAL_WORKSPACE_DIR`、`DESKPAL_EMBEDDING_PROVIDER`、`DESKPAL_MODEL_CACHE_DIR`。环境变量名不全部按字段机械拼接，完整支持项以[配置加载代码](../../src/deskpal/config/__init__.py)为准。

云请求中的用户内容、合资格记忆和必要工具结果会发给配置的服务。GUI 默认 `local_only` 会阻止云规划，只有主动选择 `cloud_allowed` 才允许该次请求进入云链路；相关记忆、目标、会话等来源限制仍生效。详见[记忆与隐私](memory-and-privacy.md)。
