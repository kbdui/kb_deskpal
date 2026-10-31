# 文档与测试整理记录

> **已中止并交还用户。** 本次处理超出了文件整理范围，包含文档重写和测试删改。当前修改尚未完成最终复测；完整改动、快照与交接状态见[中止交接](2026-10-30-file-cleanup-handoff.md)。下文记录已经采取的操作，不表示用户已认可全部变更或工作已验收。

2026-10-30。本次整理分类、命名、当前说明和测试契约，不修改产品后端或 GUI 功能。整理前已有大量未提交开发内容，未重置、提交或覆盖到旧版本；原始文档、测试及涉及的配置/脚本先保存在本地 [整理前快照](../../data/validation/repository-cleanup/before-cleanup.zip)。真实配置、业务库、模型资产及封存沙箱资源没有参与清理。

## 文档

当前入口统一为 [文档导航](../README.md)、[用户指南](../user-guide/README.md)、[开发者指南](../developer-guide/README.md)。根 README 和架构说明改为 P1–P5 现状、P6 待实施；删除当前入口中重复的旧版成绩、默认关闭、三预设/六轴、人工候选审批及跨角色共享说明。两份长讨论提炼为[人格与记忆决定](../design/companion/character-memory-decisions.md)、[执行与生命周期决定](../design/companion/execution-lifecycle-decisions.md)，原文保留供追溯。

| 原分类 | 新位置与处理 |
|---|---|
| docs/versions/v0.x、docs/planning/v0.x | archive/releases/v0.x；同版旧计划放 planning 子目录 |
| docs/handoffs、旧 docs/architecture 审计 | archive/handoffs、archive/audits；审计文件改用 kebab-case |
| docs/planning/gui-refactor | archive/gui-refactor；统一短报告单独移至 reports/implementation-log.md |
| docs/planning/companion-model | 原始设计与讨论放 archive/companion-model；当前采纳决定在 design/companion |
| doc 中两份初始设计/交接 | archive/origins/system-design.md、development-handoff.md |
| docs/guides | 有效依赖与 embedding 指南并入 developer-guide，开发约定合并到 development-and-testing.md |
| 旧 roadmap | archive/roadmap-before-redesign.md；有效后续方向在 roadmap/post-release.md |

旧指南也留有整理前副本，不再作为当前导航内容。历史资料统一注明适用边界；V0.6 失败、封存对象、所有权限制与未完成实机证据没有压缩成“已通过”。历史命令中的旧路径和计数保留原貌，实际 Markdown 链接更新到新位置。开发计划、阶段实施记录、原始讨论和测试证据分别保留，不混称已经交付。

今后：当前业务结论写 design，实施与验收写 planning，持续短报告只追加 [implementation-log](../reports/implementation-log.md)，一次性维护写 maintenance。稳定指南不带日期，一次性记录用 YYYY-MM-DD，Markdown 文件使用英文小写 kebab-case。

## 测试

测试保持 unit/integration/gui 层次，内部按 agent、memory、workflow、maintenance、chat、pet 等职责分目录，去掉项目 v0.x、HM08、phase 编号文件名前缀。CLI 并入 integration/cli，门控 API 测试移至 live，人工滚轮探针移至 manual。Windows 隔离 mock 保留在 unit/execution_sandbox，仍参加默认离线收集；真实 schema 和原生探针协议版本不被视作过期内容。

共享窗口、维护、会话和提议夹具提取到 tests/support；仓库路径由 REPO_ROOT 统一定位。更新所有现存测试的脚本、配置样例、C 夹具、子进程 PYTHONPATH 与导入，以及诊断脚本的测试指纹路径。pytest 使用完整包名/importlib，默认 testpaths 仍是全部 tests；未增加 xfail、忽略目录或为了通过而新增跳过。

明确退役的内容：独立 InteractionState 规则/信号测试、旧预设与人格版本回滚/数值情绪归约器测试、对应旧整合链路，以及已移除的六轴/cue/人工偏好控制表单与信号分组断言。替代检查位于 role_context、multi_round_loop、feedback_learning、proactive_controls 和对应 GUI 测试。保留严格 JSON、工具拒绝、来源修订、隐私、恢复与预算检查；仍实现的低层 record_execution 兼容接口保留幂等和不覆盖检查。

表达协议改用仅 reply_text 的当前格式，保留重复键、非有限数、大小/深度、未知字段、工具调用拒绝、私密诊断和原生 JSON 单次调用检查。旧“默认七天删除候选”改为不确定记录可无期限保留；涉及到期清理的测试显式提供期限，继续验证正文清理与删除回执。补齐 Paused/AwaitingInput 的状态矩阵，schema 断言对应 workflow 5、memory 3，不把当前合法版本当作未知版本。

详细移动映射和逐项退役依据保存在本地 [path-map.json](../../data/validation/repository-cleanup/path-map.json)、[retired-tests.json](../../data/validation/repository-cleanup/retired-tests.json)；可用快照核对原始内容。新的执行入口见 [tests/README](../../tests/README.md)。

## 验证与剩余问题

测试由 6 Luna 高执行，父 Agent 复核。全部使用独立合成配置、工作区和数据库，Qt 离屏；未调用真实模型、下载资产或使用真实凭据。需要的 Windows 文件发布验证只针对合成临时资料。

整理前全量收集为 3258 项并有 1 个已删除模块导入错误；分组执行为 unit 2376 passed / 161 failed / 4 skipped、integration+CLI 294 passed / 131 failed / 2 skipped、GUI 267 passed / 23 failed。原始失败没有覆盖或改记为通过。

整理后首轮收集成功；P1–P5 新链路的 158 项定向回归通过。完整回归和针对契约更新的复测结果在下方补录。当前不得据此宣称整仓通过，尤其是兼容链路中旧规划器夹具、旧独立表单操作、真实恢复失败和历史诊断脚本仍需复核。有效失败不会因为难修而删除，也未通过修改产品功能或放宽权限断言来掩盖。

静态入口新增 [check_repository_layout.py](../../scripts/check_repository_layout.py)，检查当前与历史 Markdown 的本地链接、当前指南中的字面文件引用；不运行应用。存档中的原始终端命令不作为有效执行入口检查，实机与真实模型效果仍未验证。
