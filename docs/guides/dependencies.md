# 依赖清单与维护

更新：2026-10-15，对应 0.3.0。直接依赖统一在 [pyproject.toml](../../pyproject.toml)，requirements 文件只提供分组入口；在仓库根目录运行。`-e .` 安装当前项目，extra 追加可选功能。

| 分组 | 直接声明 | 安装入口 | 实际状态 |
| --- | --- | --- | --- |
| 基础 | python-dotenv >=1.0 | [requirements.txt](../../requirements.txt) | 本地配置与离线后端 |
| 开发 | pytest >=7.4、mypy >=1.10 | [requirements-dev.txt](../../requirements-dev.txt) | Python 3.11/3.12 回归与类型检查，最终计数见版本报告 |
| 截图 | mss >=10.0,<11 | [requirements-screenshot.txt](../../requirements-screenshot.txt) | 可选；V0.2 已有，当前回归覆盖 |
| CPU embedding | fastembed ==0.9.0 | [requirements-embedding.txt](../../requirements-embedding.txt) | V0.3 已接入，真实模型仅在 Python 3.12 验证 |

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

[constraints/windows-py312.txt](../../constraints/windows-py312.txt)记录 Windows x64 / CPython 3.12 的 **43 项**观察版本，覆盖 dev、screenshot、CPU embedding 及本地构建工具闭包。它约束可安装版本，不会凭空安装未选择的 extra。

这是精确版本约束，**不是哈希锁文件**、平台无关 lock 或干净环境安装成功证明。最终 pip check、约束一致性与测试结果见 [V0.3 测试报告](../versions/v0.3/test-report.md)。不能把整个个人 Conda 环境 pip freeze 当作项目依赖；未来更换目标环境时应从项目闭包重新解析和验证。

模型权重不属于 Python 依赖锁。FastEmbed adapter 记录资产文件 SHA-256 与预处理版本，运行只加载已有缓存，不自动下载。首次准备、cache-only 使用和索引重建见 [Embedding 指南](embedding-setup.md)。

## V0.4 依赖准备（尚未实施）

[本机评估](../planning/v0.4/environment-assessment.md)确认现有环境可开发持久目标/提议/任务恢复；无需队列框架、ORM、Qt 或 Live2D。Windows 应补 `tzdata` 条件依赖：本机 Conda 提供 IANA 数据，但当前 pyproject 没有保证普通 Windows Python 可用。实施时在 pyproject 增加声明、安装验证后更新约束；本轮尚未改安装清单或环境，现有 43 项约束仍是 V0.3 快照。

## 更新规则

1. 先修改 pyproject 对应直接依赖，再更新已验证目标的约束；requirements 保持引用 extra，避免两套手写版本范围。
2. 基础组只放所有后端用户必须安装的库；截图、embedding、未来 UI SDK 继续留在 adapter 可选组。
3. 升级后核验依赖解析、pip check、双 Python 离线回归和实际功能；真实模型/网络验收单独报告，不以 mock 替代。
4. 模型资产或前处理升级需新 manifest 和索引重建；Python 包固定不代表模型快照固定。
5. 不提交凭据、个人配置、缓存、运行库或私人数据库。用户自行处理 Git 提交。
