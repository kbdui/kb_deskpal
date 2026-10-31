> 历史存档：保留当时的设计、失败与验收边界；不代表当前实现或新的执行授权。当前入口见 [文档导航](../../../README.md)。

# 托管 BGE：真实下载与离线 CPU 链路

日期：2026-10-23。用户明确授权补充“下载真实模型文件 → 校验并登记为托管缓存 → 离线加载 → CPU 生成有效向量”。状态：**真实主链、两项预检、54 项定向回归和 strict 146 文件全部通过**。Git 由用户提交。

## 范围与通过条件

使用主环境 Python 3.12.4、FastEmbed 0.9.0、ONNX Runtime 1.31.0。不安装新依赖，不读取已有模型缓存、私人配置/记忆/凭据，不调用聊天服务。新建专用运行目录及 HF 缓存，只有显式下载阶段允许访问公开模型源。

1. 从全新空目录经真实 `MaintenanceService.cache_prepare` 下载固定 `BAAI/bge-small-zh-v1.5`；不可借旧权重、手写 manifest 或替换 SDK 获得通过。
2. 发布后逐文件核对托管清单的字节数与 SHA-256，检查 revision、目录归属及 stage 发布完成。
3. 新进程在导入 SDK 前设置离线标志并拦截 socket/DNS；经真实维护入口再次执行 hash 和离线推理验证，网络尝试必须为零。
4. 真实 provider 编码固定公开中文 query/documents；确认实际 session 使用 CPU、每个向量为 512 维有限值、L2 范数近似 1，重复 query 稳定，进程退出释放资源。验证后托管清单及资产不变。

下载阶段上限 900 秒，离线阶段 180 秒，总执行上限 1200 秒。下载环境受限时按工具权限流程在新 label 复测，保留首败；不关闭 TLS 校验、不切未经确认的镜像、不自动安装包。离线网络拦截证明当前进程没有尝试连接，不宣称物理断网抓包审计或模型检索质量基准。

主 Agent 负责架构与故障归因，6.1 Sol 高思考编写可重复诊断，6 Luna 高思考执行固定命令。证据保存到忽略目录 `data/validation/v09/bge-managed-20261023/`，包括实际命令、环境、首败与复测、stdout/stderr、阶段回执和源代码前后哈希。

## 实际结果

`live-initial` 在沙箱内执行，2026-10-23 15:31:15Z 至 15:32:01Z，exit 1、未超时。真实 FastEmbed 向公开源下载时遭遇 Windows socket `WinError 10013`，SDK 重试后失败，没有取得模型权重。原始日志与阶段/外层回执保留，不计通过；按工具权限流程使用新 label/root `live-escalated` 重试。

通过工具 `require_escalated` 获准后执行 `live-escalated`，外层 exit 0，43.95 秒，无超时；下载 worker 39.453 秒，fresh offline worker 2.844 秒。两个 worker 为不同新进程，phase 各自使用独立空 HF_HOME；只复用本轮发布的托管模型目录。

| 检查 | 实际结果 |
| --- | --- |
| 模型来源 | SDK 默认公开源，`models--Qdrant--bge-small-zh-v1.5/snapshots/46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59`，逻辑模型名 `BAAI/bge-small-zh-v1.5` |
| 托管发布 | 9 个资产＋manifest，共 10 个文件，95,225,625 bytes（包含 manifest） |
| 发布回执 | `status=ready, managed=true, verification=previously_verified` |
| 资产校验 | 每个实际文件的大小与 SHA-256 均与清单一致；下载后、离线前、离线后资产及 revision 相同 |
| 清单 revision | `e1b7c6d13d280b5a598615f0db18162515574e0f481bc73f73a4c59d1362735d` |
| 新进程维护校验 | `status=ready, verification=verified_offline`，经真实 `MaintenanceService.cache_verify` |
| 实际执行后端 | 观察到 `provider._model.model.model` 为真实 ONNX `InferenceSession`，`get_providers()=["CPUExecutionProvider"]` |
| query 与重复 query | 两个 512 维向量，全为有限值，L2 norm=1，最大绝对差=0 |
| 3 条 documents | 均为 512 维有限向量，L2 norm 分别为 1、0.9999999999999999、1 |
| 离线网络尝试 | Python socket/DNS guard＋audit hook 记录 0；HF/Transformers offline 标志在 SDK 导入前设置 |
| stage | 无残留 stage 目录；持久同级租约锁不当作模型目录删除 |

Windows 未启用创建符号链接权限，HF 提示并使用真实普通文件缓存；这次没有关闭产品的 symlink/hardlink 安全拒绝，也没有物化伪造权重来绕过校验。

## 复现与证据

主脚本为 `scripts/check_managed_embedding.py`；新增诊断没有改生产 API。它使用新建合成 TOML（offline 聊天 provider、空 credential_ref）及专用 data/workspace/HF/cache 目录。下载必须显式 `--allow-download`，root 必须不存在且父目录已存在；不自动安装包、不读取个人环境或模型缓存。

```powershell
python scripts/check_managed_embedding.py --root data/validation/v09/bge-managed-20261023/live-new --allow-download --download-timeout 900 --offline-timeout 180
# 模型仍位于该诊断 root 时仅复查；不会再次下载
python scripts/check_managed_embedding.py --root data/validation/v09/bge-managed-20261023/live-new --offline --offline-timeout 180
```

本轮真实解释器为 `D:/anaconda/Anaconda3/envs/deskpal_env/python.exe`。外层命令和源代码前后 SHA-256 分别位于证据目录 `attempts/live-initial/` 与 `attempts/live-escalated/`；成功阶段的原始日志、回执和汇总在 `live-escalated/runs/20261023T153232Z-f9eafa6a/`。首次实测结束时权重位于合成测试目录；随后用户要求用于应用，已按下节迁移并引用。原始日志中的路径保留为历史证据。

## 应用引用（用户后续要求）

已将本次真实下载的 `live-escalated/model-cache` 迁至仓库 `data/models/bge-small-zh-v1.5`，保留原有 `data/models/fastembed` 旧缓存。迁移前后逐资产大小/SHA-256 与清单 revision 一致；通过共享缓存独占租约与实际 data_dir 实例锁保护迁移，不覆盖已有目录。

本地忽略文件 `config.toml` 通过 `SetupService` CAS 保存并保留原始恢复副本，其他 TOML 语义不变；真实配置加载已确认下列引用生效，没有修改全局默认值或聊天模型配置：

```toml
[memory]
embedding_provider = "fastembed"
model_cache_dir = "data/models/bge-small-zh-v1.5"
```

模型已离开旧诊断 root，不能再对旧 `live-escalated` root 运行诊断 `--offline`。应用模型复查使用 `python -m deskpal --config config.toml --maintenance cache_verify`；此维护入口不启动 Core，也不读取私人记忆库。迁移与新路径验证证据位于 `data/validation/v09/bge-install-20261023/`；模型、实际配置及恢复副本继续被 Git 忽略。

迁移后已使用项目 `deskpal_env` 的 Python 再次验证：当前实际配置引用匹配，维护回执 `ready/verified_offline`，实际 ONNX session 仅 `CPUExecutionProvider`，query 与 3 条 documents 为有限 512 维单位向量，网络尝试 0，清单 SHA-256 前后仍为上述 revision。成功回执为 `evidence/install-verify-deskpal-env-r3.json` 及对应 raw log；使用真实 dataclass 字段记录 manifest。最初误用缺少 embedding 依赖的 Anaconda base，以及诊断回执字段错误的记录另行保留，未作为模型通过证据。运行应用需使用已安装可选依赖的项目环境；本次未向 base 安装包，也未读取个人数据库或启动 Core。

## 结果边界

该证据补齐当前 Windows Python 3.12 环境的真实托管准备与离线 CPU 输出链路，不是中文检索质量/性能基准、跨平台保证、物理断网抓包或模型发布者签名验证。SDK、模型快照或预处理变化后需要重新验证。GUI 物理桌面、历史 HM08 拟人化真实模型和发布验收不在本次范围。

## 预检与定向检查

- 两项预检通过：未选下载/离线模式时 exit 2 且不创建 root；另一个 `-I` 进程在未导入模型 SDK 时，验证 connect/connect_ex/create_connection/getaddrinfo 和预绑定 connect 均被 guard/audit 拦截，预期路径各有计数，真实网络连接没有发生。见 `preflight.json`。
- strict exit 0：`src` 与原 3 个检查脚本＋新增诊断脚本共 146 文件无问题，见 `postchecks/strict/`。
- 定向 pytest 首次因外层 PowerShell runner 的 JUnit 参数拼接错误 exit 4、没有运行测试；原记录保留。修正的是忽略目录 runner，生产/诊断/测试源码未变。新 label `postchecks/pytest-rerun1/`：52 passed / 2 skipped，exit 0。两个 skip 都是沙箱限制实际硬链接创建，另在正常权限下同选择复测后补记结果。

通过工具 `require_escalated` 获准后，用新的 `postchecks/pytest-escalated/`、basetemp 和 JUnit 文件复测相同 `tests/unit/test_model_cache_management.py tests/unit/test_v03_retrieval.py`：**54 passed / 0 skipped，exit 0，未超时**，包含实际硬链接拒绝检查。没有为通过而改测试断言或放宽资产安全规则。最终只读检查 `git diff --check` 通过；当前缓存与两个阶段的清单 revision/资产/字节数一致。

以上各批 `src/tests/scripts` 源码 SHA-256 前后均一致。历史 V0.3 adapter 和上一轮 fake SDK 的通过不代替本轮真实链路。
