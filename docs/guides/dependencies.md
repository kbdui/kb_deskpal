# 依赖清单与维护

项目依赖统一在根目录 [pyproject.toml](../../pyproject.toml) 声明，requirements 文件提供分组安装入口，使用同一声明。安装入口中的 `-e .` 表示安装本地项目及其运行依赖，`[extra]` 追加对应可选组；在仓库根目录执行命令。

## 当前直接依赖

| 分组 | 声明 | 安装入口 | 状态 |
| --- | --- | --- | --- |
| 基础 | python-dotenv >=1.0 | [requirements.txt](../../requirements.txt) | V0.2 已验证 |
| 开发 | pytest >=7.4、mypy >=1.10 | [requirements-dev.txt](../../requirements-dev.txt) | V0.2 已验证 |
| 截图 | mss >=10.0,<11 | [requirements-screenshot.txt](../../requirements-screenshot.txt) | V0.2 实测 10.2.0 |
| CPU embedding | fastembed ==0.9.0 | [requirements-embedding.txt](../../requirements-embedding.txt) | V0.3 候选；本机安装/推理待验证 |

基础依赖会随每个可选组一起安装。FastEmbed 的 numpy/onnxruntime 等传递依赖由 pip 按包元数据解析；模型权重属于独立资源，缓存路径和验证方法见 [Embedding 指南](embedding-setup.md)。

## 安装

```powershell
conda activate deskpal_env
# 开发环境：基础 + 测试/类型工具
python -m pip install -r requirements-dev.txt
# 按需要添加功能
python -m pip install -r requirements-screenshot.txt
python -m pip install -r requirements-embedding.txt
python -m pip check
```

也可用等价 extras 命令，例如 `python -m pip install -e ".[dev,screenshot,embedding]"`。只运行基础后端时使用 `requirements.txt`；上面的三组命令不要求每个使用者都执行。

## 维护规则与可复现性

1. 新增/调整直接依赖时修改 pyproject.toml 中对应组，并更新本页说明。requirements 入口保持引用该组，避免两个文件各自定义同一套版本范围。
2. 基础组只放全部用户必须安装的库；截图、embedding、未来 UI/Live2D 等能力分为可选组，具体 SDK 留在 adapter。
3. 当前清单是安装声明，**不是完整的传递依赖锁文件**。FastEmbed 的直接版本已固定，但所有传递包与模型快照尚未整体锁定。
4. 在干净的 Python 3.11/3.12 验证环境完成安装、pip check、回归与真实功能验证后，再保存各目标环境的完整解析约束/哈希记录；记录 Python、平台和模型 revision/校验值。不能把整个个人 Conda 环境的 pip freeze 直接当作项目依赖清单。
5. 版本升级先在隔离环境验证依赖解析与行为，再更新声明及已验证约束。模型变化还需重建索引，与 Python 包升级分别记录。

本次增加安装入口与可选声明，没有执行安装或生成未经验证的全量锁文件；当前业务版本仍为 V0.2，embedding extra 不代表 V0.3 功能已经接入。
