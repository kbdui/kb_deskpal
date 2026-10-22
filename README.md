# Personal AI Agent Desktop Companion — V0.5 候选版

Windows 个人智能 Agent 桌宠项目。当前版本 **0.5.0rc1**：在既有持久任务、来源化记忆、目标与本地提议后端上，加入 Qt Widgets 主窗口、后台交互、准确授权、托盘与通知。Python 3.11+。实机验收缺项见 [V0.5 状态](docs/versions/v0.5/delivery.md)，不宣称全部版本门槛已通过。

提供 GUI 与命令行入口；Live2D、后台云规划和跨启动完整聊天档案仍属后续范围。V0.8 原人格/情绪实现的最小测试见[历史报告](docs/versions/v0.8/test-report.md)。2026-10-22 后续拟人化增量已实现至 H5：有界会话、可控表达、当前情境候选和并发遗忘，H6 材料完成后已按用户授权集中测试，**真实模型体验尚未验收通过**。实现见[开发记录](docs/versions/v0.8/humanization-development-status.md)，首败、修补、定向复测及限制见[本轮测试报告](docs/versions/v0.8/humanization-test-report.md)；历史测试不覆盖新代码。架构见 [ARCHITECTURE.md](ARCHITECTURE.md)，GUI 基础用法见 [V0.5 操作指南](docs/versions/v0.5/operations.md)和[接口](docs/versions/v0.5/interfaces.md)，CLI 继续参考 [V0.4 操作指南](docs/versions/v0.4/operations.md)。

V0.5 [开工前评估](docs/planning/v0.5/environment-assessment.md)与[计划](docs/planning/v0.5/development-plan.md)保留为规划快照；实际证据见 [测试报告](docs/versions/v0.5/test-report.md)。

V0.7静态图片桌宠已实现，2026-10-21按用户要求带未决项[归档](docs/versions/v0.7/delivery.md)，未全部验收通过。主窗口设置可启用原创机器人及有限表情差分，支持拖动、显示/隐藏、置顶和鼠标穿透；原生混合DPI、安装及30分钟运行已有证据，长测预览诊断失败与内存增长归因仍保留。使用与验证范围见[V0.7运行说明](docs/versions/v0.7/operations.md)和[测试报告](docs/versions/v0.7/test-report.md)。不新增运行依赖或第二Core；产品仍0.5.0rc1，Live2D延期至正式版后更新，V0.6隔离实验仍暂停。

## 安装与运行

GUI（可选依赖，与 CLI 同数据目录互斥）：

```powershell
conda activate deskpal_env
python -m pip install -r requirements-gui.txt -c constraints/windows-py312-gui.txt
python -m deskpal.gui --config config.toml
```

加 --offline 可进行本次离线运行。GUI 默认 local_only，阻止整次云规划；使用 DeepSeek 时需主动选择 cloud_allowed。任务页按独立请求处理，会话页提供进程内有界多轮上下文。无 GUI 依赖仍可运行后端：

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

## 本地人格与情绪设置（V0.8 开发中）

人格与模拟情绪默认关闭；使用本地管理命令查看当前 `revision`，写操作必须携带该值。示例中的 `REV` 需替换为最近一次查询或修改回执中的整数。

```powershell
python -m deskpal --companion show
python -m deskpal --companion configure --companion-parameters '{"expected_revision":REV,"enabled":true,"emotions_enabled":true,"preset":"warm"}'
python -m deskpal --companion preference --companion-parameters '{"expected_revision":REV,"scope":"today","verbosity":"brief","humor_allowed":false}'
python -m deskpal --companion cue --companion-parameters '{"expected_revision":REV,"mode":"focused","ttl_seconds":600}'
python -m deskpal --companion reset --companion-parameters '{"expected_revision":REV,"target":"affect"}'
python -m deskpal --companion rollback --companion-parameters '{"expected_revision":REV,"persona_version":1}'
```

预设为 `balanced/warm/concise`；风格仅接受 `formality/warmth/humor/verbosity/expressiveness/curiosity` 六项 0..1 数值。偏好作用域为 `session/today/persistent`，长度为 `brief/normal/detailed`；`today` 按配置的 `proactive.timezone` 到当地下一午夜结束。cue 为 `neutral/supportive/focused`，最多 1800 秒，会话偏好和 cue 不跨重启保存。所有管理命令强制离线，无需模型 key，不经过规划或执行工具；运行任务期间设置写操作会明确拒绝，可在任务结束后重试。

已有工具结果的 Reflect 追加受限风格指令；当前请求中的明确表达要求优先。通常工具路径仍为一次 Planner + 一次 Reflect。禁用人格时无工具路径沿用 Planner 普通回复，仅一次请求；启用且有有效风格时最多增加一次无工具表达调用，Planner 草稿明确视为未验证的 assistant 输出，表达失败回原回复并附警告。`candidate_review` 的可选记忆评估调用按原设置执行。风险确认、失败、取消和待复核文字保持中性。云表达不接收本地互动偏好、cue 或任务情绪；工具权限、任务事实和状态独立于人格。

生产状态在实例锁后保存到 `companion.sqlite3`，低层 `create_agent` 的人格状态只在内存中。可选人格读取、样式或情绪写入失败时回退中性，不阻断工具、不重复执行；损坏存储不会自动覆盖，管理写入会拒绝。遗忘先重置派生表达状态，重置失败则清理保持 Pending，重试或启动续办成功后才报告完成。任务情绪只接收已提交的 Completed/Failed 及最小结果摘要；不保存请求正文、工具参数或结果内容。

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
  companion/     可选声明式人格、显式互动偏好与有限情绪模拟（V0.8待验收）
  tasks/ memory/ scheduling/ tools/ sandbox/ config/ execution_log/
tests/
docs/
doc/             原始设计资料
```

默认 data/ 内含 workflow.sqlite3、memory.sqlite3、scheduling.sqlite3、companion.sqlite3、execution.jsonl；文件工具根为 workspace/。一个规范 data_dir 只允许一个生产实例。关闭超时会保留在用资源/锁，不强杀同步工具；程序退出期间无系统通知。

生产集成使用 `create_application`；`create_agent` 保留 V0.3 同步进程内任务契约，用于低层嵌入与兼容测试，不提供 V0.4 持久恢复保证。Git 提交由用户处理。

## 开发与验证

```powershell
python -m pytest -q
python -m mypy --strict src scripts/check_memory_api.py scripts/benchmark_memory.py scripts/benchmark_workflow.py
```

离线测试使用合成数据，不调用真实 API；真实 API 需新的单独授权。当前证据见 [V0.5 测试报告](docs/versions/v0.5/test-report.md)与[候选版交付说明](docs/versions/v0.5/delivery.md)；[V0.4 报告](docs/versions/v0.4/test-report.md)保留为历史后端证据。[文档导航](docs/README.md)、[分阶段路线](docs/planning/roadmap.md)、[长期记忆/人格/情绪设计](docs/planning/companion-model/README.md)区分已实现与未来规划。
