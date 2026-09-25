"""Cached MapLibre screenshots for immutable map-change feed cards."""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import urlopen
from urllib.parse import quote

from PIL import Image, ImageStat
from websocket import WebSocketTimeoutException, create_connection

log = logging.getLogger(__name__)

RENDER_VERSION = "v7"
DEFAULT_ROOT = Path(__file__).parent / "data" / "change_thumbnails"
READY_EXPRESSION = """JSON.stringify({
  ready: document.documentElement.dataset.renderReady === 'true',
  error: document.body.dataset.renderError || null
})"""


class _DevToolsPage:
    """Minimal Chrome DevTools Protocol client for one local page."""

    def __init__(self, websocket_url: str):
        self.socket = create_connection(websocket_url, timeout=1, origin="http://127.0.0.1")
        self._message_id = 0

    def close(self) -> None:
        self.socket.close()

    def call(self, method: str, params: dict | None = None, *, deadline: float) -> dict:
        self._message_id += 1
        message_id = self._message_id
        self.socket.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while time.monotonic() < deadline:
            try:
                response = json.loads(self.socket.recv())
            except WebSocketTimeoutException:
                continue
            if response.get("id") != message_id:
                continue
            if "error" in response:
                raise RuntimeError(f"Chrome DevTools {method} failed: {response['error']}")
            return response.get("result", {})
        raise TimeoutError(f"Chrome DevTools timed out during {method}")


class ChangeThumbnailRenderer:
    """Render one immutable WebP per change area, with process-wide deduping."""

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        base_url: str | None = None,
        browser: str | None = None,
        timeout: float = 45.0,
    ):
        self.root = Path(root or os.getenv("WARDOTFUN_CHANGE_THUMBNAIL_DIR") or DEFAULT_ROOT)
        self.base_url = (base_url or os.getenv("WARDOTFUN_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
        self.browser = browser or os.getenv("WARDOTFUN_CHROME_BIN") or self._find_browser()
        self.timeout = timeout
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="change-thumbnail")
        self._pending: set[str] = set()
        self._pending_lock = threading.Lock()

    @staticmethod
    def _find_browser() -> str | None:
        for candidate in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        return None

    @staticmethod
    def _area_id(value: str) -> str:
        return str(uuid.UUID(str(value)))

    def cached_path(self, area_id: str) -> Path:
        return self.root / RENDER_VERSION / f"{self._area_id(area_id)}.webp"

    def etag(self, area_id: str) -> str:
        material = f"wardotfun:map-change-thumbnail:{RENDER_VERSION}:{self._area_id(area_id)}"
        return '"' + hashlib.sha256(material.encode()).hexdigest() + '"'

    def enqueue(self, area_id: str) -> bool:
        area_id = self._area_id(area_id)
        if self.cached_path(area_id).is_file():
            return False
        with self._pending_lock:
            if area_id in self._pending:
                return False
            self._pending.add(area_id)
        self._executor.submit(self._render_pending, area_id)
        return True

    def _render_pending(self, area_id: str) -> None:
        try:
            self.render(area_id)
        except Exception:
            log.exception("Map-change thumbnail render failed for %s", area_id)
        finally:
            with self._pending_lock:
                self._pending.discard(area_id)

    def close(self, *, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=not wait)

    def render(self, area_id: str, *, force: bool = False) -> Path:
        area_id = self._area_id(area_id)
        destination = self.cached_path(area_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        lock_path = destination.parent / ".render.lock"
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if destination.is_file() and not force:
                return destination
            if not self.browser:
                raise RuntimeError("Chrome/Chromium is not installed")
            with tempfile.TemporaryDirectory(
                prefix="wardotfun-thumbnail-",
                dir=destination.parent,
                ignore_cleanup_errors=True,
            ) as temporary:
                temporary_path = Path(temporary)
                png = temporary_path / "capture.png"
                profile = temporary_path / "chrome-profile"
                target = (
                    f"{self.base_url}/change-thumbnail.html?area="
                    f"{quote(area_id, safe='')}"
                )
                self._capture_png(target, png, profile)
                with Image.open(png) as captured:
                    captured.load()
                    if captured.size != (768, 432):
                        raise RuntimeError(f"unexpected screenshot dimensions: {captured.size}")
                    variation = sum(ImageStat.Stat(captured.convert("RGB")).stddev)
                    if variation < 18:
                        raise RuntimeError("thumbnail scene did not finish rendering")
                    webp = temporary_path / "capture.webp"
                    captured.convert("RGB").save(webp, "WEBP", quality=84, method=6)
                webp.replace(destination)
                os.chmod(destination, 0o644)
                log.info("Rendered map-change thumbnail %s", destination)
                return destination

    def _capture_png(self, target: str, png: Path, profile: Path) -> None:
        """Wait for MapLibre's tile-ready signal, then capture through CDP."""
        stderr_path = profile.parent / "chrome.stderr"
        command = [
            self.browser,
            "--headless=new",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--hide-scrollbars",
            "--enable-unsafe-swiftshader",
            "--use-angle=swiftshader",
            "--run-all-compositor-stages-before-draw",
            "--force-device-scale-factor=1",
            "--window-size=768,432",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile}",
            target,
        ]
        deadline = time.monotonic() + self.timeout
        with stderr_path.open("wb") as stderr:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=stderr)
            page = None
            try:
                websocket_url = self._page_websocket(profile, target, process, deadline)
                page = _DevToolsPage(websocket_url)
                page.call("Runtime.enable", deadline=deadline)
                page.call("Page.enable", deadline=deadline)
                page.call(
                    "Emulation.setDeviceMetricsOverride",
                    {"width": 768, "height": 432, "deviceScaleFactor": 1, "mobile": False},
                    deadline=deadline,
                )
                self._wait_and_capture(page, png, deadline)
            finally:
                if page is not None:
                    page.close()
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        if not png.is_file():
            error = stderr_path.read_text(errors="replace")[-1200:]
            raise RuntimeError(f"headless Chrome did not capture the scene: {error}")

    @staticmethod
    def _page_websocket(profile: Path, target: str, process, deadline: float) -> str:
        port_file = profile / "DevToolsActivePort"
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"headless Chrome exited before capture ({process.returncode})")
            if port_file.is_file():
                port = port_file.read_text().splitlines()[0]
                try:
                    with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as response:
                        pages = json.load(response)
                    for page in pages:
                        if page.get("type") == "page" and page.get("url", "").startswith(target):
                            return page["webSocketDebuggerUrl"]
                except (OSError, ValueError, IndexError, KeyError):
                    pass
            time.sleep(0.05)
        raise TimeoutError("headless Chrome page did not become available")

    @staticmethod
    def _wait_and_capture(page: _DevToolsPage, png: Path, deadline: float) -> None:
        while time.monotonic() < deadline:
            result = page.call(
                "Runtime.evaluate",
                {"expression": READY_EXPRESSION, "returnByValue": True},
                deadline=deadline,
            )
            raw_status = result.get("result", {}).get("value")
            if raw_status:
                status = json.loads(raw_status)
                if status.get("error"):
                    raise RuntimeError(f"thumbnail scene failed: {status['error']}")
                if status.get("ready"):
                    # Give the canvas two real compositor frames after MapLibre's idle event.
                    time.sleep(0.1)
                    capture = page.call(
                        "Page.captureScreenshot",
                        {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
                        # Tile loading may legitimately consume the full scene
                        # budget. Once ready, give Chrome a separate window to
                        # read back the already-rendered canvas.
                        deadline=time.monotonic() + 10,
                    )
                    png.write_bytes(base64.b64decode(capture["data"]))
                    return
            time.sleep(0.1)
        raise TimeoutError("thumbnail vector tiles did not finish loading")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("area_id")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--base-url")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    renderer = ChangeThumbnailRenderer(base_url=args.base_url)
    try:
        print(renderer.render(args.area_id, force=args.force))
    finally:
        renderer.close()


if __name__ == "__main__":
    main()
