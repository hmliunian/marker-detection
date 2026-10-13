"""The browser transport only exposes saved result JSON and declared previews."""

import http.client
import json
import subprocess
import sys
import threading
from contextlib import contextmanager

import pytest

from marker_detection.web import create_server, main


@pytest.fixture
def output(tmp_path):
    result = tmp_path / "results"
    result.mkdir()
    (result / "frames").mkdir()
    (result / "analysis.json").write_text(
        json.dumps({"episode_id": "试验片段", "primary_hand": "left", "decision": "rejected"}, ensure_ascii=False),
        encoding="utf-8",
    )
    frames = [
        {
            "frame_index": 100,
            "timestamp_ns": 1_790_128_230_663_178_001,
            "time_s": 0.0,
            "xyz_m": [0.1, 0.2, 0.3],
            "valid": True,
            "quality": {"valid": False, "issues": ["speed_exceeded"]},
            "preview": "frames/000100.jpg",
        },
        {"frame_index": 120, "time_s": 0.2, "valid": False, "xyz_m": None, "preview": None},
    ]
    (result / "frames.json").write_text(json.dumps(frames), encoding="utf-8")
    (result / "frames/000100.jpg").write_bytes(b"\xff\xd8\xff\xe0fixture-image")
    (result / "frames/unlisted.jpg").write_bytes(b"private-unlisted-image")
    (result / "private.txt").write_text("not-a-preview", encoding="utf-8")
    return result


@contextmanager
def serving(output):
    server = create_server(output, port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def request(server, path, method="GET", headers=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_json_api_preserves_contract_unicode_nanoseconds_and_original_files(output):
    original = {name: (output / name).read_bytes() for name in ("analysis.json", "frames.json")}
    with serving(output) as server:
        assert server.server_address[0] == "127.0.0.1"
        status, headers, body = request(server, "/api/analysis")
        assert status == 200
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        assert json.loads(body)["episode_id"] == "试验片段"
        assert "Access-Control-Allow-Origin" not in headers
        status, _, body = request(server, "/api/frames")
        assert status == 200
        frames = json.loads(body)
        assert frames[0]["timestamp_ns"] == 1_790_128_230_663_178_001
        assert frames[0]["valid"] is True and frames[0]["quality"]["valid"] is False
        assert frames[1]["xyz_m"] is None
        assert body == original["frames.json"]
    for name, before in original.items():
        assert (output / name).read_bytes() == before


def test_only_manifest_previews_are_served_with_correct_type(output):
    with serving(output) as server:
        status, headers, body = request(server, "/frames/000100.jpg")
        assert status == 200
        assert headers["Content-Type"] == "image/jpeg"
        assert body == (output / "frames/000100.jpg").read_bytes()
        assert request(server, "/frames/unlisted.jpg")[0] == 404
        assert request(server, "/private.txt")[0] == 404
        assert request(server, "/analysis.json")[0] == 404
        assert request(server, "/frames.json")[0] == 404
        assert request(server, "/frames/")[0] == 404


@pytest.mark.parametrize(
    "path",
    [
        "/../private.txt",
        "/frames/../analysis.json",
        "/frames/%2e%2e/analysis.json",
        "/frames/%2e%2e%2fanalysis.json",
        "/frames/%252e%252e/analysis.json",
        "/frames/..%5cprivate.txt",
        "/frames/000100.jpg%00",
    ],
)
def test_traversal_and_invalid_paths_are_not_exposed(output, path):
    with serving(output) as server:
        status, _, body = request(server, path)
        assert status == 404
        assert b"not-a-preview" not in body


def test_allowlisted_preview_symlink_cannot_escape_directory_even_after_startup(output):
    outside = output.parent / "outside.jpg"
    outside.write_bytes(b"outside-secret")
    with serving(output) as server:
        preview = output / "frames/000100.jpg"
        preview.unlink()
        preview.symlink_to(outside)
        status, _, body = request(server, "/frames/000100.jpg")
        assert status == 403
        assert b"outside-secret" not in body


def test_metadata_symlink_cannot_expose_json_outside_results(output):
    outside = output.parent / "outside.json"
    outside.write_text('{"secret":"outside"}')
    (output / "analysis.json").unlink()
    (output / "analysis.json").symlink_to(outside)
    with pytest.raises(ValueError, match="越过"):
        create_server(output, port=0)


def test_untrusted_host_and_write_requests_are_refused(output):
    with serving(output) as server:
        assert request(server, "/api/analysis", headers={"Host": "external.example"})[0] == 403
        assert request(server, "/api/analysis", headers={"Host": "localhost:9999"})[0] == 200
        assert request(server, "/api/analysis", method="POST")[0] == 501


def test_missing_preview_does_not_block_other_diagnostics(output):
    (output / "frames/000100.jpg").unlink()
    with serving(output) as server:
        assert request(server, "/api/frames")[0] == 200
        assert request(server, "/frames/000100.jpg")[0] == 404


def test_static_page_is_served_from_package(output):
    with serving(output) as server:
        status, headers, body = request(server, "/")
        assert status == 200
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        assert b"/plotly.min.js" in body


def test_plotly_is_available_locally_and_head_has_no_body(output):
    with serving(output) as server:
        status, headers, body = request(server, "/plotly.min.js")
        assert status == 200
        assert headers["Content-Type"] == "text/javascript; charset=utf-8"
        assert b"plotly" in body[:500].lower()
        assert len(body) > 100_000
        assert request(server, "/api/frames", method="HEAD")[2] == b""


@pytest.mark.parametrize(
    "data",
    [
        {},
        [1],
        [{"frame_index": 0, "time_s": 0}, {"frame_index": 0, "time_s": 1}],
        [{"frame_index": True, "time_s": 0}],
        [{"frame_index": 0, "time_s": float("nan")}],
        [{"frame_index": 0, "time_s": float("inf")}],
        [{"frame_index": 0, "time_s": "0"}],
        [{"frame_index": 0, "time_s": 0, "timestamp_ns": 123.0}],
        [{"frame_index": 0, "time_s": 0, "preview": "../secret.jpg"}],
        [{"frame_index": 0, "time_s": 0, "preview": "https://external.example/secret.jpg"}],
    ],
)
def test_invalid_results_are_rejected_before_binding(output, data):
    (output / "frames.json").write_text(json.dumps(data))
    with pytest.raises(ValueError):
        create_server(output, port=0)


def test_empty_frames_are_valid_for_an_episode_level_diagnosis(output):
    (output / "frames.json").write_text("[]")
    with serving(output) as server:
        assert json.loads(request(server, "/api/frames")[2]) == []


def test_cli_explains_port_conflict_without_a_traceback(output, capsys):
    with serving(output) as server:
        assert main([str(output), "--port", str(server.server_address[1])]) == 2
    assert "已被占用" in capsys.readouterr().err


def test_cli_interrupt_prints_tunnel_command_and_closes_server(output, monkeypatch, capsys):
    server = create_server(output, port=0)
    port = server.server_address[1]

    def interrupt():
        raise KeyboardInterrupt

    monkeypatch.setattr("marker_detection.web.create_server", lambda *args: server)
    monkeypatch.setattr(server, "serve_forever", interrupt)
    assert main([str(output)]) == 0
    text = capsys.readouterr().out
    assert f"http://127.0.0.1:{port}" in text
    assert f"ssh -N -L {port}:127.0.0.1:{port}" in text
    assert "已关闭" in text
    assert server.socket.fileno() == -1


def test_import_and_cli_help_do_not_load_qt():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import marker_detection.web; "
            "assert not any(name.startswith(('PySide6', 'matplotlib')) for name in sys.modules); "
            "marker_detection.web.main(['--help'])",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "SSH" in result.stdout
