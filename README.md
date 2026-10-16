# Personal AI Agent Desktop Companion — V0.4

Windows 个人智能 Agent 桌宠项目的本地后端。当前版本 **0.4.0**：持久目标、本地提议、任务队列、步骤账本和重启审查；保留 V0.3 来源化记忆、候选审核、三字段画像与可选 CPU 语义检索。Python 3.11+。

当前入口为命令行；Qt/Live2D、人格情绪、后台云规划、完整多轮上下文属于后续版本。架构见 [ARCHITECTURE.md](ARCHITECTURE.md)，实际用法见 [V0.4 操作指南](docs/versions/v0.4/operations.md)和[接口](docs/versions/v0.4/interfaces.md)。

V0.5 已完成开工前[环境与依赖评估](docs/planning/v0.5/environment-assessment.md)、[开发计划](docs/planning/v0.5/development-plan.md)及[验收边界](docs/planning/v0.5/acceptance-boundaries.md)：计划加入 Windows GUI、托盘和任务/记忆/目标交互，当前仍为 0.4.0，尚未安装 Qt 或实现界面。

## 安装与运行

```powershell
conda activate deskpal_env
python -m pip install -r requirements-dev.txt -c constraints/windows-py312.txt
python -m deskpal --request "帮我执行一个任务" --json
python -m deskpal --list-tools
```

基础安装用 requirements.txt；截图和本地 embedding 分别用 requirements-screenshot.txt、requirements-embedding.txt。直接声明统一在 pyproject.toml，requirements 只引用分组；Windows 还会安装 tzdata。约束包含 Windows CPython 3.12 的 44 项观察版本，不是跨平台哈希锁。见 [依赖维护](docs/guides/dependencies.md)与 [Embedding 安装](docs/guides/embedding-setup.md)。

默认 Provider 是明确标识的离线演示，无需 key/网络；它用固定规则生成回复与工具计划，不具备真实模型的自然语言理解能力。也可用安装后的 deskpal 命令。

## 工具与确认

工厂注册 19 个工具：基础 echo/time/read_text/write_text、Calendar、Timer、Document 及授权截图。完整参数用 `--list-tools --json` 查看。路径仅限配置工作区，拒绝符号链接/Junction，不递归删除。截图依赖可选，PNG 不自动上传。

```powershell
python -m deskpal --request "write note.txt 你好" --json
# Waiting，零工具调用；退出后查看持久计划：
python -m deskpal --workflow task --id TASK_ID
# 用上一步真实 hash 确认原 Task：
python -m deskpal --workflow confirm --id TASK_ID --plan-hash HASH --authorize write_text
python -m deskpal --request "read note.txt" --json
```

交互模式支持 /confirm TASK_ID、/confirm-high-risk TASK_ID、/cancel TASK_ID。Level 1 使用普通授权；移动/删除等 Level 2 需要独立高危授权，普通 --authorize 不能放行。单次请求也可明确预授权工具：

```powershell
python -m deskpal --request "write another.txt 内容" --authorize write_text
python -m deskpal --tool timer_create --arguments '{"duration_seconds":60,"label":"休息"}' --authorize timer_create --json
```

同一进程同一 Plan 的授权可累积；重启清空普通/高危授权。恢复不会重放工具或自动补云。单次请求退出码 Completed=0、Failed/Uncertain=1、Waiting/NeedsReview=2（管理命令返回自身回执）。部分执行与未知步骤需人工复核，不能直接重跑原完整 Plan。

## 目标、本地建议与恢复

```powershell
python -m deskpal --workflow goal-create --title "整理项目" --criteria "用户检查目录并明确确认"
python -m deskpal --workflow goals
python -m deskpal --workflow tasks --limit 100
python -m deskpal --workflow proposals --state all
python -m deskpal --workflow diagnostics
```

Goal/Milestone 由用户显式创建、关联日程或 Task，并用 revision 修改/完成。Task 成功只形成进展证据，不自动完成目标。新 Goal/来源绑定默认 local_only；配置云 key、接受建议均不扩大来源用途。

主动默认关闭；显式启用 [proactive] enabled 后，仅按本地规则生成待查看 Proposal，不后台调用云、采屏或执行工具。首次开启从事件尾部开始，历史补读与来源库重建必须显式复核。每流每批最多 100 条、每轮最多 10 个新提议；TTL 从到期时间起算，静默只影响命令边界展示。

接受提议原子创建唯一 Task/Submission，随后仍检查来源用途和工具权限。在云模式中 local_only 意图会 NeedsReview；明确选择离线模式可本地处理。详细 Goal→绑定→提议→接受→用户完成证据示例见 [操作指南](docs/versions/v0.4/operations.md)。

任务/计划/步骤持久化到 workflow.sqlite3。Queued、Planning、Waiting 重启后先 NeedsReview；没有 attempt 的任务可 resume/重新确认。部分完成只复核，剩余意图另建任务；Started 无可靠结果则 Uncertain，人工复核不授予重试资格。

## 记忆与隐私

```powershell
python -m deskpal --memory remember --content "叫我小明" --local-only
python -m deskpal --memory list --history
python -m deskpal --memory profile
python -m deskpal --memory correct --memory-id ID --revision 1 --content "叫我小林"
python -m deskpal --memory forget --memory-id ID --revision REV
```

默认 explicit_only，普通聊天不自动入长期库；“记住：内容”在 LLM 前保存。candidate_review 须显式启用，候选仍须用户审核。画像仅 preferred_name、reply_language、verbosity。可选 cache-only FastEmbed 不自动下载模型；真实模型/检索历史证据见 [V0.3 报告](docs/versions/v0.3/test-report.md)。

遗忘先持久阻断，再删除 Memory 与明确派生工作副本；失败保持 Pending，启动先续办，完成前不展示/派发相关内容。独立 Goal/Calendar 不按相似文字删除；已写历史日志、备份、导出与外部/云端副本有独立保留范围。生产 JSONL 仅元数据，outbox 失败可重投同 event ID，绝不重放工具。

## 配置与数据

`--config config.toml` 显式选择配置；优先级：进程环境变量 > 配置目录 .env > TOML > 默认值。相对路径以配置文件目录为准，未指定配置时以当前目录为准。.env 按字面解析且不修改全局环境；密钥禁止写 TOML 或 Git。

API 模式使用兼容 Responses 协议的 adapter，provider=openai，model/base_url/api_key_env 按实际服务配置，密钥填写本地 .env。请求、合资格记忆与必要工具结果会发给所配置服务；本地存储不等于本地模型推理。配置项见 [config.example.toml](config.example.toml)，历史协议范围见 [V0.3 接口](docs/versions/v0.3/interfaces.md)。

```text
src/deskpal/
  application/   装配、实例锁、队列、恢复与 CLI
  agent/         Core 请求编排
  planner/ llm/  计划与模型接口
  workflow/      持久任务/计划/步骤、依赖、outbox、删除工作单
  goals/         Goal/Milestone/SourceBinding
  proactive/     本地提议与可靠事件消费
  tasks/ memory/ scheduling/ tools/ sandbox/ config/ execution_log/
tests/
docs/
doc/             原始设计资料
```

默认 data/ 内含 workflow.sqlite3、memory.sqlite3、scheduling.sqlite3、execution.jsonl；文件工具根为 workspace/。一个规范 data_dir 只允许一个生产实例。关闭超时会保留在用资源/锁，不强杀同步工具；程序退出期间无系统通知。

生产集成使用 `create_application`；`create_agent` 保留 V0.3 同步进程内任务契约，用于低层嵌入与兼容测试，不提供 V0.4 持久恢复保证。Git 提交由用户处理。

## 开发与验证

```powershell
python -m pytest -q
python -m mypy --strict src scripts/check_memory_api.py scripts/benchmark_memory.py scripts/benchmark_workflow.py
```

离线测试使用合成数据，不调用真实 API；真实 API 需新的单独授权。V0.4 实际证据见 [测试报告](docs/versions/v0.4/test-report.md)，交付范围见 [交付说明](docs/versions/v0.4/delivery.md)。[文档导航](docs/README.md)、[分阶段路线](docs/planning/roadmap.md)、[长期记忆/人格/情绪设计](docs/planning/companion-model/README.md)区分已实现与未来规划。
