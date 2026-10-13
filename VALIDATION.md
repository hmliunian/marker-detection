# 验证与实验记录

日期：2026-10-13。对应 `umi-marker-screening 0.1.0`、本目录 `uv.lock` 和参考 `UMI_v3.json`。本文件保留工程验证、早期 7 条真实回放，以及后续 100+3 条筛选实验；各阶段的配置和验证范围分别说明。这些记录不等同于数据集准确率评测。

## 100 条主数据与 3 条第三方样本实验

本节从已保存的 `artifacts/screening-103-20261013/` 整理。证据为 `experiment.json`、`settings.json`、`results.json`、`summary.json`、`audit.json`、`sensitivity.json` / `.csv` 和逐条日志；本次文档整理没有重跑或改写该实验。

运行时间为 2026-10-13 20:25–20:27（UTC+8），记录的 HEAD 为 `2bc4fbead5452b18359050b8c6a6fe1ef2c5fc03`，具体执行源码以 `experiment.json` 的 `source_sha256` 为准。使用 4 张 RTX 5090，每卡一个子进程进行 CUDA 视频解码，CPU 执行检测和求解；总耗时 163.682 秒。这是本机单次批处理记录，包含一条处理失败，不能作为其他硬件的性能保证。

冻结阈值：帧和时长覆盖率均 ≥90%，最长连续无效 ≤0.5 秒，最大异常帧数 0，最少有效帧数 3，相机相对速度上限 1.5 m/s，重投影误差上限 1 px；marker 尺寸为 0.0525 m。该实验早于 `max_missing_frame_ratio` 配置项，没有单独启用缺失帧比例上限。当前示例 JSON 的覆盖率为 85%，不应将它代入解释这份历史结果。

| 数据组 | 总数 | accepted | rejected | excluded_carry | review | process_error | 通过率 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 主数据 | 100 | 23 | 67 | 7 | 2 | 1 | 23 / 100 = 23.00% |
| 第三方 marker 介入 | 3 | 0 | 3 | 0 | 0 | 0 | 0 / 3 = 0.00% |

主数据去掉 7 条搬运后，分母为 93（仍含 2 条待审核和 1 条处理失败），通过率为 23 / 93 = 24.73%。搬运单列来自当前单臂 action 任务范围，不计为质量拒绝。

主数据 67 条拒绝按互斥原因组合拆分如下：

| 拒绝原因组合 | 条数 |
| --- | ---: |
| 双手都有操作证据 | 22 |
| 仅异常帧超限 | 26 |
| 仅覆盖率不足 | 1 |
| 异常帧超限 + 覆盖率不足 | 4 |
| 覆盖率不足 + 连续缺失超限 | 5 |
| 异常帧超限 + 覆盖率不足 + 连续缺失超限 | 9 |
| 合计 | 67 |

因此，按单个原因统计时会重叠：异常帧超限 39 条、覆盖不足 19 条、连续缺失超限 14 条。26 条“仅异常帧超限”中有 8 条各含 1 帧异常、5 条各含 2 帧异常；即使覆盖率达标，默认的零异常容忍也会使它们被拒绝。

处理失败样本为 `47de444c68fd40cde548f9cd55d3c28e`（翻转）。视频存在，但检查结果没有 `head_left` / `head_right` 标定；CLI 返回 `Head-left video and its calibration are required to compute camera-relative XYZ`，退出码 2。它单独计为 `process_error`，批次状态为 `needs_review`。

已保存的审计记录确认：103 个唯一输入全部尝试，102 条完成分析；核对了 26,177 帧及对应 CSV 行，预览存在，逐帧有效性及 XYZ 一致；输入哈希、运行期间源码、参数与几何一致，实际 CUDA 后端已核实。7 条早期参考回放在主手、筛选结论、原因、帧数和质量指标上保持一致。这里引用的是当次审计记录，不表示当前源码仍与历史冻结源码相同。

### 阈值敏感性分析

重评估使用已保存的完整 `frames.json` 副本，只改变整条 episode 的异常容忍数、覆盖率和最长缺失阈值。主控手、位姿、速度阈值与每帧有效掩码保持不变，原有轨迹及筛选产物没有被覆盖。

| 最大异常帧数 | 最低覆盖率 | 最长缺失 / s | 主数据通过数 | 第三方通过数 |
| ---: | ---: | ---: | ---: | ---: |
| 0 | 0.90 | 0.5 | 23 | 0 |
| 1 | 0.90 | 0.5 | 31 | 0 |
| 2 | 0.90 | 0.5 | 36 | 0 |
| 3 | 0.90 | 0.5 | 40 | 0 |
| 5 | 0.90 | 0.5 | 45 | 0 |
| 10 | 0.90 | 0.5 | 49 | 0 |
| 0 | 0.85 | 0.5 | 23 | 0 |
| 0 | 0.80 | 0.5 | 24 | 0 |
| 0 | 0.90 | 1.0 | 23 | 0 |
| 0 | 0.85 | 1.0 | 23 | 0 |
| 0 | 0.80 | 1.0 | 24 | 0 |
| 2 | 0.85 | 1.0 | 36 | 0 |
| 3 | 0.85 | 1.0 | 40 | 0 |
| 5 | 0.80 | 1.0 | 49 | 0 |

通过数量增加仅表示筛选条件变宽，不能据此声称定位精度或身份识别能力提高。第三方样本仅 3 条，当前也没有人工逐帧身份和三维位置真值；阈值仍需结合诊断轨迹与实际使用要求选择。

### 实验产物

以下均为本机 `artifacts/screening-103-20261013/` 中已生成的文件；`artifacts/` 被 Git 忽略，新克隆不会带上这些结果。

| 路径 | 内容 |
| --- | --- |
| `report.html`、`overview.png` / `.svg` | 可离线打开的报告和汇总图。 |
| `episodes.csv`、`main-100.csv`、`third-3.csv`、`accepted.csv` | 全部明细、分组明细和通过列表。 |
| `summary.json`、`episode_summary.json`、`audit.json` | 统计、逐条摘要和一致性核对。 |
| `experiment.json`、`settings.json`、`geometry.json`、`manifest.json` | 运行参数、输入清单、源码与输入哈希。 |
| `results.json`、`progress.jsonl`、`logs/` | 逐条返回状态、运行过程和错误日志。 |
| `sensitivity.json`、`sensitivity.csv` | 上述阈值重评估结果。 |
| `main/<episode_id>/`、`third/<episode_id>/` | 成功分析的逐帧结果，可传给 `just gui`。 |

### 实验脚本与复现前提

以下脚本是这批 100+3 数据的专用工具。普通单条录制及自定义配置使用 README 中的 `just analyze`。

批处理要求输入清单恰有 100 条 `samples`，每条 `source_directory` 指向包含 `episode.mcap` 和对应 `<id>.json` 的原始目录；第三方目录恰有 3 个 ZIP。清单和原始数据需要另行准备。安装项目依赖后，可以使用新结果目录启动新的一轮：

```bash
.venv/bin/python scripts/run_screening_experiment.py \
  --manifest /path/to/manifest.json \
  --third /path/to/third_marker \
  --output artifacts/screening-new \
  --gpus 0
```

`--gpus 0` 表示一个进程使用 GPU 0；有 4 张可用卡时可设为 `0,1,2,3`。脚本严格使用 CUDA 解码，且要求输出目录尚不存在。它使用程序默认参数，不读取示例配置，也没有 `--config` 参数。

两个后处理脚本的调用方式如下，执行前必须满足各自的冻结基线要求：

```bash
.venv/bin/python scripts/summarize_screening_experiment.py artifacts/screening-new
.venv/bin/python scripts/screening_sensitivity.py artifacts/screening-new
```

- 汇总脚本要求 103 条结果、与 `experiment.json` 一致的源码和输入哈希，以及早期 7 条 `artifacts/real-*-v2/` 参考产物；内部验收仍绑定 90% 覆盖率、0.5 秒缺失和零异常的基线。当前源码已加入配置功能，直接拿它重新汇总旧批次会触发源码哈希不一致检查。
- 敏感性脚本要求该固定批次的 26 条仅异常拒绝、19 条覆盖不足等基线事实，并校验原始判定及逐帧有效性；它不适合作为任意数据集的通用调参命令。上表来自已保存的敏感性结果，不从后来增补的脚本场景推测未执行结果。
- 脚本会在指定实验目录写入或更新汇总和敏感性报告。保存历史证据时应保留原实验目录，并在匹配源码和产物条件下使用独立副本处理；这些后处理命令不属于新克隆的无数据快速开始步骤。

## 配置功能的工程回归

配置功能提交 `9d6d328` 的验证记录：`just check` 通过，`just test` 共 202 项通过；其中质量与 CLI 相关测试 88 项通过。覆盖配置读取和保存、缺失比例边界、非法参数、CSV 有效性与旧 `--settings` 兼容。详细开发记录见 [.agent/tasks/2026-10-13-marker-screening-boundaries.md](.agent/tasks/2026-10-13-marker-screening-boundaries.md)。本次 README 整理仅进行文档、命令及已保存统计的核对，未重新执行该算法回归或真实实验。

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
