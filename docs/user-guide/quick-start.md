# 安装与快速开始

本指南从源码目录运行。需要 Python 3.11 或更高版本；仓库提供 Windows CPython 3.12 的依赖约束。以下命令在仓库根目录的 PowerShell 中执行。

## 1. 准备 Python 环境

建议使用独立环境。已有 `deskpal_env` 的用户直接激活该环境；也可以新建虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version
```

安装图形界面依赖：

```powershell
python -m pip install -r requirements-gui.txt -c constraints/windows-py312-gui.txt
python -m pip check
```

上述约束用于 Windows Python 3.12。其他 Python/平台可先省略 `-c ...`，但需要单独验证解析与兼容性。仅使用命令行可安装 `requirements.txt`。截图和语义检索分别需要 `requirements-screenshot.txt`、`requirements-embedding.txt`；首次体验无需安装。

若使用当前已配置 BGE 的工作区，可直接 `conda activate deskpal_env` 使用已有环境，无需再创建 `.venv` 或下载模型。本地配置已引用 `data/models/bge-small-zh-v1.5`。若选择新建环境并沿用该配置，还需安装 `requirements-embedding.txt`；只有 GUI 依赖不足以加载 BGE，缺少 embedding 依赖时会提示并回退词法检索。新克隆不会附带这些本地配置和权重，准备流程见[数据维护](maintenance.md)。

## 2. 启动配置向导

```powershell
python -m deskpal.gui
```

有配置时直接启动；缺配置时进入向导。默认优先使用当前目录已有的 `config.toml`，否则 Windows 使用 `%LOCALAPPDATA%/Deskpal/config.toml`。为了明确隔离个人数据，也可以指定新的配置位置：

```powershell
python -m deskpal.gui --setup --config E:/Deskpal/config.toml
```

在向导中选择 `offline`，检查工作区、数据目录和缓存目录后保存，再启动应用。离线演示无需 API key。数据目录与工作区应独立，缓存允许使用默认的数据目录子目录。目录和密钥说明见[配置与模型连接](configuration.md)。

已有配置的用户可用下列命令临时离线运行，文件中的 provider 不会被改写：

```powershell
python -m deskpal.gui --config config.toml --offline
```

## 3. 执行第一个任务

1. 在对话输入区域选择独立请求，输入 `time` 并提交。离线演示会规划读取当前时间。
2. 输入 `write hello.txt 你好，Deskpal`。
3. 到任务页选择该任务，核对工具 `write_text`、路径、内容、权限和计划 hash。
4. 明确勾选普通工具授权并确认。文件写入配置的工作区，默认是 `workspace/hello.txt`。
5. 输入 `read hello.txt`，检查返回的工具结果。

写文件请求停在 `Waiting` 是正常确认流程。不要仅根据聊天中的文字判断执行成功，以任务状态和步骤结果为准。

如果更喜欢终端，先退出 GUI，然后运行：

```powershell
python -m deskpal --request "time" --json
python -m deskpal --list-tools --json
```

CLI 会使用已有默认配置；若其中是云 provider，这些普通请求会按云模式处理。需要离线 CLI 时先在配置中选择 `offline`，CLI 普通入口没有 GUI 的 `--offline` 参数。

## 4. 保存与退出

默认关闭主窗口即退出；启用“关窗留托盘”后需要从托盘选择退出。看到 `Closing` 时等待当前同步调用返回和资源释放。应用完全退出后，才能用同一数据目录启动 CLI 或进行维护。

下一步：[桌面界面与陪伴设置](desktop.md)和[任务与授权](tasks-and-permissions.md)。安装失败见[排错](troubleshooting.md)。
