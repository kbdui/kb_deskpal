# 中文 Embedding 安装与运行

更新：2026-10-15，对应 0.3.0。实际 adapter 已接入 **BAAI/bge-small-zh-v1.5 + FastEmbed 0.9.0 CPU**，真实推理已在 Windows Python 3.12 验证。Python 3.11 的离线回归不代表该环境的真实模型验收。完整效果、性能与发布门槛见 [V0.3 测试报告](../versions/v0.3/test-report.md)。

## 依赖与缓存是两步

模型用于向量检索，不是聊天 LLM。运行使用 ONNX Runtime CPU，无需 PyTorch/CUDA、独立 Qdrant 服务；默认 embedding 关闭，基础 CLI/记忆不需要安装此组。

```powershell
conda activate deskpal_env
python --version
python -m pip install -r requirements-embedding.txt -c constraints/windows-py312.txt
python -m pip check
```

[Windows CPython 3.12 约束](../../constraints/windows-py312.txt)记录 43 项观察版本，涵盖项目开发/截图/embedding 和构建工具；不是带哈希锁，也不保证跨平台或 Python 3.11 相同解析。其他目标环境需重新核验，见[依赖指南](dependencies.md)。模型权重独立于 pip 安装。

首次准备模型需要操作者明确允许联网运行下载脚本：

```powershell
python scripts/check_embedding.py
```

脚本默认可以下载公开权重至 `data/models/fastembed/`，不会读取 .env、真实记忆或调用云聊天 API。它用固定演示文本检查 shape `(3, 512)`、有限值与归一化，仅是 smoke check。已经有缓存时用：

```powershell
python scripts/check_embedding.py --offline
```

`--offline` 仅允许缓存。本轮模型来自已有缓存，无须再次下载。运行应用的 `FastEmbedProvider` **始终** `local_files_only=True`；没有缓存或缺依赖时返回警告并走词法，不静默下载。另在新进程拦截 socket connect/connect_ex/create_connection，真实模型加载、512维查询、SQLite检索/删除通过，network_attempts=0；这不是物理断网抓包审计，详见版本测试报告。

## 启用与维护

```toml
[memory]
mode = "explicit_only"
embedding_provider = "fastembed"
model_cache_dir = "data/models/fastembed"
semantic_threshold = 0.55
max_context_bytes = 8000
```

model_cache_dir 按配置文件所在目录解析。启动使用 CPUExecutionProvider、默认四线程、512 维与小批量，无多进程模型池。首次检索可增量构建缺失的当前 revision 索引；也可显式执行：

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
