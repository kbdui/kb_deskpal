# 依赖清单与维护

更新：2026-10-16，含 V0.4 时区与 V0.5 候选版 GUI 依赖。直接依赖统一在 [pyproject.toml](../../pyproject.toml)，requirements 文件只提供分组入口；在仓库根目录运行。`-e .` 安装当前项目，extra 追加可选功能。

| 分组 | 直接声明 | 安装入口 | 实际状态 |
| --- | --- | --- | --- |
| 基础 | python-dotenv >=1.0；Windows 条件 tzdata | [requirements.txt](../../requirements.txt) | 本地配置、离线后端和 IANA 时区 |
| 开发 | pytest >=7.4、mypy >=1.10 | [requirements-dev.txt](../../requirements-dev.txt) | Python 3.11/3.12 回归与类型检查，最终计数见版本报告 |
| 截图 | mss >=10.0,<11 | [requirements-screenshot.txt](../../requirements-screenshot.txt) | 可选；V0.2 已有，当前回归覆盖 |
| CPU embedding | fastembed ==0.9.0 | [requirements-embedding.txt](../../requirements-embedding.txt) | V0.3 已接入，真实模型仅在 Python 3.12 验证 |
| GUI | PySide6-Essentials >=6.12,<6.13 | [requirements-gui.txt](../../requirements-gui.txt) | 主/兼容测试环境已安装 6.12.0，shiboken6 配套 |
| GUI 开发 | dev/gui 加 pytest-qt >=4.5,<4.6 | [requirements-gui-dev.txt](../../requirements-gui-dev.txt) | 4.5.0；真实 Qt 事件循环测试，实机项另测 |

## 安装

```powershell
conda activate deskpal_env
python -m pip install -r requirements-dev.txt -c constraints/windows-py312.txt
python -m pip install -r requirements-screenshot.txt -c constraints/windows-py312.txt
python -m pip install -r requirements-embedding.txt -c constraints/windows-py312.txt
python -m pip check
```

只需基础后端时用 requirements.txt；不要求所有用户安装截图/embedding。extras 等价入口为 `python -m pip install -e ".[dev,screenshot,embedding]" -c constraints/windows-py312.txt`。约束用于经过核验的 Windows CPython 3.12，不能直接保证其他系统或 3.11 安装。

## 约束的范围

[constraints/windows-py312.txt](../../constraints/windows-py312.txt)记录 Windows x64 / CPython 3.12 的 **44 项**观察版本，覆盖 dev、screenshot、CPU embedding、tzdata 及本地构建工具闭包。它约束可安装版本，不会凭空安装未选择的 extra。

这是精确版本约束，**不是哈希锁文件**、平台无关 lock 或干净环境安装成功证明。最终 pip check、约束一致性与测试结果见 [V0.3 测试报告](../versions/v0.3/test-report.md)。不能把整个个人 Conda 环境 pip freeze 当作项目依赖；未来更换目标环境时应从项目闭包重新解析和验证。

模型权重不属于 Python 依赖锁。FastEmbed adapter 记录资产文件 SHA-256 与预处理版本，运行只加载已有缓存，不自动下载。首次准备、cache-only 使用和索引重建见 [Embedding 指南](embedding-setup.md)。

## V0.4 依赖

[本机评估](../planning/v0.4/environment-assessment.md)对应的持久目标/提议/任务恢复使用标准库 SQLite、threading、queue 控制和 OS 锁，无需队列框架、ORM、Qt 或 Live2D。pyproject 已加入 Windows 条件 `tzdata`，Python 3.11/3.12 环境均安装并验证 2026.5；constraints 增加对应 pin。requirements 仍引用 pyproject，无需再手写第二份依赖版本。模型缓存不变，V0.4 不新增下载模型或后台云 SDK。

## V0.6 评估中的依赖

[V0.6环境评估](../planning/v0.6/environment-assessment.md)已盘点双Python、Qt、MSVC/SDK与候选Win32适配。当前pywin32/psutil未安装；它们不是已经冻结的必须依赖，先验证标准库ctypes或薄native helper路线，再决定是否增加Windows execution extra和requirements入口。现有两个constraints继续保留；阶段0验证后才新增执行约束与runtime/helper manifest，不用空锁文件或整份Conda freeze冒充已验证闭包。

本阶段不需要新embedding/Live2D/CUDA或云SDK；专用worker runtime、最小ACL、真进程测试环境仍待原型准备。运行时可用性不能由pip check或发现编译器目录代替。本次评估没有安装依赖。

## 更新规则

V0.5 已声明 gui/gui-dev 可选组与两个 requirements 入口，主/兼容环境均安装验证。新增 [Windows 3.12 GUI 约束](../../constraints/windows-py312-gui.txt)复用原44项后端约束并固定 Essentials/shiboken/pytest-qt；它仍不是哈希锁或 Python 3.11 通用保证。安装使用 `python -m pip install -r requirements-gui-dev.txt -c constraints/windows-py312-gui.txt`。本阶段无需 Live2D、CUDA 或新增云 SDK，实际验证见 [V0.5 报告](../versions/v0.5/test-report.md)。

1. 先修改 pyproject 对应直接依赖，再更新已验证目标的约束；requirements 保持引用 extra，避免两套手写版本范围。
2. 基础组只放所有后端用户必须安装的库；截图、embedding、未来 UI SDK 继续留在 adapter 可选组。
3. 升级后核验依赖解析、pip check、双 Python 离线回归和实际功能；真实模型/网络验收单独报告，不以 mock 替代。
4. 模型资产或前处理升级需新 manifest 和索引重建；Python 包固定不代表模型快照固定。
5. 不提交凭据、个人配置、缓存、运行库或私人数据库。用户自行处理 Git 提交。
