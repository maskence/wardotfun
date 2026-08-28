import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
UPDATER_PATH = ROOT_DIR / "utils" / "manage_city_market_map.py"
STATE_PATH = Path(__file__).parent / "data" / "mapper_cache" / "market_map_updater.json"


class MarketMapUpdateService:
    def __init__(self, on_updated=None):
        self.interval = int(os.getenv("MARKET_MAP_UPDATE_INTERVAL", str(24 * 60 * 60)))
        self.retry_interval = int(os.getenv("MARKET_MAP_UPDATE_RETRY_INTERVAL", str(60 * 60)))
        self.initial_delay = int(os.getenv("MARKET_MAP_UPDATE_INITIAL_DELAY", "10"))
        self.enabled = os.getenv("MARKET_MAP_UPDATER_ENABLED", "1").lower() not in {"0", "false", "no"}
        self._on_updated = on_updated
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._state = self._load_state()

    def start(self):
        if not self.enabled or self._thread:
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="market-map-updater")
        self._thread.start()
        logger.info("Market-map updater scheduled every %d seconds", self.interval)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def status(self):
        with self._lock:
            state = dict(self._state)
        last_success = state.get("last_success")
        state.update({
            "enabled": self.enabled,
            "interval_seconds": self.interval,
            "next_run": (last_success + self.interval) if last_success else None,
        })
        return state

    def _run_loop(self):
        last_success = self._state.get("last_success")
        wait_seconds = self.initial_delay
        if last_success:
            wait_seconds = max(0, last_success + self.interval - time.time())
        while not self._stop.wait(wait_seconds):
            succeeded = self._run_once()
            wait_seconds = self.interval if succeeded else self.retry_interval

    def _run_once(self):
        started_at = time.time()
        self._update_state(running=True, last_started=started_at, last_error=None)
        command = [sys.executable, str(UPDATER_PATH), "--non-interactive"]
        try:
            result = subprocess.run(
                command,
                cwd=ROOT_DIR,
                capture_output=True,
                text=True,
                timeout=60 * 60,
                check=False,
            )
        except Exception as exc:
            logger.exception("Scheduled market-map update failed to run")
            self._update_state(running=False, last_error=str(exc))
            return False

        output = "\n".join((result.stdout or "").splitlines()[-30:])
        if result.returncode != 0:
            error = (result.stderr or output or f"exit code {result.returncode}").strip()
            logger.error("Scheduled market-map update failed: %s", error)
            self._update_state(running=False, last_error=error[-2000:])
            return False

        if output:
            logger.info("Scheduled market-map update output:\n%s", output)
        if self._on_updated:
            try:
                self._on_updated()
            except Exception as exc:
                logger.exception("Market-map update succeeded but reload failed")
                self._update_state(running=False, last_error=f"reload failed: {exc}")
                return False
        completed_at = time.time()
        self._update_state(
            running=False,
            last_success=completed_at,
            last_duration_seconds=round(completed_at - started_at, 2),
            last_error=None,
        )
        logger.info("Scheduled market-map update completed in %.2fs", completed_at - started_at)
        return True

    def _load_state(self):
        try:
            payload = json.loads(STATE_PATH.read_text())
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _update_state(self, **changes):
        with self._lock:
            self._state.update(changes)
            payload = dict(self._state)
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            temp_path = STATE_PATH.with_suffix(".tmp")
            temp_path.write_text(json.dumps(payload, indent=2))
            temp_path.replace(STATE_PATH)
        except OSError as exc:
            logger.warning("Unable to save market-map updater state: %s", exc)
