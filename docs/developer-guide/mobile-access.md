# 手机入口实现

2026-11-09 首版采用标准库的 HTTP 服务和独立轻量网页，无新增第三方 Python/前端依赖。HTTP 服务只承担单用户入口，公网 TLS 由 FRP 或反向代理提供；不会将 Qt WebChannel 或通用后台操作接口暴露到网络。

```text
手机 HTML/CSS/JS → HTTP 认证与来源检查 → 明确允许的命令
    → ApplicationBridge 的现有命令 / 读取 / 控制队列
    → ClientGateway → DesktopFacade → 唯一 ApplicationService
    → SubmissionDispatcher → AgentCore → 原有学习及最终表达
```

`application/client.py` 定义操作与字段白名单，复用无 Qt 依赖的 DesktopFacade。普通 submit 不接受预置工具授权；confirm 仍要求明确计划哈希、任务版本、普通和高风险工具列表。没有直接调用 Core、第二个 dispatcher 或角色数据库连接。

`mobile/access.py` 管理独立 SQLite 授权库，保存监听配置、设备名、三十天到期时间及令牌 SHA-256 摘要。一次性八位配对码仅存在内存，五分钟过期，最多尝试五次，每个来源另有限流。角色导出、恢复和人格/记忆结构不变。应用本地授权库与角色数据库生命周期分离。

`mobile/server.py` 提供固定静态资源、`GET /api/session`、`POST /api/pair`、`POST /api/command`、`POST /api/logout`。不提供任意路径的文件服务、CORS 或插件维护入口。POST 需要匹配的 Origin 和 JSON Content-Type，登录后还需 CSRF 令牌。公网来源只接受规范化 HTTPS 来源，Cookie 含 HttpOnly/SameSite=Strict，公网访问另含 Secure；不信任转发头。最多八个 HTTP 请求线程，socket 读取超时五秒，正文最多 64 KiB，回复使用现有桥的 4 MiB 上限；不记录 URL、Cookie、配对码或聊天正文。

后台启动后按需恢复手机监听，未启用过时不创建授权库。桌面本地设置使用 `mobile_status/enable/disable/pair/revoke`，这些管理操作不在远程白名单里。HTTP 命令进入现有有界队列并得到独立 Future 回执；HTTP 工作线程不等待模型，也不依赖 Qt 主线程排空消息。执行前后重新检查设备授权和监听实例。隐私 epoch 变化或关闭会解除等待并拒绝旧命令；后台实际关闭前先停止监听，已经执行的业务仍按原有任务语义保留。

手机每两秒半在前台查询分页历史和任务版本，仅在任务变化时重新读取审核详情。选择会话、角色实例和隐私/会话 epoch 都参与迟到结果检查。草稿只属于当前标签页、后台实例和会话；不会同步覆盖桌面草稿。未确认操作保留稳定 command_id；重试先查询已有回执。追加/澄清的 accepted=false 必须保留输入。原有取消接口按 task_id 请求取消，没有新增移动端任务数据库或通用插件渠道系统。

浏览器代码位于 `mobile/assets/`，由 setuptools package-data 打包。桌面只增加手机设置分类，消息展示和输入仍沿用现有桌面实现。手机采用独立响应式布局、黑线狗/猫默认图标、共享称呼，执行记录按需弹窗；首版不开放手机端头像设置。

验证入口：`tests/unit/mobile/`、`tests/integration/mobile/`、`tests/gui/desktop/test_mobile_access.py`，以及原有 bridge 回归。全部使用临时目录、离线模型和回环监听。受限 Windows 环境若禁止回环或 Qt 子进程，应明确记录环境阻断，在允许本机连接的环境执行，不能据此跳过授权断言。实机手机和 FRP 不包含在自动化结论内。
