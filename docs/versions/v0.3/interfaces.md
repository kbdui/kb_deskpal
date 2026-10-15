# V0.3 记忆接口契约

对应代码版本 **0.3.0**，日期 2026-10-15。本文描述实际实现；[原规划](../../planning/v0.3/development-plan.md)和[完整机制提案](../../planning/companion-model/README.md)中的其他接口不自动成为承诺。V0.2 工具、调度、双授权与 Sandbox 契约继续适用。

## 模块与数据

实际入口为 `memory/contracts.py`、`repository.py`、`coordinator.py`、`retrieval.py`、`embedding.py`、`evaluation.py`。Application 工厂注入 `MemoryCoordinator`，核心事实源为 `SQLiteMemoryRepository`，数据库 schema 为 v2。

| 类型 | 实际含义 |
| --- | --- |
| `MemorySource` | 原文、来源类型、owner/session/task、带时区时间和 `use_policy`；主机生成身份，模型不得提供授权 |
| `MemoryDraft` | 完整断言与原文 quote、claim type、kind、subject/predicate/value、scope、画像字段、重要度/抽取分数、有效区间、过期、pin、半衰期 |
| `MemoryItem` | ID、递增 revision、状态、来源、断言、记录/接受时间、supersedes 和更正原因 |
| `RetrievalBundle` | records、`(id, revision)` 依赖、epoch、warnings、UTF-8 bytes_used |
| `ProfileEntry` | key/value、scope、记忆 ID/revision、用途策略；是当前投影，不另造事实 |
| `DeletionReceipt` | 删除 revision/epoch、关联 task IDs、已清除和保留范围 |
| `EmbeddingManifest` | model、dimensions、version；索引须匹配模型资产、预处理和内容 revision |

状态枚举为 candidate/accepted/disputed/superseded/archived/rejected/deleted/legacy；枚举存在不意味着全部状态都有 CLI 转换入口。claim type 为 user_report/observed_event/external_claim/hypothesis/legacy。**accepted 表示允许使用，不能证明客观真实**；抽取分数和向量分数均不能提升证据资格。DTO 为冻结 dataclass。

## 写入、审核与更正

`MemoryCoordinator.remember(content, *, task_id=None, use_policy=CLOUD_ALLOWED, profile_key='', profile_value='', scope='global') -> MemoryItem` 是显式保存。内容须非空、UTF-8 ≤ 8000 bytes；明显凭据形式被拒绝，此检查不能识别所有自然语言秘密。

Core 的 `记住：内容`、`记住:内容`、`记住 内容` 在检索、Planner 和任何 LLM 调用之前保存并返回回执，不再依赖聊天模型响应。独立管理入口 `AgentCore.memory_command(action, **parameters)` 与请求共用串行门，管理载荷不送模型，日志只记录操作名。它不是自然语言更正意图解析器。

- `accept(id, expected_revision)` / `reject(...)` 仅审核当前 candidate，CAS 不匹配抛 `RevisionConflict`。候选默认 TTL 七天，到期在仓储访问时清理；不是常驻清理服务。接受时清除主机默认候选期限。
- `correct(id, expected_revision, content, *, temporal=False)` 只更正当前 accepted。返回新的记忆 ID，旧项增加 revision 并 superseded；不能假设原 ID 就是新项。
- `temporal=True` 表示偏好随时间变化：旧项保持 accepted，但截至变更边界，新的事实从边界生效；普通更正将旧断言排除。
- 已确认的画像冲突不会静默覆盖；必须按当前 revision 更正。
- `record_execution(task_id, summary)` 保存带工具来源的 observed_event，以 task 去重；相同任务的不同证据被拒绝，不覆盖已经更正的记录。工具成功后可选保存/评估失败只给 warning，不要求重做工具。

`mode` 为 `explicit_only`（默认）、`candidate_review`、`paused`。默认普通聊天不落长期记忆；工具完成可保存有界事件。candidate_review 每个根 task 最多一次独立评估，使用持久化消费标记，失败/空结果/拒绝/删除都不会隐式重试。paused 停止自动抽取与工具事件保存，显式管理仍可用；不等于全链路隐身。

## 画像、检索与隐私

仅支持 `preferred_name`（1–40 字符）、`reply_language`（zh/en）、`verbosity`（brief/detailed）。主机识别整句明确偏好，例如“叫我小明”“用中文回答”“请简短回答”；`--profile-key/value` 也必须与原文匹配。只有当前、有效、accepted、user_report、用户消息/确认来源才能进入投影。模型对文档、假设或他人的推测不能成为画像。

当前检索只使用 owner=`local`、scope=`global`；其他 scope 可存储但不参与当前检索/画像。云模型模式在检索前过滤 `local_only`；本地管理/导出可以查看这些内容。`local_only` 保护该记忆出云用途，不覆盖用户本轮重新键入的相同文本或历史日志。

词法与可选语义排名融合，画像优先，重要度与时间衰减用于相关性平分排序；不因访问次数增加真实性，不自动硬删 accepted。允许返回空，无 recent fallback。默认最多五条、8000 UTF-8 bytes；预算包含完整带来源 JSON 证据，放不下则整条省略，不能裁断断言。证据以数据而非工具/系统指令提供。

`MemoryRetriever.retrieve(query, *, as_of=None)` 支持显式时间查询（见源码签名），历史有效断言可参与 as_of 检索，仍须符合当前过期/删除/用途规则。索引保留可供历史查询的当前 accepted 历史区间；旧 revision、候选、legacy 和已删项不会因向量得分进入上下文。Core 在外发/执行边界再次校验 revision；依赖被改、删、过期时 Waiting 私有计划及授权失效，须重新提交和确认新计划。

## 删除、迁移与兼容

`forget(id, expected_revision)` 对该 ID 所有修订中引用的来源求共享来源闭包，保守删除共享原文的所有关联记忆、历史、向量和画像投影；保留无正文 tombstone 与不透明去重标记，防止来源重放。Core 同时清理相关 Task 文本/结果和 Waiting 计划。自定义 Task 存储不支持 scrub 时回执会列为保留。删除不是逐字节介质擦除，也不清除已经存在的 execution.jsonl、`.v1.bak`、用户导出、外部副本或服务端数据。

打开 schema v1 文件前以 SQLite backup API 创建并验证 `memory.sqlite3.v1.bak`；已有同名备份时拒绝覆盖，需操作者核验恢复。迁移事务保留旧 ID/时间/元数据，将旧记录标为 legacy，排除默认检索与画像。未知 schema 拒绝启动。迁移前自行保护真实数据；此文不授权删除备份。

`SQLiteMemoryManager` 仍为 schema v1 旧兼容类/fixture；工厂不再使用它。Coordinator 保留 `save/search/delete` 适配：旧 save 不允许覆盖现存 ID，写入 legacy 隔离项；search 走新检索，delete 走删除闭包。这不是两个仓储可同时修改同一文件的许可。

## 配置与可选模型

`[memory]` 默认 `mode='explicit_only'`、`embedding_provider='disabled'`、`model_cache_dir='data/models/fastembed'`、`max_context_bytes=8000`、`max_candidates=5`、`semantic_threshold=0.55`、`evaluation_timeout_seconds=15.0`、`evaluation_max_output_tokens=2048`。评估 timeout ≤ 60s、输出上限 ≤ 8192 tokens；每次评估只调用一次无工具的 LLM。严格 schema 拒绝畸形 JSON、重复键、非有限分数、越界数量、无原文 quote、工具调用及未支持字段；整个结果校验后才入候选。

FastEmbed 0.9.0 adapter 使用已存在缓存、CPUExecutionProvider、四线程、512 维 BAAI/bge-small-zh-v1.5。查询仅加一次中文前缀，文档不加；L2 归一化，manifest 含资产 SHA-256 和预处理版本。超过真实 tokenizer 的 512 tokens 时拒绝完整断言的语义编码，保留词法路径，不静默截断。模型缺失/异常、坏索引、版本不匹配给 warning 并退化词法，运行时不自动下载。重建不改变来源确认资格。

## 命令行

管理命令强制离线 Provider，不需要云 key，也不调用 Planner；可继续使用配置目录和模型缓存。示例 ID/revision 必须换成上一条实际 JSON 返回值。

```powershell
python -m deskpal --memory remember --content "叫我小明" --local-only
python -m deskpal --memory list --history
python -m deskpal --memory list --status candidate
python -m deskpal --memory profile
python -m deskpal --memory accept --memory-id ID --revision 1
python -m deskpal --memory reject --memory-id ID --revision 1
python -m deskpal --memory correct --memory-id ID --revision 1 --content "叫我小林"
python -m deskpal --memory correct --memory-id ID --revision 1 --content "请详细回答" --temporal
python -m deskpal --memory forget --memory-id ID --revision 1
python -m deskpal --memory export --history
python -m deskpal --memory rebuild-index
```

交互入口支持 `/remember 内容`、`/memory list|profile|export|rebuild-index`、`/accept ID REV`、`/reject ID REV`、`/correct ID REV 内容`、`/forget ID REV`；高级 policy/history/temporal 用一次性参数。管理成功退出 0，配置/操作错误 1；正常请求 Completed/Failed/Waiting 仍为 0/1/2。导出为 stdout JSON，目前没有 importer。
