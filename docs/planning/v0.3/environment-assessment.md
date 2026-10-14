# V0.3 环境、工具与依赖评估

日期：2026-10-13。状态：**规划草案，尚未安装新依赖或实现功能**。路线依据是 `doc/` 两份原始设计及当前 [V0.2 交付](../../versions/v0.2/delivery.md)。开发顺序见 [开发计划](development-plan.md)，通过标准见 [验收边界](acceptance-boundaries.md)。

## 结论

可以立即开始记忆/用户模型/目标/主动提议的接口与 SQLite 开发。**原定 V0.3 不需要 Live2D、Qt、CUDA、Docker 或独立向量数据库服务。** 推荐先完成可追溯的数据语义，再联调本地 CPU embedding；不要先安装大型训练环境。

原始路线的 V0.3 是长期记忆、用户模型、目标系统与主动行为；Live2D、人格、情绪在 V1.0。原设计提出 SQLite + Vector Database 混合存储，但不限定产品或必须独立服务。本计划建议 SQLite 为事实来源、可重建向量索引为检索加速层；语义检索若作为本版验收能力，必须有真实 embedding 模型实测，不能用文字包含或假向量冒充。

## 本轮本机实查

| 项目 | 已查状态 | 判断 |
| --- | --- | --- |
| 系统 | Windows 11 家庭版中文版，64 位 | 保持 Windows 优先开发 |
| 主环境 | `D:/anaconda/Anaconda3/envs/deskpal_env`，Python 3.12.4 | 已具备 V0.2 editable 项目与开发工具 |
| 最低版本验证环境 | 仓库 `data/validation/py311`，Python 3.11.17 | 上一轮 V0.2 已完整回归；本轮未重跑业务测试 |
| SQLite | 3.53.4；本轮在内存库实际创建 FTS5 虚拟表成功 | 可开发事务、迁移和候选词法检索；不是语义检索证明 |
| CPU | Ryzen 7 5800H，8 核/16 线程 | 适合优先验证小规模 CPU 检索 |
| 显卡 | RTX 3050 Laptop GPU、AMD Radeon Graphics | 名称已查；未测显存/CUDA/驱动兼容，GPU不作为前提 |
| 内存 | 系统可见约 13.9 GiB，检查时空闲约 2.1 GiB | 是动态快照；模型推理峰值仍须实测，避免一次装载多套大型模型 |
| 可用磁盘 | D 约 111.3 GiB、E 约 144.7 GiB | 当前有缓存空间，具体模型体积未认证 |
| 已有模块 | pytest、mypy、python-dotenv、mss | 不需为本轮规划重复安装 |
| 当前缺少 | numpy、fastembed、onnxruntime、sentence_transformers、torch、chromadb、sqlite_vec、faiss、PySide6 | 此表不是全部安装清单；以下按方案取舍 |

本轮只检查模块存在性，未导入模型运行库或下载权重；没有读取真实配置/密钥，也未执行真实 API/截屏。

## 分阶段依赖选择

| 能力 | 建议方案 | 准备要求 |
| --- | --- | --- |
| Memory Evaluator | 复用 LLMProvider 的独立文本请求，严格 JSON 校验；离线规则替身 | 暂无需新 SDK；新增次数/输出/超时预算和开关，不把评估当工具规划 |
| 用户事实与目标 | SQLite + dataclass/Protocol；有来源、版本、状态及事务迁移 | 标准库足以起步；备份和迁移只作用于明确目标库 |
| 主动提议 | 复用 Scheduling 的持久事件，增加独立提议/outbox 服务与前台调度 | threading/queue/SQLite 足够；不要让 scheduler worker 直接调用 Core.run |
| 词法检索 | 保留 literal search；可评估 FTS5 子串/分词方案 | 已确认 FTS5 存在，中文 tokenization/短查询命中仍需测试，不宣称天然支持中文语义 |
| 语义检索（推荐独立里程碑） | EmbeddingProvider + CPU 本地多语言模型；SQLite 保存小规模向量及模型元数据 | 候选是 FastEmbed/ONNX Runtime 路线；可能引入 numpy，具体版本和多语言模型兼容性须先验证 |
| 可选替代 | sentence-transformers 路线或远端 embedding Provider | 前者另评估 PyTorch/资源占用；后者独立配置端点与密钥、显式启用文本外发；不同时装两套 |
| 大规模索引 | 后续按规模比较 sqlite-vec、FAISS、Chroma/Qdrant | 当前不预装；查询规模与重建成本证明需要后再选 |
| 可选通知/UI | 后续选择系统通知适配器或 PySide6 | 不属于推荐 V0.3 后端验收；不会为了“主动行为”强制引入桌宠窗口 |

现有 DeepSeek 配置可继续承担文本评估；本轮未重新调用真实 API。当前 LLMProvider 只有 chat/generate，不能直接充当 embedding 接口，也不能假定聊天模型或 API key 能调用未核验的 embedding 服务。

轻量 embedding 的具体候选在实施时应检查：Windows x64 wheel、Python 3.11/3.12、中文检索质量、最大序列长度、是否要求 query/passage 前缀、模型许可、下载来源、版本/校验值和缓存路径。不要仅根据“多语言”名称验收；截断、量化和前缀须随模型版本固化。

## 联网核验状态与下一步

本轮官方文档的搜索工具、连接器和直接 HTTPS 获取均失败。因此 **FastEmbed/ONNX 的确切兼容版本、候选模型清单和 FTS5 中文 tokenizer 方案尚未认证**；下列是后续核验入口，而非本轮已读且通过的证据：

- [FastEmbed 官方仓库](https://github.com/qdrant/fastembed)
- [ONNX Runtime 官方安装说明](https://onnxruntime.ai/docs/install/)
- [SQLite FTS5 官方文档](https://www.sqlite.org/fts5.html)

实施时先在隔离环境固定一套 CPU 依赖，安装和权重下载前明确包/版本/来源/磁盘位置，再验证两 Python 版本可导入、真实向量有限且维度稳定、中文用例有效、断网缓存可运行及内存峰值。模型未准备好时可以继续 SQLite/接口开发；语义模块必须标为未验收或明确降级，不能伪报语义通过。本轮不修改 pyproject.toml、deskpal_env 或用户 .env。
