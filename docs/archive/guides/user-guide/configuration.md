> 历史存档：保留当时的设计、失败与验收边界；不代表当前实现或新的执行授权。当前入口见 [文档导航](../../../README.md)。

# 配置与模型连接

通过“设置 → 模型与连接”配置模型与密钥，通过“设置 → 数据与高级”进入维护。需要独立维护时，应用先等待后台、图片和小窗完整结束，再切换和重新启动。本轮源码重构仍待集中测试，状态见[开发记录](../../gui-refactor/development-status.md)。

## 配置从哪里读取

显式指定 `--config 路径` 时读取该文件。未指定时优先当前目录已有 `config.toml`，否则选择用户配置目录；Windows 为 `%LOCALAPPDATA%/Deskpal/config.toml`。新安装默认云模型，缺配置可进入设置或先浏览本地内容，发送在连接就绪后启用；不自动保存配置，也不使用示例回复伪装真实模型。

配置优先级为：**进程环境变量 > 配置目录的 `.env` > TOML > 内置默认值**。相对目录以配置文件所在目录解析。维护页区分文件值和生效值；修改 TOML 后仍未生效，通常需要检查环境覆盖。

完整字段见[配置样例](../../../../config.example.toml)。手工创建时复制为个人 `config.toml`，不要覆盖已有配置。样例和内置默认值不包含模型权重，也不会自动开启语义检索。

## 当前工作区的 BGE 配置

本工作区已下载并验证 `BAAI/bge-small-zh-v1.5`，模型位于 `data/models/bge-small-zh-v1.5`；仓库根目录的本地 `config.toml` 已生效：

```toml
[memory]
embedding_provider = "fastembed"
model_cache_dir = "data/models/bge-small-zh-v1.5"
```

这是当前本地配置，不改变下表中的内置默认值。相对路径以该配置文件所在目录解析；复制配置到其他位置后需重新核对模型路径。用已具备 embedding 依赖的 `deskpal_env` 启动，当前缓存无需再次下载；完整退出应用后，可执行[数据维护](../../../user-guide/maintenance.md)中的离线校验。

模型权重、实际 `config.toml` 和恢复副本不随 Git 提交。新克隆或另一台机器需自行准备依赖与模型，并在实际使用的配置中启用。BGE 仅用于本地语义检索，聊天 provider 仍由 `[llm]` 决定。

## 云默认、本地模型与离线演示

普通聊天使用设置中选定的连接，不逐条询问本地/云。已明确配置的旧 offline 或连接保留原选择。需要本地推理时在设置中切换本地，填写已部署的本机回环端点；当前仅支持兼容 Responses 的服务，不负责安装或启动本地模型。仅支持 Chat Completions 的服务尚不可用。本地模式禁用环境代理与重定向，不回退云服务。

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
execution_location = "cloud"
protocol = "responses"
credential_mode = "environment"
request_use_default = true
model = "替换为服务支持的模型名"
base_url = "https://你的服务地址/v1"
api_key_env = "OPENAI_API_KEY"
timeout_seconds = 30.0
expression_output_format = "prompt"
```

`openai` 是本项目兼容 Responses 协议的 adapter 名称。云模式使用 HTTPS；本地模式设 `execution_location="local"`，仅接受本机回环地址，可用 HTTP，无鉴权本地服务可设 `credential_mode="none"`。地址填 API 根路径，adapter 请求 `/responses`。保存只做本地校验；“测试连接”另行明确触发固定公共文本请求，可能产生少量费用。保存、已生效和连接验证成功是三个状态。

`expression_output_format = "json_object"` 仅用于明确支持原生 JSON 的表达请求；默认保留 `prompt`。修改后仍需验证具体服务，离线测试不证明真实模型效果。

## 保存密钥

可选择以下任一方式：

- 设置密码框（默认）：选择 `system_vault`，经独立后台保存至当前用户 Windows 凭据库，TOML 仅保存 `credential_ref` 引用。
- 外部环境（高级）：选择 `environment`，在启动终端设置 `$env:OPENAI_API_KEY = '替换为密钥'`，或在配置目录 `.env` 中设置该变量。

凭据模式决定使用系统凭据还是环境，不在某个来源缺失时悄悄换另一个。旧配置没有模式字段时按旧规则迁移，保留实际环境优先级；环境对配置字段的显式覆盖仍会提示。若使用其他变量名，令 `api_key_env` 与之相同；仓库 `.env.example` 示例 `DEEPSEEK_API_KEY` 不会自动改成 `OPENAI_API_KEY`。已有密钥不回填，不写入聊天、TOML 或 Git。

密钥操作明确选择保留、替换或移除；清空本次输入不等于删除凭据。替换/移除在配置发布成功后清理旧引用，失败保留待清理状态。系统凭据库不可用会报错，不降级明文；可自行选择外部环境模式。配置保存仍保留原文恢复副本；损坏配置的明确修复会重建支持的设置，原文副本需单独保管。

## 目录和常用设置

| 配置项 | 默认值 | 用途 |
| --- | --- | --- |
| `sandbox.workspace_dir` | `workspace` | 文件工具允许操作的根目录 |
| `sandbox.max_file_bytes` | `1048576` | 文件工具大小限制，1 MiB |
| `storage.data_dir` | `data` | 四个 SQLite 库及执行日志 |
| `memory.mode` | `automatic` | 自动学习及模型复核；`paused` 暂停写入，不设置人工候选审批 |
| `memory.embedding_provider` | `disabled` | 可选 `fastembed`，用于本地语义检索 |
| `memory.model_cache_dir` | `data/models/fastembed` | 检索模型缓存 |
| `agent.max_tool_steps` | `8` | 单计划工具步骤上限 |
| `workflow.queue_capacity` | `100` | 持久请求队列容量 |
| `proactive.enabled` | `true` | 主动检查开关，使用已配置模型；工具权限仍单独落实 |
| `proactive.timezone` | `Asia/Shanghai` | 主动聊天静默时段及当天要求的时间边界 |

常用环境覆盖包括 `DESKPAL_LLM_PROVIDER`、`DESKPAL_LLM_MODEL`、`DESKPAL_LLM_BASE_URL`、`DESKPAL_DATA_DIR`、`DESKPAL_WORKSPACE_DIR`、`DESKPAL_EMBEDDING_PROVIDER`、`DESKPAL_MODEL_CACHE_DIR`。环境变量名不全部按字段机械拼接，完整支持项以[配置加载代码](../../../../src/deskpal/config/__init__.py)为准。

保存云连接时说明后续普通新输入将由该服务处理；`request_use_default` 控制新请求默认用途，聊天不再设逐条开关。既有仅本地来源、长期记忆、目标和类别许可仍有效，选择云不追溯放宽它们。本地 embedding 与云聊天设置相互独立。详见[记忆与隐私](../../../user-guide/memory-and-privacy.md)。

Phase 05 提供聊天窗口右上角“通知”快捷入口，分别暂停／恢复主动聊天和提醒；也可在聊天中说明期限。此控制不暂停执行任务、不删除日程，也不修改 Windows 通知权限。任务暂停通过任务卡控制，显示“正在暂停”后在下一次模型判断前生效；点击继续沿原任务处理，已完成的操作保留。
