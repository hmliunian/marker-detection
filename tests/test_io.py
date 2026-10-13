import json
import struct
import zipfile

import numpy as np
import pytest
from mcap.writer import IndexType, Writer
from mcap_ros2.writer import Writer as RosWriter
from test_flatbuffer import reflection_schema, video_payload

from marker_detection.io import inspect_mcap, load_episode

SEP = "\n" + "=" * 80 + "\n"
TIME = SEP + "MSG: builtin_interfaces/Time\nint32 sec\nuint32 nanosec"
HEADER = SEP + "MSG: std_msgs/Header\nbuiltin_interfaces/Time stamp\nstring frame_id" + TIME
VIDEO = "builtin_interfaces/Time timestamp\nstring frame_id\nuint8[] data\nstring format" + TIME
JOINT = "std_msgs/Header header\nstring[] name\nfloat64[] position\nfloat64[] velocity\nfloat64[] effort" + HEADER
CAMERA = (
    "std_msgs/Header header\nuint32 height\nuint32 width\nstring distortion_model\n"
    "float64[] d\nfloat64[9] k\nfloat64[9] r\nfloat64[12] p\nuint32 binning_x\nuint32 binning_y\n"
    "sensor_msgs/RegionOfInterest roi"
    + HEADER
    + SEP
    + "MSG: sensor_msgs/RegionOfInterest\nuint32 x_offset\nuint32 y_offset\nuint32 height\nuint32 width\nbool do_rectify"
)
POINTS = (
    "std_msgs/Header header\nuint32 height\nuint32 width\nsensor_msgs/PointField[] fields\n"
    "bool is_bigendian\nuint32 point_step\nuint32 row_step\nuint8[] data\nbool is_dense"
    + HEADER
    + SEP
    + "MSG: sensor_msgs/PointField\nstring name\nuint32 offset\nuint8 datatype\nuint32 count"
)
STAMP = {"sec": 1_790_128_230, "nanosec": 663_178_001}
MOMENT = 1_790_128_230_663_178_001


def camera_info(height=352):
    return {
        "header": {"stamp": STAMP, "frame_id": "head_left"},
        "frame_id": "head_left",
        "height": height,
        "width": 640,
        "distortion_model": "plumb_bob",
        "d": [0.0] * 5,
        "k": [268.5, 0, 320, 0, 268.5, height / 2, 0, 0, 1],
        "r": [1.0, 0, 0, 0, 1, 0, 0, 0, 1],
        "p": [268.5, 0, 320, 0, 0, 268.5, height / 2, 0, 0, 0, 1, 0],
        "binning_x": 0,
        "binning_y": 0,
        "roi": {"x_offset": 0, "y_offset": 0, "height": 0, "width": 0, "do_rectify": False},
    }


def legacy_file(
    path, topic="/camera/coracam_head/left_h264/video", calibration=True, duplicate=False, **writer_options
):
    with path.open("wb") as stream:
        writer = Writer(stream, **writer_options)
        writer.start()
        sid = writer.register_schema("foxglove.CompressedVideo", "flatbuffer", reflection_schema())
        cid = writer.register_channel(topic, "flatbuffer", sid)
        writer.add_message(cid, MOMENT + 1234, video_payload(), MOMENT + 1234)
        if duplicate:
            cid = writer.register_channel("/observation/coracam_head/color/left/encoded", "flatbuffer", sid)
            writer.add_message(cid, MOMENT + 1235, video_payload(), MOMENT + 1235)
        if calibration:
            camera = camera_info()
            camera["topic"] = "/robot/camera/head/left/camera_info"
            writer.add_metadata("camera_info", {"camera_head_left": json.dumps(camera)})
        writer.add_metadata("task", {"task.id": "A_003"})
        writer.finish()


@pytest.mark.parametrize(
    "topic", ["/camera/coracam_head/left_h264/video", "/observation/coracam_head/color/left/encoded"]
)
def test_legacy_aliases_payload_ns_calibration_and_missing_streams(tmp_path, topic):
    path = tmp_path / "episode.mcap"
    legacy_file(path, topic)
    episode = load_episode(path)
    assert episode.task_id == "A_003"
    assert episode.videos["head_left"][0].timestamp_ns == MOMENT
    assert episode.videos["head_left"][0].timestamp_source == "payload.timestamp"
    assert episode.videos["head_left"][0].format == "h264"
    assert episode.grippers == {}
    assert episode.tactile == {}
    assert "Missing tactile stream: left_finger0" in episode.warnings
    assert episode.cameras["head_left"].k.shape == (3, 3)
    assert episode.cameras["head_left"].p.shape == (3, 4)
    assert episode.cameras["head_left"].height == 352
    info = inspect_mcap(path)
    assert info["channels"][0]["message_encoding"] == "flatbuffer"
    json.dumps(info, allow_nan=False)


def test_analysis_json_is_not_used_as_collection_sidecar(tmp_path):
    path = tmp_path / "episode.mcap"
    legacy_file(path)
    (tmp_path / f"{tmp_path.name}.json").write_text(json.dumps({"action_id": "A_999", "values": [1, 2]}))
    (tmp_path / "metadata.json").write_text(json.dumps({"task": {"id": "A_888"}}))
    episode = load_episode(path)
    assert episode.task_id == "A_003"
    assert episode.metadata["sidecar_sources"] == []


def test_zip_collection_sidecar_and_task_source_are_discovered(tmp_path):
    path = tmp_path / "episode.mcap"
    legacy_file(path)
    archive_path = tmp_path / "collection.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.write(path, "nested/episode.mcap")
        archive.writestr(
            "nested/collection.json", json.dumps({"start_time_us": 1, "stop_time_us": 2, "task.action_id": "A_020"})
        )
        archive.writestr("nested/analysis.json", json.dumps({"task.id": "A_999"}))
    episode = load_episode(archive_path)
    assert episode.task_id == "A_020"
    assert episode.metadata["task_id_source"].endswith("!nested/collection.json")
    assert episode.metadata["mcap_source"].endswith("!nested/episode.mcap")
    assert episode.episode_id == "collection"
    assert not (tmp_path / "nested").exists()


def test_conflicting_calibration_is_not_silently_overridden(tmp_path):
    path = tmp_path / "episode.mcap"
    legacy_file(path)
    (tmp_path / "collection.json").write_text(json.dumps({"camera_head_left": camera_info(height=480)}))
    with pytest.raises(ValueError, match="Conflicting.*calibration"):
        load_episode(path)


def test_duplicate_channels_for_same_camera_are_not_merged(tmp_path):
    path = tmp_path / "episode.mcap"
    legacy_file(path, duplicate=True)
    with pytest.raises(ValueError, match="Multiple channels"):
        load_episode(path)


def write_cdr(path, force_value=3.0):
    with RosWriter(str(path)) as writer:
        video_schema = writer.register_msgdef("foxglove_msgs/msg/CompressedVideo", VIDEO)
        writer.write_message(
            "/camera/head/left/compressed",
            video_schema,
            {"timestamp": STAMP, "frame_id": "head_left", "data": b"video", "format": "h264"},
            log_time=MOMENT + 555,
        )
        camera_schema = writer.register_msgdef("sensor_msgs/msg/CameraInfo", CAMERA)
        writer.write_message("/camera/head/left/camera_info", camera_schema, camera_info(480), log_time=MOMENT)
        joint_schema = writer.register_msgdef("sensor_msgs/msg/JointState", JOINT)
        writer.write_message(
            "/arm/left/gripper/joint_state",
            joint_schema,
            {
                "header": {"stamp": STAMP, "frame_id": "left_gripper"},
                "name": ["left_gripper_width"],
                "position": [0.034],
                "velocity": [],
                "effort": [],
            },
            log_time=MOMENT + 777,
        )
        tactile_schema = writer.register_msgdef("sensor_msgs/msg/PointCloud2", POINTS)
        # Non-default field order, a big-endian payload and row padding test the
        # actual PointCloud2 layout rather than assuming contiguous 6-float data.
        data = (struct.pack(">fff", 0, force_value, 4.0) * 5 + b"PADD") * 5
        writer.write_message(
            "/arm/left/tactile/finger0",
            tactile_schema,
            {
                "header": {"stamp": STAMP, "frame_id": "left_gripper"},
                "height": 5,
                "width": 5,
                "fields": [
                    {"name": name, "offset": offset, "datatype": 7, "count": 1}
                    for name, offset in [("fz", 0), ("fx", 4), ("fy", 8)]
                ],
                "is_bigendian": True,
                "point_step": 12,
                "row_step": 64,
                "data": data,
                "is_dense": True,
            },
            log_time=MOMENT + 888,
        )


def test_documented_cdr_contract_keeps_units_timestamps_and_force_layout(tmp_path):
    path = tmp_path / "converted.mcap"
    write_cdr(path)
    episode = load_episode(path)
    assert episode.videos["head_left"][0].timestamp_ns == MOMENT
    assert episode.cameras["head_left"].height == 480
    assert episode.grippers["left"].unit == "m"
    assert episode.grippers["left"].values.tolist() == [0.034]
    assert episode.grippers["left"].timestamps_ns.tolist() == [MOMENT]
    assert episode.tactile["left_finger0"].values.tolist() == [125.0]
    assert episode.tactile["left_finger0"].unit == "unknown"
    assert "right_finger0" not in episode.tactile
    assert episode.metadata["formats"] == ["cdr"]


def test_nonfinite_tactile_remains_unavailable_not_zero(tmp_path):
    path = tmp_path / "nonfinite.mcap"
    write_cdr(path, force_value=float("nan"))
    episode = load_episode(path)
    assert np.isnan(episode.tactile["left_finger0"].values[0])
    assert any("Non-finite tactile/left_finger0" in warning for warning in episode.warnings)


def test_multiple_mcaps_in_zip_require_an_unambiguous_input(tmp_path):
    path = tmp_path / "ambiguous.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("one.mcap", b"not read")
        archive.writestr("two.mcap", b"not read")
    with pytest.raises(ValueError, match="exactly one MCAP"):
        load_episode(path)


def test_no_summary_mcap_still_reports_encoding_and_counts(tmp_path):
    path = tmp_path / "unindexed.mcap"
    legacy_file(
        path,
        use_statistics=False,
        use_summary_offsets=False,
        index_types=IndexType.NONE,
        repeat_channels=False,
        repeat_schemas=False,
        use_chunking=False,
    )
    result = inspect_mcap(path)
    assert result["formats"] == ["flatbuffer"]
    assert result["message_count"] == 1
    assert result["videos"]["head_left"]["count"] == 1
    assert result["task_id"] == "A_003"


def test_task_definition_action_is_used_when_collection_has_no_task(tmp_path):
    path = tmp_path / "converted.mcap"
    write_cdr(path)
    (tmp_path / "task.json").write_text(
        json.dumps({"id": 99999, "item_info": {"steps": [{"action": {"id": "A_003"}}]}})
    )
    episode = load_episode(path)
    assert episode.task_id == "A_003"
    assert episode.metadata["task_id_source"].endswith("task.json")


def test_multiple_collection_sidecars_need_explicit_choice(tmp_path):
    path = tmp_path / "episode.mcap"
    legacy_file(path)
    for name in ("one.json", "two.json"):
        (tmp_path / name).write_text(json.dumps({"start_time_us": 1, "stop_time_us": 2, "task.action_id": "A_020"}))
    with pytest.raises(ValueError, match="Multiple collection sidecars"):
        load_episode(path)
    episode = load_episode(path, sidecar=tmp_path / "two.json")
    assert episode.task_id == "A_020"
    assert len(episode.metadata["sidecar_sources"]) == 1


@pytest.mark.parametrize(
    "include_stamp, expected, source", [(True, 0, "payload.timestamp"), (False, 123, "mcap.publish_time")]
)
def test_real_zero_time_is_distinct_from_absent_payload_time(tmp_path, include_stamp, expected, source):
    path = tmp_path / "time.mcap"
    # Both variants are legal embedded message definitions; absence is distinct
    # from a time object whose numeric fields equal zero.
    definition = VIDEO if include_stamp else "string frame_id\nuint8[] data\nstring format"
    message = {"frame_id": "head_left", "data": b"encoded", "format": "h264"}
    if include_stamp:
        message["timestamp"] = {"sec": 0, "nanosec": 0}
    with RosWriter(str(path)) as writer:
        schema = writer.register_msgdef("foxglove_msgs/msg/CompressedVideo", definition)
        writer.write_message("/camera/head/left/compressed", schema, message, log_time=456, publish_time=123)
    packet = load_episode(path).videos["head_left"][0]
    assert packet.timestamp_ns == expected
    assert packet.timestamp_source == source
