"""Cached MapLibre screenshots for immutable map-change feed cards."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

from PIL import Image, ImageStat

log = logging.getLogger(__name__)

RENDER_VERSION = "v6"
DEFAULT_ROOT = Path(__file__).parent / "data" / "change_thumbnails"


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
            with tempfile.TemporaryDirectory(prefix="wardotfun-thumbnail-", dir=destination.parent) as temporary:
                temporary_path = Path(temporary)
                png = temporary_path / "capture.png"
                profile = temporary_path / "chrome-profile"
                target = (
                    f"{self.base_url}/change-thumbnail.html?area="
                    f"{quote(area_id, safe='')}"
                )
                command = [
                    self.browser,
                    "--headless=new",
                    "--disable-dev-shm-usage",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--hide-scrollbars",
                    "--enable-unsafe-swiftshader",
                    "--use-angle=swiftshader",
                    "--run-all-compositor-stages-before-draw",
                    "--force-device-scale-factor=1",
                    "--window-size=768,432",
                    "--virtual-time-budget=15000",
                    f"--user-data-dir={profile}",
                    f"--screenshot={png}",
                    target,
                ]
                completed = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self.timeout,
                    check=False,
                )
                if completed.returncode or not png.is_file():
                    error = completed.stderr.decode("utf-8", "replace")[-1200:]
                    raise RuntimeError(f"headless Chrome failed ({completed.returncode}): {error}")
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
