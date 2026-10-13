"""Explicit Firefox integration check; separate from the offline unit suite."""

import argparse
import json
import shutil
import threading
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support.ui import WebDriverWait

from marker_detection.web import create_server


def main() -> int:
    parser = argparse.ArgumentParser(description="实际 Firefox 浏览器检查本地轨迹网页")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--screenshot", type=Path, required=True)
    parser.add_argument("--frame", type=int, default=0)
    args = parser.parse_args()
    executable = shutil.which("geckodriver")
    if not executable or not shutil.which("firefox"):
        parser.error("本检查需要现有 firefox 和 geckodriver，不自动下载浏览器")
    expected_frames = json.loads((args.directory / "frames.json").read_text())
    expected_analysis = json.loads((args.directory / "analysis.json").read_text())
    index = next(i for i, frame in enumerate(expected_frames) if frame["frame_index"] == args.frame)
    profile_root = Path(".tools/firefox-profiles").resolve()
    profile_root.mkdir(parents=True, exist_ok=True)
    options = Options()
    options.add_argument("-headless")
    # Keep profile files visible to a Firefox installed through snap.
    service = Service(executable_path=executable, service_args=["--profile-root", str(profile_root)])
    server = create_server(args.directory, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    browser = None
    try:
        browser = webdriver.Firefox(service=service, options=options)
        browser.set_window_size(1500, 1150)
        browser.get(f"http://127.0.0.1:{server.server_address[1]}/")
        wait = WebDriverWait(browser, 30)
        wait.until(lambda driver: driver.execute_script("return document.body.dataset.ready === 'true'"))
        assert (
            browser.execute_script("return window.markerReview.analysis.episode_id") == expected_analysis["episode_id"]
        )
        assert browser.execute_script("return window.markerReview.frames.length") == len(expected_frames)
        # Real controls must navigate, including the raw original frame number.
        if len(expected_frames) > 1:
            browser.find_element(By.ID, "next-frame").click()
            wait.until(lambda driver: driver.execute_script("return window.markerReview.index") == 1)
            browser.find_element(By.ID, "previous-frame").click()
            wait.until(lambda driver: driver.execute_script("return window.markerReview.index") == 0)
        browser.execute_script(
            "const input=document.getElementById('frame-number'); input.value=arguments[0];"
            "input.dispatchEvent(new Event('change',{bubbles:true}));",
            args.frame,
        )
        wait.until(lambda driver: driver.execute_script("return window.markerReview.index") == index)
        assert int(browser.find_element(By.ID, "frame-slider").get_attribute("value")) == index
        shown_time = float(browser.find_element(By.ID, "frame-time").get_attribute("value"))
        assert abs(shown_time - expected_frames[index]["time_s"]) < 0.002
        # Verify preview has really loaded and plots have no invented bridging.
        wait.until(
            lambda driver: driver.execute_script(
                "return [...document.images].some(img => img.complete && img.naturalWidth > 0 "
                "&& new URL(img.src).pathname.endsWith('/' + arguments[0]))",
                expected_frames[index]["preview"],
            )
        )
        assert browser.execute_script("return document.getElementById('plot-time').data.length") >= 3
        renderer = browser.execute_script("return document.getElementById('plot-3d').dataset.renderer")
        assert renderer in {"canvas", "webgl"}, "A rendered 3D view is required; a WebGL error is not a pass"
        if renderer == "canvas":
            canvas = browser.find_element(By.CSS_SELECTOR, "#plot-3d canvas")
            colored_pixels = browser.execute_script(
                "const c=arguments[0], pixels=c.getContext('2d').getImageData(0,0,c.width,c.height).data;"
                "let count=0; for(let i=0;i<pixels.length;i+=4) "
                "if(pixels[i+3]>0 && Math.max(pixels[i],pixels[i+1],pixels[i+2])-"
                "Math.min(pixels[i],pixels[i+1],pixels[i+2])>30) count++; return count;",
                canvas,
            )
            assert colored_pixels > 100, "3D axes and trajectory must actually be drawn"
            original_view = browser.execute_script("return arguments[0].toDataURL()", canvas)
            ActionChains(browser).move_to_element(canvas).click_and_hold().move_by_offset(60, 30).release().perform()
            wait.until(lambda driver: driver.execute_script("return arguments[0].toDataURL()", canvas) != original_view)
        bad_connections = browser.execute_script(
            "return [...document.querySelectorAll('.js-plotly-plot')].flatMap(p => p.data || [])"
            ".some(trace => trace.connectgaps === true)"
        )
        assert not bad_connections, "Missing observations must not be connected"
        invalid = [
            i
            for i, frame in enumerate(expected_frames)
            if not (frame.get("valid", False) and frame.get("quality", {}).get("valid", True))
        ]
        time_curves = browser.execute_script(
            "return [...document.querySelectorAll('.js-plotly-plot')].flatMap(p => p.data || [])"
            ".filter(t => t.type !== 'scatter3d' && t.y?.length === arguments[0] "
            "&& t.mode?.includes('lines')).map(t => t.y)",
            len(expected_frames),
        )
        assert len(time_curves) == 3, "Expected an X/Y/Z curve for the complete frame axis"
        assert all(curve[i] is None for curve in time_curves for i in invalid), "Invalid frames must break XYZ lines"
        if invalid:
            browser.execute_script("return window.markerReview.selectFrame(0)")
            browser.find_element(By.ID, "next-invalid").click()
            destination = next((i for i in invalid if i > 0), invalid[0])
            wait.until(lambda driver: driver.execute_script("return window.markerReview.index") == destination)
            browser.execute_script("return window.markerReview.selectFrame(arguments[0])", invalid[-1])
            browser.find_element(By.ID, "next-invalid").click()
            wait.until(lambda driver: driver.execute_script("return window.markerReview.index") == invalid[0])
        # WebDriver awaits the returned Promise, including image and plot updates.
        browser.execute_script("return window.markerReview.selectFrame(arguments[0])", index)
        args.screenshot.parent.mkdir(parents=True, exist_ok=True)
        browser.save_screenshot(str(args.screenshot.resolve()))
        assert args.screenshot.stat().st_size > 20_000
        print(
            json.dumps(
                {
                    "browser": browser.capabilities.get("browserVersion"),
                    "episode_id": expected_analysis["episode_id"],
                    "frames": len(expected_frames),
                    "selected_frame": args.frame,
                    "invalid_frames": len(invalid),
                    "renderer": renderer,
                    "screenshot": str(args.screenshot.resolve()),
                },
                ensure_ascii=False,
            )
        )
    finally:
        if browser is not None:
            browser.quit()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
