"""Local, read-only review of saved marker trajectories and their source frames."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
from pathlib import Path

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import QEvent, QLibraryInfo, QPointF, QRectF, QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

COLORS = ("#168581", "#5471c4", "#a56a32")
INVALID_COLOR = "#cc7730"
DECISIONS = {
    "accepted": ("合格", "#167f65", "#e5f4ed"),
    "rejected": ("不合格", "#a44435", "#faebe7"),
    "review": ("待审核", "#95611e", "#fff1d9"),
    "excluded_carry": ("搬运 · 单独标记", "#626b83", "#ebedf3"),
}
REASONS = {
    "": "—",
    "ok": "有效观测",
    "accepted": "有效观测",
    "missing": "位姿缺失",
    "no_marker": "未检测到主手 marker",
    "no_primary_marker": "未检测到主手 marker",
    "primary_hand_unresolved": "主控手未确定",
    "no_ippe_candidate": "没有可用的单 tag 位姿解",
    "marker_identity_ambiguous": "主手 marker 身份存在歧义",
    "stereo_calibration_unavailable": "缺少可用的双目标定",
    "ambiguous_pose_no_right_marker": "单 tag 位姿有歧义，右目缺少对应 marker",
    "pnp_failed": "PnP 位姿求解失败",
    "nonfinite_pose": "位姿包含非有限数值",
    "nonpositive_depth": "位姿深度不是正值",
    "reprojection_error": "重投影误差超过阈值",
    "marker_geometry_inconsistent": "多 tag 观测与支架几何不一致",
    "velocity_gate": "相机系相对速度超过阈值",
    "no_pose": "未获得有效位姿",
    "ambiguous": "身份或位姿存在歧义",
    "speed_exceeded": "相邻位姿速度超过阈值",
    "jump": "轨迹跳变",
    "both_hands_active": "非搬运任务中双手参与",
    "both_hands_have_activity_evidence": "非搬运任务中双手均有参与证据",
    "carry_task": "搬运任务，单独标记",
    "carry_task_separate": "搬运任务，单独标记",
    "one_active_hand_other_open": "一侧参与操作，另一侧保持打开",
    "other_hand_activity_unresolved": "另一只手是否参与尚不确定",
    "no_reliable_activity_evidence": "缺少可靠的主控手活动证据",
    "coverage_below_threshold": "有效覆盖率低于阈值",
    "missing_frame_ratio_exceeds_threshold": "缺失帧比例超过阈值",
    "missing_interval_exceeds_threshold": "连续缺失时长超过阈值",
    "anomalous_frames_exceed_threshold": "异常帧数量超过阈值",
    "insufficient_valid_frames": "有效帧不足",
    "empty_trajectory": "没有轨迹帧",
    "missing_observation": "缺少有效观测",
    "invalid_timestamp": "时间戳无效",
    "nonincreasing_timestamp": "时间戳未递增",
    "nonfinite_xyz": "坐标包含非有限数值",
    "invalid_xyz_shape": "坐标不是三维向量",
    "invalid_xyz_value": "坐标数值无效",
    "nonfinite_kinematics": "无法计算有限的运动指标",
    "acceleration_exceeded": "相对加速度超过阈值",
    "reappearance_speed_exceeded": "缺失后重现位移超过速度阈值",
    "upstream_anomaly": "位姿求解检测到异常",
}


def _xyz(value: object) -> np.ndarray:
    try:
        result = np.asarray(value, dtype=float)
    except (TypeError, ValueError):
        return np.full(3, np.nan)
    if result.shape != (3,) or not np.isfinite(result).all():
        return np.full(3, np.nan)
    return result


def _reason(value: object) -> str:
    text = str(value or "")
    if text.startswith("raw_") and text[4:] in REASONS:
        return "原始位置：" + REASONS[text[4:]]
    return REASONS.get(text, text)


def load_analysis(directory: str | Path) -> tuple[dict, list[dict]]:
    """Load the public output contract without rewriting or sorting evidence."""
    directory = Path(directory)
    with (directory / "analysis.json").open(encoding="utf-8") as handle:
        analysis = json.load(handle)
    with (directory / "frames.json").open(encoding="utf-8") as handle:
        frames = json.load(handle)
    if not isinstance(analysis, dict) or not isinstance(frames, list):
        raise ValueError("analysis.json 应为对象，frames.json 应为逐帧列表")
    if not all(isinstance(row, dict) for row in frames):
        raise ValueError("frames.json 含有非对象帧")
    frame_ids = [int(row["frame_index"]) for row in frames]
    if len(set(frame_ids)) != len(frame_ids):
        raise ValueError("frames.json 的 frame_index 必须唯一")
    for row in frames:
        if not np.isfinite(float(row["time_s"])):
            raise ValueError("frames.json 的 time_s 必须为有限数值")
    return analysis, frames


class FramePreview(QLabel):
    """A fitted source image; annotations are drawn in original pixel coordinates."""

    def __init__(self) -> None:
        super().__init__("选择一帧以查看图像")
        self.original = QPixmap()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.setStyleSheet("background: #edf1f3; color: #6c7882; border-radius: 7px;")

    def set_frame(self, path: Path | None, detections: list[dict]) -> None:
        self.original = QPixmap(str(path)) if path else QPixmap()
        if self.original.isNull():
            self.clear()
            self.setText("此帧没有图像预览\n轨迹与诊断信息仍可查看")
            return
        painter = QPainter(self.original)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont()
        font.setPixelSize(max(15, round(self.original.width() / 55)))
        font.setBold(True)
        painter.setFont(font)
        label_rectangles: list[QRectF] = []
        for detection in detections:
            try:
                corners = np.asarray(detection["corners"], dtype=float)
                if corners.shape != (4, 2) or not np.isfinite(corners).all():
                    continue
                selected = bool(detection.get("selected", False))
                color = QColor("#24e0bf" if selected else "#ffbb62")
                painter.setPen(QPen(color, max(2, self.original.width() / 420)))
                painter.drawPolygon(QPolygonF([QPointF(*point) for point in corners]))
                label = f"ID {detection['id']} · {'主手' if selected else '未采用'}"
                metrics = painter.fontMetrics()
                width, height = metrics.horizontalAdvance(label) + 10, metrics.height() + 6
                x = max(0, min(float(corners[:, 0].min()), self.original.width() - width))
                top, bottom = float(corners[:, 1].min()), float(corners[:, 1].max())
                rect = QRectF(x, max(0, top - height - 5), width, height)
                for offset in range(0, self.original.height(), height + 4):
                    candidates = (top - height - 5 - offset, bottom + 5 + offset)
                    available = False
                    for y in candidates:
                        if y < 0 or y + height > self.original.height():
                            continue
                        candidate = QRectF(x, y, width, height)
                        if not any(candidate.adjusted(-3, -3, 3, 3).intersects(other) for other in label_rectangles):
                            rect, available = candidate, True
                            break
                    if available:
                        break
                label_rectangles.append(rect)
                painter.fillRect(rect, QColor(15, 30, 38, 220))
                painter.drawText(
                    rect.adjusted(5, 0, -5, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label
                )
            except (KeyError, TypeError, ValueError):
                continue
        painter.end()
        self._fit()

    def _fit(self) -> None:
        if not self.original.isNull():
            self.setPixmap(
                self.original.scaled(
                    self.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()


class AnalysisWindow(QMainWindow):
    """Frame selection links image evidence, a 3D point, and XYZ time cursors."""

    def __init__(self, directory: str | Path) -> None:
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.directory = Path(directory).expanduser().resolve()
        self.analysis, self.frames = load_analysis(self.directory)
        self.frame_ids = np.asarray([int(row["frame_index"]) for row in self.frames])
        self.times = np.asarray([float(row["time_s"]) for row in self.frames])
        self.xyz = np.asarray([_xyz(row.get("xyz_m")) for row in self.frames]).reshape(-1, 3)
        self.valid = np.asarray(
            [bool(row.get("valid", False)) and bool(row.get("quality", {}).get("valid", True)) for row in self.frames],
            dtype=bool,
        ) & np.isfinite(self.xyz).all(axis=1)
        self.valid_xyz = self.xyz.copy()
        self.valid_xyz[~self.valid] = np.nan
        self.raw_xyz = np.asarray(
            [
                _xyz(row.get("raw_xyz_m") if row.get("raw_xyz_m") is not None else row.get("xyz_m"))
                for row in self.frames
            ]
        ).reshape(-1, 3)
        self.current_index = -1
        self.setWindowTitle("Marker 轨迹审核")
        self.resize(1460, 980)
        self.setMinimumSize(980, 740)
        self._build_ui()
        self._draw_trajectories()
        if self.frames:
            self.select_frame(0)
        else:
            self.frame_status.setText("该结果没有视频帧；请查看 episode 结论与原因。")
            self.slider.setEnabled(False)
            self.frame_spin.setEnabled(False)
            self.time_spin.setEnabled(False)
            for button in self.frame_buttons:
                button.setEnabled(False)

    def _build_ui(self) -> None:
        self.setStyleSheet(
            "QMainWindow, QWidget {background: #f6f8fa; color: #223342;}"
            "QLabel {background: transparent;}"
            "QPushButton {background: white; border: 1px solid #ccd6de; "
            "border-radius: 5px; padding: 7px 13px;}"
            "QPushButton:hover {background: #eaf4f3; border-color: #168581;}"
            "QPushButton:disabled {color: #a6b0b8;}"
            "QSpinBox, QDoubleSpinBox {background: white; border: 1px solid #ccd6de; "
            "border-radius: 4px; padding: 5px;}"
            "QSlider::groove:horizontal {height: 5px; background: #d8e2e8; "
            "border-radius: 2px;}"
            "QSlider::handle:horizontal {width: 15px; margin: -5px 0; "
            "background: #168581; border-radius: 7px;}"
        )
        content = QWidget()
        self.setCentralWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(10)
        header = QHBoxLayout()
        title = QLabel("Marker 轨迹审核")
        title.setStyleSheet("font-size: 23px; font-weight: 650;")
        header.addWidget(title)
        hand = {"left": "左手", "right": "右手"}.get(self.analysis.get("primary_hand"), "未确定")
        self.hand_label = QLabel(f"主控手  {hand}")
        self.hand_label.setStyleSheet("font-size: 15px; padding-left: 18px;")
        header.addWidget(self.hand_label)
        header.addStretch()
        label, color, background = DECISIONS.get(self.analysis.get("decision"), ("未知结论", "#626b83", "#ebedf3"))
        self.decision_label = QLabel(label)
        self.decision_label.setStyleSheet(
            f"color: {color}; background: {background}; font-size: 14px; "
            "font-weight: bold; border-radius: 5px; padding: 7px 15px;"
        )
        header.addWidget(self.decision_label)
        open_button = QPushButton("打开结果")
        open_button.clicked.connect(self._open_result)
        header.addWidget(open_button)
        layout.addLayout(header)
        episode = QLabel(str(self.analysis.get("episode_id", self.directory.name)))
        episode.setStyleSheet("color: #697885; font-size: 12px;")
        episode.setToolTip(str(self.analysis.get("source", self.directory)))
        episode.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(episode)
        layout.addWidget(self._summary_bar())

        reasons = self.analysis.get("reasons") or self.analysis.get("primary_hand_evidence", {}).get("reasons", [])
        self.reasons_label = QLabel(
            "结论依据：" + "；".join(_reason(reason) for reason in reasons) if reasons else "结论依据：未记录额外原因"
        )
        self.reasons_label.setWordWrap(True)
        self.reasons_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.reasons_label.setStyleSheet("color: #586875; font-size: 12px;")
        layout.addWidget(self.reasons_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        image_panel = QWidget()
        image_layout = QVBoxLayout(image_panel)
        image_layout.setContentsMargins(0, 0, 8, 0)
        image_heading = QLabel("图像证据    ·    绿色为选中主手，橙色为未采用观测")
        image_heading.setStyleSheet("font-size: 13px; font-weight: 600;")
        image_layout.addWidget(image_heading)
        self.preview = FramePreview()
        image_layout.addWidget(self.preview, 1)
        self.frame_status = QLabel()
        self.frame_status.setWordWrap(True)
        self.frame_status.setMinimumHeight(40)
        self.frame_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        image_layout.addWidget(self.frame_status)
        splitter.addWidget(image_panel)

        plot_panel = QWidget()
        plot_layout = QVBoxLayout(plot_panel)
        plot_layout.setContentsMargins(8, 0, 0, 0)
        plot_heading = QLabel("三维轨迹    ·    米 / 头部左相机坐标系    ·    拖动旋转")
        plot_heading.setStyleSheet("font-size: 13px; font-weight: 600;")
        plot_layout.addWidget(plot_heading)
        self.figure3d = Figure(facecolor="#ffffff", constrained_layout=True)
        self.canvas3d = FigureCanvasQTAgg(self.figure3d)
        self.canvas3d.setMinimumHeight(260)
        self.axis3d = self.figure3d.add_subplot(111, projection="3d")
        plot_layout.addWidget(self.canvas3d, 1)
        splitter.addWidget(plot_panel)
        splitter.setSizes([650, 650])
        layout.addWidget(splitter, 5)

        curve_heading = QLabel("XYZ 连续性    ·    橙色区域为无效帧；缺失断线；× 为被拒绝的原始位置")
        curve_heading.setStyleSheet("font-size: 13px; font-weight: 600;")
        layout.addWidget(curve_heading)
        self.figure_time = Figure(facecolor="#ffffff")
        self.canvas_time = FigureCanvasQTAgg(self.figure_time)
        self.canvas_time.setMinimumHeight(210)
        self.axes_time = self.figure_time.subplots(3, 1, sharex=True)
        self.figure_time.subplots_adjust(left=0.07, right=0.99, top=0.95, bottom=0.18, hspace=0.13)
        layout.addWidget(self.canvas_time, 3)
        self.canvas_time.mpl_connect("button_press_event", self._plot_clicked)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, max(0, len(self.frames) - 1))
        self.slider.valueChanged.connect(self.select_frame)
        layout.addWidget(self.slider)
        controls = QHBoxLayout()
        previous = QPushButton("← 上一帧")
        previous.clicked.connect(lambda: self.select_frame(self.current_index - 1))
        following = QPushButton("下一帧 →")
        following.clicked.connect(lambda: self.select_frame(self.current_index + 1))
        invalid = QPushButton("下一处无效")
        invalid.clicked.connect(self._next_invalid)
        self.frame_buttons = (previous, following, invalid)
        for button in self.frame_buttons:
            controls.addWidget(button)
        controls.addSpacing(10)
        controls.addWidget(QLabel("帧号"))
        self.frame_spin = QSpinBox()
        self.frame_spin.setKeyboardTracking(False)
        self.frame_spin.setRange(
            int(self.frame_ids.min()) if self.frames else 0,
            int(self.frame_ids.max()) if self.frames else 0,
        )
        self.frame_spin.valueChanged.connect(self._frame_number_changed)
        controls.addWidget(self.frame_spin)
        controls.addWidget(QLabel("时间"))
        self.time_spin = QDoubleSpinBox()
        self.time_spin.setDecimals(3)
        self.time_spin.setSuffix(" s")
        self.time_spin.setKeyboardTracking(False)
        self.time_spin.setRange(
            float(self.times.min()) if self.frames else 0,
            float(self.times.max()) if self.frames else 0,
        )
        self.time_spin.valueChanged.connect(self._time_changed)
        controls.addWidget(self.time_spin)
        self.position_label = QLabel()
        controls.addWidget(self.position_label)
        controls.addStretch()
        parameters = QPushButton("指标与参数")
        parameters.clicked.connect(self._show_parameters)
        controls.addWidget(parameters)
        layout.addLayout(controls)
        note = QLabel("无效帧不会通过插值补齐。轨迹连续性用于辅助审核，不代表绝对位置精度。")
        note.setStyleSheet("color: #71808b; font-size: 11px;")
        layout.addWidget(note)

    def _summary_bar(self) -> QFrame:
        bar = QFrame()
        bar.setStyleSheet("QFrame {background: white; border: 1px solid #e0e7ec; border-radius: 6px;}")
        layout = QHBoxLayout(bar)
        summary = self.analysis.get("summary", {})
        count = len(self.frames)
        valid_count = int(self.valid.sum())
        metrics = [
            ("视频帧", str(count)),
            ("有效位置", f"{valid_count} / {count}"),
            ("有效覆盖", f"{valid_count / count:.1%}" if count else "—"),
            ("时间范围", f"{self.times[-1] - self.times[0]:.2f} s" if count else "—"),
        ]
        for keys, label, unit in (
            (
                ("longest_gap_s", "longest_missing_s", "max_missing_duration_s", "longest_missing_duration_s"),
                "最长缺失",
                "s",
            ),
            (
                ("max_raw_speed_m_s", "max_speed_m_s", "max_speed_mps", "max_observed_speed_mps"),
                "原始最大相对速度" if summary.get("max_raw_speed_m_s") is not None else "最大相对速度",
                "m/s",
            ),
        ):
            value = next((summary[key] for key in keys if key in summary), None)
            if isinstance(value, (int, float)) and np.isfinite(value):
                metrics.append((label, f"{value:.3f} {unit}"))
        for label, value in metrics:
            widget = QLabel(
                f"<span style='color:#697885'>{label}</span><br>"
                f"<span style='font-size:19px;font-weight:600'>{value}</span>"
            )
            widget.setStyleSheet("border: none; padding: 2px 12px;")
            layout.addWidget(widget, 1)
        return bar

    def _invalid_intervals(self) -> list[tuple[float, float]]:
        intervals = []
        index = 0
        while index < len(self.frames):
            if self.valid[index]:
                index += 1
                continue
            start = index
            while index + 1 < len(self.frames) and not self.valid[index + 1]:
                index += 1
            left = (self.times[start - 1] + self.times[start]) / 2 if start else self.times[start]
            right = (
                (self.times[index] + self.times[index + 1]) / 2 if index + 1 < len(self.frames) else self.times[index]
            )
            intervals.append((float(left), float(right)))
            index += 1
        return intervals

    def _draw_trajectories(self) -> None:
        (self.trajectory_line,) = self.axis3d.plot(
            *self.valid_xyz.T, color=COLORS[0], linewidth=1.7, marker=".", markersize=3, label="Valid observations"
        )
        rejected = ~self.valid & np.isfinite(self.raw_xyz).all(axis=1)
        if rejected.any():
            self.axis3d.scatter(
                *self.raw_xyz[rejected].T, color=INVALID_COLOR, marker="x", s=30, label="Rejected raw positions"
            )
        (self.current_point,) = self.axis3d.plot([], [], [], "o", color="#233745", markersize=8, zorder=10)
        self.axis3d.set(xlabel="X (m)", ylabel="Y (m)", zlabel="Z (m)")
        self.axis3d.tick_params(labelsize=8)
        self.axis3d.view_init(elev=23, azim=-60)
        self.axis3d.set_box_aspect((1, 1, 1))
        finite = np.concatenate((self.valid_xyz[self.valid], self.raw_xyz[rejected]), axis=0)
        if len(finite):
            center = (finite.min(axis=0) + finite.max(axis=0)) / 2
            radius = max(float(np.ptp(finite, axis=0).max()) * 0.55, 0.01)
            self.axis3d.set_xlim(center[0] - radius, center[0] + radius)
            self.axis3d.set_ylim(center[1] - radius, center[1] + radius)
            self.axis3d.set_zlim(center[2] - radius, center[2] + radius)
        else:
            self.axis3d.text2D(
                0.5, 0.5, "No finite positions", ha="center", transform=self.axis3d.transAxes, color="#788792"
            )
        self.axis3d.legend(loc="upper left", fontsize=8, frameon=False)
        self.time_lines = []
        self.time_cursors = []
        for component, axis in enumerate(self.axes_time):
            (line,) = axis.plot(
                self.times,
                self.valid_xyz[:, component],
                color=COLORS[component],
                linewidth=1.3,
                marker=".",
                markersize=3,
            )
            self.time_lines.append(line)
            if rejected.any():
                axis.scatter(
                    self.times[rejected],
                    self.raw_xyz[rejected, component],
                    color=INVALID_COLOR,
                    marker="x",
                    s=24,
                    zorder=3,
                )
            for left, right in self._invalid_intervals():
                axis.axvspan(left, right, color="#f0c993", alpha=0.3, linewidth=0)
            self.time_cursors.append(axis.axvline(0, color="#233745", linewidth=1, alpha=0.75))
            axis.set_ylabel(f"{'XYZ'[component]} (m)", fontsize=9)
            axis.grid(alpha=0.35, color="#b9c8d1")
            axis.tick_params(labelsize=8)
            axis.spines[["top", "right"]].set_visible(False)
            axis.spines[["left", "bottom"]].set_color("#d6dfe5")
        self.axes_time[-1].set_xlabel("Time from first frame (s)", fontsize=9)
        self.canvas3d.draw_idle()
        self.canvas_time.draw_idle()

    def select_frame(self, index: int) -> None:
        """Select by list position, independently of the original frame_index."""
        if not self.frames:
            return
        self.current_index = max(0, min(int(index), len(self.frames) - 1))
        row = self.frames[self.current_index]
        for widget, value in (
            (self.slider, self.current_index),
            (self.frame_spin, int(row["frame_index"])),
            (self.time_spin, float(row["time_s"])),
        ):
            with QSignalBlocker(widget):
                widget.setValue(value)
        self.position_label.setText(f"序列 {self.current_index + 1} / {len(self.frames)}")
        valid = self.valid[self.current_index]
        point = self.valid_xyz[self.current_index] if valid else self.raw_xyz[self.current_index]
        if np.isfinite(point).all():
            self.current_point.set_data_3d([point[0]], [point[1]], [point[2]])
            self.current_point.set_color("#233745" if valid else INVALID_COLOR)
            coordinates = ", ".join(f"{value:.4f}" for value in point)
            position = f"{'XYZ' if valid else '原始 XYZ（未接受）'} = ({coordinates}) m"
        else:
            self.current_point.set_data_3d([], [], [])
            position = "XYZ 缺失"
        ids = ", ".join(str(value) for value in row.get("marker_ids", [])) or "无"
        reasons = [] if row.get("reason") in ("ok", "accepted") and not valid else [_reason(row.get("reason"))]
        reasons.extend(_reason(issue) for issue in row.get("quality", {}).get("issues", []))
        metrics = []
        raw_speed = row.get("quality", {}).get("raw_speed_m_s")
        error = row.get("reprojection_error_px")
        if isinstance(raw_speed, (int, float)) and np.isfinite(raw_speed):
            metrics.append(f"原始相对速度 {raw_speed:.3f} m/s")
        if isinstance(error, (int, float)) and np.isfinite(error):
            metrics.append(f"重投影误差 {error:.3f} px")
        self.frame_status.setText(
            f"帧 {row['frame_index']} · {row['time_s']:.3f} s · "
            f"{'有效' if valid else '无效'} · marker {ids}\n"
            f"{position}   |   {'；'.join(dict.fromkeys(reasons))}"
            + ("\n" + "   ·   ".join(metrics) if metrics else "")
        )
        self.frame_status.setToolTip(f"原始时间戳：{row.get('timestamp_ns', '—')} ns")
        path = None
        if row.get("preview"):
            candidate = (self.directory / row["preview"]).resolve()
            if candidate.is_relative_to(self.directory):
                path = candidate
        self.preview.set_frame(path, row.get("detections", []))
        for cursor in self.time_cursors:
            cursor.set_xdata([row["time_s"], row["time_s"]])
        self.frame_buttons[0].setEnabled(self.current_index > 0)
        self.frame_buttons[1].setEnabled(self.current_index + 1 < len(self.frames))
        self.frame_buttons[2].setEnabled(bool((~self.valid).any()))
        self.canvas3d.draw_idle()
        self.canvas_time.draw_idle()

    def set_frame_by_number(self, number: int) -> None:
        matching = np.flatnonzero(self.frame_ids == number)
        if not len(matching):
            raise ValueError(f"结果中没有原始帧号 {number}")
        self.select_frame(int(matching[0]))

    def _frame_number_changed(self, value: int) -> None:
        if self.frames:
            self.select_frame(int(np.argmin(np.abs(self.frame_ids - value))))

    def _time_changed(self, value: float) -> None:
        if self.frames:
            self.select_frame(int(np.argmin(np.abs(self.times - value))))

    def _plot_clicked(self, event) -> None:
        if event.inaxes in self.axes_time and event.xdata is not None:
            self._time_changed(event.xdata)

    def _next_invalid(self) -> None:
        candidates = np.flatnonzero(~self.valid)
        if len(candidates):
            following = candidates[candidates > self.current_index]
            self.select_frame(int(following[0] if len(following) else candidates[0]))

    def _show_parameters(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("本次分析的指标与参数")
        dialog.resize(700, 600)
        layout = QVBoxLayout(dialog)
        label = QLabel("以下为本次分析保存的证据与阈值，修改参数后需重新分析。")
        label.setWordWrap(True)
        layout.addWidget(label)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(
            json.dumps(
                {
                    "summary": self.analysis.get("summary", {}),
                    "config": self.analysis.get("config", {}),
                    "source": self.analysis.get("source"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        layout.addWidget(text)
        dialog.exec()

    def _open_result(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "打开分析结果目录", str(self.directory.parent))
        if not directory:
            return
        try:
            window = AnalysisWindow(directory)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            QMessageBox.warning(self, "无法读取结果", str(exc))
            return
        window.show()
        self._opened_window = window
        self.hide()


def _platform_error() -> str | None:
    """Check Linux prerequisites before Qt can abort the whole process."""
    if not sys.platform.startswith("linux") or QApplication.instance() is not None:
        return None
    display = os.environ.get("DISPLAY")
    wayland = os.environ.get("WAYLAND_DISPLAY")
    platform = os.environ.get("QT_QPA_PLATFORM", "").split(";", 1)[0].split(":", 1)[0].strip().lower()
    displayless = {"offscreen", "minimal", "minimalegl", "vnc", "eglfs", "linuxfb"}
    browser_hint = "纯 SSH 请使用 just gui <结果目录> 启动浏览器入口，并按提示设置 SSH 端口转发。"
    if not display and not wayland and platform not in displayless:
        return (
            "当前 Linux 会话没有 DISPLAY/WAYLAND_DISPLAY，无法打开桌面 Qt 窗口。"
            f"{browser_hint}需要离屏截图时可显式设置 QT_QPA_PLATFORM=offscreen。"
        )
    if platform == "xcb" and not display:
        return f"QT_QPA_PLATFORM=xcb 需要有效的 DISPLAY。{browser_hint}"
    use_xcb = platform == "xcb" or (not platform and display and not wayland)
    if use_xcb:
        plugin = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)) / "platforms" / "libqxcb.so"
        try:
            ctypes.CDLL(str(plugin))
        except OSError as exc:
            return (
                f"无法加载 Qt xcb 桌面插件：{exc}\n"
                "请按缺库名称安装桌面依赖；Ubuntu/Debian 常见缺库 libxcb-cursor.so.0 "
                "对应 sudo apt install libxcb-cursor0。\n"
                f"{browser_hint}"
            )
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="查看 marker 筛选结果和逐帧三维轨迹")
    parser.add_argument("directory", type=Path, help="包含 analysis.json 和 frames.json 的目录")
    parser.add_argument("--screenshot", type=Path, help="保存真实 GUI 截图并退出，可用 QT_QPA_PLATFORM=offscreen")
    parser.add_argument("--frame", type=int, help="选择原始 frame_index，默认为首帧")
    args = parser.parse_args(argv)
    if error := _platform_error():
        print(error, file=sys.stderr)
        return 2
    owns_application = QApplication.instance() is None
    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName("Marker 轨迹审核")
    application.setStyle("Fusion")
    try:
        window = AnalysisWindow(args.directory)
        if args.frame is not None:
            window.set_frame_by_number(args.frame)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if owns_application:
            application.closeAllWindows()
            application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            application.shutdown()
        parser.error(str(exc))
    window.show()
    result = {"code": 0}
    if args.screenshot:

        def capture() -> None:
            try:
                window.canvas3d.draw()
                window.canvas_time.draw()
                application.processEvents()
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                if not window.grab().save(str(args.screenshot)):
                    raise OSError(f"无法保存截图：{args.screenshot}")
            except OSError as exc:
                print(str(exc), file=sys.stderr)
                result["code"] = 1
            finally:
                window.close()
                application.quit()

        QTimer.singleShot(300, capture)
    code = application.exec()
    if owns_application:
        application.closeAllWindows()
        application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        application.shutdown()
    return result["code"] or code


if __name__ == "__main__":
    raise SystemExit(main())
