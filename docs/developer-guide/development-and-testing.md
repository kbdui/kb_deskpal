# 开发环境与验证

Python 要求 `>=3.11`，Windows CPython 3.12 是仓库约束文件的目标环境。所有命令在仓库根目录和项目虚拟环境执行。用户安装过程见[快速开始](../user-guide/quick-start.md)。

## 安装与入口

```powershell
python -m pip install -r requirements-gui-dev.txt -c constraints/windows-py312-gui.txt
python -m pip check
python -m deskpal --help
python -m deskpal.gui --help
```

仅后端开发安装 `requirements-dev.txt` 并使用 `constraints/windows-py312.txt`。截图、embedding 另有可选分组；不需要完整 Qt SDK、GPU、Live2D 或 PyTorch。

`requirements*.txt` 引用 `pyproject.toml` 中的可编辑安装分组。约束是观察版本记录，不是跨平台哈希锁；在 Python 3.11 等其他环境解析需独立验证。详细规则见[依赖维护](../guides/dependencies.md)。

入口映射：

| 命令 | 实现 |
| --- | --- |
| `python -m deskpal` / `deskpal` | `__main__.py → application.cli:main` |
| `python -m deskpal.gui` / `deskpal-gui` | `gui.__main__ → gui.main:main` |
| `python -m deskpal --maintenance ...` | CLI 提前分流至 `maintenance.cli:main`，不启动生产 Core |

## 测试分层

当前工作区在 `deskpal_env` 中已有 FastEmbed 0.9.0 和 ONNX Runtime CPU 依赖，应用配置引用 `data/models/bge-small-zh-v1.5`；不要用未安装 embedding 的 Anaconda base 运行模型验证。新环境若要使用当前配置中的 BGE，需要额外安装 `requirements-embedding.txt`，模型资产不会随代码克隆。内置默认仍关闭 embedding，普通合成测试应显式配置或注入，不依赖这份本地权重。

| 目录 | 验证重点 |
| --- | --- |
| `tests/unit/` | 配置、领域、工具/权限、检索、协议、缓存/归档和固定错误 |
| `tests/integration/` | 持久生产流程、恢复、隐私、迁移、任务/记忆/表达的组合 |
| `tests/gui/` | Qt bridge/host/window、迟到结果、桌宠、维护与进程互斥 |
| `tests/test_cli.py`、`tests/test_v04_cli.py` | CLI 参数、输出、退出码、持久管理流程 |
| `tests/fixtures/` | 合成检索数据与 Windows 原生诊断夹具 |

离线测试使用合成临时配置、数据和注入 Provider/backend；不要读取个人 `.env`、真实库或系统凭据。真实 API 与模型下载需单独授权和记录，不把普通 pytest 当作隐含联网许可。

## 常用验证命令

完整离线回归和基础 strict：

```powershell
python -m pytest -q
python -m mypy --strict src scripts/check_memory_api.py scripts/benchmark_memory.py scripts/benchmark_workflow.py
```

定向检查示例：

```powershell
python -m pytest -q tests/unit/test_tools.py tests/unit/test_confirmation.py tests/unit/test_sandbox.py
python -m pytest -q tests/integration/test_product_restore.py tests/unit/test_product_archives.py
python -m pytest -q tests/gui/test_bridge.py tests/gui/test_host.py tests/gui/test_product_maintenance_gui.py
```

定向通过后，按共享接口影响决定是否扩大回归。GUI 开发还需检查相关脚本：

```powershell
python -m mypy --strict src scripts/benchmark_gui.py scripts/check_gui_displays.py scripts/check_gui_stability.py scripts/check_desktop_capacity.py
```

运行 GUI 测试需要 gui-dev 分组；offscreen 环境由对应测试配置处理，不能据其通过推定真实桌面交互完成。

## 基准和手工验收工具

| 脚本组 | 用途与边界 |
| --- | --- |
| `benchmark_memory.py`、`benchmark_workflow.py`、`check_memory_api.py` | 合成记忆/工作流能力和容量；资源结果需注明环境 |
| `benchmark_gui.py`、`check_desktop_capacity.py` | 离线 GUI/历史容量与资源测量 |
| `check_gui_displays.py`、`check_gui_manual.py` | 原生窗口/显示器及人工交互辅助，需主动安排实机阶段 |
| `check_v07_*.py` | 静态桌宠素材、展示、输入、分发和资源验证 |
| `check_embedding.py` | 旧 adapter smoke，默认可下载至 `data/models/fastembed`；`--offline --cache-dir 路径` 只用指定已有缓存，不生成托管清单 |
| `check_managed_embedding.py` | 显式 `--allow-download`＋新 root 测真实托管准备；新进程拦截 socket/DNS 并检查真实 CPU session；`--offline` 仅复查仍保留模型的诊断 root，不自动引用至应用 |
| `check_humanization_live.py` | 真实模型诊断，需独立授权、参数检查和证据保管 |
| `diagnose_*`、`probe_*` | 大部分用于暂停的 Windows 隔离实验，不是日常产品回归入口 |

执行脚本前先读 `--help` 和对应版本操作说明，将输出放到新的合成验证目录，避免覆盖首败证据。长测、真实 API、真实模型与物理桌面交互应按任务范围安排。

## 怎样报告结果

当前应用模型已搬出旧 `live-escalated` 诊断 root；只复查已安装的模型使用 `python -m deskpal --config config.toml --maintenance cache_verify`，先退出同 data_dir 的应用。重新验证真实下载链路时使用脚本的全新 root，父目录须已存在，明确提供下载许可；不要把 `data/models/bge-small-zh-v1.5` 当作诊断 `--root`，也不要为了普通回归重复下载。

后续真实链路及迁移验证已通过：约 95 MB 托管模型、逐资产 hash、实际 CPU 512 维归一化向量、重复 query 一致、离线网络尝试 0；定向回归 54 项通过，包含新脚本的 strict 146 文件通过。命令与首败/复测保留在[真实链路报告](../versions/v0.9/managed-embedding-live-report.md)。网络证据为 Python guard/audit，不是物理断网抓包；不代替检索质量、GUI 实机或 HM08 聊天体验验收。

记录命令、解释器/依赖、数据性质、时限、实际退出码、通过/失败/跳过、证据路径和限制。保留首败，修补后另写复测；超时即使出现“passed”也不能记整个进程通过。权限跳过和 fake SDK 不能替代原生边界证据。

截至本指南整理时，产品化集中验证的历史结果为主环境去重 1,289 通过/2 跳过，Python 3.11 兼容环境 169 通过/1 跳过，详情及 strict 范围见[原始报告](../versions/v0.9/test-report.md)。这些数字是既有记录，不是本次文档编写重新运行的结果。

真实模型拟人化问题仍有[暂缓项](../versions/v0.8/hm08-deferred-issues.md)，静态桌宠存在[未结验收](../versions/v0.7/deferred-tests.md)。提交说明应保持这些边界，Git 提交由用户处理。
