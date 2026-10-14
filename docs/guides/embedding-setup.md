# 中文 Embedding 选择与安装

核验日期：2026-10-14。推荐 **BAAI/bge-small-zh-v1.5 + FastEmbed 0.9.0（CPU）**，用于本项目当前中文短记忆的检索基线。本轮核对官方发布元数据与代码，尚未在 deskpal_env 安装或下载权重；本机效果、峰值内存和双 Python 回归仍待实测。

## 选择理由与后续替代

FastEmbed 的该模型条目为中文、512 维、512 token 输入截断，模型体积估计 0.09 GB；这是权重估计，不是全部安装空间或运行内存。其 ONNX 来源为 Qdrant 的转换仓库。模型不是聊天模型，职责是生成检索向量；DeepSeek 继续负责文本规划/评估。[FastEmbed 0.9.0 模型条目](https://raw.githubusercontent.com/qdrant/fastembed/v0.9.0/fastembed/text/onnx_embedding.py)

CPU 路线适合作为这台 Ryzen 7 5800H 机器的初始验证，先使用少量线程/小批次并测内存；这是工程选型判断，不是已测性能结论。后续中文质量不足可比较 bge-base-zh-v1.5；大量多语言/长文场景再比较 BGE-M3，不一开始同时装多套模型。BGE-M3 官方说明支持多语言、1024 维与最多 8192 tokens。[BGE-M3 模型说明](https://huggingface.co/BAAI/bge-m3)

FastEmbed 使用 ONNX Runtime；普通 CPU 包不需要安装 PyTorch/CUDA，也不需要部署 Qdrant 服务。pip 会解析 numpy、onnxruntime 等运行依赖。[FastEmbed 官方安装](https://qdrant.github.io/fastembed/)、[依赖声明](https://github.com/qdrant/fastembed/blob/v0.9.0/pyproject.toml)

## 安装与首次验证

在 PowerShell 中执行：

```powershell
cd E:\no_game\git\kb_deskpal\kb_deskpal
conda activate deskpal_env
python --version
python -m pip install -r requirements-embedding.txt
python -m pip check
python scripts/check_embedding.py
```

0.9.0 是本轮已核对的发布版本，元数据支持 Python >=3.10；项目仍以 3.11/3.12 为支持验证目标，发布元数据不代替本机安装验证。[PyPI 0.9.0](https://pypi.org/project/fastembed/0.9.0/)

首次运行脚本会从公开模型仓库下载资源，保存到仓库 `data/models/fastembed/`（被 Git 忽略）。不读取 .env、不调用 DeepSeek、不上传测试句子；首次下载必须有网络。代码明确选择 CPUExecutionProvider，使用三条固定演示记忆，检查向量形状/有限值/归一化并输出检索排序。

预期 shape 为 `(3, 512)`。这只证明加载和基本推理，不能替代 V0.3 的中文检索基准或容量验收。脚本未在本轮实际推理，不能将“预期”写成通过。

缓存就绪后验证离线加载：

```powershell
python scripts/check_embedding.py --offline
```

`--offline` 使用 local_files_only；缺缓存时应失败，不自动下载。真实断网验收还应临时断网后执行同命令，观察没有下载请求。若 Hugging Face 下载不可用，可在可联网机器准备同一官方快照/缓存后转移并重测，不随意切换不明模型源。

## 查询/文档编码和项目接入

脚本只在查询侧添加 BGE 推荐的检索指令，文档侧不加；使用已归一化向量的点积作为余弦排序。query_embed 是模型相关接口，不能假定它总会自动补该指令。本项目会记录前缀策略，避免重复加前缀。[BAAI 使用说明](https://huggingface.co/BAAI/bge-small-zh-v1.5)、[FastEmbed 查询接口](https://raw.githubusercontent.com/qdrant/fastembed/v0.9.0/fastembed/text/text_embedding_base.py)

V0.3 接入拟通过 `EmbeddingProvider.embed_query()/embed_documents()`，具体库留在 adapter，Core 不 import FastEmbed。固定模型、后端、维度、归一化、前缀、token 分块和内容版本；模型文件 revision/校验值与完整依赖解析结果在实际验证后记录。库版本固定不等于权重快照已经固定。

改用新模型/后端/前缀时重建已有索引，不能混用向量。保存文本按 token 预算分块，不把 2000 个字符当成 512 tokens。相似度用于相关性排序，不代表事实可信度；阈值需在本项目中文数据集上校准，不能套用 0.5 即“相关”的规则。数据库仍保存确认状态、来源和更正版本，检索不得复活已删除/被替代事实。

依赖入口现为 requirements-embedding.txt，引用 pyproject.toml 的独立 embedding extra（FastEmbed 0.9.0）；维护规则见 [依赖清单](dependencies.md)。本机安装、模型推理与完整传递依赖锁定仍待验证，其他能力不要求安装 embedding 组。
