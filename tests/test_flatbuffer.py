"""Small independent BFBS fixtures exercise the binary reader offline."""

import flatbuffers
import pytest

from marker_detection.flatbuffer import FlatbufferSchema


def reflection_schema(name="foxglove.CompressedVideo"):
    """Make a tiny schema with a timestamp struct, bytes, text and optional float."""
    builder = flatbuffers.Builder(512)

    def field(label, offset, base, element=0, index=-1, optional=False):
        text = builder.CreateString(label)
        builder.StartObject(6)
        builder.PrependUint8Slot(0, base, 0)
        builder.PrependUint8Slot(1, element, 0)
        builder.PrependInt32Slot(2, index, -1)
        typ = builder.EndObject()
        builder.StartObject(12)
        builder.PrependUOffsetTRelativeSlot(0, text, 0)
        builder.PrependUOffsetTRelativeSlot(1, typ, 0)
        builder.PrependUint16Slot(3, offset, 0)
        builder.PrependBoolSlot(11, optional, False)
        return builder.EndObject()

    def obj(label, fields, is_struct=False, size=0):
        text = builder.CreateString(label)
        builder.StartVector(4, len(fields), 4)
        for item in reversed(fields):
            builder.PrependUOffsetTRelative(item)
        vector = builder.EndVector()
        builder.StartObject(5)
        builder.PrependUOffsetTRelativeSlot(0, text, 0)
        builder.PrependUOffsetTRelativeSlot(1, vector, 0)
        builder.PrependBoolSlot(2, is_struct, False)
        builder.PrependInt32Slot(4, size, 0)
        return builder.EndObject()

    timestamp = obj("test.Timestamp", [field("sec", 0, 9), field("nsec", 8, 8)], True, 16)
    root = obj(
        name,
        [
            field("timestamp", 4, 15, index=1),
            field("data", 6, 14, element=4),
            field("format", 8, 13),
            field("frame_id", 10, 13),
            field("optional_value", 12, 12, optional=True),
        ],
    )
    builder.StartVector(4, 2, 4)
    builder.PrependUOffsetTRelative(timestamp)
    builder.PrependUOffsetTRelative(root)
    objects = builder.EndVector()
    builder.StartObject(5)
    builder.PrependUOffsetTRelativeSlot(0, objects, 0)
    builder.PrependUOffsetTRelativeSlot(4, root, 0)
    schema = builder.EndObject()
    builder.Finish(schema, file_identifier=b"BFBS")
    return bytes(builder.Output())


def video_payload(sec=1_790_128_230, nsec=663_178_001, optional=None):
    builder = flatbuffers.Builder(128)
    data = builder.CreateByteVector(b"\x00\x00\x00\x01\x65\x00\x01")
    fmt = builder.CreateString("h264")
    frame = builder.CreateString("camera_head_left")
    builder.StartObject(5)
    builder.Prep(8, 16)
    builder.Pad(4)
    builder.PrependUint32(nsec)
    builder.PrependInt64(sec)
    stamp = builder.Offset()
    builder.PrependStructSlot(0, stamp, 0)
    builder.PrependUOffsetTRelativeSlot(1, data, 0)
    builder.PrependUOffsetTRelativeSlot(2, fmt, 0)
    builder.PrependUOffsetTRelativeSlot(3, frame, 0)
    if optional is not None:
        builder.PrependFloat64Slot(4, optional, None)
    root = builder.EndObject()
    builder.Finish(root)
    return bytes(builder.Output())


def test_struct_time_binary_data_and_missing_optional_are_distinct():
    decoder = FlatbufferSchema(reflection_schema(), "foxglove.CompressedVideo")
    value = decoder.decode(video_payload())
    assert value["timestamp"] == {"sec": 1_790_128_230, "nsec": 663_178_001}
    assert value["data"] == b"\x00\x00\x00\x01\x65\x00\x01"
    assert value["format"] == "h264"
    assert value["optional_value"] is None
    assert decoder.decode(video_payload(optional=0.0))["optional_value"] == 0.0


def test_declared_root_must_match_embedded_schema():
    with pytest.raises(ValueError, match="root type"):
        FlatbufferSchema(reflection_schema(), "discover.TactileData")


@pytest.mark.parametrize("bad", [b"", b"invalid schema", b"\xff\xff\xff\xffBFBS"])
def test_invalid_schema_is_not_guessed(bad):
    with pytest.raises(ValueError):
        FlatbufferSchema(bad, "foxglove.CompressedVideo")


@pytest.mark.parametrize("bad", [b"", b"\xff\xff\xff\xff", b"\x04\x00\x00\x00"])
def test_truncated_payload_is_rejected(bad):
    decoder = FlatbufferSchema(reflection_schema(), "foxglove.CompressedVideo")
    with pytest.raises(ValueError):
        decoder.decode(bad)
