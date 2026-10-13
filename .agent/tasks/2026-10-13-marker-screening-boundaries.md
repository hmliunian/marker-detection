# Marker 数据筛选边界建立记录

日期：2026-10-13。

状态：首轮实现和纯SSH网页修复均已完成，用户授权的 multi-agent 实现、集成与小样本验收已执行。原 READY 边界继续适用，详细结果见 `VALIDATION.md`。Git作者已在本仓库配置，本地首次提交完成；指定分支推送被GitLab首次提交权限拒绝，需Maintainer处理远端初始化。

## 2026-10-13 SSH GUI 与首次推送修复边界

状态：READY。用户反馈 `just gui` 缺 xcb 依赖退出134，随后明确使用纯SSH终端；提供了本仓库Git作者姓名和邮箱，并给出要推送的 `origin feat/umi-check` 命令。

| 边界 | 本次结论 | 状态与依据 |
| --- | --- | --- |
| 目标与验收 | 纯SSH可运行 `just gui`，通过仅监听loopback的网页和SSH端口转发查看同样的帧/marker/XYZ/3D/缺失信息；保留桌面Qt入口。完成首个提交并正常推送指定分支。 | 已确认：用户明确纯SSH及作者配置。 |
| 工作区与基线 | 当前 `feat/umi-check` 无HEAD，33份项目文件已暂存且无未暂存更改；origin查询无分支。现有Xorg为gdm，当前会话无DISPLAY/Wayland，无正在运行的本项目GUI。 | 已核实；不切换分支、不修改其他进程。 |
| 修改范围与非目标 | `src/marker_detection/web.py`及网页静态资源、对应测试、gui.py启动提示、justfile/pyproject/uv.lock及现有说明/验证记录；不改主手/位姿/质量算法及已有产物。 | 已填写；独立项目内修复。 |
| 业务与架构不变量 | 网页读取现有analysis/frames契约；有效性仍为valid与quality.valid合取，缺失断线、原始候选单独显示；不重复求解或改写筛选结论。 | 沿用原边界。 |
| 输入输出与数据 | 使用已有real-zero-touch-v2及third/pickup结果；静态页面/脚本本地提供，不走CDN，服务只允许分析产物与必要预览，不开放任意文件。截图写artifacts下新增路径。 | 已填写。 |
| 运行环境与资源 | uv+just独立环境，新增浏览器绘图依赖按锁文件安装；单个只读HTTP服务默认127.0.0.1，浏览器验证用本机现有Firefox/驱动（若可用）。 | 已填写；不要求用户安装X11或桌面。 |
| 外部副作用与费用 | 本项目依赖下载、localhost服务；SSH转发由用户终端连接。Git仅本仓库配置用户明确给出的作者，提交当前项目相关暂存内容并普通推送origin/feat/umi-check；不force、不部署、不改源数据、无模型费用。 | 用户提供精确作者与推送目标；远端为空已核实。 |
| 验证计划 | 静态检查、服务器路由/路径隔离/坏输入测试、浏览器实际渲染与帧联动及桌面入口友好错误；保留Qt离屏回归。Git提交前检查暂存范围，推送后比对本地HEAD与远端分支。 | 已填写；不能以offscreen通过声称纯SSH可弹桌面窗口。 |
| 交付与回退 | 使用说明给出SSH转发及浏览器地址，更新真实验证结果；Git以本项目首个提交交付。回退仅撤回新增Web入口和关联改动，既有数据/产物保留。 | 已填写。 |
| 待确认事项 | 无；姓名Xuran Yao、邮箱635301928@qq.com、仅local配置；纯SSH意味着浏览器入口符合当前使用环境。 | 已确认。 |

调查依据：`ldd libqxcb.so`仅缺libxcb-cursor.so.0；Ubuntu24.04当前无该包，sudo需要密码。用户澄清纯SSH后，不继续安装无助于SSH显示的桌面依赖，转为本机loopback网页查看。先前Qt截图仅验证offscreen路径，未覆盖xcb桌面启动。

## 本轮简短边界

- 目标：先形成并填写用户确认的边界；随后按用户最新授权，以三个 agent 和主 agent 实现数据读取、主手判定、三维 action、整条筛选及 GUI。
- 范围：在项目边界已列路径实现独立 Python 工具，包含 MCAP 新旧读取、主手判定、参考 AprilTag/PnP 位姿、逐帧质量与筛选、本地 GUI、测试和 uv/just 工程入口；只读使用相邻参考仓库和原数据。
- 权威项目边界：目标、十项必填边界、数据契约、实验及验收条件统一保存在 `PROJECT_BOUNDARIES.md`，不在本记录维护第二份表。
- 基线：`main` 尚无首个提交；原有 5 个文件全部在暂存区，工作区与其暂存内容一致。已有输入格式文档任务属于另一项已完成工作，本轮不改写其记录。
- 副作用：建立项目独立环境和锁文件；依赖安装按系统权限执行，真实旧 MCAP 和 third_marker 样本只读，实验结果只写 `artifacts/`，单进程逐条执行。尝试现成 CUDA 解码并记录实际后端。GUI 本机运行；不调用外部付费模型、不推送。
- 检查：执行 just check/test；回放普通旧格式、另一旧命名和第三方包；新 CDR 仅做离线契约验证。检查 GUI 的图像、XYZ 曲线、三维轨迹、缺失和帧选择联动，保存截图供本会话复核。保留原有 5 份文件的暂存 blob。
- 交付与回退：交付源码、配置、锁文件、just 入口、使用说明与验证记录；仅撤回本任务明确路径，不清理原数据或其他任务文件。作者身份缺失不能通过猜测或改写全局配置解决。

## 实现分工与共享接口

- 主 agent：工程入口、`models.py`、视频解码、AprilTag/PnP/双目几何、CLI 集成、真实数据和 GUI 验证、最终文档。
- 数据 agent：`io.py`、`flatbuffer.py`、相应读取测试；输出统一 Episode/VideoPacket/CameraCalibration/ScalarSeries，支持原 MCAP 和含 MCAP 的 zip。
- 判定 agent：`primary_hand.py`、`quality.py` 及相应测试；不依赖 GUI，不重复实现检测。
- GUI agent：`gui.py` 和 GUI 相关测试；读取分析产物，不执行或另写筛选算法。采用本地 PySide6 + Matplotlib，不引入网站托管。
- 输出契约：`analysis.json` 保存 episode/主手/筛选结论/配置/来源；`frames.json` 保存按帧时间戳、XYZ、有效性、原因、观测和图像路径；`frames/` 保存 GUI 使用的压缩预览。缺失 XYZ 为 null，轨迹不跨缺口连线。
- 验证入口依照 PROJECT_BOUNDARIES.md；所有新增单元测试围绕可观察行为和故障语义，合成投影用于校验几何，不能替代真实数据验收。

## 调查结果

- 数据 agent：三个 `third_marker` 包均为 FlatBuffer MCAP，21 路 channel、5 种 BFBS schema；只读内存解压并查摘要，没有写出原始数据或解码影像。已有旧 MCAP 路径可追溯且抽查存在；真实 ROS2 CDR 样本未找到。
- 位姿 agent：参考算法提供相机系位姿和按帧门控，不提供主控手、第三方身份或有效 episode 的业务标准。连续性、缺失及身份需要各自保留证据。
- 环境 agent：本机有 4 张 RTX 5090，但已查 tinynav marker 路径没有 GPU 实现；本项目工程环境尚未建立，不能把相邻项目的依赖视为本项目已可运行。
- 用户后续明确：搬运单独标记；非搬运发现双手参与即剔除；依照 tinynav 算法，如其提供 GPU 版本则优先。上述决定已合并到项目边界。
- 用户进一步确认：action 指三维坐标；缺失或异常超阈值时整条 episode 剔除。首轮从参考 `camera_T_target` 提取米制三维位置，姿态仅作内部求解与诊断。
- 已解决 Q1、Q2。用户随后确认参考 marker 尺寸，并要求真实新格式数据暂不处理；首轮新格式支持按文档编写。实际标定和身份差异仍随样本核查，没有据此声称真实算法或新格式验收通过。
- 首次源码核查：参考节点确实从 `camera_T_target` 直接发布 x/y/z；其现有 MCAP 读取器面向 ROS2 CDR，独立项目需补旧格式适配。读取器另有 CUDA/FFmpeg 视频解码路径，后续可优先验证，不等同于 marker 检测在 GPU 上执行。入口和依据已补入项目边界。
- 用户最新核对要求：逐帧判断 XYZ 连续性，可以用 AI 辅助，GUI 展示轨迹。边界已加入相邻位移/速度、缺失区间、XYZ 时间曲线及三维轨迹联动要求；不要求先提供人工标注集。AI 辅助不扩展为外部付费模型调用。
- 本次恢复后重新查看 Git 与进程，仍为同一工作区和 7 份暂存文档，未见中断留下的 marker/MCAP/ffmpeg 计算任务。仅更新本任务两份文档。

## 原有暂存基线

| 路径 | 暂存 blob |
| --- | --- |
| `.agent/tasks/2026-10-13-umi-input-format.md` | `34942a29808eb0f40e96cf2e7cdbd96791822d7c` |
| `README.md` | `5798dabf23258a6a60d4bff802c1f3b3bdc628b1` |
| `UMI_INPUT_FORMAT.md` | `9f7af40d8034c3561151682a81c84e8d884b3ef5` |
| `gripper_state_analysis.json` | `7b9327f5f8c30d7c79842676f267526e7b17cd52` |
| `gripper_state_analysis.txt` | `d632ca0c55e73f1617010bc1d8144c84d015ef4c` |

## 验证与提交

- 通过：核对两份文档的相对链接；原有来源、新建任务记录及新增源码引用的目标均已读取或核实存在。
- 通过：`git diff --cached --check -- PROJECT_BOUNDARIES.md .agent/tasks/2026-10-13-marker-screening-boundaries.md`，退出码 0，无空白错误。
- 通过：原有 5 个暂存 blob 与上表一致，原文件没有本轮工作区改动；本轮只新增并暂存两份明确文档。
- 已核实：`git config --get user.name` 和 `git config --get user.email` 均无配置值，退出码 1；未修改身份配置。
- 提交结果：尝试 `git commit --only` 仅提交本轮两份文档，返回 `Author identity unknown`，没有生成提交。文档保留在工作区和暂存区，没有设置或猜测 Git 作者身份。
- 上述提交与文档检查发生于边界建立阶段；实现阶段结果补充如下，不以早期“未执行”状态覆盖当前事实。

## 实现交付与最终验证

- 交付：`src/marker_detection/` 10个模块、`tests/` 8个测试文件、参考配置与超参数示例、`pyproject.toml`/`uv.lock`/`justfile`/`.gitignore`、`USAGE.md`、`VALIDATION.md`；复用本任务记录，未建立第二份开发表。
- 使用三个 agent 分别实现读取、判定和 GUI；后续交叉验证位姿与 CLI，主 agent 实现几何、CUDA 解码和集成。共123项测试通过；Ruff静态/格式通过。实际命令和结果见 VALIDATION.md。
- 7条真实完整旧MCAP/ZIP以GPU0逐条回放成功：零触觉倒液体accepted，拿起/关闭/3条third rejected，搬运excluded_carry。拒绝原因、完整帧轴、原始候选与参数均保留；不是以“分析进程成功”代替数据合格。
- 最终复核只采用 `artifacts/real-*-v2/`，早期产物仍保留，不覆写或删除。逐帧AI辅助复核保存于VALIDATION.md，Qt截图见相应结果目录。
- GPU验证是FFmpeg/CUDA硬件视频解码，AprilTag/PnP仍CPU。真实CDR按用户要求未执行；合成CDR读取契约通过。没有位置真值，不宣称绝对精度或全数据准确率。
- 修复独立测试发现的Pupil浮点参数/析构顺序、Qt退出生命周期，以及CLI非有限诊断持久化问题。失败原因和重新验证记录均已保留。
- wheel离线构建通过，内置配置可从临时解包目录独立加载；不依赖相邻仓库运行，不打包原数据或环境缓存。
- 首轮结束时尚无可用Git作者身份，故当时保留工作区交付，没有猜测配置或推送；原有五份暂存文件的blob与上表一致。后续用户已提供身份并指定推送目标，见下方修复记录。

## SSH 修复交付与最终验证

- 三个agent分别实现网页服务与29项接口测试、交互网页、Qt启动诊断；主agent整合工程入口、依赖、浏览器验证和交付文档。复用同一任务记录。
- `just gui`现在运行本地只读网页，Qt移至`just gui-native`。无DISPLAY会话启动网页成功；Qt入口缺少显示时友好返回2，缺xcb库时提前诊断。未安装系统包。
- Firefox首次截图暴露无WebGL时三维空白，随后实现Canvas回退并重新实测。正常零触觉、拿起误检和第三方714共3组浏览器验收通过：图像/帧联动、无效帧跳转与回绕、XYZ缺失断线、三维绘制像素和鼠标旋转均通过；截图保存为各结果目录的`web-review.png`。
- `just check`通过，21个Python文件格式通过；`just test`共161项通过。离线wheel重新构建为`artifacts/ssh-gui-package/`，共17文件，最终HTML、web入口和参考配置均在包内且可独立加载。源码数据算法与已有分析JSON未修改。
- 用户提供的`Xuran Yao <635301928@qq.com>`已仅写入本仓库Git配置。首次提交范围包含已有暂存项目和本次修复，原始5份文档/分析内容保持不变；`.venv/`、`.tools/`、`artifacts/`不纳入提交。按用户命令普通推送`origin/feat/umi-check`，最终Git结果以本地HEAD与远端分支核对为准。
- Git实际结果：首次提交`f19d54c`成功，作者与用户给定一致，37个项目文件纳入提交；暂存空白检查通过，原始5个blob保持一致。随后`git push --set-upstream origin feat/umi-check`被远端pre-receive hook拒绝，提示默认分支受保护、当前角色不能推送initial commit，要求Maintainer完成首次推送。再次`git ls-remote --heads origin`成功且结果为空，确认没有远端分支；未force、未改保护规则或绕过角色权限。此记录以单独文档提交保存。
