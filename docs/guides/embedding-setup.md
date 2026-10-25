# 中文 Embedding 安装与运行

2026-10-23 新增维护入口：GUI“配置与数据维护”或 CLI `--maintenance cache_status/cache_verify/cache_prepare/cache_clear` 支持托管准备、明确下载许可、离线验证、带 revision 的自有资产清理及使用中租约保护，见[产品化操作说明](../versions/v0.9/operations.md)。继 fake SDK、硬链接及 Windows 原生锁测试后，已补齐[真实托管 BGE 链路](../versions/v0.9/managed-embedding-live-report.md)：从新缓存下载约 95 MB 模型、发布清单并复核 SHA-256、新进程离线 CPU 生成 512 维归一化向量，网络尝试为零。此证据不包含 GUI 物理桌面交互。旧脚本产生的非托管缓存可验证，不能直接当作有清理资格的托管目录。

更新：2026-10-15，对应 0.3.0。实际 adapter 已接入 **BAAI/bge-small-zh-v1.5 + FastEmbed 0.9.0 CPU**，真实推理已在 Windows Python 3.12 验证。Python 3.11 的离线回归不代表该环境的真实模型验收。完整效果、性能与发布门槛见 [V0.3 测试报告](../versions/v0.3/test-report.md)。

## 依赖与缓存是两步

模型用于向量检索，不是聊天 LLM。运行使用 ONNX Runtime CPU，无需 PyTorch/CUDA、独立 Qdrant 服务。内置默认与公开样例仍关闭 embedding；当前工作区的本地 `config.toml` 已启用 `fastembed` 并引用 `data/models/bge-small-zh-v1.5`，原 `data/models/fastembed` 旧缓存保留。这些模型权重和真实配置不随 Git 分发。

当前模型已从诊断目录迁入应用目录并通过[新路径验证](../versions/v0.9/managed-embedding-live-report.md)。已有环境使用 `conda activate deskpal_env`，无需重新下载；先完全退出同 data_dir 的应用，再运行：

```powershell
python -m deskpal --config config.toml --maintenance cache_status
python -m deskpal --config config.toml --maintenance cache_verify
```

以下安装步骤供新环境使用。沿用当前启用 BGE 的配置时必须具备 embedding 依赖；只安装基础或 GUI 分组不会安装它，缺依赖时回退词法检索。

```powershell
conda activate deskpal_env
python --version
python -m pip install -r requirements-embedding.txt -c constraints/windows-py312.txt
python -m pip check
```

[Windows CPython 3.12 约束](../../constraints/windows-py312.txt)记录 44 项观察版本，涵盖项目开发/截图/embedding、tzdata 和构建工具；不是带哈希锁，也不保证跨平台或 Python 3.11 相同解析。其他目标环境需重新核验，见[依赖指南](dependencies.md)。模型权重独立于 pip 安装。

新机器没有模型时，可经 GUI 维护或 `--maintenance cache_prepare` 明确允许下载，先保存一个空/不存在的缓存路径，见[用户维护步骤](../user-guide/maintenance.md)。已有托管模型使用 `cache_verify`，不要重复准备非空目录。

开发者重新验证托管维护完整链路可运行 `python scripts/check_managed_embedding.py --root <不存在的专用目录> --allow-download`（父目录须已存在）；此脚本会下载真实模型并生成独立证据，不自动修改应用配置。模型仍留在该诊断 root 时，使用同一 root 加 `--offline` 可只复查。本次模型已搬出旧 `live-escalated`，不能再用旧 root 复查；也不要把应用缓存路径直接用作诊断 `--root`。脚本不读个人配置/凭据、不安装依赖，详见[真实链路报告](../versions/v0.9/managed-embedding-live-report.md)。

下列旧脚本只验证 adapter，不生成托管清单；无缓存时默认允许下载：

```powershell
python scripts/check_embedding.py
```

脚本默认可以下载公开权重至 `data/models/fastembed/`，不会读取 .env、真实记忆或调用云聊天 API。它用固定演示文本检查 shape `(3, 512)`、有限值与归一化，仅是 smoke check。已经有缓存时用：

```powershell
python scripts/check_embedding.py --offline
```

旧脚本的 `--offline` 仅允许缓存，默认路径仍为旧 `data/models/fastembed`；若检查当前应用资产，须再加 `--cache-dir data/models/bge-small-zh-v1.5`，完整托管校验优先使用维护入口。运行应用的 `FastEmbedProvider` **始终** `local_files_only=True`；没有缓存或缺依赖时返回警告并走词法，不静默下载。历史 V0.3 的新进程真实推理、检索/删除证据见对应版本报告；当前托管链路与迁移后验证另见上方报告，网络 guard 证据均不等同物理断网抓包。

## 启用与维护

```toml
[memory]
mode = "explicit_only"
embedding_provider = "fastembed"
model_cache_dir = "data/models/bge-small-zh-v1.5"
semantic_threshold = 0.55
max_context_bytes = 8000
```

以上与当前本地启用值一致；合并到已有 `[memory]` 表，不要重复声明表头。`model_cache_dir` 按配置文件所在目录解析，代码未改内置默认路径。启动使用 CPUExecutionProvider、默认四线程、512 维与小批量，无多进程模型池。首次检索可增量构建缺失的当前 revision 索引；也可显式执行：

```powershell
python -m deskpal --config config.toml --memory rebuild-index
```

CLI 管理入口强制离线聊天 Provider，不需要云 key；重建索引不意味着接受候选或提升来源资格。模型文件、tokenizer、配置的资产 SHA-256 和预处理版本都纳入 `EmbeddingManifest`；重建/查询必须匹配完整 manifest 和断言 revision，不混用旧模型向量。

查询侧仅添加一次 `为这个句子生成表示以用于检索相关文章：`，文档不加；向量校验有限值/维度并 L2 归一化。adapter 使用未截断 tokenizer 预检，超过 512 tokens 的完整断言拒绝语义编码并保留词法路径。当前没有分块器，因此不能把“2000 中文字符”误作模型可完整编码的长度。

权重/SDK/查询前缀/归一化或 token 策略变化需要新 manifest 并重建。缓存损坏、模型初始化/编码失败、坏向量、索引版本不匹配均显式 warning；原文和候选确认不受索引失败改变。不要把相似度当作事实可信度。

## 效果与资源验证

版本化合成数据集：`tests/fixtures/memory-retrieval-zh.json`。独立校准 24 正/12 负确定阈值 0.55，再在 80 正/20 负 held-out 上一次评估：真实语义融合 Recall@5=0.8375、无关误召回=0.10；词法 Recall@5=0.625，语义补救 17 条。阈值不可依据 held-out 重调。

```powershell
python scripts/benchmark_memory.py --output data/validation/v03/benchmark-memory.json
```

5,000 条容量测试分别记录模型加载、编码、预热完整查询与峰值内存。初版预热 p95 约65秒；最终 5,000 条已有索引、20次预热查询 p95=1.7971秒，模型/检索峰值增量297.4MiB，达到 ≤2秒/≤1GiB 门槛。冷加载、首次构建和测量局限单列于 [V0.3 测试报告](../versions/v0.3/test-report.md)，不能把预热结果当冷启动 SLA。

这些是项目小规模合成基准，不是公开中文 benchmark 或真实私人对话质量认证。其他模型、多语言、长文分块或 reranker 的比较属于后续设计，尚未接入。
