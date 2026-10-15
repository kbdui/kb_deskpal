# V0.3 环境、工具与依赖评估

状态更新：**本页为开工前环境快照，已进入 0.3.0 实施**（2026-10-15）。下文“缺少/未安装/未实现”只描述当时状态，不能用于判断当前环境。最新 [依赖指南](../../guides/dependencies.md)、[Embedding 指南](../../guides/embedding-setup.md)、[V0.3 测试报告](../../versions/v0.3/test-report.md)提供实际状态。

当前已有 FastEmbed 0.9.0 CPU 模型缓存并在 Python 3.12 运行真实 512 维推理；运行适配器 cache-only，不下载。Python 3.11/3.12 离线回归分别验证，真实 FastEmbed 只验证 3.12。Windows CPython 3.12 的 43 项依赖观察约束已记录，非哈希锁或跨平台保证。此前空闲内存/磁盘是历史动态数值，最终资源门槛须看实际报告。

## 原始环境评估快照（2026-10-13/14）

初查日期：2026-10-13；更新：2026-10-14。状态：**规划草案，尚未安装新依赖或实现功能**。按 [分阶段路线](../roadmap.md) 收窄为记忆/画像/检索，目标和主动提议移到 V0.4。本机硬件/模块表仍是 10-13 的检查快照；本轮新增官方依赖核验，不把旧空闲内存当当前可用量。开发顺序见 [开发计划](development-plan.md)，通过标准见 [验收边界](acceptance-boundaries.md)。

## 结论

可以立即开始记忆/用户模型的接口与 SQLite 开发。**本版不需要 Live2D、Qt、CUDA、Docker 或独立向量数据库服务。** 推荐先完成可追溯的数据语义，再联调本地 CPU embedding；首选方案见 [Embedding 安装指南](../../guides/embedding-setup.md)。

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
| 用户事实 | SQLite + dataclass/Protocol；有来源、版本、状态及事务迁移 | 标准库足以起步；备份和迁移只作用于明确目标库 |
| 目标/主动提议（V0.4） | 复用 Scheduling 的持久事件，增加独立提议/outbox 服务与前台调度 | 作为下一版准备；不要让 scheduler worker 直接调用 Core.run |
| 词法检索 | 保留 literal search；可评估 FTS5 子串/分词方案 | 已确认 FTS5 存在，中文 tokenization/短查询命中仍需测试，不宣称天然支持中文语义 |
| 语义检索（本版独立里程碑） | EmbeddingProvider + BAAI/bge-small-zh-v1.5 CPU；SQLite 保存小规模向量及模型元数据 | 首选 FastEmbed 0.9.0，依赖解析包含 ONNX Runtime/numpy；本机安装与中文效果待验收 |
| 可选替代 | sentence-transformers 路线或远端 embedding Provider | 前者另评估 PyTorch/资源占用；后者独立配置端点与密钥、显式启用文本外发；不同时装两套 |
| 大规模索引 | 后续按规模比较 sqlite-vec、FAISS、Chroma/Qdrant | 当前不预装；查询规模与重建成本证明需要后再选 |
| 可选通知/UI | 后续选择系统通知适配器或 PySide6 | 不属于推荐 V0.3 后端验收；不会为了“主动行为”强制引入桌宠窗口 |

现有 DeepSeek 配置可继续承担文本评估；本轮未重新调用真实 API。当前 LLMProvider 只有 chat/generate，不能直接充当 embedding 接口，也不能假定聊天模型或 API key 能调用未核验的 embedding 服务。

轻量 embedding 的具体候选在实施时应检查：Windows x64 wheel、Python 3.11/3.12、中文检索质量、最大序列长度、是否要求 query/passage 前缀、模型许可、下载来源、版本/校验值和缓存路径。不要仅根据“多语言”名称验收；截断、量化和前缀须随模型版本固化。

## 官方核验更新与下一步

10-13 的联网获取失败。10-14 已成功核对 FastEmbed 官方 0.9.0 发布元数据、对应源码与 BAAI 模型说明：支持列表含所选中文模型，CPU 可用方案已明确；这仍不是本机运行/中文效果认证。FTS5 中文 tokenizer 方案尚未实测。来源及安装步骤集中于 [Embedding 指南](../../guides/embedding-setup.md)。

- [FastEmbed 官方仓库](https://github.com/qdrant/fastembed)
- [ONNX Runtime 官方安装说明](https://onnxruntime.ai/docs/install/)
- [SQLite FTS5 官方文档](https://www.sqlite.org/fts5.html)

实施时按指南安装并记录完整解析结果和权重快照，再验证两 Python 版本可导入、真实向量有限且维度稳定、中文用例有效、断网缓存可运行及内存峰值。模型未准备好时可以继续 SQLite/接口开发；语义模块必须标为未验收或明确降级，不能伪报语义通过。当前新增了 pyproject.toml 的独立 embedding extra 与 requirements 安装入口，规则见 [依赖维护](../../guides/dependencies.md)；尚未安装、实际推理或修改用户 .env。
