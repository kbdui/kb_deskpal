# V0.5 候选版交付状态

日期：2026-10-16。代码版本 **0.5.0rc1**，状态：**已完成本轮恢复收尾，未宣称全部 V0.5 验收通过**。100% / 200% 原生双屏 194 项检查、双 Python 最终回归、安装与 1800.234 秒同源码长测已有通过记录。人工托盘激活/菜单恢复/退出、默认关窗、普通通知展示/点击保持未授权及勿扰抑制时任务查询已通过；完整缩放矩阵、休眠等缺项及首次 Python 3.11 测试停滞调查仍保留。实际证据见测试报告，恢复点见[开发交接](development-status.md)。

此次恢复已完成自动化证据核对、测试助手 Ready 屏障修补及有界诊断，双 Python 各1203 passed / 4 skipped、strict89文件通过。原状重复未复现停滞；完整GC确见worker收集，但各环境单轮16个Bridge/8个Host的销毁线程均匹配创建线程，不能据此归因。详见[稳定性调查](stability-investigation.md)。简单机械测试由6 Luna执行，主Agent负责诊断设计与审查。

三次人工临时会话正常关闭，后台线程与实例锁检查通过，工具执行次数均0、合成目标文件未写入；用户已恢复系统勿扰原状态。产品源码未变，原有长测和wheel证据仍适用。新增手工探针经双Python strict检查；逐项范围与原始记录见[Windows 实机记录](windows-checklist.md)。

已实现 Windows Qt Widgets 主窗口、后台命令/查询/控制桥、真实任务闭环、准确计划确认、任务恢复、记忆审核/更正/遗忘、Goal/Milestone/Binding/Proposal 管理、只读后端设置、GUI 偏好、静态占位、托盘与默认关闭的本地通用通知。

入口、数据用途及操作见[操作指南](operations.md)，架构和契约见[接口](interfaces.md)，证据见[测试报告](test-report.md)。原[评估](../../planning/v0.5/environment-assessment.md)、[开发计划](../../planning/v0.5/development-plan.md)与[验收边界](../../planning/v0.5/acceptance-boundaries.md)保留为规划快照。

主要整合修复包括：非阻塞确认与持久幂等；真分页和组合快照；初始化退出所有权；关闭先停止领取、最终等桥接线程退出；有界 Qt 交付；记忆 epoch/迟到回调与 Pending 清理屏障；通知有界增量游标和 quiet 后重新核验。后端仍只有一个 Core 执行者，所有工具继续走 Tool Manager/Sandbox。

双屏实测进一步修复隐藏长表单撑大窗口、DPR 往返尺寸累增、保存位置重复叠加标题栏偏移和临时工作区约束覆盖正常尺寸。修复后 194 项原生检查通过；用户选择本次先完成跨屏和窗口恢复，物理拔插暂不进行。

主环境 deskpal_env 与项目内 Python 3.11 测试环境均已安装 GUI 依赖，旧 3.11 项目安装元数据已刷新。GUI 为 optional extra，requirements 继续引用 pyproject；不要求 CLI 用户安装 Qt。没有新模型下载、Live2D/CUDA、云 API 调用或用户 Git 提交。

保留限制：GUI/CLI 同 data_dir 互斥；慢同步调用只能协作取消；通知不保证 OS 显示/撤回；无完整多轮上下文、人格情绪、Live2D、无人值守自动工具或 OS 级隔离。主窗口以基础表单和完整 JSON 详情承载审查，后续版本可继续优化呈现，不改变权限/删除边界。

正式 V0.5 门槛仍按验收矩阵执行。缺硬件或未完成的实机项独立保留，不能把离屏测试或候选版当作完整 Windows 认证。
