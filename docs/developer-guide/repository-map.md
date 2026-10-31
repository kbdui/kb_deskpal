# 仓库地图

| 路径 | 用途 |
|---|---|
| src/deskpal | 产品代码，按领域与应用边界划分 |
| tests/unit、integration、gui | 按功能分组的离线测试；说明见[测试目录](../../tests/README.md) |
| tests/live、manual、fixtures、support | 显式联网门控、手动探针、合成夹具、稳定复用支持 |
| scripts | 容量、模型、GUI 和历史 Windows 诊断入口；运行前核对范围 |
| docs/design、planning | 已确认设计与六阶段开发验收 |
| docs/user-guide、developer-guide | 当前使用与接入指南 |
| docs/reports、maintenance | 持续短报告与一次性维护结果 |
| docs/roadmap、archive | 后续方向与历史资料 |
| constraints、requirements*.txt、pyproject.toml | 环境约束、安装分组及直接依赖 |
| data、workspace、本地 config.toml/.env | 非版本化运行资料，不作为普通测试输入 |

文档文件用 kebab-case，Python 测试用 `test_<行为或模块>.py`；产品版本不作为新测试文件名前缀。真正的数据库或原生协议版本仍保留版本含义。
