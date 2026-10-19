# V0.6 工具迁移表

状态：阶段0设计。下表按当前工厂注册的19个内置工具盘点；**目标分类尚未实现，不代表已隔离或可强停**。当前仍由V0.5可信工具管理器执行，权限等级保持原义。

| 工具 | 现有等级 | V0.6目标执行类别 | 首版自动规则候选 |
| --- | --- | --- | --- |
| echo | AUTOMATIC | 受限worker，有界输入输出 | 是，仅显式规则Grant |
| time | AUTOMATIC | 受限worker，UTC时间输出 | 是，仅显式规则Grant |
| read_text | AUTOMATIC | 文件broker精确目标/版本快照→worker | 是，仅精确文件及内容版本Grant |
| write_text | CONFIRM | 文件broker受控新建/覆盖，宿主完成凭据 | 否 |
| document_search | AUTOMATIC | 文件broker受限目录元数据查询 | 否 |
| document_classify | AUTOMATIC | broker有界文件清单→worker扩展名分类 | 否 |
| document_copy | CONFIRM | 文件broker绑定源/目标及覆盖前提 | 否 |
| document_move | HIGH_RISK | 文件broker复制/移除阶段事实，不承诺回滚 | 否 |
| document_delete | HIGH_RISK | 文件broker精确文件身份删除 | 否 |
| calendar_create | CONFIRM | 日程broker有界命令/宿主SQLite | 否 |
| calendar_list | AUTOMATIC | 日程broker分页只读 | 否 |
| calendar_update | CONFIRM | 日程broker目标ID/revision及参数绑定 | 否 |
| calendar_delete | CONFIRM | 日程broker精确目标删除 | 否 |
| calendar_events | AUTOMATIC | 日程broker有界事件游标 | 否 |
| timer_create | CONFIRM | 计时器broker持久化命令，不启动等待线程 | 否 |
| timer_list | AUTOMATIC | 计时器broker分页只读 | 否 |
| timer_cancel | CONFIRM | 计时器broker精确目标取消 | 否 |
| timer_events | AUTOMATIC | 计时器broker有界事件游标 | 否 |
| screenshot_capture | CONFIRM | 显式桌面代理，屏幕/区域/输出绑定 | 否 |

AUTOMATIC是旧工具级应用策略，不是持久自动授权。所有首版候选也须经dry-run及独立Grant、期限/预算、用途和最新revision核验；自动结果不进入云反思。

独立worker的终止证明不覆盖宿主broker未决调用。文件提交和SQLite操作有单独完成/未知事实，截图若仍同步则标记协作停止；未具备可靠停止的能力不能列为已强停或被自动规则引用。每项后续补：执行模式、代码契约版本、身份/路径范围、限额、取消/提交顺序、真实测试证据和遗忘清理索引。
