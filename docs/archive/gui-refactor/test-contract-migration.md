> 历史存档：保留当时的设计、失败与验收边界；不代表当前实现或新的执行授权。当前入口见 [文档导航](../../README.md)。

# D1–D5 既有测试契约迁移记录

2026-10-26。当前为开发交付阶段：**未运行 pytest、mypy、GUI、网络或产品测试**。本记录依据源码与实际 diff 的静态审查；AST 解析仅核对语法，不能证明功能通过。工作区已有改动全部保留，未 stage、commit 或 push。

关联：[最小测试清单](development-test-plan.md)、[开发记录](development-status.md)、[源码接口](../../developer-guide/gui-refactor.md)。本记录只覆盖 `tests/unit`、`tests/integration` 的既有契约迁移，GUI 和 CLI 测试由各自整合任务负责。

## 新默认与凭据来源

全新 `LLMConfig` / `load_config(environ={})` 选择 `openai`、`cloud`、`system_vault`，无模型/凭据时 `ready=False`。允许启动浏览本地数据与设置，不代表可提交模型请求；维护回执的 `can_start` 与 `llm_ready` / `configured` 分开。

| 文件 | 迁移理由与保留的保障 |
| --- | --- |
| `tests/unit/test_config.py` | 默认期望改为未配置云连接；缺模型/密钥从解析失败迁为可浏览但未就绪，保留 URL、类型、密钥脱敏及 `.env` 隔离断言。原离线“不读取环境密钥”测试改为显式离线，并增加新默认、环境显式 vault、TOML 显式 vault 的禁止读取断言；即使合成环境含 key，也不能隐式取用。 |
| `tests/unit/test_product_credentials.py` | 显式 hermetic 环境不访问 vault，返回未就绪而非解析异常；离线覆盖仍 ready 且不属云。保留假 vault 适配、真实 vault 禁止访问、旧文件环境密钥优先、repr 脱敏和回滚断言。 |
| `tests/unit/test_product_maintenance_service.py` | 缺凭据云配置 `can_start=True`，同时 `llm_ready=False` / `configured=False`；显式离线覆盖可就绪。保留禁止 vault 读取、缺凭据问题标记、provider/model 不暗改及原文件字节不变断言。 |
| `tests/unit/test_product_setup.py` | 既有保存 fixture 已明确离线，保留原文、CAS、路径占用与敏感字段拒绝；已有新增 memory mode 保存测试保留。本轮不全局替换模型工厂。 |
| `tests/integration/test_humanization_migration.py`、`tests/integration/test_product_restore.py` | 直接构造 `AppConfig` 的旧迁移/恢复工作流明确注入 `LLMConfig(provider="offline")`，继续验证原离线行为，不依赖新默认或隐藏网络替身。 |

其他现有离线工作流 fixture 已显式选择 `DESKPAL_LLM_PROVIDER=offline`；配置值独立性等只读测试继续使用真实默认。受控云行为测试仍用其已有合成模型、假密钥和局部 provider 替身；未添加覆盖全仓默认的 mock。

`load_config` 的运行时 no-read 断言针对未选中环境凭据源和显式离线。维护 `SetupService.inspect()` 会主动查询选定来源的凭据存在性，用于维护页显示；相关测试继续使用假环境/假 vault，不能将存在性查询解释为模型请求授权。

## 持久化与授权契约

| 文件 | 迁移理由与保留的保障 |
| --- | --- |
| `tests/integration/test_humanization_migration.py` | 成功迁移后的工作库版本期望为3；冻结 schema1 SQL、旧计划 canonical/hash 与备份版本1仍保持。 |
| `tests/unit/test_v04_workflow.py` | 当前 `WorkflowStore` 生成的在线备份版本期望为3，继续重开并验证原队列任务。 |
| `tests/unit/test_v07_presentation_models.py` | fixture 由当前 `WorkflowStore` 生成，重开后版本期望为3；名称改为 current schema，继续断言仅恢复索引、表 SQL 不变。未把它伪造为旧 schema2 迁移 fixture。 |
| `tests/unit/test_product_archives.py` | 新建备份默认格式2，scope 包括四库与托管素材；无素材 fixture 应明确 `assets=[]`。保留四库清单、校验和、排除范围及非真实性证明断言。 |
| `tests/integration/test_humanization_application.py`、`tests/integration/test_v04_application.py`、`tests/integration/test_v08_companion_integration.py` | 不完整授权在受理阶段抛 `WorkflowBlocked`；仍验证 Waiting、原计划/hash、零 attempt/零效果和重启清空授权。重启缺权限检查改读 `task_review` 的普通/高风险分类，不移除权限要求。 |
| `tests/integration/test_v05_desktop.py`、`tests/integration/test_v08_companion_integration.py` | GUI facade 的确认命令携准确 `expected_revision`；缺授权不能生成已受理回执。保留重复 command_id 单次效果、错误 hash、零工具执行和终态保障。 |

冻结的 schema1/2 迁移输入及原备份版本不能随当前版本改写。新增 `tests/integration/test_persistent_conversations.py` 的 schema2 输入与备份版本2断言保留，`test_gui_refactor_d2.py`、素材及模型连接新测试不属于本轮旧 fixture 重写范围。

## 静态核对与待执行

- 审查上述测试的实际 diff，对照 `config`、`maintenance.setup/service`、`workflow.store/migrations`、应用确认与任务审核源码核对新契约。
- 对本记录列出的13个既有测试文件做 `ast.parse`，未导入项目模块、未调用测试函数、未构造应用、未读取真实配置/密钥/数据库。
- B1/B2/B3/W1 全部仍待用户授权执行；命令与停止条件见[最小测试清单](development-test-plan.md)。不能以本次静态核对声称回归通过。
