# V0.8 开发记录

后续增量（2026-10-22）：[拟人化 H0–H5](humanization-development-status.md)已完成源码整合，**没有运行验证，H6 尚未开展**。下文 2026-10-21 的通过计数只对应原最小实现，不覆盖当前新增/修改代码。

日期：2026-10-21（Asia/Shanghai）。状态：**开发、接口整合及用户指定最小自动化测试已执行，仍有验收边界；不是正式发布。** 产品版本保留 `0.5.0rc1`。Git由用户提交，本轮无stage/commit/push/PR。

后续测试更新：用户已授权[最小T01–T09](minimal-test-plan.md)，三个Sol编写测试、Luna执行。新增76项定向通过；既有回归首轮1019通过/1过时断言失败/2环境跳过，修补后主环境受影响集119通过、Python3.11兼容集171通过，strict106源文件通过。T07仅验证运行中Busy拒绝与Core代际、未提供运行中受理真实删除；两符号链接权限项未补测。完整首败、修补、证据覆盖事故与范围见[测试报告](test-report.md)。下文“未测试”描述保留为此前开发阶段历史。

## 本轮范围与分工

主 Agent 阅读架构、跨版本实际接口、`docs/guides` 开发/依赖约定、路线及人格情绪设计，冻结[开发计划](../../planning/v0.8/development-plan.md)与[验收边界](../../planning/v0.8/acceptance-boundaries.md)，负责架构/接口审查和整合。三个 **6.1 Sol、高思考**子 Agent 分别编写领域、后端和界面；本轮没有委派或运行测试。后续固定机械测试由6 Luna高思考执行。

| 文件范围 | 实现 |
| --- | --- |
| `src/deskpal/companion/{__init__,runtime,policy}.py` | 声明式人格/六轴、CAS与32版历史、显式偏好/cue、情绪衰减/预算/去重/重置、独立SQLite与固定诊断 |
| `src/deskpal/agent/{core,ports}.py` | 窄Protocol、受限表达链路、真实终态汇总、隐私代际/锁、失败中性降级 |
| `src/deskpal/application/{factory,service,desktop,cli}.py` | 实例锁后装配/资源栈、管理门禁、独立端点、离线CLI管理/严格JSON |
| `src/deskpal/gui/{companion_state,companion_panel,bridge,window,pet_controller}.py` | 有界DTO、显式控制页、预读CAS、编辑保留、后台通道、静态情绪映射和完成反馈优先 |
| `docs/planning/v0.8/`、`docs/versions/v0.8/`、文档导航/路线、根架构与README | 规划、实际接口、操作、开发记录和待执行测试清单 |

## 主 Agent 审查与整合

人工阅读源码、调用方与diff；这不等于静态检查或测试通过。审查后由对应子Agent落实了以下整合：

- 普通聊天在启用人格时使用独立无工具表达，Planner仍不收到情绪；工具任务复用原Reflect。成本与fallback明确记录，不为每个维度增加调用。
- 情绪只在权威持久Completed/Failed后提交，本任务的模拟效果影响后续读取，避免先奖励成功再遇最终提交失败。确认/取消/不确定不形成惩罚。
- 遗忘先推进代际并重置，旧任务不能发布风格/情绪；reset失败保持Pending，续办须再次成功reset。存储故障统一固定诊断code，与GUI禁写判断一致。
- 瞬态表达降级但存储可读时保留真实配置与CAS，仅中性化情绪/cue，允许显式设置恢复；真正存储不可读才禁止GUI写入，避免同修订默认配置造成冲突或锁死恢复入口。
- 严格校验坏state/persona history/budget和巨大整数；提供时钟注入。消费回执与额度跨重置保留，容量满后冻结新效果。
- 表现可达性修正：后端长期返回最近Completed，故不仅Idle，有限完成反馈结束的Completed也可静态装饰；状态标签仍权威，情绪pleased不重启完成计时。
- 六轴均有有限表达语义；GUI刷新保留未保存编辑，隐私/生命周期/新鲜度与同修订配置冲突均有门禁。V0.7七字段presentation契约保持。

## 开发阶段历史：当时未验证与后续入口

按用户要求，**没有运行 pytest、mypy、编译、GUI、真实API、安装、容量/资源/长测或诊断脚本**。没有新增行为测试文件，没有修改既有测试断言或把未决项记为通过。尚不能证明代码可运行、strict通过、并发行为或真实风格体验达标。

下一步先交用户[测试清单](test-plan.md)，其后按清单编写/执行。实际契约见[interfaces.md](interfaces.md)，使用与调用成本见[operations.md](operations.md)。新功能默认关闭、无新依赖。云风格仅固定preset；本地偏好/情绪不外发，当前offline固定模拟回复不证明真实模型风格。

本轮不实现自由角色卡、关系好感积分/自动学习、自然语言情绪分类、历史事件导入、完整多轮上下文或ResponseEnvelope重构。V0.6保持暂停，Live2D延期，V0.7长测预览/内存未决问题继续按原[后续清单](../v0.7/deferred-tests.md)保留。
