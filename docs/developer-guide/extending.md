# 扩展与修改指南

扩展前先定位[模块](modules.md)，判断变更是否触及计划契约、来源用途、持久 schema 或关闭所有权。以下是代码接入方法，不是运行时插件安装接口。

## 新增可信工具

1. 在 `tools/` 相应主题中实现 `Tool` Protocol：`spec` 属性和 `execute(arguments, context)`。
2. 提供严格对象 JSON Schema、可信 `permission_level` 和 `contract_version`。权限来自注册对象，不能接受模型提供的等级。
3. 在 `application/factory.py:_create_core()` 注册工具；新增依赖由工厂注入并指定关闭所有权。
4. 文件工具复用 WorkspaceSandbox；日程工具依赖 SchedulingManager Protocol；禁止另开旁路直接执行。
5. 验证正常/非法参数、权限拒绝时零副作用、计划契约变化、路径边界、失败分类及必要恢复行为。

结构示例（需接入工厂后才会注册）：

```python
from typing import Any, Mapping

from deskpal.tools.models import PermissionLevel, ToolContext, ToolResult, ToolSpec


class TextLengthTool:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="text_length",
            description="返回文本字符数",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string", "maxLength": 1000}},
                "required": ["text"],
                "additionalProperties": False,
            },
            permission_level=PermissionLevel.AUTOMATIC,
            contract_version="1",
        )

    def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult(success=True, output={"length": len(arguments["text"])})
```

Tool Manager 执行前验证参数；工具仍需检查涉及业务语义的约束。改变既有工具参数、默认行为、权限或副作用语义时升级 contract_version，保证旧计划不会在新契约下静默执行。

当前工具为进程内可信代码。新增第三方 Python 工具并不获得 OS 隔离；隔离执行需独立设计和验收。

## 新增模型服务

在 `llm/` 实现 `LLMProvider.chat(messages, tools=...)` 与 `generate(prompt)`，返回统一 `LLMResponse/ToolCall`。厂商参数、响应及错误转换放在 adapter，Core/Planner 不解析厂商结构。

同时更新配置允许值、装配工厂和安装分组；必要时更新维护向导的 provider 校验。保留地址策略、凭据保护、有限 JSON、稳定 call ID 和传输错误分类。使用可注入传输测试，不以离线通过代替真实服务验收。

原生 JSON 是可选 `NativeJSONProvider` 能力：实现 `supports_json_output/chat_json`，由受控表达路径显式选用。规划/普通聊天维持其契约，表达仍需本地 schema 和信号证据审核，失败不得自动无限请求。

## 增加 GUI 操作

先在 Application/领域建立可测试的 Qt-free 业务接口，再在 `DesktopFacade.execute()` 定义操作与有界 DTO，最后通过 bridge 和 view model 接入 Widget。按操作性质加入 READ/CONTROL 通道或普通命令。

所有写入检查当前 revision/epoch；隐私相关操作要处理清屏、旧回调拒绝和 Pending。授权界面展示原计划，不从聊天文本拼出执行参数。回执只表示提交时先保留“未完成”语义，再读取权威结果。

新增后台资源时更新 Host/Launcher 关闭顺序，验证初始化中关闭、慢调用、维护切换和迟到结果，避免 UI 先退出、worker 仍使用数据库。

## 修改记忆、会话或表达

保持来源身份、revision、用途、审核、删除阻断和预算。新增可出云字段必须明确类别许可与来源许可，按最严格限制组合。规划与表达投影不同，不能把人格、旧草稿或全部历史直接混入规划提示。

变更会话裁剪、模型输出 envelope、偏好范围或信号提取时，覆盖字节/轮数限制、草稿来源、局部失败、重复键、未经确认候选、隐私并发和重启不恢复上下文。

## 修改存储或依赖

schema 改动需说明旧版迁移、未知版拒绝、中途失败、备份/恢复兼容和重启行为；不要直接覆盖用户数据库。新依赖统一声明在 `pyproject.toml`，requirements 只引用分组，Windows 约束需按目标环境重新验证。

完成改动后按[开发与验证](development-and-testing.md)选择相关检查，并同步用户指南、开发主题和实际接口记录。
