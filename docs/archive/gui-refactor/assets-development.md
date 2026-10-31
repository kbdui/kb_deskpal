> 历史存档：保留当时的设计、失败与验收边界；不代表当前实现或新的执行授权。当前入口见 [文档导航](../../README.md)。

# D1/D4 外观素材与归档开发交付

日期：2026-10-25。状态：源码开发与静态阅读审查；未执行测试、mypy、真实 Qt 界面或模型验证。开发授权不代表验收通过。

## 装配与调用

`deskpal.assets.AssetService(workflow_store, data_dir, decoder=None)` 复用工作库 `read_connection/transaction`；不新建库或自行迁移。生产实例锁由 Application 持有。统一 schema 2→3 迁移执行 `assets.schema.ASSET_SCHEMA`；表为 `asset`、`appearance_profile`。

GUI 通过 Application 的一次性 `configure_asset_decoder` 注入 `gui.asset_decoder.QtAssetDecoder`（服务方法 `set_decoder`）。模块导入不加载 Qt，解码调用才惰性加载；不创建 QApplication。CLI 可查询默认/已保存元数据；未注入 codec 时导入和预览返回固定中文“图片解码组件不可用”。

固定槽位：`user_avatar`、`character_avatar`、`pet_main`、`pet_thinking`、`pet_focused`、`pet_pleased`、`pet_alert`。

| 方法 | 契约 |
| --- | --- |
| `snapshot()` | 七个 `AppearanceProfile`；缺少自定义差分时 `effective_asset_id` 回退当前自定义主图；无主图才使用内置 |
| `prepare(path, slot, generation=...)` | 有界读取一次源文件；返回 token/尺寸/hash，不更换当前引用；同槽 generation 必须递增 |
| `preview(token=... / asset_id=..., max_edge=480)` | 恰好一种选择；返回 `AssetPreview(png: bytes,width,height,asset_id,token)` |
| `apply(slot, token, expected_revision, generation=..., display_mode='contain', crop=None)` | generation+profile CAS；原子发布不可变 PNG 后，同库事务更新引用；返回新 profile |
| `reset(slot, expected_revision)` | 独立槽位恢复默认；清掉该槽未应用 token |
| `discard(token)` | 删除内存暂存；返回是否存在 |
| `collect(limit=32)` | 有界回收未引用元数据/托管副本；保留所有当前引用；返回删除计数与 more |

`AssetPreview.png` 只用于专用本地图片 worker/信号，不能经过通用 JSON bridge、base64、QSettings、模型或云。source path 仅本次导入使用，不进入元数据或回执。头像 mode 为 `contain/square/circle`，桌宠为 `contain`；crop 是四个归一化坐标 `(x,y,width,height)`，由显示适配器按比例渲染。

## 边界与崩溃语义

只允许扩展名和真实内容一致的 PNG/JPEG；5 MiB、单边 2048、最多 4,194,304 像素；解码前后检查，拒 APNG、MPO/拼接 JPEG、空图和全透明桌宠。Qt 解码后复制 RGBA 像素到新 QImage，去掉 EXIF/text/ICC 等元数据，再输出 PNG。JPEG 不自动抠图。重编码也受 5 MiB 上限约束。表情组当前引用合计最多 8 百万像素/20 MiB。

最多七个未应用 token，每个最多 5 MiB、有效 15 分钟；持久资产元数据最多 128 条，受控目录扫描最多 256 个文件。apply 自动先回收无引用资源，正常多次换图不会因历史副本累积到 128 条而停用。

发布使用 `.import-<asset_id>.tmp`→无覆盖硬链接 `<asset_id>.png`→移除暂存→profile 事务提交。初始化/collect 仅恢复同 ID、samefile、恰好两个 hardlink 的受控中断发布对；第三条外部链接或不匹配文件拒绝。提交失败只留下未引用副本；collect 可回收。源文件不会被改写。

## 归档格式 2

四库数量保持不变。新 manifest `format_version=2`、scope=`four_sqlite_stores_and_managed_assets`，增加 `assets` 条目，字段 `name/bytes/sha256/width/height/media_type`；名字只能是 `assets/<32位小写hex>.png`。仍排除配置、凭据、日志、工作区和模型缓存。manifest 上限 64 KiB，最多 128 素材，单图 5 MiB，四库与图片合计 1 GiB。

同一个 data_dir InstanceLock 覆盖四库在线快照、快照中当前 profile 引用、对应 PNG/hash/尺寸及规范 PNG 结构验证。只备份当前 profile 引用的副本；无引用旧元数据可能仍在库中，恢复后由 collect 回收，因此垃圾回收中断不会因无引用文件缺失而阻止备份。

format 1 的 schema 1/2 四库备份继续检查/恢复，升级后外观默认；恢复回执 `appearance_defaults_required=true`。schema 3 不能伪装成不含素材契约的 format 1。format 2 恢复在创建目标前完整验证四库、全部图片和所有当前引用，再沿既有 `.restore-*` 标记屏障逐文件发布；图片尚未就绪、目录内容变化、重复路径、路径穿越、坏 hash、超限或中断均不能启动。resume 同样核对原归档和受控目录。

## 未执行验证与后续整合

已编写 `tests/unit/test_appearance_assets.py`、`tests/unit/test_appearance_archives.py`，并将旧归档 scope 断言更新为格式 2。覆盖 CAS、旧 generation、差分回退、CLI codec 缺省、透明桌宠、受控临时文件恢复/外部 hardlink 拒绝、正常换图回收、格式 2 素材往返、真实 schema 2 构造的 format 1 兼容、坏 hash/路径/缺素材、图片发布中断与恢复。均未运行。

实际 Qt PNG/JPEG 插件行为、EXIF 旋转及清理、PNG 编码块兼容、DPR/crop 显示、缓存和原生 worker 生命周期需在用户授权的测试阶段验证。应用装配、专用 AssetBridge、设置表单、头像和桌宠订阅由其他任务整合；本交付没有修改这些 UI 所有权。
