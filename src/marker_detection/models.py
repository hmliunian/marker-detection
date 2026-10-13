"""Shared observations; timestamps stay in integer nanoseconds."""

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


@dataclass
class CameraCalibration:
    width: int
    height: int
    k: np.ndarray
    d: np.ndarray
    r: np.ndarray
    p: np.ndarray
    frame_id: str = ""
    distortion_model: str = "plumb_bob"
    source: str = ""


@dataclass
class VideoPacket:
    timestamp_ns: int
    data: bytes
    format: str = "h264"
    timestamp_source: str = "payload"


@dataclass
class ScalarSeries:
    timestamps_ns: np.ndarray
    values: np.ndarray
    unit: str = "unknown"
    source: str = ""


@dataclass
class Episode:
    source: str
    episode_id: str
    videos: dict[str, list[VideoPacket]] = field(default_factory=dict)
    cameras: dict[str, CameraCalibration] = field(default_factory=dict)
    grippers: dict[str, ScalarSeries] = field(default_factory=dict)
    tactile: dict[str, ScalarSeries] = field(default_factory=dict)
    task_id: str | None = None
    metadata: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def source_name(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())
