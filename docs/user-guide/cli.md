# 命令行

在仓库根目录和项目环境运行，同一数据目录不能与 GUI 同时占用。命令和参数以当前 help 为准：

```powershell
python -m deskpal --help
python -m deskpal --config config.toml --list-tools --json
python -m deskpal --config config.toml --request "查看当前日程" --json
python -m deskpal --config config.toml --memory list --json
python -m deskpal --config config.toml --companion show --json
```

普通 request 交由配置的模型判断，不因输入看起来是固定命令就绕过规划。`--tool` 是显式提出确定工具调用的管理入口，仍受 Core 与工具校验，不等于无限执行权限。JSON 适合脚本输出，GUI 平时应使用可读内容。

角色、任务、记忆和维护写操作依赖具体身份、修订及对应参数，操作前读取当前状态。旧的 companion configure/cue/preference/rollback 不属于当前 CLI。底层兼容 accept/reject、proposal 等接口也不表示新版要求人工审核每条学习记录。

维护入口可先查看说明：

```powershell
python -m deskpal --maintenance diagnostics --help
```

不要照搬历史报告中的任务 ID、hash、数据库或封存实验路径。真实模型请求会使用当前连接并可能产生服务费用；离线流程与测试见[开发指南](../developer-guide/development-and-testing.md)。
