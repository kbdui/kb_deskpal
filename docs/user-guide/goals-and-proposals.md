# 目标与提议

GUI 入口统一在“安排”的目标与建议区域。关联对象按当前任务、计时器和日程的名称选择；原始 ID 留在技术详情或 CLI。业务来源的用途属性仍独立于模型连接选择，不能由云密钥自动扩大。

目标用于记录你明确想完成的事情，里程碑用于拆分人工可确认的阶段，来源绑定把日程、计时器或任务与目标联系起来。它们不自动生成任意执行链。

## 建立和完成目标

GUI 目标页可创建 Goal、Milestone 和 Binding。CLI 示例：

```powershell
python -m deskpal --workflow goal-create --title "整理项目资料" --criteria "我检查目录并确认资料归档完成"
python -m deskpal --workflow goals
python -m deskpal --workflow goal --id GOAL_ID
python -m deskpal --workflow milestone-create --goal-id GOAL_ID --expected-revision REV --title "整理说明文档" --criteria "我确认文档可查阅"
```

`milestone-create` 需要父目标的当前 revision。更新、绑定等写入也要求当前 revision，冲突时重新读取。新目标和来源绑定默认 `local_only`；配置云 key 不会自动扩大其用途。

完成目标需要用户提供证据：

```powershell
python -m deskpal --workflow goal-status --id GOAL_ID --expected-revision REV --state Completed --evidence "已人工核对归档目录，满足成功标准"
```

仅在实际核对后提交完成证据。任务 `Completed` 只是进展证据，不能自动把目标标为完成。

## 绑定来源

先取得真实日程、计时器或任务 ID，再建立绑定：

```powershell
python -m deskpal --workflow bind --goal-id GOAL_ID --expected-revision REV --source task --source-id TASK_ID
python -m deskpal --workflow bindings --goal-id GOAL_ID
```

`--source` 可选 `calendar/timer/task`；绑定到里程碑可附加 `--milestone-id`。绑定和目标有各自版本，始终以最新查询回执为准。

## 启用本地提议

主动功能默认关闭。合并到配置并重启：

```toml
[proactive]
enabled = true
timezone = "Asia/Shanghai"
quiet_start = "22:00"
quiet_end = "08:00"
```

现有规则围绕目标截止窗口、已绑定日程/计时到期、关联任务完成后的里程碑复核生成 Proposal。它们使用本地规则，不后台调用云、采屏或执行工具。首次启用从事件尾部开始，不默认补读全部历史。

查看并处理提议：

```powershell
python -m deskpal --workflow proposals --state all
python -m deskpal --workflow proposal --id PROPOSAL_ID
python -m deskpal --workflow accept-proposal --id PROPOSAL_ID --expected-revision REV
python -m deskpal --workflow ignore-proposal --id PROPOSAL_ID --expected-revision REV
```

接受与忽略是二选一示例；每次重新读取 revision。接受会原子建立唯一 Task/Submission，后续仍检查来源用途和工具授权。不要把“已接受”理解成“已执行”。

默认提议 TTL 为 86400 秒，从到期时间起算；安静时段只控制展示。来源库重建、事件身份变化或需补读历史时必须显式复核，详细管理参数见[CLI 帮助](cli.md)与[历史后端操作说明](../versions/v0.4/operations.md)。
