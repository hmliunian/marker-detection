# 首轮实现验证记录

日期：2026-10-13。对应 `umi-marker-screening 0.1.0`、本目录 `uv.lock` 和参考 `UMI_v3.json`。以下是工程验证和小样本复核，不能解释为全数据集准确率。

## 首轮算法实现的工程验证（SSH 修复前）

| 检查 | 实际结果 |
| --- | --- |
| `just setup` | 通过；按锁文件检查独立 `.venv`，32 个环境包。 |
| `just check` | 通过；Ruff 导入/静态检查和 18 个 Python 文件格式检查。 |
| `just test` | 123 项离线测试通过；含 7 项 Qt GUI 测试及截图进程正常退出。 |
| `just inspect` | 第三方 ZIP 只读识别为旧 FlatBuffer/BFBS，21 路 channel；head 双目标定、gripper 和触觉读取正常。 |
| `just analyze … --video-backend cuda` | 7 条完整真实旧格式 episode 成功生成产物，双目实际后端均为 `cuda:pyav:0`。 |
| Qt 离屏截图（原 `just gui`，现 `just gui-native`） | 第三方样本真实 Qt 窗口截图成功，进程退出码 0；图像、marker、帧时间、XYZ 与三维轨迹联动。 |
| 离线 wheel 构建与隔离导入 | 通过；wheel 含 15 个文件和内置 `data/UMI_v3.json`，无数据集/环境缓存；解包至 `/tmp` 后成功从该包加载配置，不读取相邻仓库。 |

单元与集成覆盖：动态 BFBS、旧两类 topic 命名、ZIP/无索引 MCAP、真实缺通道语义、合成 CDR 视频/标定/gripper/触觉和点云布局；零触觉/持续夹持/双手/搬运；B 帧呈现顺序及整数纳秒时间戳；真实渲染 AprilTag 的四种方向和 target XYZ、单/多 tag、双目消歧、重复 ID、位姿生命周期；完整时间轴缺失/速度/异常及 CSV 有效性；GUI 正常/拒绝/缺失截图。

读取 agent 另外核对 7 个真实旧样本（含全部三个 third ZIP、13/14/15/21 通道变体）；4 条样本的全部 gripper 时间戳与值逐点吻合既有导出，拿起样本的 776 个已导出触觉观测也吻合。详细本地产物：[readers-validation.json](artifacts/readers-validation.json)。真实 CDR 录制数据按约定未验收。

## 最终真实回放

以下使用修复后的浮点 `decode_sharpening=0.25`，输出目录均以 `-v2` 结尾。修复前目录保留作调查历史，不作为最终结果。默认实验阈值：覆盖率 ≥ 90%、最长缺失 ≤ 0.5 秒、最大异常帧数 0、相对速度 ≤ 1.5 m/s。全部保留完整帧时间轴，没有截段或插值。

| 样本（ID 前缀） | 主手 | 有效帧 / 总帧 | 最长缺失 | episode 结论与主要依据 |
| --- | --- | ---: | ---: | --- |
| 拿起 `b6fba8…` | 右 | 192 / 194 | 0.066667 s | rejected：119 帧重投影失败，原始坐标跳变，119–120 帧原始速度异常。 |
| 关闭 `37d6a8…` | 左 | 233 / 234 | 0.033327 s | rejected：149 帧支架几何不一致，当前异常容忍数为 0。 |
| third `714ea4…` | 左 | 161 / 262 | 2.799999 s | rejected：101 帧几何异常，有效覆盖率 61.45%。 |
| third `f0e57d…` | 左 | 188 / 271 | 2.700025 s | rejected：几何/重投影异常及原始速度异常，有效覆盖率 69.37%。 |
| third `f581f9…` | 左 | 29 / 243 | 2.166648 s | rejected：几何、深度和重投影问题，有效覆盖率 11.93%。 |
| 零触觉倒液体 `4bef60…` | 右 | 195 / 195 | 0 s | accepted：持续部分开度支持右手夹持，另一手保持打开；最大相对速度 0.438 m/s。 |
| 搬运 `e93af5…` | 单独标记 | 不生成单臂位置 / 166 | 不适用 | excluded_carry：采集任务 `A_003`，没有强行给左/右单手 action。 |

**这不是人工真值评测。** 不合格表示当前实验参数拒绝该条数据，不代表其每一帧都没有使用价值；整条筛选按用户决定执行。尤其关闭样本只出现 1 帧几何问题，是否允许这类异常应通过后续实验选择阈值，本轮没有为了提高通过率修改阈值。

本地产物：

- [拿起](artifacts/real-pickup-v2/analysis.json)、[关闭](artifacts/real-close-v2/analysis.json)。
- [third 714](artifacts/real-third-714-v2/analysis.json)、[third f0](artifacts/real-third-f0-v2/analysis.json)、[third f5](artifacts/real-third-f5-v2/analysis.json)。
- [零触觉倒液体](artifacts/real-zero-touch-v2/analysis.json)、[搬运](artifacts/real-carry-v2/analysis.json)。

每个目录均有 `frames.json`、`actions.csv` 和预览帧，`analysis.json` 保存完整输入绝对路径、标定、时间来源及参数。运行产物被 Git 忽略，新的工作区需要按实际输入路径重新生成。

## GUI 与本会话 AI 辅助复核

这里记录模型读取数值与真实 Qt 截图得到的复核意见，和确定性筛选结果分开保存；没有调用外部付费模型服务。

1. **拿起，119 帧，3.966681 秒**：[截图](artifacts/real-pickup-v2/gui-review.png)。右手 ID5/6 清楚可见，额外 ID4 候选为手/背景附近很扁的四边形。原始坐标为 `[-0.03956, -0.12466, 0.29928] m`，与上一帧相差 `0.445627 m`，原始速度 `13.3686 m/s`，重投影误差 `78.6116 px`。支持拒绝该候选，不能将它当成实际手部高速运动。GUI 在119–120处断线、保留原始诊断点；120帧几何恢复，但与错误原始点相邻的速度仍触发质量检查。
2. **第三方 714，70 帧，2.333322 秒**：[截图](artifacts/real-third-714-v2/gui-review.png)。右上第三方夹爪 ID1、原左手 ID3 和另一手 ID4/6 同时可见；ID1 与 ID3 虽属于配置的左手 ID 集合，却不符合一个支架的几何。当前帧保持无效，原始解作为诊断。GUI 在32–115和244–260帧断线，最长缺失2.8秒，支持整条拒绝。根因不是简单“两个 ID 属于左手就都采用”。
3. **零触觉倒液体，100 帧，3.333316 秒**：[截图](artifacts/real-zero-touch-v2/gui-review.png)。右手 ID6 为选中观测，另一手观测未采用。195帧 XYZ 连续，X 方向仍有局部抖动；最大相邻位移 `14.598 mm`（81→82帧），最大相对速度 `0.438 m/s`，未超过当前门控。零触觉没有被解释为未操作。

上述复核支持可定位的连续性和身份异常判断；当前没有三维真值，也不能保证仅有一个同 ID 第三方夹爪可见时的物理身份。

## 实现中发现并修复的问题

- Pupil 1.0.4.post11 wrapper 将 sharpening 转成整数，使0.25变为0；本项目显式保留浮点值，并以 native 实际参数回归验证。随后重跑全部最终样本。
- Pupil wrapper 先销毁 tag family、后销毁 detector，引起多实例 native heap corruption；本地子类按 detector→family 释放，重复构造/检测/close/GC 测试通过，没有改动依赖安装目录。
- Qt 窗口的 QObject 析构顺序曾导致进程退出崩溃；修复后截图测试和真实 GUI 退出码均为0。
- GUI 最初只显示门控后速度峰值；现明确显示原始速度峰值及当前帧的重投影误差，避免过滤后的平滑轨迹掩盖误检。
- 非有限诊断值保存为 null，保留拒绝原因；输入标定的非有限 K/D/R/P 在计算前显式拒绝，不通过序列化清洗放行。

后续仍待实验校准活动、缺失、覆盖率及异常容忍阈值；真实 CDR、绝对三维精度和全数据集泛化均未在本轮宣称通过。

## 纯 SSH 网页修复与验证

用户实际在纯 SSH 终端运行，既无 DISPLAY/Wayland 会话，也缺少 `libxcb-cursor.so.0`。此前的 Qt 离屏截图不能证明桌面 xcb 可以启动。本次将 `just gui` 改为仅监听 `127.0.0.1` 的网页服务，Qt 保留在 `just gui-native`；未安装系统桌面依赖，未改动读取、位姿或筛选算法。

| 检查 | 实际结果 |
| --- | --- |
| `just check` | Ruff 静态检查通过，21 个 Python 文件格式检查通过。 |
| `just test` | 161 项通过，包含16项Qt检查、29项网页服务检查；保留算法回归及Qt离屏截图。 |
| `just gui artifacts/real-zero-touch-v2 --port 0` | 无 DISPLAY 的会话正常启动，在随机loopback端口返回网页并打印SSH转发命令；不加载Qt，测试后已关闭。 |
| 原生 GUI 无图形会话 | 返回2并提示使用网页入口/SSH端口转发，不再触发Qt退出134。缺xcb库的提前诊断也通过回归测试。 |
| 网页读取与路径 | 原始JSON字节和整数纳秒不改写；只开放声明的预览、本地脚本和接口。越界符号链接、未声明文件、路径穿越及无效输入测试通过。 |
| Firefox 157.0.1 实际浏览器 | 下表3组样本通过帧切换、原始帧号定位、图像实际加载、XYZ断线、无效帧跳转与回绕；三维Canvas真实像素和鼠标旋转通过检查。 |
| 离线wheel | `artifacts/ssh-gui-package/umi_marker_screening-0.1.0-py3-none-any.whl` 共17个文件，含最终HTML、web.py、marker-web入口与参考配置；从wheel独立导入并读取资源通过，无环境缓存或数据集。旧 `artifacts/package/` 保留为历史包。 |

无界面Firefox初次运行发现WebGL不可用，已补充自动Canvas三维回退，支持旋转、缩放、观测点选、当前帧标记，三个坐标轴使用相同尺度。以下实际截图均使用Canvas路径；Plotly仍用于XYZ时间曲线。页面的有效性、断线及原始拒绝位置与已有产物一致，不通过修改或插值原数据得到连续轨迹。

| 样本与选中帧 | 实际核对 | 新增截图 |
| --- | --- | --- |
| 零触觉，100帧 | 195/195有效，右手ID6；三维轨迹及XYZ曲线连续，最大原始相对速度0.438m/s。 | [网页截图](artifacts/real-zero-touch-v2/web-review.png) |
| 拿起，119帧 | 192/194有效；119–120断线，当前原始错误位置与13.369m/s峰值可见，图像保留未采用marker。 | [网页截图](artifacts/real-pickup-v2/web-review.png) |
| 第三方714，70帧 | 161/262有效；101帧拒绝，32–115及244–260区间断线并保留原始诊断点，图像可见ID1/3/4/6。 | [网页截图](artifacts/real-third-714-v2/web-review.png) |

浏览器检查入口为 `just test-web <结果目录> <截图路径> --frame <原始帧号>`，使用现有Firefox/geckodriver与可选Selenium依赖。服务和浏览器在本机验收；用户自己的SSH连接与本地浏览器按 [USAGE.md](USAGE.md) 建立端口转发。
