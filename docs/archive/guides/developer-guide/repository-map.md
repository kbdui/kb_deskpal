> 历史存档：保留当时的设计、失败与验收边界；不代表当前实现或新的执行授权。当前入口见 [文档导航](../../../README.md)。

# 仓库与文件导航

项目采用 Python `src` 布局，构建与依赖声明统一在 `pyproject.toml`。下面列出开发中需要关注的源码和资料；`build/`、`dist/`、各类缓存不是源码入口。

```text
kb_deskpal/
├─ pyproject.toml              包信息、入口、依赖分组、资源打包、pytest 设置
├─ requirements*.txt          引用 pyproject 的安装分组
├─ constraints/               Windows Python 3.12 观察版本约束
├─ config.example.toml        可公开提交的配置样例
├─ .env.example               密钥变量名示例，不含真实值
├─ README.md                  项目入口与当前状态
├─ ARCHITECTURE.md            总体架构与保证边界
├─ src/deskpal/               产品源码
├─ tests/                     unit / integration / gui / fixtures
├─ scripts/                   基准、手工验收辅助和隔离实验诊断
├─ docs/
│  ├─ user-guide/             按使用任务组织的用户指南
│  ├─ developer-guide/        按实现主题组织的开发者指南
│  ├─ guides/                 既有依赖、开发、embedding 专题
│  ├─ versions/               接口、开发状态、验收和交付记录
│  ├─ planning/               实施前计划、提案与验收边界
│  └─ handoffs/               历史交接
└─ doc/                       原始总体设计与交接资料
```

`config.toml`、`.env`、`data/`、`workspace/` 是本地运行资料或配置位置，实际目录受配置影响。不要把私人值、数据库或运行输出放入示例文档。

当前工作区的 BGE 托管资产位于 `data/models/bge-small-zh-v1.5/`，根目录含 `.deskpal-model-cache.json`，本地 `config.toml` 已启用并引用它。原 `data/models/fastembed/` 旧缓存保留；`data/validation/` 只保留诊断与历史证据，不再作为应用的 BGE 权重来源。这些目录和真实配置均不随 Git 分发。可复现链路脚本为 [check_managed_embedding.py](../../../../scripts/check_managed_embedding.py)，准备/运行区别见[开发环境与验证](../../../developer-guide/development-and-testing.md)。

## 源码目录职责

| 目录/文件 | 主要职责 | 建议先看 |
| --- | --- | --- |
| `application/` | 装配、实例锁、生产 facade、队列、恢复、隐私、CLI | [factory.py](../../../../src/deskpal/application/factory.py)、[service.py](../../../../src/deskpal/application/service.py) |
| `agent/` | Core 编排与依赖协议 | [core.py](../../../../src/deskpal/agent/core.py)、[ports.py](../../../../src/deskpal/agent/ports.py) |
| `planner/` | 有界计划、LLM 规划、确定性工具规划 | [models.py](../../../../src/deskpal/planner/models.py)、[llm_planner.py](../../../../src/deskpal/planner/llm_planner.py) |
| `llm/` | 通用消息、Provider、传输、URL 策略、调用预算网关 | [ports.py](../../../../src/deskpal/llm/ports.py)、[calls.py](../../../../src/deskpal/llm/calls.py) |
| `workflow/` | 工作库、持久任务、计划/attempt、依赖、outbox、迁移 | [store.py](../../../../src/deskpal/workflow/store.py)、[journal.py](../../../../src/deskpal/workflow/journal.py) |
| `tasks/` | Task DTO/状态和低层内存实现 | [models.py](../../../../src/deskpal/tasks/models.py) |
| `memory/` | 记忆来源、审核、CAS、画像、检索与 embedding | [contracts.py](../../../../src/deskpal/memory/contracts.py)、[coordinator.py](../../../../src/deskpal/memory/coordinator.py) |
| `goals/`、`proactive/` | 显式目标/里程碑/绑定、本地提议与事件消费 | [目标管理](../../../../src/deskpal/goals/manager.py)、[规则引擎](../../../../src/deskpal/proactive/engine.py) |
| `scheduling/` | 日程、计时器、到期事件与来源身份 | [service.py](../../../../src/deskpal/scheduling/service.py) |
| `tools/`、`sandbox/` | 可信工具、参数/权限校验、工作区路径约束 | [manager.py](../../../../src/deskpal/tools/manager.py)、[workspace.py](../../../../src/deskpal/sandbox/workspace.py) |
| `conversation/`、`response/` | 有界会话投影、受限表达与协议审核 | [会话运行时](../../../../src/deskpal/conversation/runtime.py)、[表达组合](../../../../src/deskpal/response/composer.py) |
| `companion/` | 声明式人格、显式互动偏好、有限情绪和持久化 | [runtime.py](../../../../src/deskpal/companion/runtime.py)、`interaction_runtime.py`（旧模块已退役） |
| `presentation/` | Qt-free 元数据、任务状态与表情映射 | [models.py](../../../../src/deskpal/presentation/models.py)、[state.py](../../../../src/deskpal/presentation/state.py) |
| `gui/` | Qt 窗口、后台桥、宿主、桌宠、托盘、通知与维护 | [launcher.py](../../../../src/deskpal/gui/launcher.py)、[host.py](../../../../src/deskpal/gui/host.py) |
| `maintenance/`、`credentials.py` | 独立配置、缓存、四库归档/恢复、系统凭据 | [service.py](../../../../src/deskpal/maintenance/service.py)、[credentials.py](../../../../src/deskpal/credentials.py) |
| `config/`、`execution_log/`、`_json.py` | 配置装载、结构化日志、JSON 快照 | [配置](../../../../src/deskpal/config/__init__.py)、[日志](../../../../src/deskpal/execution_log/__init__.py) |
| `execution/` | 暂停的隔离协议/runtime/Windows 实验 | [暂停检查点](../../releases/v0.6/pause-checkpoint-2026-10-19-1457.md) |

## 从问题找文件

- 启动失败或资源未释放：`application/factory.py`、`application/instance.py`、`gui/launcher.py`、`gui/host.py`。
- 授权后行为不符：`agent/core.py`、`workflow/journal.py`、`tools/manager.py`、`sandbox/workspace.py`。
- 重启后任务状态异常：`workflow/journal.py`、`application/service.py`、`application/dispatcher.py`。
- 记忆、历史或风格数据出云范围：`application/interaction.py`、`application/privacy.py`、`llm/calls.py`、`memory/contracts.py`。
- GUI 旧回调覆盖新状态：`gui/bridge.py`、`gui/channel.py`、`gui/chat.py`、`gui/interaction_state.py`、`gui/companion_state.py`。
- 配置/备份/缓存故障：`maintenance/setup.py`、`maintenance/archives.py`、`maintenance/cache.py`、`memory/cache_lock.py`。

继续阅读[架构与设计边界](../../../developer-guide/architecture.md)和[模块代码说明](../../../developer-guide/modules.md)。
