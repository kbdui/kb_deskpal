# 开发与测试

使用 Python 3.11+ 独立环境；Windows CPython 3.12 约束对应仓库观察环境。仅后端安装 requirements-dev.txt，GUI 开发安装 requirements-gui-dev.txt。依赖声明规则见[dependencies](dependencies.md)，本地检索准备见[embedding-setup](embedding-setup.md)。

```powershell
python -m pip install -r requirements-gui-dev.txt -c constraints/windows-py312-gui.txt
python -m pip check
python -m pytest --collect-only -q
python -m pytest -q
```

测试路径与定向命令统一维护在[tests/README](../../tests/README.md)。默认仍收集全部离线组，未把旧失败隐藏到 xfail 或新跳过标记中；真实 API 测试保留显式门控，Qt 需要可选依赖。offscreen 不证明物理桌面交互、真实模型效果或 OS 隔离。

静态检查按修改范围执行；全量参考命令：

```powershell
python -m mypy --strict src scripts/check_memory_api.py scripts/benchmark_memory.py scripts/benchmark_workflow.py
```

GUI 脚本另检查 benchmark_gui.py、check_gui_displays.py、check_gui_stability.py 和 check_desktop_capacity.py。diagnose_*、probe_* 多为封存的 Windows 沙箱诊断；读取说明并明确范围后才执行，不默认恢复实验。

## 实施和委派

按照当前用户要求，父 Agent 编写实现、测试方案和测试代码；固定命令、链接核对等简单机械执行交给 **6 Luna 高**。父 Agent复核故障并修改，不将重开发交给测试 Agent。失败必须保留命令、退出码、原始日志和节点，不放宽断言或随意跳过。用户未授权代码或测试的讨论任务只写文档。

所有普通测试使用全新合成临时配置、工作区和数据库，不读取个人 .env、真实密钥、系统凭据或已有模型缓存。真实模型、下载和原生实机另行界定；不得因为需要运行旧脚本而复用封存 SID、profile 或真实资料。输出写入新的 data/validation 子目录，保留首败，超时要终止并记录未完成状态。

修改共享接口前核对调用方，完成相关验证后按影响扩大。交付列设计依据、接口、结果与限制；历史计数不作当前全量通过证明。Git 由用户提交，不提交配置、凭据、运行日志、数据库或缓存。
