# V0.7 静态桌宠归档记录

归档日期：2026-10-21（Asia/Shanghai）。**按用户要求结束本轮开发与测试，带未决项归档；不是全部验收通过或正式发布。** 产品版本保持0.5.0rc1，无Git stage、commit、push、reset或PR。本次归档指保存源码、材料和证据索引，不移动/清理工作区或打包私人数据。

用户收尾指令：“不做表情切换时序诊断，简单分析内存分配归因与做必要修复，完成后先归档v0.7”，并要求记录以后建议但本轮不做的测试。该指令替代此前继续新增时序/分配诊断运行的安排。

## 已实现与已验证

- 五张随包原创透明机器人PNG，实际加载、固定画布/命中区域、有限表情切换、状态文字和可靠降级。
- 可选且默认关闭的透明桌宠，拖动/导航、显示隐藏、置顶/穿透、独立位置恢复；代表性100%/200%实际跨屏往返。用户确认右屏外观正常，右屏禁用再启用恢复原位置；最终回主屏DPR1，无拖动误导航。
- 唯一Application/Core所有权，表现层只消费七字段元数据；不持DB/API key、不执行或确认工具。后台有界QImage准备，GUI拥有QPixmap/窗口/绘制，Host统一等待真实资源结束。
- 新状态/GUI测试、受影响Python3.12回归158通过、3.11兼容277通过；定向慢Core/慢loader响应与回收达标。结果范围有重叠，不累加成总覆盖量。
- 离线wheel及含中文空格target资源hash一致，安装版原生运行；无Qt CLI帮助入口已测，完整干净环境CLI业务未认证。

## 30分钟长测与保留问题

安装版同构建1800.406秒，3轮预热、20轮组合/120条控制；心跳p95 63ms、最大157ms，控制最大109ms，正常关闭282ms。启宠空闲CPU相对禁宠增量0.4138个百分点；20轮禁用区间private最大增量14.098MiB、句柄最大+5，未超对应预算。

**Qt正常退出不等于长测整体通过：helper exit1，原三条预览诊断失败完整保留。** 两次identity未匹配与一条集合汇总失败未完成时序归因；不认定为已修复，也不据此直接判为生产绘制缺陷。用户明确本轮不继续该诊断。

private仍缓慢上升。源码确认探针在进程内保留增长的原始记录；各轮实际缓存归零、loader结束、待销毁窗口清空。未做heap/native分配取证，不能排除产品泄漏。见[内存简析](memory-review-2026-10-21.md)。未定位到明确生产缺陷，本轮无必要生产代码修复，没有改测试预期或删除失败。

因此T01–T07已有对应范围证据，**T08保留未完整通过状态**；本轮按用户授权先归档，不将原门槛改成通过。后续建议见[未执行测试清单](deferred-tests.md)。

## 归档与恢复入口

| 内容 | 位置 |
| --- | --- |
| 实际接口/操作 | [interfaces.md](interfaces.md)、[operations.md](operations.md) |
| 全部批次、首败与验收范围 | [test-report.md](test-report.md) |
| 父架构/线程审查 | [review-2026-10-21.md](review-2026-10-21.md) |
| 20日原始证据 | [implementation-20261020](../../../data/validation/v07/implementation-20261020/) |
| 21日原始证据与wheel | [implementation-20261021](../../../data/validation/v07/implementation-20261021/) |
| 长测逐轮资源复算 | [resource-analysis.md](../../../data/validation/v07/implementation-20261021/final-soak-installed-r1/resource-analysis.md) |
| 本轮收尾状态/指纹 | [archive-20261021](../../../data/validation/v07/archive-20261021/) |

wheel：`implementation-20261021/wheel-offline-r1/wheel/deskpal-0.5.0rc1-py3-none-any.whl`，SHA-256 `a41cb693bb7028a308bf9e94c48e9831f2a636020b65cafeea8fdbb43c84189b`。它是本地验收构建，未发布。安装target为`implementation-20261021/wheel-install-r2/安装目标 含中文`。归档不重新构建或修改它们。

后续工作从本记录和deferred-tests开始，先确定需恢复的具体条目；代码继续按用户指定由6.1 Sol high编写、常规测试由6 Luna high执行，父Agent审查。收尾时两子Agent因用量限制报错停止，没有仍在后台运行的子Agent测试；未运行的两个新诊断脚本保留为草稿，不冒充已通过工具。

Live2D延至正式版后更新；人格情绪主要留V0.8。本归档不完成V0.6阶段0、不提供隔离执行能力，也不将V0.5旧未结项改为通过。当前非商业本机研发，未来发行另核许可和安装闭包。
