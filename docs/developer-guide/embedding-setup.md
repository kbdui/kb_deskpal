# 本地语义检索准备

Embedding 用于记忆向量检索，与聊天模型不同。当前适配器为 FastEmbed 0.9.0 / BAAI bge-small-zh-v1.5，使用 ONNX Runtime CPU；无需 GPU、PyTorch 或独立向量数据库服务。是否启用及缓存路径以实际配置为准，新克隆不包含模型权重。

在目标环境安装可选分组；下列约束对应 Windows CPython 3.12：

```powershell
python -m pip install -r requirements-embedding.txt -c constraints/windows-py312.txt
```

依赖与模型资产分开准备。模型下载需明确允许，托管准备会记录清单和哈希；已有模型只需验证，不应为了普通回归重复下载。使用缓存前核对资产与配置引用，缺少依赖或资产时按实际提示处理，不把词法回退视作语义效果通过。

退出同一数据目录应用后，可通过维护入口检查当前缓存：

```powershell
python -m deskpal --config config.toml --maintenance cache_status
python -m deskpal --config config.toml --maintenance cache_verify
```

check_managed_embedding.py 用于单独验证显式下载、托管登记和离线推理，需要全新诊断 root；不能把正在使用的模型目录当作可清理的实验 root。普通测试注入合成 backend，不读取个人缓存、下载模型或访问真实 API。

旧环境实测只证明当时的环境，详见[2026-10-23 历史证据](../archive/releases/v0.9/managed-embedding-live-report.md)；安装细节与旧脚本边界见[整理前指南](../archive/guides/developer-guide/embedding-setup.md)。不要据旧报告假定当前机器已安装相同资产。
