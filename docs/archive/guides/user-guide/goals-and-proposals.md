> 历史存档：保留当时的设计、失败与验收边界；不代表当前实现或新的执行授权。当前入口见 [文档导航](../../../README.md)。

# 目标与主动关注

GUI 入口统一在“安排”的目标与关注区域。关联对象按当前任务、计时器和日程的名称选择；原始 ID 留在技术详情或 CLI。业务来源的用途属性仍独立于模型连接选择，不能由云密钥自动扩大。

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

## 主动关注与检查

主动能力默认启用，使用设置中已配置的模型；已有配置明确写 `enabled=false` 时仍尊重关闭选择。当前档案运行时，系统接收目标截止、绑定日程／计时到期和任务进展等机会，结合最新交流判断是否值得开口；没有必要时保持安静。模型可以有理由地安排稍后复查，不把未回复当成空闲或催促理由。

“安排 → 关注”显示正在关注什么、依据和检查状态，无需逐条接受。原来的 Proposal 继续作为内部事件来源及历史兼容记录，不再是日常主动聊天的人工待办。普通主动聊天经过与用户请求相同的 AgentCore；需要许可的实际操作仍显示准确操作并等待确认。

聊天窗口“通知”菜单分别控制主动聊天与提醒，也可直接说“今天别主动找我”“连提醒也暂停，明早八点恢复”。暂停通知不删除日程，不取消任务；暂停期间的提醒保留在收件箱，恢复后不集中补弹。未激活角色不运行后台，重新进入时也不会补播全部旧关注。

## 多步任务暂停与继续

任务卡支持“暂停任务”“继续任务”和“追加要求”。收到暂停请求后先显示“正在暂停”；当前调用及已决定的一组步骤可以完成，到下一次模型判断前才显示“已暂停”。此时执行线程释放，可以处理其他工作。若本轮已直接进入收尾，任务可以正常完成，界面如实显示终态。

继续使用原任务及执行结果，模型结合最新状态与追加要求决定下一步，不重放已完成操作，不自动授予工具权限。暂停期间可以追加要求，但追加本身不恢复；重启后也保持暂停。取消会结束后续工作，取消后不能按暂停继续，已完成的外部操作保留。
