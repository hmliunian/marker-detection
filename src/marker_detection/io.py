"""Read UMI observations without modifying or converting their source files."""

import io
import json
import re
import struct
import zipfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import numpy as np
from mcap.reader import make_reader
from mcap_ros2.decoder import DecoderFactory

from .flatbuffer import FlatbufferSchema
from .models import CameraCalibration, Episode, ScalarSeries, VideoPacket, source_name


def _topic_rules() -> dict[str, tuple[str, str]]:
    rules = {}
    for eye in ("left", "right"):
        key = f"head_{eye}"
        for topic in (
            f"/camera/coracam_head/{eye}_h264/video",
            f"/observation/coracam_head/{eye}_h264/encoded",
            f"/observation/coracam_head/color/{eye}/encoded",
            f"/observation/camera/head/color/{eye}/encoded",
            f"rt/observation/camera/head/color/{eye}/encoded",
            f"/camera/head/{eye}/compressed",
            f"/robot/camera/head/{eye}/compressed",
        ):
            rules[topic] = ("video", key)
        for topic in (
            f"/observation/camera_head_{eye}_camera_info/camera_calibration",
            f"/robot/camera/head/{eye}/camera_info",
            f"/camera/head/{eye}/camera_info",
        ):
            rules[topic] = ("camera", key)
    for side in ("left", "right"):
        for topic in (
            f"/observation/{side}_gripper/gripper/joint_position",
            f"/observation/{side}_gripper/joints",
            f"/robot/{side}_gripper/joint_state",
            f"/arm/{side}/gripper/joint_state",
        ):
            rules[topic] = ("gripper", side)
        for finger in (0, 1):
            for topic in (
                f"/observation/tactile_{side}_gripper_finger{finger}/tactile/tactile_point_cloud2",
                f"/observation/tactile_{side}_gripper_finger{finger}/tactile_point_cloud2",
                f"/observation/tactile_{side}_gripper_finger{finger}/tactile",
                f"/robot/tactile/{side}_gripper/finger{finger}/points",
                f"/arm/{side}/tactile/finger{finger}",
            ):
                rules[topic] = ("tactile", f"{side}_finger{finger}")
    return rules


TOPIC_RULES = _topic_rules()
_TYPES = {
    "flatbuffer": {
        "video": "foxglove.CompressedVideo",
        "camera": "foxglove.CameraCalibration",
        "gripper": "foxglove.JointStates",
        "tactile": "discover.TactileData",
    },
    "cdr": {
        "video": "foxglove_msgs/msg/CompressedVideo",
        "camera": "sensor_msgs/msg/CameraInfo",
        "gripper": "sensor_msgs/msg/JointState",
        "tactile": "sensor_msgs/msg/PointCloud2",
    },
}


def _get(value, key: str, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _json_object(data: bytes | str, label: str) -> dict:
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError(f"JSON must contain an object: {label}")
    return value


def _is_collection(value: dict) -> bool:
    # Filename alone is insufficient: exported analysis JSON shares episode IDs.
    return ("start_time_us" in value and "stop_time_us" in value) or any(
        name in ("camera_head_left", "camera_head_right") and isinstance(info, (dict, str))
        for name, info in value.items()
    )


@contextmanager
def _source(path: Path, sidecar: str | Path | None):
    """Open exactly one MCAP, with same-directory collection/task documents."""
    documents = []
    if sidecar is not None:
        sidecar_path = Path(sidecar).expanduser().resolve()
        documents.append((str(sidecar_path), _json_object(sidecar_path.read_bytes(), str(sidecar_path))))
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            mcaps = [entry for entry in archive.infolist() if entry.filename.endswith(".mcap") and not entry.is_dir()]
            if len(mcaps) != 1:
                raise ValueError(f"ZIP must contain exactly one MCAP; found {len(mcaps)}")
            member = mcaps[0].filename
            parent = PurePosixPath(member).parent
            for entry in archive.infolist():
                name = PurePosixPath(entry.filename)
                if name.parent != parent or name.suffix != ".json" or entry.file_size > 8_000_000:
                    continue
                try:
                    doc = _json_object(archive.read(entry), entry.filename)
                except (ValueError, UnicodeDecodeError):
                    continue
                if name.name == "task.json" or (sidecar is None and _is_collection(doc)):
                    documents.append((f"{path}!{entry.filename}", doc))
            with io.BytesIO(archive.read(member)) as stream:
                yield stream, f"{path}!{member}", path.stem, documents
        return
    for candidate in sorted(path.parent.glob("*.json")):
        if candidate.stat().st_size > 8_000_000:
            continue
        try:
            doc = _json_object(candidate.read_bytes(), str(candidate))
        except (ValueError, UnicodeDecodeError):
            continue
        if candidate.name == "task.json" or (sidecar is None and _is_collection(doc)):
            documents.append((str(candidate), doc))
    fallback_id = path.parent.name if path.stem == "episode" else path.stem
    with path.open("rb") as stream:
        yield stream, str(path), fallback_id, documents


def _flatten(value: dict, prefix: str = "") -> dict:
    result = {}
    for key, child in value.items():
        name = f"{prefix}.{key}" if prefix else key
        result[name] = child
        if isinstance(child, dict):
            result.update(_flatten(child, name))
    return result


def _task_id(value: dict) -> str | None:
    fields = _flatten(value)
    for key in ("task.id", "task.action_id", "action_id", "action.id"):
        candidate = fields.get(key)
        if isinstance(candidate, str) and re.fullmatch(r"A_\d+", candidate):
            return candidate
    steps = _get(value.get("item_info", {}), "steps", [])
    actions = {_get(_get(step, "action", {}), "id") for step in steps}
    actions.discard(None)
    if len(actions) == 1:
        action = actions.pop()
        if isinstance(action, str) and re.fullmatch(r"A_\d+", action):
            return action
    return None


def _calibration(value, source: str) -> CameraCalibration:
    def matrix(name, shape):
        array = np.asarray(_get(value, name, _get(value, name.upper())), dtype=float)
        if array.size != np.prod(shape) or not np.all(np.isfinite(array)):
            raise ValueError(f"Invalid camera {name} in {source}")
        return array.reshape(shape)

    width, height = int(_get(value, "width", 0)), int(_get(value, "height", 0))
    if width <= 0 or height <= 0:
        raise ValueError(f"Invalid camera dimensions in {source}")
    k, r, p = matrix("k", (3, 3)), matrix("r", (3, 3)), matrix("p", (3, 4))
    d = np.asarray(_get(value, "d", _get(value, "D", [])), dtype=float).reshape(-1)
    if not np.all(np.isfinite(d)) or k[0, 0] <= 0 or k[1, 1] <= 0:
        raise ValueError(f"Invalid camera intrinsics in {source}")
    frame_id = _get(value, "frame_id", _get(_get(value, "header", {}), "frame_id", ""))
    return CameraCalibration(width, height, k, d, r, p, frame_id, _get(value, "distortion_model", "plumb_bob"), source)


def _add_camera(episode: Episode, key: str, camera: CameraCalibration):
    previous = episode.cameras.get(key)
    if previous is not None:
        same = (previous.width, previous.height) == (camera.width, camera.height) and all(
            getattr(previous, name).shape == getattr(camera, name).shape
            and np.allclose(getattr(previous, name), getattr(camera, name), atol=1e-8, rtol=1e-8)
            for name in ("k", "d", "r", "p")
        )
        if not same or previous.distortion_model != camera.distortion_model:
            raise ValueError(f"Conflicting or time-varying calibration for {key}: {previous.source}, {camera.source}")
        return
    episode.cameras[key] = camera


def _metadata_cameras(episode: Episode, document: dict, label: str):
    values = dict(document)
    nested = document.get("camera_info")
    if isinstance(nested, dict):
        values.update(nested)
    for name, value in values.items():
        if name not in ("camera_head_left", "camera_head_right"):
            continue
        if isinstance(value, str):
            value = _json_object(value, f"{label}:{name}")
        if not isinstance(value, dict):
            raise ValueError(f"Invalid camera metadata: {label}:{name}")
        expected_key = name.removeprefix("camera_")
        topic = value.get("topic")
        if topic and TOPIC_RULES.get(topic) != ("camera", expected_key):
            raise ValueError(f"Camera metadata key/topic mismatch: {label}:{name}")
        _add_camera(episode, expected_key, _calibration(value, f"{label}:{name}"))


def _timestamp(value, message) -> tuple[int, str]:
    stamp = _get(value, "timestamp")
    source = "payload.timestamp"
    if stamp is None:
        stamp = _get(_get(value, "header", {}), "stamp")
        source = "payload.header.stamp"
    if stamp is not None:
        sec, nanos = _get(stamp, "sec"), _get(stamp, "nanosec", _get(stamp, "nsec"))
        if sec is not None and nanos is not None:
            if not 0 <= int(nanos) < 1_000_000_000:
                raise ValueError("Payload nanosecond component is out of range")
            return int(sec) * 1_000_000_000 + int(nanos), source
    # A real zero stamp remains zero; only an absent stamp falls back.
    if message.publish_time:
        return int(message.publish_time), "mcap.publish_time"
    return int(message.log_time), "mcap.log_time"


def _tactile_force(value, encoding: str) -> float:
    if encoding == "flatbuffer":
        points = _get(value, "points") or []
        if len(points) != 25:
            raise ValueError(f"Expected 25 tactile points, found {len(points)}")
        forces = np.asarray([[_get(point, axis) for axis in ("fx", "fy", "fz")] for point in points], dtype=float)
    else:
        height, width = int(_get(value, "height", 0)), int(_get(value, "width", 0))
        if height * width != 25:
            raise ValueError(f"Expected 25 tactile points, found {height * width}")
        point_step, row_step = int(_get(value, "point_step")), int(_get(value, "row_step"))
        fields = {_get(field, "name"): field for field in _get(value, "fields", [])}
        raw = bytes(_get(value, "data"))
        endian = ">" if _get(value, "is_bigendian", False) else "<"
        if point_step <= 0 or row_step < width * point_step or len(raw) < height * row_step:
            raise ValueError("Truncated tactile PointCloud2 data or invalid stride")
        access = []
        for axis in ("fx", "fy", "fz"):
            field = fields.get(axis)
            datatype = _get(field, "datatype")
            if datatype not in (7, 8) or _get(field, "count") != 1:
                raise ValueError(f"Tactile field {axis} must be one FLOAT32 or FLOAT64")
            offset, size = int(_get(field, "offset")), 4 if datatype == 7 else 8
            if offset < 0 or offset + size > point_step:
                raise ValueError(f"Tactile field {axis} exceeds point_step")
            access.append((offset, endian + ("f" if datatype == 7 else "d")))
        forces = np.asarray(
            [
                [struct.unpack_from(fmt, raw, row * row_step + col * point_step + offset)[0] for offset, fmt in access]
                for row in range(height)
                for col in range(width)
            ]
        )
    # A NaN force is unavailable, never silently turned into a true zero contact.
    return float(np.linalg.norm(forces, axis=1).sum())


def _summary(reader) -> dict:
    summary = reader.get_summary()
    if summary is None or summary.statistics is None:
        channels = {}
        total = 0
        start = end = None
        for schema, channel, message in reader.iter_messages(log_time_order=False):
            info = channels.setdefault(
                channel.id,
                {
                    "id": channel.id,
                    "topic": channel.topic,
                    "message_encoding": channel.message_encoding,
                    "schema": schema.name if schema else None,
                    "schema_encoding": schema.encoding if schema else None,
                    "count": 0,
                    "supported": channel.topic in TOPIC_RULES,
                },
            )
            info["count"] += 1
            total += 1
            start = message.log_time if start is None else min(start, message.log_time)
            end = message.log_time if end is None else max(end, message.log_time)
        return {"channels": list(channels.values()), "message_count": total, "time_range_ns": [start, end]}
    stats = summary.statistics
    return {
        "channels": [
            {
                "id": channel.id,
                "topic": channel.topic,
                "message_encoding": channel.message_encoding,
                "schema": summary.schemas[channel.schema_id].name if channel.schema_id else None,
                "schema_encoding": summary.schemas[channel.schema_id].encoding if channel.schema_id else None,
                "count": stats.channel_message_counts.get(channel.id, 0) if stats else None,
                "supported": channel.topic in TOPIC_RULES,
            }
            for channel in summary.channels.values()
        ],
        "message_count": stats.message_count if stats else None,
        "time_range_ns": [stats.message_start_time, stats.message_end_time] if stats else None,
    }


def load_episode(path: str | Path, sidecar: str | Path | None = None) -> Episode:
    """Load head stereo packets, calibration, gripper width and tactile evidence.

    Sensor values keep their measurement timestamps and source units. Legacy
    width and tactile force units are unknown. Standard CDR gripper width follows
    the documented meter contract. Video packet timestamps use payload stamps.
    """
    path = Path(path).expanduser().resolve()
    with _source(path, sidecar) as (stream, mcap_source, fallback_id, documents):
        reader = make_reader(stream)
        episode = Episode(source=source_name(path), episode_id=fallback_id)
        episode.metadata = _summary(reader)
        episode.metadata.update({"mcap_source": mcap_source, "sidecar_sources": [name for name, _ in documents]})
        metadata_records = []
        for record in reader.iter_metadata():
            label = f"{mcap_source}:metadata:{record.name}"
            metadata_records.append((label, record.metadata))
            _metadata_cameras(episode, record.metadata, label)
        collections = [(name, doc) for name, doc in documents if not name.endswith("task.json")]
        if len(collections) > 1:
            raise ValueError("Multiple collection sidecars found; pass an explicit sidecar path")
        for label, document in collections:
            _metadata_cameras(episode, document, label)
        ordered = collections + [item for item in documents if item not in collections] + metadata_records
        task_ids = [(label, _task_id(document)) for label, document in ordered]
        task_ids = [(label, task) for label, task in task_ids if task]
        if task_ids:
            episode.metadata["task_id_source"], episode.task_id = task_ids[0]
            if len({task for _, task in task_ids}) > 1:
                episode.warnings.append(
                    "Conflicting task IDs: collection sidecar takes precedence; review task metadata"
                )
        for label, document in collections + metadata_records:
            fields = _flatten(document)
            candidate = fields.get("episode.id", fields.get("episode_id"))
            if isinstance(candidate, str) and candidate:
                episode.episode_id = candidate
                episode.metadata["episode_id_source"] = label
                break
        episode.metadata["formats"] = sorted({channel["message_encoding"] for channel in episode.metadata["channels"]})
        timestamp_sources = defaultdict(Counter)
        scalars = defaultdict(list)
        units = {}
        sources = {}
        decoders = {}
        factory = DecoderFactory()
        topic_owners = {}
        relevant_topics = list(TOPIC_RULES)
        for schema, channel, message in reader.iter_messages(topics=relevant_topics, log_time_order=False):
            kind, key = TOPIC_RULES[channel.topic]
            identity = (kind, key)
            previous = topic_owners.setdefault(identity, channel.id)
            if previous != channel.id:
                raise ValueError(f"Multiple channels map to {kind}/{key}; refusing to combine independent streams")
            encoding = channel.message_encoding
            expected = _TYPES.get(encoding, {}).get(kind)
            schema_name = schema.name if schema else None
            if encoding == "cdr" and schema_name and schema_name.count("/") == 1:
                schema_name = schema_name.replace("/", "/msg/")
            if expected is None or schema is None or schema_name != expected:
                raise ValueError(f"Unsupported schema/encoding for {channel.topic}: {schema_name}/{encoding}")
            expected_schema_encoding = "ros2msg" if encoding == "cdr" else "flatbuffer"
            if schema.encoding != expected_schema_encoding:
                raise ValueError(f"Schema encoding mismatch on {channel.topic}: {schema.encoding}")
            if schema.id not in decoders:
                decoders[schema.id] = (
                    FlatbufferSchema(schema.data, schema.name).decode
                    if encoding == "flatbuffer"
                    else factory.decoder_for(encoding, schema)
                )
            decoder = decoders[schema.id]
            if decoder is None:
                raise ValueError(f"No decoder for {channel.topic}")
            try:
                value = decoder(message.data)
                moment, stamp_source = _timestamp(value, message)
                timestamp_sources[channel.topic][stamp_source] += 1
                label = f"{mcap_source}:{channel.topic}"
                if kind == "video":
                    data = bytes(_get(value, "data") or b"")
                    fmt = _get(value, "format")
                    if not data or not fmt:
                        raise ValueError("Empty video data or missing codec")
                    episode.videos.setdefault(key, []).append(VideoPacket(moment, data, fmt, stamp_source))
                elif kind == "camera":
                    _add_camera(episode, key, _calibration(value, label))
                else:
                    if kind == "gripper":
                        if encoding == "flatbuffer":
                            joints = _get(value, "joints") or []
                            positions = [_get(joint, "position") for joint in joints]
                        else:
                            positions = _get(value, "position")
                        if positions is None or len(positions) != 1:
                            raise ValueError("Expected exactly one measured gripper position")
                        measured = float(positions[0]) if positions[0] is not None else float("nan")
                        units[identity] = "m" if encoding == "cdr" else "unknown"
                    else:
                        measured = _tactile_force(value, encoding)
                        units[identity] = "unknown"
                    sources[identity] = label
                    scalars[identity].append((moment, measured))
            except (ValueError, TypeError, AttributeError, struct.error) as exc:
                raise ValueError(f"Cannot read {channel.topic} at log_time={message.log_time}: {exc}") from exc
        for (kind, key), observations in scalars.items():
            times = np.asarray([time for time, _ in observations], dtype=np.int64)
            values = np.asarray([value for _, value in observations], dtype=float)
            order = np.argsort(times, kind="stable")
            series = ScalarSeries(times[order], values[order], units[(kind, key)], sources[(kind, key)])
            destination = episode.grippers if kind == "gripper" else episode.tactile
            destination[key] = series
            if not np.all(np.isfinite(values)):
                episode.warnings.append(f"Non-finite {kind}/{key} values remain unavailable (NaN), not zero")
            if np.any(np.diff(times) <= 0):
                episode.warnings.append(
                    f"Repeated or non-increasing {kind}/{key} measurement timestamps; stable timestamp sort applied"
                )
        for key, packets in episode.videos.items():
            # Keep encoded packet order; sorting an H.264 stream can corrupt it.
            if any(b.timestamp_ns <= a.timestamp_ns for a, b in zip(packets, packets[1:])):
                episode.warnings.append(f"Non-increasing {key} packet timestamps; encoded order preserved")
        episode.metadata["timestamp_sources"] = {topic: dict(counts) for topic, counts in timestamp_sources.items()}
        if any(source.startswith("mcap.") for counts in timestamp_sources.values() for source in counts):
            episode.warnings.append("Some payload timestamps are absent; MCAP timestamp fallback was used")
        for key in ("head_left", "head_right"):
            if key not in episode.videos:
                episode.warnings.append(f"Missing video stream: {key}")
            if key not in episode.cameras:
                episode.warnings.append(f"Missing camera calibration: {key}")
        for side in ("left", "right"):
            if side not in episode.grippers:
                episode.warnings.append(f"Missing gripper stream: {side}")
            for finger in (0, 1):
                key = f"{side}_finger{finger}"
                if key not in episode.tactile:
                    episode.warnings.append(f"Missing tactile stream: {key}")
        if episode.task_id is None:
            episode.warnings.append("Task action ID is unavailable")
        return episode


def inspect_mcap(path: str | Path) -> dict:
    """Return JSON-safe encoding, calibration and observation coverage facts."""
    episode = load_episode(path)

    def scalar_info(series):
        return {
            "count": len(series.values),
            "unit": series.unit,
            "source": series.source,
            "time_range_ns": [int(series.timestamps_ns[0]), int(series.timestamps_ns[-1])],
            "nonfinite_count": int(np.count_nonzero(~np.isfinite(series.values))),
        }

    return {
        "source": episode.source,
        "episode_id": episode.episode_id,
        "task_id": episode.task_id,
        **episode.metadata,
        "cameras": {
            name: {
                "width": camera.width,
                "height": camera.height,
                "k": camera.k.tolist(),
                "d": camera.d.tolist(),
                "r": camera.r.tolist(),
                "p": camera.p.tolist(),
                "frame_id": camera.frame_id,
                "distortion_model": camera.distortion_model,
                "source": camera.source,
            }
            for name, camera in episode.cameras.items()
        },
        "videos": {
            key: {
                "count": len(packets),
                "formats": sorted({packet.format for packet in packets}),
                "time_range_ns": [packets[0].timestamp_ns, packets[-1].timestamp_ns],
            }
            for key, packets in episode.videos.items()
        },
        "grippers": {key: scalar_info(series) for key, series in episode.grippers.items()},
        "tactile": {key: scalar_info(series) for key, series in episode.tactile.items()},
        "warnings": episode.warnings,
    }
