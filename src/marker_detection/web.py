"""Loopback-only, read-only browser viewer for saved trajectory results.

This module intentionally has no Qt dependency. Static assets and Plotly are
served locally so an SSH tunnel is sufficient to review existing results.
"""

from __future__ import annotations

import argparse
import errno
import json
import math
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import unquote, urlsplit

_PREVIEW_PATH = re.compile(r"frames/[A-Za-z0-9_-]+\.(?:jpg|jpeg|png)\Z", re.IGNORECASE)
_LOCAL_HOST = re.compile(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?\Z", re.IGNORECASE)
_IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def _within_directory(directory: Path, relative: str) -> Path:
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory):
        raise ValueError(f"分析产物路径越过结果目录：{relative}")
    return path


def _read_json(directory: Path, name: str) -> tuple[bytes, object]:
    path = _within_directory(directory, name)
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"))
    # This also rejects 1e999, which JSON parsers otherwise turn into infinity.
    json.dumps(value, allow_nan=False)
    return raw, value


def _load_results(directory: Path) -> tuple[bytes, bytes, set[str]]:
    analysis_raw, analysis = _read_json(directory, "analysis.json")
    frames_raw, frames = _read_json(directory, "frames.json")
    if not isinstance(analysis, dict) or not isinstance(frames, list):
        raise ValueError("analysis.json 应为对象，frames.json 应为逐帧列表")
    seen = set()
    previews = set()
    for row in frames:
        if not isinstance(row, dict):
            raise ValueError("frames.json 含有非对象帧")
        frame_id = row.get("frame_index")
        if type(frame_id) is not int or frame_id < 0 or frame_id in seen:
            raise ValueError("frames.json 的 frame_index 必须是唯一的非负整数")
        seen.add(frame_id)
        time_s = row.get("time_s")
        if type(time_s) not in (int, float) or not math.isfinite(time_s):
            raise ValueError("frames.json 的 time_s 必须为有限数值")
        if "timestamp_ns" in row and type(row["timestamp_ns"]) is not int:
            raise ValueError("frames.json 的 timestamp_ns 必须是整数纳秒")
        if "valid" in row and not isinstance(row["valid"], bool):
            raise ValueError("frames.json 的 valid 必须是布尔值")
        if "quality" in row and not isinstance(row["quality"], dict):
            raise ValueError("frames.json 的 quality 必须是对象")
        preview = row.get("preview")
        if preview is None:
            continue
        if not isinstance(preview, str) or not _PREVIEW_PATH.fullmatch(preview):
            raise ValueError("帧 preview 必须是 frames/ 下的 JPG 或 PNG 文件名")
        previews.add("/" + preview)
    return analysis_raw, frames_raw, previews


class ReviewServer(ThreadingHTTPServer):
    """A snapshot of validated JSON and an allowlist of source-frame previews."""

    daemon_threads = True

    def __init__(self, directory: Path, port: int):
        self.directory = directory
        self.analysis_raw, self.frames_raw, self.previews = _load_results(directory)
        self.plotly_script: bytes | None = None
        super().__init__(("127.0.0.1", port), _RequestHandler)


class _RequestHandler(BaseHTTPRequestHandler):
    server: ReviewServer

    def log_message(self, format: str, *args) -> None:
        # Scrubbing through frames should not flood a remote SSH terminal.
        pass

    def _send(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _error(self, status: int, message: str) -> None:
        self._send(
            status,
            json.dumps({"error": message}, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not _LOCAL_HOST.fullmatch(self.headers.get("Host", "")):
            self._error(403, "仅接受 localhost 或 127.0.0.1 的访问")
            return
        try:
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc:
                raise ValueError("Unsupported request target")
            path = unquote(parsed.path, errors="strict")
        except (ValueError, UnicodeDecodeError):
            self._error(400, "无效请求路径")
            return
        if path == "/api/analysis":
            self._send(200, self.server.analysis_raw, "application/json; charset=utf-8")
        elif path == "/api/frames":
            self._send(200, self.server.frames_raw, "application/json; charset=utf-8")
        elif path == "/":
            try:
                html = resources.files("marker_detection").joinpath("web_view.html").read_bytes()
            except (OSError, ModuleNotFoundError):
                self._error(503, "网页资源缺失，请运行 just setup")
                return
            self._send(200, html, "text/html; charset=utf-8")
        elif path == "/plotly.min.js":
            if self.server.plotly_script is None:
                try:
                    from plotly.offline import get_plotlyjs

                    self.server.plotly_script = get_plotlyjs().encode("utf-8")
                except ImportError:
                    self._error(503, "Plotly 未安装，请运行 just setup")
                    return
            self._send(200, self.server.plotly_script, "text/javascript; charset=utf-8")
        elif path in self.server.previews:
            try:
                # Resolve again at request time, so changing a preview to an
                # outside symlink after startup cannot expose other files.
                target = _within_directory(self.server.directory, path.removeprefix("/"))
                data = target.read_bytes()
            except ValueError:
                self._error(403, "预览路径越过结果目录")
                return
            except OSError:
                self._error(404, "此帧没有可用的图像预览")
                return
            self._send(200, data, _IMAGE_TYPES[Path(path).suffix.lower()])
        else:
            self._error(404, "资源不存在")


def create_server(directory: str | Path, port: int = 8765) -> ReviewServer:
    """Create an unstarted loopback server; use port=0 for an ephemeral test port.

    Call serve_forever() to run it and shutdown()/server_close() when finished.
    Analysis files are validated and loaded once; the originals stay untouched.
    """
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("端口必须在 0 到 65535 之间")
    directory = Path(directory).expanduser().resolve(strict=True)
    if not directory.is_dir():
        raise ValueError("结果路径必须是包含 analysis.json 和 frames.json 的目录")
    return ReviewServer(directory, port)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="通过 SSH 端口转发查看本地轨迹；仅监听 127.0.0.1")
    parser.add_argument("directory", help="包含 analysis.json 与 frames.json 的结果目录")
    parser.add_argument("--port", type=int, default=8765, help="本机监听端口（默认 8765）")
    args = parser.parse_args(argv)
    try:
        server = create_server(args.directory, args.port)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(f"端口 {args.port} 已被占用，请用 --port 指定其他端口。", file=sys.stderr)
        else:
            print(f"无法打开分析结果或启动查看器：{exc}", file=sys.stderr)
        return 2
    except (ValueError, OverflowError) as exc:
        print(f"分析结果或端口无效：{exc}", file=sys.stderr)
        return 2
    port = server.server_address[1]
    print(f"轨迹查看器：http://127.0.0.1:{port}", flush=True)
    print(f"在你的电脑运行：ssh -N -L {port}:127.0.0.1:{port} <用户>@<服务器>", flush=True)
    print(f"然后在本地浏览器打开 http://localhost:{port}；按 Ctrl+C 关闭服务。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n轨迹查看器已关闭。", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
