from fractions import Fraction

import av
import numpy as np
import pytest

from marker_detection import video
from marker_detection.models import VideoPacket


@pytest.fixture(scope="module")
def b_frame_video():
    """Real encoded packets in decoder order with nonmonotonic display PTS."""
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width = 64
    encoder.height = 48
    encoder.pix_fmt = "yuv420p"
    encoder.time_base = Fraction(1, 30)
    encoder.framerate = Fraction(30, 1)
    encoder.max_b_frames = 2
    encoder.gop_size = 12
    encoder.options = {"crf": "18", "preset": "medium", "x264-params": "b-adapt=0:scenecut=0"}
    packets = []
    for index in range(12):
        frame = av.VideoFrame.from_ndarray(np.full((48, 64), 20 + 18 * index, dtype=np.uint8), format="gray")
        frame.pts = index
        frame.time_base = Fraction(1, 30)
        packets.extend(encoder.encode(frame))
    packets.extend(encoder.encode(None))
    origin_ns = 1_790_000_000_000_000_123
    observations = [
        VideoPacket(origin_ns + int(Fraction(packet.pts) * packet.time_base * 1_000_000_000), bytes(packet), "h264")
        for packet in packets
    ]
    expected_times = [origin_ns + int(Fraction(index, 30) * 1_000_000_000) for index in range(12)]
    # The fixture must actually exercise reordering, not just request B frames.
    assert any(b.timestamp_ns < a.timestamp_ns for a, b in zip(observations[:-1], observations[1:], strict=True))
    return observations, expected_times


def test_b_frames_decode_in_presentation_order_with_exact_original_nanoseconds(b_frame_video):
    observations, expected_times = b_frame_video
    result = video.decode_video(observations, 64, 48, backend="cpu")
    assert result.backend == "cpu:pyav"
    assert result.timestamps_ns.tolist() == expected_times
    assert len(result.images) == 12  # Includes delayed frames released on flush.
    assert all(frame.shape == (48, 64) and frame.dtype == np.uint8 for frame in result.images)
    assert all(np.diff([frame.mean() for frame in result.images]) > 10)
    assert not result.warnings


def test_video_dimensions_must_match_calibration(b_frame_video):
    observations, _ = b_frame_video
    with pytest.raises(ValueError, match="dimensions do not match calibration"):
        video.decode_video(observations, 65, 48, backend="cpu")


@pytest.mark.parametrize("other_format", ["hevc", "av1", "jpeg"])
def test_mixed_or_unsupported_packet_formats_fail_before_decoding(b_frame_video, other_format):
    observations, _ = b_frame_video
    first = observations[0]
    incompatible = [*observations, VideoPacket(first.timestamp_ns, first.data, other_format)]
    with pytest.raises(ValueError, match="Unsupported/mixed"):
        video.decode_video(incompatible, 64, 48, backend="cpu")


def test_auto_cuda_failure_is_observable_and_falls_back_once_to_cpu(b_frame_video, monkeypatch):
    observations, expected_times = b_frame_video
    decode = video._decode
    calls = []

    def without_cuda(packets, cuda, device):
        calls.append((cuda, device))
        if cuda:
            raise RuntimeError("fixture CUDA device unavailable")
        return decode(packets, False, device)

    monkeypatch.setattr(video, "_decode", without_cuda)
    result = video.decode_video(observations, 64, 48, backend="auto", device=2)
    assert calls == [(True, 2), (False, 2)]
    assert result.backend == "cpu:pyav"
    assert result.timestamps_ns.tolist() == expected_times
    assert len(result.warnings) == 1
    assert "fixture CUDA device unavailable" in result.warnings[0]
    assert "used CPU" in result.warnings[0]


def test_explicit_cuda_failure_never_falls_back_to_cpu(b_frame_video, monkeypatch):
    observations, _ = b_frame_video
    calls = []

    def unavailable(packets, cuda, device):
        calls.append((cuda, device))
        raise RuntimeError("fixture GPU failure")

    monkeypatch.setattr(video, "_decode", unavailable)
    with pytest.raises(RuntimeError, match="CUDA video decode failed on device 3"):
        video.decode_video(observations, 64, 48, backend="cuda", device=3)
    assert calls == [(True, 3)]


def test_cuda_does_not_claim_acceleration_when_codec_used_software(b_frame_video, monkeypatch):
    observations, _ = b_frame_video

    class SoftwareCodec:
        is_hwaccel = False

        @staticmethod
        def create(*args, **kwargs):
            return SoftwareCodec()

        def decode(self, packet):
            if packet is None:
                return []
            frame = av.VideoFrame.from_ndarray(np.zeros((48, 64), dtype=np.uint8), format="gray")
            frame.pts = packet.pts
            frame.time_base = packet.time_base
            return [frame]

    monkeypatch.setattr(video, "HWAccel", lambda *args, **kwargs: object())
    monkeypatch.setattr(video.av, "CodecContext", SoftwareCodec)
    with pytest.raises(RuntimeError, match="did not use hardware acceleration"):
        video.decode_video(observations, 64, 48, backend="cuda")


def test_lost_timestamps_are_not_repaired_by_retrying_another_backend(b_frame_video, monkeypatch):
    observations, _ = b_frame_video
    calls = []

    def no_timestamps(packets, cuda, device):
        calls.append(cuda)
        raise ValueError("Decoder lost video timestamps")

    monkeypatch.setattr(video, "_decode", no_timestamps)
    with pytest.raises(ValueError, match="lost video timestamps"):
        video.decode_video(observations, 64, 48, backend="auto")
    assert calls == [True]


def test_empty_video_and_invalid_backend_are_explicit_input_errors(b_frame_video):
    with pytest.raises(ValueError, match="empty"):
        video.decode_video([], 64, 48, backend="cpu")
    observations, _ = b_frame_video
    with pytest.raises(ValueError, match="Unsupported video backend"):
        video.decode_video(observations, 64, 48, backend="magic")
