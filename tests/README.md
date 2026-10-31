# 测试目录

按测试层次和功能分组，Python 文件使用 `test_<行为或模块>.py`，不再以项目 v0.x/HM08/phase 编号分类。真实 schema 升级及原生诊断协议版本是测试内容，不因版本旧而删除。整理依据与首败记录见[维护报告](../docs/maintenance/2026-10-30-repository-cleanup.md)。

| 目录 | 范围 |
|---|---|
| unit/agent、config、llm | Core、授权、规划、配置与模型适配 |
| unit/conversation、response、memory | 历史来源、协议、检索、显式保存和评估边界 |
| unit/tools、workflow、presentation、profiles | 工具、状态、持久化、展示与档案互斥 |
| unit/maintenance、performance | 凭据、归档、缓存、恢复与有界容量基准 |
| unit/execution_sandbox | 暂停的 OS 隔离项目的离线协议/诊断 mock 回归，仍默认收集，不证明实机隔离 |
| integration/agent、application、storage、privacy、cli | 多轮、学习、主动任务、服务组合、迁移、恢复、隐私及 CLI |
| gui/chat、management、settings、desktop、pet | Qt 离屏交互、页面状态、桥接、生命周期与桌宠 |
| live | 真实 API 的显式门控；未授权不设置运行标志、不提供真实密钥 |
| manual | 不由 pytest 自动执行的人工窗口探针 |
| support、fixtures | 稳定的仓库路径/复用支持、合成 JSON 和原生 C 夹具 |

从仓库根目录运行，默认 `testpaths=["tests"]`，使用 importlib 导入方式；移动没有缩小默认范围，未增加忽略失败的 xfail 或 skip。Qt 依赖缺失时沿用原有模块跳过；真实 API 和 Windows 特定能力沿用原门控。普通回归不读取真实配置、用户数据库或系统凭据。

```powershell
python -m pytest --collect-only -q
python -m pytest -q tests/unit
python -m pytest -q tests/integration
python -m pytest -q tests/gui
python -m pytest -q
```

新版链路的定向入口：

```powershell
python -m pytest -q tests/integration/agent/test_role_context.py tests/integration/agent/test_multi_round_loop.py tests/integration/agent/test_feedback_learning.py tests/integration/agent/test_proactive_controls.py
python -m pytest -q tests/integration/storage/test_profile_lifecycle.py tests/integration/storage/test_database_redesign.py
python -m pytest -q tests/gui/settings/test_role_settings.py tests/gui/management/test_task_controls.py tests/gui/management/test_memory_learning.py tests/gui/management/test_proactive_controls.py
```

支持代码中的 REPO_ROOT 保持脚本、配置样例及子进程根路径稳定。跨测试模块的已有共享夹具改为完整包导入；新复用逻辑优先放 support，不依赖 pytest 将某个测试目录碰巧插入 sys.path。

当前保留的兼容模块有些尚未适配新版行为，失败列表在维护报告与隔离证据中公开记录；不能将“成功收集”视作回归通过，也不能按失败与否决定删测试。重复运行使用新的临时目录，保留日志、退出码及唯一节点统计，真实实机与模型效果另行验收。
