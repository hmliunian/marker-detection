# UMI 主手与三维 action 筛选

首轮独立实现：输入 MCAP 或包含一个 MCAP 的 ZIP，输出主控手、逐帧 XYZ、整条 episode 的筛选结论和本地 GUI。首次使用从 [README.md](README.md) 开始，算法依据见 [ALGORITHM.md](ALGORITHM.md)，开发约束见 [PROJECT_BOUNDARIES.md](PROJECT_BOUNDARIES.md)。

## 运行

在本目录使用 Python 3.12/3.13、uv 和 just。本工作区已建立独立 `.venv`，`just` 优先使用项目 `.tools/bin/uv`；其他机器可使用 PATH 中的 uv。依赖由 `uv.lock` 固定。

```bash
just setup
just inspect /path/to/episode.mcap
just analyze /path/to/episode.mcap artifacts/my-run --video-backend cuda
just gui artifacts/my-run
```

`just gui` 启动只监听 `127.0.0.1:8765` 的网页查看器，适用于纯 SSH 服务器，不需要 X11、Wayland 或 xcb。3D 绘图脚本和图像由本机提供，不访问 CDN。浏览器不支持 WebGL 时自动使用 Canvas 三维投影，仍可拖动旋转、滚轮缩放和点击观测定位帧。可用 `--port 8766` 更换端口。

在你的电脑另开终端建立端口转发（将 `xuran@ps` 换成你实际使用的 SSH 目标）：

```bash
ssh -N -L 8765:127.0.0.1:8765 xuran@ps
```

保持服务器上的 `just gui` 和端口转发运行，在你的电脑浏览器打开 **http://127.0.0.1:8765**。如果通过 VS Code Remote SSH 连接，也可以在“端口”面板转发8765后打开本机地址。用 Ctrl+C 关闭服务器。

真实第三方数据可直接读取 ZIP，无需手动解压：

```bash
just analyze ../dataset/third_marker/714ea4a2e2c56fdaf8aa1c5ab69fea70.zip \
  artifacts/third-example --video-backend cuda
just gui artifacts/third-example
```

分析输出目录必须不存在或为空，避免覆盖已有实验。网页 GUI 只读已有结果，不重新计算或修改原数据。

原 Qt 桌面界面保留在 `just gui-native <output>`。它需要图形桌面或 SSH X11 转发；在Ubuntu/Debian缺少xcb鼠标库时可安装 `sudo apt-get install libxcb-cursor0`。uv负责Python依赖，不安装系统X11库。纯SSH应使用上面的网页入口；安装xcb本身不会创建图形显示会话。

需要生成 Qt 离屏截图时：

```bash
QT_QPA_PLATFORM=offscreen just gui-native artifacts/third-example \
  --frame 70 --screenshot artifacts/third-example/gui.png
```

Qt的 `--frame` 是原始零起始帧号。两个GUI均可按帧或时间定位，联动图像和marker、三维轨迹及XYZ时间曲线；缺失和质量拒绝处断线，原始候选位置作为诊断显示。网页只用 `time_s` 做时间显示/导航，原始纳秒时间戳始终以分析文件为准。

## 输出契约

| 文件 | 内容 |
| --- | --- |
| `analysis.json` | 主控手、episode 结论、原因、活动证据、质量指标、全部参数、标定和后端来源；最后写入，表示分析完成。 |
| `frames.json` | 完整左目视频帧时间轴；原始纳秒时间戳、有效/原始 XYZ、marker 角点、重投影误差、相邻位移/速度/加速度、缺失与异常原因。 |
| `actions.csv` | 可直接读取的表：帧号、时间、主手、`x_m/y_m/z_m`、`frame_valid`、`episode_accepted`、原因。无效帧的 XYZ 为空。 |
| `frames/*.jpg` | 从原视频生成的压缩预览，供 GUI 定位；不改原数据。 |

action 是 `camera_T_rig @ rig_T_target` 的平移，单位米，表达在**头部左相机光学坐标系**，不是世界坐标或帧间增量。沿用参考尺寸 `0.0525 m`、安装几何和 target 点；原始标定 frame_id 另保存在报告中。尺度精度取决于配置是否符合真实硬件。

`primary_hand` 为 `left`、`right` 或 `null`。整条 episode 的 `decision` 包含：

- `accepted`：主手明确，完整时间轴通过当前实验参数。
- `rejected`：非搬运双手参与，或覆盖率、缺失、异常超阈值。有效片段仍保留供检查，整条不进入合格数据。
- `review`：主手证据不足或有效观测不足，不能强行指定另一只手。
- `excluded_carry`：任务 `A_003` 单独标记，不生成单臂 action。

程序退出码 `0` 表示分析完成，**不表示 episode 合格**。处理或输入错误退出 `2`，通过 stderr 报告；被剔除的数据不删除。

使用 `frames.json` 时，有效 action 必须同时满足 `frame.valid` 和 `frame.quality.valid`。进入合格数据还必须满足 `analysis.decision == "accepted"`。`raw_xyz_m` 用于诊断，可能对应被拒绝的跳点；不能作为有效 action。`actions.csv` 已合并帧级有效性，并单列 episode 是否合格。

## 参数与 GPU

```bash
just analyze /path/to/episode.mcap artifacts/experiment \
  --config configs/screening.example.json --video-backend auto --gpu 0
```

复制 [screening.example.json](configs/screening.example.json) 调整 `activity`、`quality`、`pose` 三部分；未列字段沿用默认值。`--settings` 与 `--config` 等价，旧命令仍可使用。完整生效参数随每次结果保存。另可用 `--geometry /path/to/UMI.json` 指定几何，`--sidecar /path/to/collection.json` 指定旧格式采集 JSON。

当前示例文件的 `quality.min_coverage` 为 `0.85`；程序未指定该字段时的默认值、下文 90% 示例及 2026-10-13 历史实验均为 `0.9`。仅修改示例文件不会影响未传 `--config` 的命令。

仅配置缺失与覆盖率时，JSON 可以只包含以下内容：

```json
{
  "quality": {
    "max_missing_frame_ratio": 0.1,
    "min_coverage": 0.9,
    "max_gap_s": 0.5
  }
}
```

`max_missing_frame_ratio` 是最大缺失帧比例，`0.1` 表示最多 10%，范围为 `[0, 1]`；省略或设为 `null` 不额外限制。这里的缺失帧包含没有观测和未通过质量检查的帧，计算为 `(总帧数 - 有效帧数) / 总帧数`。它与帧覆盖率互补；两项同时配置时须同时满足，以更严格的限制为准。`min_coverage` 还检查按时长加权的覆盖率，`max_gap_s` 单独限制最长连续无效时长（秒），等于阈值允许通过。

上述配置须显式传给 `--config` 才会加载；修改后使用新输出目录重新分析。结果的 `summary.missing_frames`、`summary.missing_frame_ratio`、`coverage` 和 `time_coverage` 可用于核对实际指标，`analysis.json` 的 `config.quality` 保存生效配置。空轨迹的缺失比例为 `null`，不会因此成为合格数据。

| 参数 | 程序默认值与含义（指定配置后以配置为准） |
| --- | --- |
| position 归一化参考 | 旧格式原始开度 `102`；CDR 米制开度 `0.102`，后者基于文档中的转换比例。旧原始值不擅自标成毫米。 |
| 开度变化 | 稳健范围达到参考开度的 `8%`；持续部分开度也作为可能夹持的证据。 |
| 触觉 | 单指 25 点力向量模长和达到 `0.5` 原始单位，持续 `0.1 s`；零触觉不证明闲置。 |
| 最长连续缺失 | `0.5 s`，包含首尾缺失，末帧按正时间间隔中位数估计持续时间。 |
| 最低覆盖率 | `90%`，帧数比例和时长比例都检查。 |
| 最大缺失帧比例 | `null`，默认不额外限制；可通过 `quality.max_missing_frame_ratio` 配置，例如 `0.1` 表示 10%。 |
| 最大异常帧 | `0`；保留原始候选中的跳变检查，过滤掉跳点不会自动使 episode 通过。 |
| 相机相对运动门控 | `1.5 m/s`、`360°/s`，沿用参考起点。 |
| 重投影/双目同步 | `1 px` / `5 ms`，沿用参考配置。 |

这些是实验起点，尚未通过标注留出集校准。首次观察到“被剔除”可在 GUI 中定位原因；不能只为提高通过率放宽参数。加速度默认只作诊断，可显式配置 `quality.max_acceleration_m_s2`。不进行平滑或插值，不跨缺失区间绘制实测连线。

CUDA 用于 H.264/HEVC **视频解码**，AprilTag/PnP/双目优化在 CPU 上执行。`auto` 尝试 CUDA，失败时记录原因并回退 CPU；`cuda` 严格要求硬件解码，失败就报错；`cpu` 可用于无显卡环境。实际后端保存在 `provenance.video_backends`，不是根据硬件型号猜测。默认单进程使用第 0 张卡。

## 实现与参考的关系

- AprilTag 角点按参考顺序处理；单 tag 使用 IPPE，歧义时用同步右目消歧，多 tag 使用 SQPnP + LM。
- 双目残差沿用水平基线模型。优化器采用 SciPy 的旋转向量 LM，参考为 Ceres 四元数优化；不声称逐位相同。只有标定与零畸变、同内参的水平双目模型兼容时，才使用该分支；否则歧义帧保持缺失。
- 直接使用解码 Y 平面和记录的 K/D；保存 R/P，不再次应用 R，避免对现有样本重复校正。仅标定矩阵一致不能证明实际图像已经正确校正，重投影与 GUI 仍需核对。
- 同一主手 ID 同时出现多处会标记身份歧义；不按最高解码分数强选。参考算法剔除不一致 tag 后的候选保留作诊断，该帧记为几何异常。另一只手和第三方不能填补主手缺失。
- 同 ID 的第三方只单独可见且轨迹也连续时，单靠这些观测无法保证识别其物理身份；当前没有外部逐帧身份或位置真值。
- 本地封装修复了当前 Pupil wrapper 的 detector/family 析构顺序，以及浮点 sharpening 被转成整数的问题；没有修改依赖安装文件。

## 检查

```bash
just check
just test
```

有Firefox和geckodriver的开发环境可额外运行实际浏览器验收（只给该命令安装可选Selenium依赖，不自动下载浏览器）：

```bash
just test-web artifacts/real-zero-touch-v2 artifacts/real-zero-touch-v2/web-review.png --frame 100
```

测试包含旧 BFBS 两类命名、ZIP、文档中的 ROS2 CDR、缺失通道、主手判定、真实编码 B 帧时间戳、合成投影/真实渲染 tag 几何、异常保留、整条筛选及 Qt 离屏截图。真实 CDR 回放按约定延后；合成契约通过不能替代真实数据验收。首轮实际回放与 GUI/AI 复核证据见 [VALIDATION.md](VALIDATION.md)。
