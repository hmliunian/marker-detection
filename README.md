# UMI Marker 数据筛选

从 UMI 录制数据中找出参与操作的手，计算夹爪的三维位置，并判断整段操作数据是否满足筛选条件。

输入是一个 **MCAP 文件**或**包含一个 MCAP 的 ZIP**。程序结合夹爪开度、触觉和 AprilTag 图像观测，输出逐帧位置、筛选原因，以及可以在浏览器中查看的轨迹。本文中的 **episode** 指一整段操作录制，**主控手**指这段操作中使用的左手或右手。

```text
MCAP / ZIP → 判断主控手 → 检测 marker、计算三维位置 → 检查缺失和异常 → 导出结果、查看轨迹
```

筛选以整条 episode 为单位。非搬运任务中双手都参与时会被拒绝；搬运任务单独标记。无效帧保留在诊断结果中，原数据不会被删除，也不通过插值或截取片段使整条数据通过。

## 快速开始

需要 **Python 3.12 或 3.13、uv、just**。在项目根目录运行以下命令，`just setup` 会按 `uv.lock` 创建独立的 `.venv` 并安装依赖；机器已有 `.tools/bin/uv` 时会优先使用，否则使用 PATH 中的 `uv`。

```bash
# 1. 安装项目依赖
just setup

# 2. 检查输入的通道、标定和编码格式
just inspect /path/to/episode.mcap

# 3. 分析一段录制，写入新的结果目录
just analyze /path/to/episode.mcap artifacts/demo \
  --config configs/screening.example.json --video-backend auto

# 4. 打开结果查看器
just gui artifacts/demo
```

将 `/path/to/episode.mcap` 替换为自己的 MCAP 或 ZIP 路径。**输入数据不随仓库提供；结果目录必须不存在或为空。** 再跑一次时换一个输出目录，例如 `artifacts/demo-v2`。上面的分析命令显式加载示例配置，其最低覆盖率目前为 **85%**；不传 `--config` 时，程序默认值为 **90%**。

输入至少需要头部左相机的视频和标定。右目视频及匹配标定用于双目消歧；旧格式还需能找到对应的采集 JSON。自动定位不到采集信息时，可在 `just analyze` 后追加 `--sidecar /path/to/collection.json`。支持的通道和格式见 [输入格式说明](UMI_INPUT_FORMAT.md)；旧 FlatBuffer 已有真实数据验证，新 ROS2 CDR 当前完成了离线契约测试，真实录制验收仍待补充。

### 在浏览器中查看

本机运行时，执行 `just gui artifacts/demo` 后打开 **http://127.0.0.1:8765**。

如果程序运行在 SSH 服务器上，保持服务器的 `just gui` 运行，在自己的电脑另开终端：

```bash
ssh -N -L 8765:127.0.0.1:8765 user@server
```

把 `user@server` 换成实际 SSH 地址，再在电脑浏览器打开 **http://127.0.0.1:8765**。使用 VS Code Remote SSH 时，也可以直接转发端口 8765。

查看器支持按帧或时间定位，同时查看原始图像、marker、三维轨迹和 X/Y/Z 曲线。缺失位置会断线，原始异常候选可用于排查原因。它读取已有分析结果；修改配置后，需要重新分析并打开新的结果目录。端口被占用时用 `just gui artifacts/demo --port 8766`，并相应修改 SSH 转发端口。按 Ctrl+C 关闭服务。

## 常用 just 命令

以下命令均在项目根目录执行。`INPUT` 表示 MCAP/ZIP 路径，`OUTPUT` 表示结果目录，实际使用时替换为自己的路径。

| 命令 | 用途 |
| --- | --- |
| `just --list` | 列出所有可用命令。 |
| `just setup` | 按锁文件安装依赖。 |
| `just inspect INPUT` | 只读检查输入格式、通道、标定和缺失信息。 |
| `just analyze INPUT OUTPUT` | 分析完整 episode，导出结果；使用程序默认参数。 |
| `just analyze INPUT OUTPUT --config configs/screening.example.json` | 使用指定 JSON 中的参数分析。 |
| `just gui OUTPUT` | 启动本地网页查看器，默认端口 8765。 |
| `just gui-native OUTPUT` | 启动 Qt 桌面查看器，需要图形桌面或 X11 转发。 |
| `just check` | 检查 Python 代码风格和格式。 |
| `just test` | 运行离线测试，Qt 使用离屏模式。 |
| `just test-web OUTPUT artifacts/web-review.png --frame 100` | 用浏览器验证交互并截图；需要 Firefox、geckodriver，会按锁文件安装可选 Selenium 依赖。 |

`just analyze` 还支持以下参数：

| 参数 | 含义 |
| --- | --- |
| `--config FILE` | 读取 `activity`、`quality`、`pose` 配置；兼容旧名称 `--settings`。 |
| `--sidecar FILE` | 显式指定旧格式采集 JSON。 |
| `--geometry FILE` | 指定 marker 几何配置，默认使用项目自带的 `UMI_v3.json`。 |
| `--video-backend auto` | 默认后端；尝试 CUDA 解码，失败后记录原因并回退 CPU。 |
| `--video-backend cpu` | 使用 CPU 解码，可用于无显卡环境。 |
| `--video-backend cuda --gpu 0` | 严格使用指定显卡解码，失败时报错；显卡编号从 0 开始。 |

GPU 加速的阶段是视频解码。AprilTag 检测、PnP 位姿求解和双目优化在 CPU 上执行。

## 修改筛选配置

编辑 [configs/screening.example.json](configs/screening.example.json)，或复制一份作为自己的配置。JSON 按用途分为三部分：`activity` 判断哪只手在操作，`pose` 控制逐帧位姿求解，`quality` 控制轨迹质量与整条 episode 的筛选。

只调整质量阈值时，可以使用这样的 JSON：

```json
{
  "quality": {
    "min_coverage": 0.85,
    "max_missing_frame_ratio": null,
    "max_gap_s": 0.5,
    "max_anomalous_frames": 0,
    "min_valid_frames": 3
  }
}
```

**配置文件只有通过 `--config` 指定才会加载；未填写的字段沿用程序默认值。** 每次实际生效的完整参数都保存在结果的 `analysis.json` 中。

| 配置项 | 控制什么 | 程序默认值 | 当前示例文件 |
| --- | --- | ---: | ---: |
| `min_coverage` | 有效帧占比和有效时长占比的下限，两项都要达到。 | `0.9`（90%） | `0.85`（85%） |
| `max_missing_frame_ratio` | 缺失帧占总帧数的上限。`0.1` 表示最多 10%，`null` 关闭这一项额外限制。 | `null` | `null` |
| `max_gap_s` | 最长一次连续无效的时长上限，单位秒，包含开头和结尾的缺失。 | `0.5` | `0.5` |
| `max_anomalous_frames` | 整条 episode 允许的异常帧数量。异常可来自几何不一致、身份歧义或位置跳变等。 | `0` | `0` |
| `min_valid_frames` | 判断轨迹至少需要的有效帧数量；不足且未触发拒绝条件时标为待审核。 | `3` | `3` |

这里的“缺失帧”包含未得到位姿和未通过质量检查的帧，按 `(总帧数 - 有效帧数) / 总帧数` 计算。它与帧覆盖率互补，例如缺失 ≤10% 对应帧覆盖率 ≥90%；覆盖率还会检查有效时长，`max_gap_s` 则检查缺失是否集中在一段时间内。

所有启用的条件都要满足。例如，覆盖率已经达标，但有 1 帧异常且 `max_anomalous_frames=0`，整条 episode 仍会被拒绝。放宽 episode 阈值不会把无效帧改成有效位姿。更多参数与时间计算约定见 [USAGE.md](USAGE.md#参数与-gpu)。

## 怎么读结果

每次分析会在输出目录生成：

| 文件 | 用途 |
| --- | --- |
| `analysis.json` | 整条结论、主控手、拒绝原因、覆盖率、最长缺失、配置和数据来源。 |
| `actions.csv` | 逐帧三维位置，含 `frame_valid` 和 `episode_accepted`，方便后续筛选使用。 |
| `frames.json` | 完整帧时间轴、原始/有效位姿、marker 观测及质量诊断。 |
| `frames/*.jpg` | 查看器使用的图像预览。 |

三维 action 为 **头部左相机光学坐标系中的 target 位置 `(x, y, z)`，单位米**。默认采用参考 marker 边长 `0.0525 m` 和安装几何；头部相机运动会反映在这条相对轨迹中。

`analysis.json` 的 `decision` 有四种取值：

| 结论 | 含义 |
| --- | --- |
| `accepted` | 主控手明确，整段数据通过当前筛选条件。 |
| `rejected` | 双手参与或质量超限，整段数据不进入合格集合。 |
| `review` | 主控手证据或有效帧不足，需要人工复核。 |
| `excluded_carry` | 搬运任务 `A_003` 单独标记，当前不输出单臂 action。 |

使用 CSV 时，同时要求 `episode_accepted=1` 和 `frame_valid=1`；无效帧的 XYZ 留空。直接读 JSON 时，有效帧须同时满足 `frame.valid` 和 `frame.quality.valid`，并检查整条结论。`raw_xyz_m` 保存诊断候选，可能包含被拒绝的跳点。

退出码 `0` 表示程序完成分析，是否通过要看 `decision`。输入或处理错误返回 `2`；批处理将这类情况单独记录为 `process_error`。

## 实验结果

2026-10-13 对 **100 条主数据 + 3 条第三方 marker 介入数据**进行了完整筛选。历史实验参数为：**覆盖率 ≥90%、连续缺失 ≤0.5 秒、异常帧容忍数 0、相对速度上限 1.5 m/s**。它与当前示例文件中的 85% 覆盖率不同。

| 数据组 | 总数 | 通过 | 拒绝 | 搬运单列 | 待审核 | 处理失败 | 通过率 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 主数据 | 100 | 23 | 67 | 7 | 2 | 1 | 23.00% |
| 第三方介入 | 3 | 0 | 3 | 0 | 0 | 0 | 0.00% |

主数据剔除 7 条搬运后，通过率为 **23 / 93 = 24.73%**，分母仍包含待审核和处理失败的数据。主数据的 67 条拒绝可分为：22 条双手参与、26 条仅异常帧超限、19 条包含覆盖不足（其中部分同时有异常或连续缺失超限）。1 条处理失败来自缺少头部相机标定，单独计数。

共 102 条成功生成分析结果，核对了 26,177 帧及对应 CSV 行；输入哈希、参数、预览和有效性一致性检查通过，7 条早期参考样本的结果一致。完整记录及复现前提见 [VALIDATION.md](VALIDATION.md#100-条主数据与-3-条第三方样本实验)。

### 放宽阈值会怎样

下面是对已保存轨迹的阈值重评估，保持主控手、位姿和逐帧有效性不变：

| 异常帧容忍数 | 最低覆盖率 | 最长连续缺失 | 主数据通过数 / 100 | 第三方通过数 / 3 |
| ---: | ---: | ---: | ---: | ---: |
| 0 | 90% | 0.5 s | 23 | 0 |
| 0 | 85% | 0.5 s | 23 | 0 |
| 2 | 90% | 0.5 s | 36 | 0 |
| 5 | 80% | 1.0 s | 49 | 0 |

单独将覆盖率改成 85%，这批数据的通过数仍为 23。更多数据通过代表筛选条件变宽；位置估计本身没有改善，也没有把被拒绝的帧补齐。当前没有逐帧三维真值，这些数字表示本批样本在指定阈值下的通过情况；3 条第三方样本不足以证明所有身份干扰都能被识别。

本机实验产物保存在 `artifacts/screening-103-20261013/`：`report.html` 可离线查看，`episodes.csv` 包含全部明细，`accepted.csv` 列出通过样本，`summary.json`、`audit.json` 和 `sensitivity.csv` 保存汇总与核对结果。**`artifacts/` 被 Git 忽略，新克隆的仓库不包含这些产物。**

## 批处理与进一步阅读

单条分析和调参使用上面的 `just analyze`。已有的 [批处理脚本](scripts/run_screening_experiment.py) 专用于 100 条主数据和 3 个第三方 ZIP，需要原始 MCAP、采集 JSON、输入清单及 CUDA 解码环境；它使用程序默认参数，不读取 `screening.example.json`。实验命令、汇总脚本的源码哈希与历史产物要求见 [验证与实验记录](VALIDATION.md#实验脚本与复现前提)。

| 位置 | 内容 |
| --- | --- |
| [USAGE.md](USAGE.md) | 详细参数、GPU 后端、桌面界面、截图与输出契约。 |
| [UMI_INPUT_FORMAT.md](UMI_INPUT_FORMAT.md) | 新旧 MCAP 格式、通道及数据来源。 |
| [ALGORITHM.md](ALGORITHM.md) | 原 README 中的 TinyNav 参考算法与源码索引。 |
| [VALIDATION.md](VALIDATION.md) | 工程测试、真实回放、实验结果及证据限制。 |
| [PROJECT_BOUNDARIES.md](PROJECT_BOUNDARIES.md) | 项目目标、数据语义和开发边界。 |
| [src/marker_detection/](src/marker_detection/) | 读取、主控手判定、位姿、质量评估及 CLI/GUI 实现。 |
| [tests/](tests/) | 离线测试和浏览器验收入口。 |

修改代码后运行 `just check` 和 `just test`。最近一次配置功能验证通过了 202 项离线测试；此前浏览器与真实数据验证的具体范围见 VALIDATION.md。
