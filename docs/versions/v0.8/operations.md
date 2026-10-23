# V0.8 运行说明

2026-10-23：[拟人化增量开发记录](humanization-development-status.md)列出新增 GUI 会话与用途控制、CLI 参数及 schema 2 备份边界；已按用户授权测试，原始结果见[集中测试报告](humanization-test-report.md)，最新修补、真实模型失败和验收判断见[HM08 改进记录](hm08-improvement-report.md)。下面原最小版说明和测试链接只作为基线参考。新增候选只在当前进程确认，跨启动不恢复聊天。

HM08 增加可选 `[llm] expression_output_format = "json_object"`（环境变量 `DESKPAL_LLM_EXPRESSION_OUTPUT_FORMAT`），默认仍为 `prompt`。仅经验证支持的 Responses 端点才显式选择该模式；它只用于 expression，Planner 和普通 chat/generate 格式保持。原生模式仍严格校验业务结构、来源和用途；失败不自动切换模式或补发请求。本次实际模型还出现过无可用输出，不能据此宣称端点稳定。工具摘要已足够时可没有额外补充段，这是正常展示；Provider 无输出仍属于失败回退，不等同合法空补充。

本轮真实问题已按用户要求[记录并暂缓](hm08-deferred-issues.md)，不再追加请求。本机试验性 json_object 覆盖已移除，恢复 prompt 默认；其余配置值不变。该恢复不表示原提示模式已通过真实体验验收。

状态：实现及最小自动化测试结果见[测试报告](test-report.md)，2026-10-21。保留现有安装/配置/启动方式与 `0.5.0rc1` 版本号，无新依赖；本版尚未完整验收。

GUI 新增“人格与模拟情绪”页。先刷新查看当前设置，明确启用人格；可另启用情绪、选择均衡/温和/简洁预设、设置有限风格轴。当前配置编辑保留，使用“重新载入配置”主动放弃编辑。后台忙碌、版本冲突或未知回执时刷新后重试，不自动再次提交。

互动偏好可选择本次会话/今天/持续保存，短时支持/专注模式最长30分钟；人格版本可回退，情绪/人格/全部设置可分别重置。全部重置不清记忆或任务；重置及关闭情绪不清旧任务消费记录和窗口预算。状态为应用模拟，不能用其判断用户真实心理或执行成功。

CLI 本地管理例子（示例 revision 必须换成当前 show 返回值）：

```powershell
python -m deskpal --config config.toml --companion show
python -m deskpal --config config.toml --companion configure --companion-parameters '{"expected_revision":0,"enabled":true,"emotions_enabled":true,"preset":"warm"}'
python -m deskpal --config config.toml --companion preference --companion-parameters '{"expected_revision":1,"scope":"today","verbosity":"brief","humor_allowed":false}'
python -m deskpal --config config.toml --companion cue --companion-parameters '{"expected_revision":2,"mode":"supportive","ttl_seconds":300}'
python -m deskpal --config config.toml --companion reset --companion-parameters '{"expected_revision":3,"target":"affect"}'
```

Windows PowerShell 向原生命令传递 JSON 的引号行为因版本/模式不同可能有差异；必要时使用项目 Python API 或 GUI，不能通过省略 CAS 绕过参数校验。CLI 参数为16KiB以内、键唯一、有限数值的 JSON 对象。上述命令仅作说明，本轮未执行。

管理入口强制 offline、无需模型 key、不运行 Planner/工具。GUI 与 CLI 同 data_dir 互斥，先完整退出 GUI 再用 CLI；不要为查询绕开实例锁。持久状态在 `companion.sqlite3`；会话偏好/cue不跨退出，today 使用 `proactive.timezone` 到下一午夜。配置坏库不自动覆盖，面板中性降级；遗忘若不能重置派生状态会显示待清理，须解决存储问题再重试，不能宣称已全清理。

工具任务通常一次 Planner + 一次 Reflect；启用人格的普通聊天最多在 Planner 后多一次表达调用，关闭时无此增量。候选记忆评估沿原设置另计。云表达只使用固定预设，GUI 自定义六轴、本地作用域偏好和短时情绪不会进入云风格提示；因此调节这些本地项不保证当前云回复发生对应变化。离线固定 mock 回复也不用于评价风格。

桌宠继续通过原设置启用。实际任务状态标签优先于情绪图，完成反馈有独立有限时段；情绪不能改变权限或触发工具。缺图、过期和失败回原中性状态。原关宠、关窗、托盘和退出所有权不变。

开发阶段先不测试的安排已由用户后续授权更新：[最小T01–T09](minimal-test-plan.md)已执行，结果与范围见[测试报告](test-report.md)。较宽建议中的真实模型、物理DPI和资源长测未执行；V0.7原资源/预览时序未决项继续保留。
