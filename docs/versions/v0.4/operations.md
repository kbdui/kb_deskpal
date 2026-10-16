# V0.4 运维与离线演示

以下是当前实现的操作说明；接口、状态和完整配置上界见 [实际接口](interfaces.md)。示例不代表资源/安装/全量验收已经通过，验收要求见 [验收边界](../../planning/v0.4/acceptance-boundaries.md)。

## 进程和数据目录

生产 CLI 是 `python -m deskpal`（安装后可用 deskpal）。一个 data_dir 同时只允许一个 Application。CLI 退出后 scheduler/规则线程停止，没有退出后仍运行的系统服务。再次启动重建 Timer 剩余时间，记录到期项；旧任务先按恢复证据复核，不自动恢复工具调用。

请使用已安装项目的环境或仓库根目录。未安装时，可在当前 PowerShell 会话设置 `$env:PYTHONPATH = (Resolve-Path .\src).Path`。用自己的 Python 路径替换示例解释器。下面完整闭环只访问临时合成目录；配置通过显式 environ 字典固定 offline，不读取个人 .env、云凭据、个人文档或屏幕。

## 完整闭环：Goal → Timer → Proposal → Task → 用户证据

这段 PowerShell 把 Python 脚本送给解释器。Timer 等待一秒，TemporaryDirectory 在退出后自动清理示例库。通过公开本地服务显式创建 Timer；接受的 Task 由离线 DirectToolPlanner 提议无副作用 echo，不把接受当成工具 grant。

```powershell
$Python = 'D:\anaconda\Anaconda3\envs\deskpal_env\python.exe'
@'
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from deskpal.application.factory import create_application
from deskpal.config import load_config
from deskpal.goals import GoalStatus, MilestoneStatus
from deskpal.planner.direct import DirectToolPlanner
from deskpal.tools.models import ToolCall

with TemporaryDirectory(prefix="deskpal-v04-example-") as temporary:
    root = Path(temporary)
    config = load_config(environ={
        "DESKPAL_LLM_PROVIDER": "offline",
        "DESKPAL_DATA_DIR": str(root / "data"),
        "DESKPAL_WORKSPACE_DIR": str(root / "workspace"),
        "DESKPAL_PROACTIVE_ENABLED": "true",
        "DESKPAL_PROACTIVE_QUIET_START": "00:00",
        "DESKPAL_PROACTIVE_QUIET_END": "00:00",
    })
    planner = DirectToolPlanner(ToolCall("echo", {"text": "Synthetic progress evidence"}, "demo-echo"))
    with create_application(config, planner=planner) as app:
        # First-enable tails must exist before the occurrence to be received.
        app.rules.poll()
        goal = app.goals.create("Review a synthetic milestone", "User reviews the echo evidence",
                                command_id="demo-goal")
        milestone = app.goals.add_milestone(goal.id, goal.revision, "First review", "User confirms evidence",
                                             command_id="demo-milestone")
        timer = app.scheduling.create_timer(1, label="Synthetic goal reminder")
        binding = app.goals.bind_source(goal.id, app.goals.get(goal.id).revision,
                                       "timer", timer.id, milestone_id=milestone.id,
                                       command_id="demo-binding")
        assert goal.use_policy == binding.use_policy == "local_only"
        time.sleep(1.1)
        app.scheduling.poll_due()
        app.rules.poll()  # A background poll may already have received the event.
        proposal, = app.proposals.list()
        task = app.accept_proposal(proposal.id, proposal.revision, command_id="demo-accept")
        result = app.wait(task.id, timeout=10)
        assert result.status.value == "Completed"
        assert app.goals.get(goal.id).status == GoalStatus.ACTIVE
        assert app.goals.get_milestone(milestone.id).status == MilestoneStatus.PENDING
        assert app.accept_proposal(proposal.id, proposal.revision,
                                   command_id="demo-accept").id == task.id

        # Explicit human commands, independent of the completed Task.
        checked = app.goals.complete_milestone(milestone.id,
            app.goals.get_milestone(milestone.id).revision,
            evidence="User inspected the synthetic echo evidence", command_id="demo-milestone-done")
        completed = app.goals.set_status(goal.id, app.goals.get(goal.id).revision,
            GoalStatus.COMPLETED, evidence="User confirmed the goal criteria", command_id="demo-goal-done")
        print(json.dumps({"proposal": app.proposals.get(proposal.id).status.value,
                          "task": result.status.value, "milestone": checked.status.value,
                          "goal": completed.status.value}, ensure_ascii=False))
'@ | & $Python -
```

预期四项分别为 Accepted、Completed、Completed、Completed。它只断言自己合成数据的闭环，未证明外部效果 exactly-once；Task 也没有自动完成 Goal。background poll 可能先处理事件，不应要求手动 poll 必须返回 1，应查询持久 Proposal。

Timer 在两次单次 CLI 调用之间到期时，需要重新打开 Application 等 scheduler 首轮，或在 Python 明确调用 `app.scheduling.poll_due(); app.rules.poll()`。如果首次启用时事件已经存在，会 tail 旧历史；需要补历史就显式选择有限 history，不能靠反复 poll 重发。

## CLI 独立合成目录与基本管理

这段 PowerShell 在全新的临时 cwd 内运行，不读个人 .env；只在 try 范围内覆盖 Provider/data/workspace/proactive 环境，之后恢复，不使用或显示云凭据。示例目录保留供查看，清理应在所有 Application 退出后由用户明确进行。不要指向日常个人 data_dir。

```powershell
$Python = 'D:\anaconda\Anaconda3\envs\deskpal_env\python.exe'
$DemoRoot = Join-Path ([IO.Path]::GetTempPath()) ('deskpal-v04-cli-' + [guid]::NewGuid().ToString('N'))
$DemoData = Join-Path $DemoRoot 'data'
$DemoWorkspace = Join-Path $DemoRoot 'workspace'
New-Item -ItemType Directory -Path $DemoRoot | Out-Null
$SavedEnvironment = @{}
foreach ($Name in @('DESKPAL_LLM_PROVIDER', 'DESKPAL_DATA_DIR', 'DESKPAL_WORKSPACE_DIR', 'DESKPAL_PROACTIVE_ENABLED')) {
    $SavedEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
}
Push-Location -LiteralPath $DemoRoot
try {
    $env:DESKPAL_LLM_PROVIDER = 'offline'
    $env:DESKPAL_DATA_DIR = $DemoData
    $env:DESKPAL_WORKSPACE_DIR = $DemoWorkspace
    $env:DESKPAL_PROACTIVE_ENABLED = 'false'

    $Goal = & $Python -m deskpal --workflow goal-create --title 'Synthetic goal' --criteria 'User verifies evidence' --command-id demo-goal | ConvertFrom-Json
    $Milestone = & $Python -m deskpal --workflow milestone-create --goal-id $Goal.id --expected-revision $Goal.revision --title 'First milestone' --criteria 'User approves' --command-id demo-milestone | ConvertFrom-Json
    $CurrentGoal = & $Python -m deskpal --workflow goal --id $Goal.id | ConvertFrom-Json
    & $Python -m deskpal --workflow goal-update --id $Goal.id --expected-revision $CurrentGoal.revision --progress-note 'Synthetic progress note' --command-id demo-note
    & $Python -m deskpal --workflow goals --limit 10 --offset 0
    & $Python -m deskpal --workflow source-identity
} finally {
    foreach ($Name in $SavedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($Name, $SavedEnvironment[$Name], 'Process')
    }
    Pop-Location
}
```

后文片段假设在相同的隔离环境内执行，且已把 GoalId/MilestoneId/TimerId/ProposalId/TaskId 变量设为真实查询返回值。数字 revision 仅演示参数位置，要换成当前对象值。Goal 新增子对象会增加父 revision；同 command ID 重试返回原成功快照，最新 revision 要重新查询。

```powershell
# deadline 必须带明确 offset；清除用 clear-deadline。
& $Python -m deskpal --workflow goal-update --id $GoalId --expected-revision 2 --deadline '2027-01-01T08:00:00+08:00' --command-id change-deadline
& $Python -m deskpal --workflow goal-update --id $GoalId --expected-revision 3 --clear-deadline --command-id clear-deadline

# bind 的 CAS 是父 Goal revision；默认 local_only。
& $Python -m deskpal --workflow bind --goal-id $GoalId --expected-revision 4 --milestone-id $MilestoneId --source timer --source-id $TimerId --command-id bind-timer
& $Python -m deskpal --workflow bindings --goal-id $GoalId
& $Python -m deskpal --workflow proposals --state Pending
& $Python -m deskpal --workflow proposals --state all
& $Python -m deskpal --workflow proposal --id $ProposalId

# 接受/忽略二选一，之后仍可查历史。
& $Python -m deskpal --workflow accept-proposal --id $ProposalId --expected-revision 1 --command-id accept-once
# & $Python -m deskpal --workflow ignore-proposal --id $ProposalId --expected-revision 1 --command-id ignore-once
& $Python -m deskpal --workflow task --id $TaskId
```

accept-proposal 是执行类命令，等待工作进入可报告结果；忙时仍持久排队。返回 AgentResult.task_id，Proposal 的 accepted_task_id 也可查询。默认低风险工具可能不需确认，Level 1/2 工具仍要按计划授权。云配置下默认 local_only 目标接受后保持待审，使用 offline 才可本地规划；不能给 accept 加 authorize 绕过审阅。相同 command ID 改载荷会拒绝，不提供 command ID 时每次生成新的 UUID。

## 恢复、准确 hash 和分级授权

CLI 退出后没有内存 grant。先查 task/plan/attempts，核对原请求、完整参数和既有效果，再决定是否可以恢复：

```powershell
$Review = & $Python -m deskpal --workflow task --id $TaskId | ConvertFrom-Json
$ExactPlanHash = $Review.plan.hash
# 仅无 attempt 的 NeedsReview 可 resume。
& $Python -m deskpal --workflow resume --id $TaskId
& $Python -m deskpal --workflow confirm --id $TaskId --plan-hash $ExactPlanHash --authorize write_text

# 高风险授权独立；普通 authorize 不能代替它。
# & $Python -m deskpal --workflow confirm --id $TaskId --plan-hash $ExactPlanHash --authorize-high-risk document_delete
# 只记录判断，不重新执行未知步骤。
# & $Python -m deskpal --workflow review-uncertain --id $UncertainTaskId --verdict unknown --evidence 'User has not established whether the effect occurred'
```

resume 无计划时重新规划并返回 AgentResult，有完好未执行计划时只返回 PlanSnapshot，Task 为 Waiting、Submission 保持 Blocked 并计入队列容量；confirm 才重新排队。没有 Submission 的低层 Task 不能 resume。confirm 拒绝错误/过期 hash、工具契约/workspace/来源/Memory 变化和已尝试计划。部分完成、失败或 Uncertain 的剩余工作必须新建明确剩余意图并重新确认，不重跑原完整计划。

`--workflow cancel --id $TaskId` 记录协作取消。若返回 Running/Planning 和 cancel_requested=true，当前工具可能仍在完成，不能据此声称效果已撤回。未知区间和取消意图可同时存在；Pending 遗忘期间只返回去正文控制回执，不能利用取消读回待清理内容。

## 消费者：检查身份后显式决定范围

```powershell
$Sources = & $Python -m deskpal --workflow source-identity --stream timer | ConvertFrom-Json
$ExpectedStore = $Sources.identity.store_id
$ExpectedGeneration = $Sources.identity.generation
& $Python -m deskpal --workflow diagnostics

# 明确从当前尾部继续，放弃该流旧 Pending inbox。
& $Python -m deskpal --workflow review-consumer --stream timer --expected-store-id $ExpectedStore --expected-generation $ExpectedGeneration --strategy tail --command-id review-timer-tail

# 确需补历史时另行选择这一个策略，不与 tail 连用当作重试。
# & $Python -m deskpal --workflow review-consumer --stream timer --expected-store-id $ExpectedStore --expected-generation $ExpectedGeneration --strategy bounded_history --after-id 0 --max-events 100 --command-id review-timer-history
```

source-identity 包含当前身份、每流 tail/anchor 和消费者；diagnostics 包含 inbox 处理原因。review 的 expected 字段必须等于当前身份，来源再变化会拒绝。tail 明确放弃旧接收范围，旧 Pending inbox 以合法原因终结；不会自动执行此操作。

bounded_history 的 after_id 必须是所选流真实事件 ID 或 0，不是另一流的全局 ID；max_events 1..10000。预算/复核尾界持久保存，到限额后跳到该复核尾界，之后新事件仍接收。原 delivery/business 去重保留，重读不等于重发。timer/calendar 分别复核。

`--workflow poll` 只在 proactive.enabled=true 时生成，返回 created 和 quiet 过滤后摘要。静默时仍可显式 proposals 查询；TTL 从原 due_at 起算，离线很久不会获得新 TTL。没有启动 Application 就没有后台 poll。

## 用户完成证据与配置用途

```powershell
& $Python -m deskpal --workflow milestone-status --id $MilestoneId --expected-revision 1 --state Completed --evidence 'User checked the result' --command-id milestone-done
$CurrentGoal = & $Python -m deskpal --workflow goal --id $GoalId | ConvertFrom-Json
& $Python -m deskpal --workflow goal-status --id $GoalId --expected-revision $CurrentGoal.revision --state Completed --evidence 'User confirmed all criteria' --command-id goal-done
```

Completed/Cancelled 是 Goal/Milestone 终态，不覆盖或重新打开；完成 Milestone 不自动完成 Goal，Task 全完成不代替用户证据。

Goal/Binding 默认 local_only。用户如需云处理，应明确修改原 Goal 和相关 Binding 的用途到 cloud_allowed，并产生新版本；绑定 Task 还受原 Task 用途约束。旧 Proposal/Task 不因单独改标签或配置 key 获得许可。

规则默认关闭。event_batch_size 上限 100，max_proposals_per_poll 上限 10，queue_capacity 默认 100/上限 10000；TTL 默认 86400 秒/上限 2592000 秒。其它配置上界见 [配置表](interfaces.md#当前配置和上界)。quiet 是 HH:MM，timezone 为可用 IANA 名称；缺时区数据明确拒绝，不静默退回 UTC。

## 删除、审计、备份与故障

```powershell
& $Python -m deskpal --workflow diagnostics
# Memory ID/revision 来自 Memory 管理查询，不使用 Goal ID。
# & $Python -m deskpal --memory forget --memory-id $MemoryId --revision $MemoryRevision
& $Python -m deskpal --workflow retry-cleanup
# 选择新明确文件名；backup 默认拒绝任何已存在目标。
$BackupPath = Join-Path $DemoRoot ('workflow-' + [guid]::NewGuid().ToString('N') + '.sqlite3')
& $Python -m deskpal --workflow backup --backup-path $BackupPath
```

forget 先持久阻断再跨库清理；Pending 不是完成。重启/retry-cleanup 续办，失败仍阻止旧正文显示/执行。无 pending 时 retry-cleanup 是 no-op，不把正常 Waiting 改成 NeedsReview。回执说明独立 Goal/日程、历史日志、备份、外部副本的保留范围；不按文字相似性删用户独立目标。

workflow backup 包含当时工作库 WAL，只备工作库，使用新文件原子发布；现有目标一律拒绝，不能覆盖其它库。没有一键跨库恢复命令。恢复前停止实例并保留现状，采用协调的停机备份；启动后检查 deletion job、Task attempt、consumer 身份/锚点。不要手工清 cursor/Started 来绕过异常，它会破坏去重和未知结果保护。

JSONL 同 event_id 可能重复；pending_audit_page 是当前页数，不是积压精确总数。审计投递不能重新执行工具，排查要同时看 diagnostics 与持久 task/attempt。worker、规则或清理错误应先修原因，再明确审阅，不能从日志文本构造执行命令。

常见退出码：0 为命令成功/Task Completed；2 为参数错误或 Waiting/NeedsReview，须结合 stderr/JSON 区分；1 为失败、拒绝或其它未完成状态；130 为用户中断。管理命令成功不等于 Goal 完成或队列清空。
