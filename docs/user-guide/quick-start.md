# 安装与快速开始

需要 Python 3.11+，在独立环境、仓库根目录安装。Windows CPython 3.12 可用仓库约束：

```powershell
python -m pip install -r requirements-gui.txt -c constraints/windows-py312-gui.txt
python -m deskpal.gui
```

其他环境需要独立验证依赖兼容性。仅后端可装 requirements.txt；截图和本地语义检索是独立可选分组，不要求首次安装就下载模型。

缺配置时进入向导，也可明确指定新的配置位置：

```powershell
python -m deskpal.gui --setup --config E:/Deskpal/config.toml
```

在设置中填写模型服务、模型名和密钥，保存生效后开始聊天。默认云模型，本地连接只需在设置切换。新克隆没有个人配置、凭据或模型权重。数据目录与外部工作区分别选择，详见[配置](configuration.md)。

在“日常聊天”自然输入问题或任务；涉及需要许可的操作时，在原任务卡核对后确认。完成后模型根据真实结果回复，执行明细在主窗口右侧查看。回车发送，Shift+回车换行。

离线演示不需要 API，但只支持固定命令，不能代替真实模型能力：

```powershell
python -m deskpal.gui --config config.toml --offline
```

应用退出并释放同一数据目录后再运行 CLI 或维护操作。下一步见[桌面与聊天](desktop.md)和[任务与授权](tasks-and-permissions.md)。
