# UMI 主要输入格式与新旧数据统一建议

核实日期：2026-10-13。本文记录当前数据事实和后续接入建议；尚未实现统一读取接口，也未执行整批格式转换或新旧格式结果对照。

## 1. 项目主要输入与当前格式

**本项目以 UMI episode 数据为主要输入。当前 100 条样本来源于旧版 UMI 数据：FlatBuffer MCAP + 配套 JSON。**

`dataset` 中的 `head.mp4`、夹爪 JSON、逐帧 CSV 和触觉分析结果，是从原始 episode 提取的分析数据，尚未转换为新规范的 ROS2 CDR MCAP。它们保留了部分传感器信息，完整多传感器数据仍需读取原始 MCAP。

项目与外部来源的位置如下：

| 对象 | 路径 | 用途 |
| --- | --- | --- |
| 本项目 | `/home/xuran/projects/umi_marker/marker_detection` | 项目文档及后续实现；`.agent` 任务记录也放在此目录内 |
| 转换参考仓库 | `/home/xuran/projects/umi_marker/mcap-convert` | 新旧格式映射与转换规则的参考来源 |
| 当前样本集 | `/home/xuran/projects/umi_marker/dataset` | 100 条导出视频、元数据及状态分析结果 |
| 原始 MCAP | `manifest.json` 的 `samples[].source_directory` 下的 `episode.mcap` | 全量视频、夹爪、IMU、触觉等原始消息 |

当前样本来自 `dataset_h200_150h` 的 `project_id=224`（UMI-marker），从 56,995 条候选中按种子 `20261013` 抽取 100 条，覆盖 16 个动作类别。

来源：[数据集说明](../dataset/README.txt)、[样本清单](../dataset/manifest.json)、[已有夹爪分析](gripper_state_analysis.txt)。

## 2. 设备类型、消息格式与导出形式

`mcap-convert` 所说的两类转换是 **UMI** 与 **小推车（XiaoTuiChe）**，分别维护在 `umi` 和 `push_cart` 分支。设备类型与数据新旧版本需要分别识别。

UMI 的新旧格式变化为：旧 FlatBuffer 消息转换为 ROS2 CDR 消息，外层容器仍是 MCAP。仅凭 `.mcap` 扩展名或 topic 名称，不能判断载荷编码。

| 比较项 | 旧版 UMI | 新规范 UMI |
| --- | --- | --- |
| 载荷与 schema | FlatBuffer 消息，内嵌 BFBS schema | CDR 消息，`ros2msg` schema |
| 视频消息 | `foxglove.CompressedVideo` | `foxglove_msgs/msg/CompressedVideo` |
| 夹爪消息 | `foxglove.JointStates`，当前每条一个 `j0.position` | `sensor_msgs/msg/JointState`，规范夹爪名称 |
| 相机标定 | 原 MCAP 的 `camera_info` Metadata；当前配套采集 JSON 也保存标定副本 | `sensor_msgs/msg/CameraInfo` topic |
| 视频处理 | 当前导出视频保留原 H.264 码流，尺寸为 `640×352` | 转换器补边至 `640×480`，重新编码 H.264，并同步修改标定 |
| 单位 | 当前分析尚未确认 position 与触觉的物理单位 | 转换器默认按夹爪 mm → m、触觉 xyz mm → m 处理，使用前必须确认源单位 |
| TF | 取决于源数据；当前夹爪状态不提供 TCP 动态轨迹 | 转换器依据配置生成局部静态 TF，不凭空生成动态位姿 |

上表的新格式描述依据转换指南，不能据此认定当前样本已经完成这些处理。UMI 分支的 `device_type` 参数只设置 Metadata 标签，不能用它选择消息编码或切换到小推车转换流程。

来源：[转换仓库说明](../mcap-convert/README.md)、[UMI 转换指南](../mcap-convert/guide.md)。

## 3. 当前数据集可直接使用的内容

每个 `<episode_id>/` 目录包含：

- `head.mp4`：头部左目完整视频，原 H.264 码流无损封装为 MP4。
- `<episode_id>.json`：原始采集元数据，包含相机标定等信息。
- `metadata.json`、`task.json`、`selection.json`：平台元数据、任务定义及抽样来源。
- `export.json`、`preview.jpg`：导出 topic、原始时间信息、帧数及代表帧。

100 条视频均为 `640×352`，合计 25,582 帧。每条检查过可见 marker 的代表帧，这不表示所有视频帧都可见 marker。

夹爪和触觉另存于：

| 路径 | 内容 |
| --- | --- |
| `../dataset/gripper_data/<episode_id>.json` | 左右夹爪完整原始 position 时间序列与来源 |
| `../dataset/gripper_data/<episode_id>_frames.csv` | 按头部左目视频帧对齐的 position 和时间差 |
| `../dataset/gripper_state_review/tactile/frames/<episode_id>_frames.csv` | 逐帧 position、四个指面的触觉特征、可用性和时间差 |

**当前导出 MP4 只有头部左目。** 可以用于该视角的 marker 检测；若复用 [README 中的双目位姿流程](README.md)，还需从原 MCAP 读取头部右目视频，并取得匹配的两目标定和时间戳。

源目录包含多份 JSON。调用转换器时应显式指定原 MCAP 和 `<episode_id>.json`，不能把平台 `metadata.json`、任务 JSON 或分析 JSON 混作采集 sidecar；现有“目录内唯一 MCAP + 唯一 JSON”的自动发现规则不适用于这些多 JSON 目录。

## 4. 已发现的兼容差异与数据语义

### 4.1 两套旧 topic 命名

| 数据 | 98 条样本 | 另外 2 条样本 |
| --- | --- | --- |
| 头部左目 | `/camera/coracam_head/left_h264/video` | `/observation/coracam_head/color/left/encoded` |
| 左夹爪 | `/observation/left_gripper/gripper/joint_position` | `/observation/left_gripper/joints` |
| 右夹爪 | `/observation/right_gripper/gripper/joint_position` | `/observation/right_gripper/joints` |

这两种命名都属于本批旧数据。另 2 条样本为 `37d6a87c621c4fb273bf3ee32e12df33` 和 `4520096f3693b2107000c5f1caf3b85d`。转换仓库历史提交 `04d0743` 已有相应别名映射，可作为读取接口的参考。

采集 JSON 记录的通道数分布为：95 条有 15 路、2 条有 21 路、2 条有 14 路、1 条有 13 路。接入时应检查实际通道与 schema，避免把固定通道数作为所有数据的共同前提。

### 4.2 夹爪、触觉与单位

依据 [gripper_state_analysis.txt](gripper_state_analysis.txt) 和 [机器可读分析](gripper_state_analysis.json)：

- 每爪保存一个连续 position，数值较大对应较大开度，接近 0 对应闭合或很小开度。夹持物体时可稳定在中间值。
- 左右 position 均齐全，采样频率约 100 Hz；物理单位仍未确认，保留原始值和未知单位标记。
- 触觉用于辅助判断接触，不能根据零触觉直接否定夹持；其物理单位也未核实。
- 97 条样本有完整四个触觉通道，3 条缺少部分通道。缺失值、超时不可用与真实零响应分别保存。
- position 和触觉属于观测状态，不能直接恢复操作者的原始二值控制指令；本批尚无统一离散开闭阈值或逐帧人工真值。

### 4.3 时间、图像与标定

逐帧 position 使用最近时间戳对齐，未插值或平滑；左右最大时间差分别约为 17.317 ms、21.919 ms。现有触觉逐帧表在通道缺失或对齐误差超过 50 ms 时置 `available=0`，特征留空；该阈值是当前触觉分析规则。

读取时保留原始整数纳秒时间戳，并明确相对时间的起点。导出记录中的 `origin_ns` 与视频第一帧时间可能不同，不能把 `export.json` 的相对时间与逐帧表的 `video_time_s` 直接混用。

图像与标定应使用同一坐标约定。当前视频保留 `640×352`；转换器上下各补 64 像素到 `640×480` 时，会同步调整 K/P 主点。新旧图像不能交叉使用未经修正的标定。

## 5. 格式统一建议

**建议先统一读取接口和数据语义，保留原始数据；完整 MCAP 转换作为按需处理。当前 100 条导出数据无需先整体转换为新 MCAP 才能开展 marker 检测。**

后续接入路径建议为：旧 MCAP + JSON、新规范 ROS2 MCAP、现有导出数据各自解码，再映射到同一套 episode/frame 接口。通过 schema 编码、channel 消息编码和明确的 topic 映射共同识别数据，记录未知或不支持的通道。

统一接口应至少包含：

| 内容 | 保留约定 |
| --- | --- |
| 来源与标识 | 原 episode ID、原始路径、格式类型；发生转换时另存输出 ID 与版本 |
| 图像 | 相机位置与左右目、帧序号、实际尺寸、原始时间戳 |
| 标定 | 对应图像的 K/D/R/P、frame_id、有效区域及来源 |
| 时间 | 原始整数纳秒时间戳、明确的相对时间起点、对齐方法与误差 |
| 夹爪状态 | 左右连续 position、原始值、单位及其确认状态 |
| 触觉 | 原始数据或明确命名的特征、每个指面的可用性与时间差；缺失与零值分开 |
| 数据质量 | 缺通道、时间间隙、未知单位、转换或对齐警告 |

转换器会对视频重新编码，改变像素值；默认单位换算也依赖源单位假设。因此，在 marker 检测基线阶段优先使用原码流与匹配标定。需要规范 MCAP 交付或 ROS2 回放时，再核实单位、通道覆盖、视频处理和标定后生成独立转换产物。

新规范数据的单位应结合来源契约与转换记录验证，避免对已经转换的数据重复缩放。转换器生成新 UUID 时，保留原 episode ID 的对应关系，继续关联本批任务、视频和状态分析结果。

## 6. 转换工具现状与后续验证

本次核实的 `mcap-convert` UMI 分支提交为 `a6485ee`。其 [兼容入口](../mcap-convert/convert_episode_ros2.py) 引用了 `mcap_convert` 包，但当前工作目录及该提交文件树未包含这个包，不能直接将 README 描述的安装接口视为已可运行。

此外，当前说明要求完整 15 路原始输入，历史实现 `04d0743` 也有对应检查。本批存在缺失通道及 21 路的变体，尚未验证能否直接通过完整转换。

后续实现或接入转换器时，需要验证：

1. 覆盖普通样本、两种 topic 命名和缺少触觉的样本，保留缺失信息。
2. 核对图像帧数、时间戳、左右相机、标定和夹爪来源；双目流程补齐右目输入。
3. 确认单位后再做物理量换算，保留原始状态与转换记录。
4. 用真实新规范样本检验新旧读取结果；若重编码，评估 marker 检测和位姿结果的变化。

上述内容为后续验收建议。本次仅整理文档，未安装转换器、批量改写数据或宣称新旧结果一致。
