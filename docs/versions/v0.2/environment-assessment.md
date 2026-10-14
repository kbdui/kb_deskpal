                 # V0.2 环境准备与范围评估

本页保留开工前的环境评估。实施后的安装状态、实际能力与验证结果见 [V0.2 交付说明](delivery.md) 和 [测试报告](test-report.md)；例如 mss 在开发阶段已安装至 deskpal_env，下面的“缺少”记录是检查当时的状态。

评估日期：2026-10-13。本文只规划下一阶段，不实现 V0.2，不安装依赖，不修改用户配置或系统权限。路线依据为 `doc/` 两份原始设计文档及 [当前架构](../../../ARCHITECTURE.md)。本地环境状态采用主 Agent 本轮实际检查结果；官方文档说明的是产品能力，不等同于本机已经安装或验证成功。

## 结论与范围

**可以直接开始 V0.2 的本地后端工具设计与开发，无需预先安装 Qt、Live2D、OpenAI SDK 或新的编译工具。** 截图工具进入实际联调时再添加截图依赖。Python 3.11/3.12 完整回归与 DeepSeek 真实调用已补验收，详见 [补测报告](../v0.1/followup-test-report.md)。

原路线的 V0.2 是 Screenshot、Calendar、Document Manager、Timer 四类工具，以及三级权限与 Sandbox。V0.1 的 echo/time/read_text/write_text 已提供工具框架、基础权限和工作区路径策略，但不等于四类生产工具全部完成。Live2D、人格、情绪与桌宠交互在原路线属于 V1.0；本次没有把桌宠预览加入 V0.2 的范围。

原设计提出“完整沙箱”，当前实现明确是可信工具的应用层策略。V0.2 若继续交付可信内置工具，应写明隔离边界；若要宣称完整操作系统隔离，需要额外的架构、Windows 权限及安全验收，不能仅安装 Python 包后宣布完成。

## 已检查的本地环境

| 项目 | 本轮已知状态 | 对开工的影响 |
| --- | --- | --- |
| 系统 | Windows 11 Home x64，版本 10.0.26200 | 可继续 Windows 优先的后端开发 |
| 用户 Python 环境 | `D:\anaconda\Anaconda3\envs\deskpal_env`，Python 3.12.4 | 保持现有环境，不为 3.11 补测降级 |
| 项目与开发工具 | 当前环境已安装 editable 项目、pytest 9.1.1、mypy 2.4.0、python-dotenv 1.2.4 | 已具备本地开发与测试条件；密钥由本地 `.env` 提供 |
| Python 3.11 补测 | 另在 `data/validation/py311` 建立隔离 Python 3.11.17 | 验证最低支持版本；最终结果见 [补测报告](../v0.1/followup-test-report.md) |
| 截图/图像包 | 当前环境缺少 mss、Pillow | 实现截图时补 mss；Pillow 按后处理需求决定 |
| 日历时区数据 | `deskpal_env` 实测 `ZoneInfo("Asia/Shanghai")` 可用；未安装独立 Python tzdata 包 | 当前无需先补 tzdata；打包或迁移到无时区数据的 Windows 环境时再验证并补充 |
| UI/Live2D 包 | 当前环境缺少 PySide6、PyQt6、live2d | 不阻塞原范围 V0.2；包名缺失不代表已核查所有 Cubism SDK 文件 |
| HTTP SDK | 当前环境缺少 openai SDK、httpx | 当前适配器使用标准库 HTTP，不要求补装 |
| C++ 工具链 | `E:\no_game\vsGenerateTool\BuildTools` 已有 VC 14.44.35207；Windows SDK 10.0.22621.0 / 10.0.26100.0 已有 | 不需重复安装 VS Build Tools；未来 Native 构建仍要实际验证版本/架构兼容 |
| CMake | `D:\cmakena\bin\cmake.exe`，3.28.0-rc3 | 工具已存在；这是预发布版本，不能据此认定目标 SDK 构建已通过 |
| 路径安全实测 | 真实 Junction 逃逸测试通过；真实 symbolic symlink 越界测试在一次临时管理员进程中单独通过（1 passed），普通权限完整 suite 仍因 WinError 1314 跳过该项 | 两类路径重解析均已实测；普通权限创建符号链接的限制不能靠安装 Python 包解决，本次未启用开发者模式或更改系统策略 |

上述缺少的包只是当前环境清单，不是全部安装清单。Cubism SDK/Core、可用模型资源、GPU/OpenGL 渲染能力和所选 Python binding 的 Native 构建结果尚未完成专项验证。

## V0.2 本地补充需求

| 能力 | 推荐最小实现与范围 | 是否需要补装本地工具/包 | 验收重点 |
| --- | --- | --- | --- |
| Screenshot | 单屏/指定屏幕/区域截图，PNG 保存到受控工作区；每次显式授权捕获，不默认常驻采集 | 主要新增包为 mss；它的工具函数可直接编码 PNG，Pillow 只在缩放、复杂裁剪或其他后处理需要时添加 | 多显示器、负坐标、DPI 与区域边界；授权前无捕获；失败不留下误报成功的文件 |
| Calendar | 本地 SQLite 日程，创建/查询/修改/删除，存储明确时区与提醒时间 | sqlite3、datetime、zoneinfo 等主要为标准库；若选 IANA 时区名，应验证此 Windows 环境时区数据是否可用，缺数据时才考虑 tzdata | 增删改持久化、UTC/本地展示、重复提醒、重启后恢复与遗漏提醒策略 |
| Document Manager | 工作区内检索、分类、复制/移动等文件操作；删除与覆盖单独授权 | pathlib、shutil、os、sqlite3 等主要为标准库；解析 PDF/Office/OCR 属于额外范围，按格式再选包 | Unicode 路径、同名/覆盖、跨工作区拒绝、Junction/链接逃逸、操作失败后的已完成副作用 |
| Timer | 倒计时、创建/查询/取消定时任务；先采用进程内调度，明确程序退出后的行为 | time、threading/事件、heapq、sqlite3 等主要为标准库；不要求先装 APScheduler 或外部服务 | monotonic 计时、取消、程序关闭、系统休眠/唤醒、到期事件与重复通知控制 |
| 权限 | 沿用可信注册权限元数据，Level 0 自动、Level 1 确认、Level 2 高风险明确确认 | 不需要靠新包实现权限等级 | 截图/写入/移动/删除分别定级；Level 2 不与普通工具白名单确认混为同一体验 |
| Sandbox | 保留工作区路径与文件大小规则，按需要增加可中止 worker 与时间/资源上限 | 不存在“安装一个包即可完整隔离”的准备动作 | 超时后实际停止执行、Windows 文件/网络权限、受控 IPC、审计；操作系统隔离必须另设验收 |

MSS 官方示例包含 `mss.tools.to_png`，Pillow 是处理像素的可选路径。因此基础截图保存不必把 Pillow 作为强制依赖。[MSS 官方示例](https://python-mss.readthedocs.io/stable/examples.html)

本地 Calendar/Timer 不需要 Google、Outlook、邮箱或云账号。外部日历同步、跨设备同步、系统通知/托盘、开机运行和程序退出后仍触发提醒，均应在明确范围后增加相应适配器或运行机制。不能把一次 `sleep()` 阻塞 Agent 请求当作完整定时任务系统。

截图工具成功保存 PNG 不意味着当前 LLM 能理解图片。当前 ChatMessage 主要承载文本；若需要识别屏幕内容，须另行设计带权限的图像输入 DTO/Provider 转换与外发规则，不自动把截图上传云端。

## DeepSeek 与真实 API 验收状态

DeepSeek 官方已提供 Responses API，给出的 base URL 为 `https://api.deepseek.com`，示例模型为 `deepseek-flash`，支持 function 工具调用；接口为无状态服务，不支持 `previous_response_id`。这与当前适配器的单次规划、独立 Reflect 和 `/responses` 请求方向一致，可先沿用配置中的协议适配器，而不要求更换 SDK。[DeepSeek 官方 Responses API 指南](https://api-docs.deepseek.com/guides/responses_api/)

保留用户选择的 `deepseek-flash` 与该 base URL，不替用户更换模型。当前适配器名/配置值 `openai` 表示协议路径，实际请求目标由 base_url 决定，不能据此把真实服务商记录成已验证的 OpenAI 服务。

**本轮真实 API 已验证。** 用户改为在被 Git 忽略的本地 `.env` 保存新密钥，`config.toml` 中 `api_key_env` 引用 `DEEPSEEK_API_KEY`。新增 python-dotenv，进程变量优先于配置目录 `.env`，读取不修改全局环境；显式测试环境映射完全跳过 `.env`，避免测试触碰用户凭据。

主 Agent 使用用户选择的 DeepSeek 配置完成 3 次限量 smoke 请求：基础聊天、强制 echo 协议调用及独立总结；又完成 2 次限量完整 Agent 请求，由生产工厂装配的 Planner 在四个工具中自动选择 echo，经 Core 执行、反思，保存 Completed 任务、SQLite 摘要与日志。每次最多 128 个输出 token，关闭推理；没有显示密钥或响应原文。强制 echo 的 smoke 验证与自动选择的整合验收分别记录，不宣称所有模型能力或高级自主行为已认证。真实 HTTP/认证/余额/网络错误仍为失败，不以 skip 掩盖。

DeepSeek 支持图像输入也不等于当前文本 DTO 已支持多模态。后续 Provider 扩展仍应局限于 `llm` 与 Application 组装；Core 不引入厂商 SDK、不保存模型内部推理。

## 可选 UI 与未来 Live2D 准备

若以后明确增加桌宠/工具预览窗口，优先验证 PySide6 wheel。在支持的 Python/Windows 组合上，PySide6 wheel 包含 Qt 二进制，通常不需要另装完整 Qt 安装器；PyQt6 是替代选择，不必同时安装两套。[Qt for Python 官方入门](https://doc.qt.io/qtforpython-6/gettingstarted.html)

Live2D 播放需要以下几类准备，不能用一个 `pip install live2d` 替代：

| 项目 | 当前准备状态 | 到 Live2D 阶段需要验证的事项 |
| --- | --- | --- |
| Cubism SDK / Core | 本轮未核查可用 SDK/Core 下载包、版本与授权 | 选择 Windows x64 支持的 Native 版本、对应 Core 与 Framework，确认分发及使用条件 |
| Python 接入 | 当前缺少 live2d 包，未选 binding | 官方平台能力以 Native/Web 等 SDK 为基础；社区 Python binding 不等于官方原生 Python SDK，需要检查维护状态、Python 3.12 wheel/ABI、Core 版本和 Qt/OpenGL 集成 |
| 模型资源 | 本轮未核查可使用的完整模型包 | 准备已获许可的 `.model3.json`、`.moc3`、纹理，按模型需要包含动作/表情/物理资源；检查模型能被所选 SDK 加载 |
| 渲染与驱动 | 本轮未做图形专项测试 | 实测 OpenGL context、GPU 驱动、透明窗口、DPI、多屏、帧率与退出清理；远程桌面等运行场景另测 |
| Native 编译 | VS Build Tools / Windows SDK / CMake 已存在 | 实际运行匹配版本的 x64 Native sample/binding 构建，检查 CMake generator、MSVC runtime、SDK 搜索路径及所需 GLFW/GLEW 等依赖 |
| Cubism Editor | 本轮未核查安装 | 自己制作/编辑模型时才需要；播放已有可用模型不要求安装 Editor |

Live2D 官方 Native OpenGL 示例说明了 Windows 下使用 Visual Studio 与 CMake 的构建流程，以及示例中的 GLFW/GLEW 准备；具体流程随 SDK 版本变化。现有编译工具是“已安装”，Cubism Native sample 是“尚未验证”，二者不能合并成构建已通过。[官方 OpenGL 示例构建](https://docs.live2d.com/en/cubism-sdk-tutorials/sample-build-opengl/)

SDK 的 Windows 和渲染支持以所选版本的官方平台表为准，不凭社区包名推定官方支持范围。[Live2D 官方平台支持](https://docs.live2d.com/en/cubism-sdk-manual/platform/)

## 建议的 V0.2 架构与验收顺序

1. 先固定四类工具的输入/输出、错误语义、权限等级和持久化规则，仍经 Tool Manager → Sandbox → Tools 执行；UI 直接调用也必须使用同一安全入口。
2. Calendar 和 Timer 增加独立本地存储/调度服务，由 Application 管理线程与关闭；工具只提交、查询、取消任务。Core 保持同步请求 API，不能因长计时阻塞整个运行循环。
3. 为截图和文件批量操作设计结果 DTO，明确工作区路径、成功/失败和副作用。高风险操作的确认应包含具体操作、目标路径和覆盖/删除内容；授权按请求生效，不能直接把旧 Waiting 重规划结果视为原计划确认。
4. 在可信工具边界内完成权限与路径验收，再决定是否投入完整 OS 隔离。Job Object 可用于进程/资源管理，但不是文件访问或网络隔离本身；完整方案还要验证 worker 权限、能力代理、文件 ACL/受限令牌或 AppContainer 等设计与 Windows 实际行为。截图等需要桌面访问的能力可能要通过单独受控代理，而非把所有工具放进同一进程权限假设。
5. 后端与四类工具验收通过后，再根据明确需求开展 Qt 预览或 Live2D 专项。可在未来新增 `screenshot`、`ui`、`live2d` optional extras 或分组 requirements，并在兼容性测试后固定版本；本次不创建这些依赖，也不改 `pyproject.toml`。

UI/Live2D 模块不得反向成为 Core 的导入依赖；关闭资源和调度事件经 Application/交互层对接。SDK 下载、模型资源、截图、编译产物与用户配置应放在独立可配置路径并按许可/隐私要求忽略版本控制，仓库只保留必要说明及确有分发许可的资源。

开工顺序不被缺少 Qt/Live2D 包阻塞：先完成 V0.1 补测与真实 API 状态说明，然后设计本地 Calendar/Timer/Document 工具及安全契约，截图联调时添加 mss。若验收目标仍包括“完整 OS 沙箱”，该项需要单独里程碑；当前环境包安装充分与否不能替代隔离证明。
