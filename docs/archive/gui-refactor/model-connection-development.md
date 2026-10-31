> 历史存档：保留当时的设计、失败与验收边界；不代表当前实现或新的执行授权。当前入口见 [文档导航](../../README.md)。

# D3 模型连接开发交接

2026-10-25。开发实现，尚未执行产品测试、静态类型检查、GUI、真实模型或真实凭据库验证。

## 配置与就绪状态

`LLMConfig` 默认 `provider=openai`、`execution_location=cloud`、`protocol=responses`、`credential_mode=system_vault`、`request_use_default=true`。`is_cloud` 由实际执行位置决定，`ready` 表示模型名和所选来源的凭据满足本地前置条件，并不证明远端模型可用。`credential_source` 只返回安全来源标签。

已有显式 `offline` 保持固定离线演示。已有 `openai` 缺少 `credential_mode` 时，按旧规则保留当前环境/.env 优先、其次系统引用的有效来源；保存显式模式后不再回退另一来源。新建配置由 SetupService 明确写入 vault 模式，不依赖偶然存在的环境密钥。

`load_config(path, allow_missing=True)` 只允许文件缺失，不忽略损坏或危险连接；仍以指定文件的父目录解析路径，不偷偷建立默认 TOML。GUI 入口使用此参数。缺配置先进入设置，可暂不配置而浏览已有本地内容。factory 使用 `UnavailableLLMProvider`，调用只能报配置未就绪，不返回离线示例。请求受理由 ApplicationService 的 ready 前置屏障阻断。

`AppConfig.configuration_revision` 是本次实际读取 TOML bytes 的 SHA-256，缺文件为 `missing`；运行中的配置不可变，切换走完整关闭和重新创建。生产 settings 的 active_revision 应展示此值。embedding 的 provider、缓存目录和下载行为没有随聊天默认云而改变。

## 本地传输

首期只实现兼容 Responses 的 `/responses` 文本和工具协议；没有宣称 Chat Completions 兼容。云连接强制 HTTPS；local 只允许数字 loopback 或 localhost。localhost 在无 DNS 查询的情况下规范为 127.0.0.1。本地默认传输显式禁用环境代理，所有重定向均拒绝；无云回退。`none` 可用于无需鉴权的本地服务，不需要伪造占位密钥。

factory 的记忆检索、记忆协调和 companion cloud 参数使用 `is_cloud`。ModelCallGateway capability 区分 local、unavailable、offline_demo、remote；用途和来源许可没有放宽。

## 维护与敏感通道

保留 `MainWindow.maintenance_requested` → `DesktopLauncher.request_maintenance()`。Launcher 等旧 Host.finished 后才构造维护 worker，Host 必须已 Closed 且桌宠资源释放；维护 window.finished 后才重建生产 Host。StartupFailed 同样先安全关闭，再进入可恢复设置。

`MaintenanceBridge.send_sensitive(operation, parameters, credential_secret=None, generation=0)` 只支持 setup_save/setup_repair。parameters 必须不含秘密，密钥由单独 keyword 注入一次性内存载荷；不经通用 Channel、hash、命令 ledger、QSettings 或 JSON 序列化。普通 send 拒绝 credential_secret。请求结束、取消或关闭后清理引用，密码框提交/离开分区/关闭后清空；不承诺 Python 堆物理清零。

`setup_save`/`setup_repair` 接受 `credential_action=keep|replace|remove`。replace 使用随机新引用，vault 或发布失败保留原配置；CAS 失败不写凭据。replace/remove 在配置租约内持久记录不含秘密的清理引用，只在发布成功后清理非当前 OS 条目，失败以 `credential_cleanup_pending/credential_cleanup_refs` 明确反馈，后续保存重试清理。keep 改主机时拒绝将旧 vault 凭据或同名环境来源静默转移。有效 vault 连接也拒绝环境 endpoint 覆盖到不同主机。

保存回执带 `saved=true`、`requires_restart=true`、`active_revision=null`。维护界面写“下次启动采用”，不能把已保存叫作当前生效。

`connection_test` 参数为 `expected_revision`、`confirm=true`，在配置租约和实例锁内核对版本，单独发送固定公共文本、无工具/历史/记忆/人格，可能产生服务费用。验证失败不删除已保存配置。结果只含 verified 状态、revision、protocol 和 private_context_sent=false；不返回模型文本或秘密。

## 待授权执行的最小验证

新增 `tests/unit/test_gui_model_connections.py`，准备覆盖：新云缺配置可浏览、旧 offline 保留、旧 env+ref 保留环境优先、显式 vault 不回退 env、本地地址拒绝与 DNS 规范、禁代理/拒重定向/无伪造授权、keep/replace/remove/CAS/vault 失败保护，以及固定连接测试必须确认。

建议 T1 命令（本轮未运行）：

```powershell
python -m pytest tests/unit/test_gui_model_connections.py tests/unit/test_product_setup.py tests/unit/test_product_credentials.py tests/unit/test_product_maintenance_service.py tests/unit/test_config.py
python -m mypy --strict src scripts/check_memory_api.py scripts/benchmark_memory.py
```

已有 config 默认值和缺凭据报错断言可能需要按新“未就绪但可浏览”契约更新；不得靠放宽来源/网络边界让旧断言通过。GUI 生命周期与真实 Windows vault、loopback 服务协议需在后续独立授权测试阶段验证。
