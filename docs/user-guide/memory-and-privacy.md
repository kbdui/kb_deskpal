# 记忆与隐私

默认 `memory.mode = "explicit_only"`，普通聊天不自动写入长期记忆。明确输入“记住：内容”会在模型调用前保存；也可在记忆页或 CLI 显式保存。

## 保存与审核

```powershell
python -m deskpal --memory remember --content "称呼我为小明" --local-only
python -m deskpal --memory list --history
python -m deskpal --memory profile
```

`--local-only` 表示该记忆不能发给云模型。可支持的画像字段为 `preferred_name/reply_language/verbosity`，画像需要相应的结构化记录，任意文字不会自动成为全部画像字段。

显式启用 `candidate_review` 后，可选评估流程会提取候选；候选仍需在记忆页接受或拒绝。产生候选不表示已经成为可用长期记忆。语义检索仅是找到相关记录的辅助手段，相似度不等于事实可信度。

当前工作区已启用本地 BGE CPU 语义检索，模型引用 `data/models/bge-small-zh-v1.5`。它将查询与合资格记忆编码为向量，配合词法检索查找相关记录；启用它不会自动开启候选提取、保存普通聊天或改变来源用途许可。向量在本机计算，后续允许发送给云聊天模型的记忆内容仍受用途限制。模型路径和离线校验见[数据维护](maintenance.md)。

## 更正与遗忘

先读取当前 ID 和 revision，再进行修改：

```powershell
python -m deskpal --memory correct --memory-id MEMORY_ID --revision REV --content "称呼我为小林"
python -m deskpal --memory forget --memory-id MEMORY_ID --revision REV
```

每次成功写入后重新读取回执；更正可能生成新 ID 和 `supersedes` 关系。不要复用旧 revision。`--temporal` 表示时间变化、保留旧有效时段，适用于确实需要历史记录的更正。

遗忘先持久阻断使用，再清理记忆与明确派生工作副本。失败保持 Pending，启动先续办；未完成前相关内容不能重新显示或派发。GUI 提示“未提交”表示 Busy/版本冲突等原因导致尚未受理，可以重新加载后主动提交；已提交但 Pending 时应使用“重试清理”。

CLI 可查看和重试隐私流程：

```powershell
python -m deskpal --privacy status
python -m deskpal --privacy retry
```

## 哪些数据会保留

| 数据 | 保留与删除边界 |
| --- | --- |
| 会话上下文 | 本次运行有界保存；关闭会话清空，任务记录独立保留 |
| 长期记忆及明确派生工作副本 | 按来源关系阻断、清理；失败保持 Pending |
| 独立目标、日程 | 不按相似文字推断关联并删除 |
| 历史日志、备份、导出、工作区文件 | 有独立保留范围，记忆遗忘不会自动清除全部副本 |
| 外部或云端副本 | 本地遗忘不保证外部删除，需要在对应服务处理 |

四库备份包含私人记忆和任务内容，未加密；恢复旧备份可能重新引入后来已遗忘的数据。参见[数据维护](maintenance.md)。

## 云用途怎样控制

GUI 请求默认 `local_only`，会阻止整次云规划。主动选择 `cloud_allowed` 也不能解除记忆、目标、绑定等来源的 `local_only`。有界会话历史、偏好、自定义人格等新增表达数据还受独立类别许可约束，默认关闭。

CLI 普通 `--request` 按所配置 provider 运行；需要明确标注请求用途时用：

```powershell
python -m deskpal --workflow submit --text "time" --use-policy local_only
```

在云 provider 下，这可能因用途限制停在 `NeedsReview`；需要本地执行时先选择离线配置。管理记忆、互动、人格和隐私的入口不经过普通云规划。

分享排错材料时，只提供固定故障回执和必要的非秘密路径；不要分享 `.env`、数据库、备份、含秘密的配置原文副本或完整私人任务结果。
