"""Decode recorded access units, retaining decoder presentation timestamps."""

from dataclasses import dataclass, field
from fractions import Fraction

import av
import numpy as np
from av.codec.hwaccel import HWAccel

from .models import VideoPacket


@dataclass
class DecodedVideo:
    timestamps_ns: np.ndarray
    images: list[np.ndarray]
    backend: str
    warnings: list[str] = field(default_factory=list)


def _luminance(frame: av.VideoFrame) -> np.ndarray:
    # Match tinynav's Y-plane input, including its original intensity range.
    if frame.format.name in {"gray", "yuv420p", "yuvj420p", "nv12"}:
        plane = frame.planes[0]
        return np.frombuffer(plane, dtype=np.uint8).reshape(-1, plane.line_size)[: frame.height, : frame.width].copy()
    return frame.to_ndarray(format="gray")


def _decode(packets: list[VideoPacket], cuda: bool, device: int) -> DecodedVideo:
    hardware = HWAccel("cuda", device=str(device), allow_software_fallback=False) if cuda else None
    codec_name = packets[0].format.lower().replace("h265", "hevc")
    codec = av.CodecContext.create(codec_name, "r", hwaccel=hardware)
    codec.thread_count = 1
    codec.pkt_timebase = Fraction(1, 1_000_000_000)
    origin = int(packets[0].timestamp_ns)
    images, timestamps = [], []

    def collect(decoded):
        for frame in decoded:
            if frame.pts is None or frame.time_base is None:
                raise ValueError("Decoder lost video timestamps; refusing to guess frame alignment")
            timestamps.append(origin + int(Fraction(frame.pts) * frame.time_base * 1_000_000_000))
            images.append(_luminance(frame))

    for observation in packets:
        packet = av.Packet(observation.data)
        packet.pts = int(observation.timestamp_ns) - origin
        packet.dts = None
        packet.time_base = Fraction(1, 1_000_000_000)
        collect(codec.decode(packet))
    collect(codec.decode(None))
    if not images:
        raise ValueError("Video decoded no frames")
    if cuda and not codec.is_hwaccel:
        raise RuntimeError("CUDA was requested but the decoder did not use hardware acceleration")
    return DecodedVideo(np.asarray(timestamps, dtype=np.int64), images, f"cuda:pyav:{device}" if cuda else "cpu:pyav")


def decode_video(
    packets: list[VideoPacket], width: int, height: int, backend: str = "auto", device: int = 0
) -> DecodedVideo:
    if not packets:
        raise ValueError("Video channel is empty")
    if backend not in {"auto", "cpu", "cuda"}:
        raise ValueError(f"Unsupported video backend: {backend}")
    formats = {packet.format.lower().replace("h265", "hevc") for packet in packets}
    if len(formats) != 1 or not formats <= {"h264", "hevc"}:
        raise ValueError(f"Unsupported/mixed compressed video formats: {formats}")
    warnings = []
    if backend == "cpu":
        result = _decode(packets, False, device)
    else:
        try:
            result = _decode(packets, True, device)
        except (av.FFmpegError, RuntimeError, NotImplementedError) as exc:
            if backend == "cuda":
                raise RuntimeError(f"CUDA video decode failed on device {device}: {exc}") from exc
            warnings.append(f"CUDA video decode failed; used CPU: {type(exc).__name__}: {exc}")
            result = _decode(packets, False, device)
    if any(image.shape != (height, width) for image in result.images):
        raise ValueError(f"Decoded image dimensions do not match calibration {width}x{height}")
    result.warnings.extend(warnings)
    return result
