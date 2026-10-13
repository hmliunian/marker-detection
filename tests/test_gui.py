"""Check real Qt frame navigation and missing-observation rendering offscreen."""

import json
import os
import subprocess
import sys

import numpy as np
import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from marker_detection import gui
from marker_detection.gui import AnalysisWindow, load_analysis


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    owns_application = QApplication.instance() is None
    app = QApplication.instance() or QApplication([])
    yield app
    if owns_application:
        app.closeAllWindows()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.shutdown()


@pytest.fixture
def output(tmp_path):
    analysis = {
        "episode_id": "gui-contract-example",
        "source": "fixture.mcap",
        "primary_hand": "left",
        "decision": "rejected",
        "reasons": ["speed_exceeded"],
        "summary": {"longest_gap_s": 0.4, "max_speed_m_s": 0.3, "max_raw_speed_m_s": 3.0},
        "config": {"max_speed_m_s": 1.5},
    }
    frames = []
    for index, frame_id in enumerate((10, 20, 30, 40)):
        frames.append(
            {
                "frame_index": frame_id,
                "timestamp_ns": 1_700_000_000_000_000_003 + index * 200_000_000,
                "time_s": index * 0.2,
                "xyz_m": [index * 0.001, 0.02, 0.3],
                "raw_xyz_m": None,
                "valid": True,
                "reason": "ok",
                "marker_ids": [0],
                "detections": [
                    {
                        "id": 0,
                        "corners": [[20, 30], [80, 30], [80, 90], [20, 90]],
                        "selected": True,
                    }
                ],
                "preview": "frames/000000.png",
                "quality": {"valid": True, "issues": []},
            }
        )
    frames[1].update(xyz_m=None, valid=False, reason="no_marker", preview=None, detections=[])
    frames[2].update(
        xyz_m=[0.6, 0.02, 0.3],
        quality={"valid": False, "issues": ["speed_exceeded"], "raw_speed_m_s": 3.0},
        reprojection_error_px=12.5,
    )
    (tmp_path / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
    (tmp_path / "frames.json").write_text(json.dumps(frames), encoding="utf-8")
    (tmp_path / "frames").mkdir()
    image = QImage(320, 180, QImage.Format.Format_RGB32)
    image.fill(QColor("#cadce3"))
    assert image.save(str(tmp_path / "frames/000000.png"))
    return tmp_path


def test_frame_navigation_links_image_coordinates_and_original_frame_number(qt_app, output):
    window = AnalysisWindow(output)
    window.show()
    qt_app.processEvents()
    try:
        assert window.current_index == 0
        assert window.frame_spin.value() == 10
        assert not window.preview.original.isNull()
        assert "左手" in window.hand_label.text()
        assert "不合格" in window.decision_label.text()
        metrics = [label.text() for label in window.findChildren(QLabel) if "原始最大相对速度" in label.text()]
        assert len(metrics) == 1 and "3.000 m/s" in metrics[0]

        window.slider.setValue(1)
        assert window.current_index == 1
        assert window.frame_spin.value() == 20
        assert window.time_spin.value() == 0.2
        assert window.preview.original.isNull()
        assert "XYZ 缺失" in window.frame_status.text()
        assert len(window.current_point.get_data_3d()[0]) == 0
        for cursor in window.time_cursors:
            assert cursor.get_xdata() == [0.2, 0.2]

        QTest.mouseClick(window.frame_buttons[1], Qt.MouseButton.LeftButton)
        assert window.frame_spin.value() == 30
        assert "未接受" in window.frame_status.text()
        assert "速度超过阈值" in window.frame_status.text()
        assert "原始相对速度 3.000 m/s" in window.frame_status.text()
        assert "重投影误差 12.500 px" in window.frame_status.text()
        assert window.current_point.get_data_3d()[0] == [0.6]

        window.time_spin.setValue(0.59)
        assert window.current_index == 3
        assert window.frame_spin.value() == 40
        window.set_frame_by_number(10)
        assert window.current_index == 0
        assert "1700000000000000003" in window.frame_status.toolTip()
        with pytest.raises(ValueError, match="没有原始帧号"):
            window.set_frame_by_number(11)
    finally:
        window.close()


def test_missing_and_quality_rejected_observations_break_every_trajectory(qt_app, output):
    window = AnalysisWindow(output)
    try:
        assert window.valid.tolist() == [True, False, False, True]
        for line in window.time_lines:
            assert np.isnan(line.get_ydata()[1:3]).all()
            assert np.isfinite(line.get_ydata()[[0, 3]]).all()
        for coordinate in window.trajectory_line.get_data_3d():
            assert np.isnan(coordinate[1:3]).all()
        window._next_invalid()
        assert window.frame_spin.value() == 20
        window._next_invalid()
        assert window.frame_spin.value() == 30
        assert window.raw_xyz[2, 0] == 0.6
    finally:
        window.close()


@pytest.mark.parametrize("frame_number", (10, 20, 30))
def test_cli_captures_real_widget_at_requested_original_frame(output, frame_number):
    screenshot = output / "gui.png"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", MPLCONFIGDIR=str(output / "mpl"))
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "marker_detection.gui",
            str(output),
            "--frame",
            str(frame_number),
            "--screenshot",
            str(screenshot),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    image = QImage(str(screenshot))
    assert not image.isNull()
    assert image.width() >= 980 and image.height() >= 740


def test_empty_results_show_episode_decision(qt_app, output):
    (output / "frames.json").write_text("[]", encoding="utf-8")
    window = AnalysisWindow(output)
    try:
        assert not window.slider.isEnabled()
        assert "没有视频帧" in window.frame_status.text()
        assert "不合格" in window.decision_label.text()
    finally:
        window.close()


def test_rejects_duplicate_frame_numbers(output):
    path = output / "frames.json"
    frames = json.loads(path.read_text(encoding="utf-8"))
    frames[1]["frame_index"] = frames[0]["frame_index"]
    path.write_text(json.dumps(frames), encoding="utf-8")
    with pytest.raises(ValueError, match="必须唯一"):
        load_analysis(output)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux display discovery")
def test_native_cli_headless_session_returns_helpful_error_instead_of_qt_abort(output):
    env = dict(os.environ, MPLCONFIGDIR=str(output / "mpl"))
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM"):
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-m", "marker_detection.gui", str(output)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert "DISPLAY/WAYLAND_DISPLAY" in result.stderr
    assert "just gui" in result.stderr and "浏览器" in result.stderr
    assert "SSH 端口转发" in result.stderr
    assert "could not be initialized" not in result.stderr
    assert "Traceback" not in result.stderr
    assert not result.stdout


def test_xcb_missing_library_is_reported_before_application_creation(monkeypatch, output, capsys):
    class NewApplication:
        @staticmethod
        def instance():
            return None

        def __init__(self, *args):
            pytest.fail("Qt must not be started after its platform dependency check failed")

    attempted = []

    def missing_library(path):
        attempted.append(path)
        raise OSError("libxcb-cursor.so.0: cannot open shared object file")

    monkeypatch.setattr(gui.sys, "platform", "linux")
    monkeypatch.setattr(gui, "QApplication", NewApplication)
    monkeypatch.setattr(gui.ctypes, "CDLL", missing_library)
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM", "xcb")
    assert gui.main([str(output)]) == 2
    capture = capsys.readouterr()
    assert len(attempted) == 1 and attempted[0].endswith("/platforms/libqxcb.so")
    assert "libxcb-cursor.so.0" in capture.err
    assert "sudo apt install libxcb-cursor0" in capture.err
    assert "just gui" in capture.err


def test_existing_application_is_reused_without_checking_its_old_display_environment(qt_app, monkeypatch):
    monkeypatch.setattr(gui.sys, "platform", "linux")
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gui.ctypes, "CDLL", lambda path: pytest.fail("An existing Qt application needs no reload"))
    assert gui._platform_error() is None


@pytest.mark.parametrize(
    ("system", "platform", "display", "wayland"),
    [
        ("linux", "offscreen", None, None),
        ("linux", "minimal", None, None),
        ("linux", "wayland", None, "wayland-0"),
        ("linux", None, None, "wayland-0"),
        ("darwin", None, None, None),
        ("win32", None, None, None),
    ],
)
def test_other_platforms_do_not_load_xcb_dependencies(monkeypatch, system, platform, display, wayland):
    class NewApplication:
        @staticmethod
        def instance():
            return None

    monkeypatch.setattr(gui.sys, "platform", system)
    monkeypatch.setattr(gui, "QApplication", NewApplication)
    monkeypatch.setattr(gui.ctypes, "CDLL", lambda path: pytest.fail("This platform does not use xcb"))
    for name, value in (("QT_QPA_PLATFORM", platform), ("DISPLAY", display), ("WAYLAND_DISPLAY", wayland)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert gui._platform_error() is None
