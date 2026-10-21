# V0.7 静态桌宠接口与线程边界

更新：2026-10-20。当前产品版本仍为0.5.0rc1。本文件描述已实现接口，实际验收结论见test-report.md（未完成前不得称交付）。

## 权威状态

桌宠经既有ApplicationBridge的read通道提交 `presentation`，参数必须为空对象。DesktopFacade/ApplicationService沿用已有初始化、Closing和cleanup门禁；不接受task_id、动作或授权字段。返回精确七字段：

| 字段 | 约束 |
| --- | --- |
| schema_version | 原生整数1 |
| session_id | 宿主会话标识，非空字符串 |
| sequence | 非负整数；audit_outbox AUTOINCREMENT持久高水位，不因已投递行删除倒退 |
| memory_epoch | 既有记忆权威代际，非负整数 |
| status | 原TaskStatus或Idle/Unknown |
| task_id | 不透明任务ID或null，不携正文 |
| revision | 非负整数；无任务时为0 |

桥接信封另含request_id、generation、privacy_epoch和ok；GUI本地接收时钟控制新鲜度。generation/privacy_epoch不是后端七字段的一部分。禁止传入Application/Core、数据库连接、API key、请求/结果/错误/计划正文。

选取优先级保持唯一后台工作流语义：Planning/Running及Claimed的Created/Waiting优先，其后Queued的FIFO，再是Created/Waiting/NeedsReview/Uncertain，最后最新终态。Claimed不等于Running。读取失败/无效数据使GUI显示Unknown，不能伪装空闲。

WorkflowStore在已识别schema1的初始化事务内补齐三个附加索引；表、列、schema版本和权限不变。现存大历史第一次启动有索引构建成本，在已有后台factory中完成。查询只读取元数据，通过固定候选查询避免每秒排序全部历史；队列分支依赖现有pending容量和唯一Claimed约束，不能声称任意损坏数据库的扫描时间有硬上界。

## 展示与输入

固定表达ID：neutral、thinking、focused、pleased、alert。Planning映射thinking，Running映射focused，新Completed短时一次pleased，Failed映射alert；其余中性图和准确中文文字。首次显示/恢复/记忆代际变化建立基线，不重播历史成功。

文字即时更新；thinking装饰延迟250ms、一般切图最短500ms、完成反馈1500ms。Waiting/Failed/Unknown、隐私失效和Closing抢占旧装饰。memory_epoch前进允许同sequence的合法元数据变化，立即取消旧反馈；同代际同sequence冲突仍拒绝。每个装饰回调复核代际、任务和revision。隐藏/禁用停止专用查询，显示时最多一条在途、约每秒查询一次。

点击只请求主窗口导航；拖动超过Qt阈值后不发导航。角色、图片、状态与UI均不确认/恢复计划，不运行工具或改参数。窗口形状取neutral基线，换表情不改变窗口/命中区域；全窗穿透通过Qt原生窗口flag启停，主窗口与托盘均有恢复开关。

## 图片和生命周期

固定随包PNG，不接受远程URL、任意资源路径、外部manifest或可执行角色文件。neutral必需，差分可选并回中性图；缺neutral/坏图/超量/解码失败使用项目自有降级标记，不能视为美术验收通过。

一个Python后台线程负责资源读取、QImageReader/QImage解码与480像素预缩放；请求最多一个在途和一个合并最新请求。结果携加载代际，QImage发布后不再修改。QPixmap、QWidget、QPainter、窗口flag、偏好及QObject最终销毁在GUI线程。GUI不join/wait/读盘/解码。

准备2秒超时使旧结果失效并降级，但仍持有线程引用至实际退出。关宠清缓存、停止timer并异步销毁窗口；保留retired窗口引用直到destroyed。DesktopHost同时等待bridge Closed、图片线程结束和所有桌宠窗口销毁后才dispose托盘和退出Qt；等待时保持Closing及可见宿主，不伪装已释放。

绘制异常发一次分类信号，清缓存并固定降级，不重新呈现同一故障pixmap；不修改任务状态或重放工具。没有自有GL上下文、第二个Core或独立渲染进程。

## 兼容与资源

沿用gui可选依赖PySide6-Essentials>=6.12,<6.13；CLI/后端不导入Qt。包资源在pyproject.toml中统一声明。预算与T01–T08以[验收边界](../../planning/v0.7/acceptance-boundaries.md)为准；实际数值和未验证项必须查看当次测试证据。
