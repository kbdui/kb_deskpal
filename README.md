# Deskpal

面向 Windows 的角色聊天与桌宠 Agent，提供 Qt 图形界面和命令行。Python 3.11+；包版本仍为 `0.5.0rc1`，历史文档中的版本编号不表示当前发布版本。

截至 2026-10-30，人格、记忆与多轮执行重构 **Phase 01–05 已实现并完成各阶段定向隔离验证，Phase 06 整体验收待进行**。真实模型效果、原生桌面操作与完整回归不能由阶段通过数替代。当前全量测试基线及整理后的结果见[整理记录](docs/maintenance/2026-10-30-repository-cleanup.md)。

## 安装与运行

在仓库根目录的独立 Python 环境中执行；下面约束适用于 Windows CPython 3.12：

```powershell
python -m pip install -r requirements-gui.txt -c constraints/windows-py312-gui.txt
python -m deskpal.gui --config config.toml
```

缺配置时可启动配置向导。默认使用云模型连接，在设置中填写服务、模型和 API 密钥；本地模型连接也在设置中选择。凭据、模型权重及业务数据库不随仓库分发。截图和本地语义检索分别使用可选依赖分组，见[快速开始](docs/user-guide/quick-start.md)。

仅后端可安装 `requirements.txt`，执行 `python -m deskpal --help`。固定命令演示需要明确选择 offline provider，不等同于模型理解能力。

## 当前功能

- 每个角色保存独立的聊天、人格、记忆、任务、日程和受管形象；切换时关闭旧后台再打开新后台。连接、凭据、外部工作区与模型缓存应用级共用。
- 角色卡与学到的习惯参与理解、规划和表达；模型按工具实际结果多轮判断。授权、追加要求、暂停、继续和取消由任务链路落实。
- 默认开启自动学习，以待确认缓冲承接不确定理解，由模型复核；工具明细留在执行账本。聊天清空与长期记忆遗忘分别处理。
- 普通主动聊天统一经过 AgentCore；内部整理使用同一调度器。提醒由调度履约，主动聊天与提醒可以分别暂停。
- 主窗口集中聊天、安排、记忆、工具插件和设置；静态桌宠提供小对话窗。Live2D、OS 沙箱与完整云端类别许可属于[正式版后规划](docs/roadmap/post-release.md)。

## 文档与测试

从[文档导航](docs/README.md)、[当前架构](ARCHITECTURE.md)和[测试说明](tests/README.md)开始。开发与验收按[六阶段计划](docs/planning/companion-redesign/README.md)推进；后续短报告统一追加到[实施日志](docs/reports/implementation-log.md)。

历史接口、失败证据和交接统一放在 [docs/archive](docs/archive/README.md)。存档不作为当前操作指令，也不意味着原先未通过的验收已经完成。
