"""Standalone temporal-map ingestion process.

Run under systemd instead of inside uvicorn. Every source is protected by both a
process-local schedule and a PostgreSQL advisory lock, so accidental duplicate
workers cannot publish competing snapshots.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
import signal
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

try:
    from .database import PostGISDatabase
    from .mapper_service import (
        GoogleMyMapsMapperSource,
        ISWMapperSource,
        MapperService,
        capture_raw_responses,
    )
    from .geolocation_service import PostGISGeoConfirmedGeolocationsSource
    from .temporal_repository import TemporalMapRepository, canonical_json
except ImportError:  # direct execution from backend/
    from database import PostGISDatabase
    from mapper_service import (
        GoogleMyMapsMapperSource,
        ISWMapperSource,
        MapperService,
        capture_raw_responses,
    )
    from geolocation_service import PostGISGeoConfirmedGeolocationsSource
    from temporal_repository import TemporalMapRepository, canonical_json

log = logging.getLogger(__name__)
DEFAULT_ARCHIVE_DIR = Path(__file__).parent / "data" / "raw_archive"


class RawArchive:
    """Content-addressed, gzip-compressed upstream response archive."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or os.getenv("WARDOTFUN_RAW_ARCHIVE_DIR") or DEFAULT_ARCHIVE_DIR)

    @staticmethod
    def _content_type(headers: dict[str, str]) -> str:
        for key, value in headers.items():
            if key.lower() == "content-type":
                return value.split(";", 1)[0].strip().lower()
        return "application/octet-stream"

    @staticmethod
    def _header(headers: dict[str, str], wanted: str) -> str | None:
        for key, value in headers.items():
            if key.lower() == wanted.lower():
                return value
        return None

    @staticmethod
    def _suffix(url: str, content_type: str) -> str:
        if "json" in content_type or urlparse(url).path.endswith(".json"):
            return ".json.gz"
        if "xml" in content_type or "kml" in content_type or urlparse(url).path.endswith(".kml"):
            return ".kml.gz"
        return ".bin.gz"

    def store(
        self,
        source_id: str,
        url: str,
        body: bytes,
        headers: dict[str, str] | None = None,
        *,
        captured_at: datetime | None = None,
    ) -> dict:
        captured_at = captured_at or datetime.now(timezone.utc)
        headers = headers or {}
        digest = hashlib.sha256(body).hexdigest()
        content_type = self._content_type(headers)
        # The hash, not observation time, is the archive identity: repeated 200
        # responses with identical bytes reuse one file indefinitely.
        directory = self.root / digest[:2]
        path = directory / f"{digest}{self._suffix(url, content_type)}"
        if not path.exists():
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / f".{path.name}.{uuid.uuid4().hex}.tmp"
            with temporary.open("wb") as target:
                with gzip.GzipFile(
                    filename="",
                    mode="wb",
                    fileobj=target,
                    compresslevel=9,
                    mtime=0,
                ) as compressed:
                    compressed.write(body)
                target.flush()
                os.fsync(target.fileno())
            try:
                temporary.replace(path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return {
            "sha256": digest,
            "path": str(path),
            "content_type": content_type,
            "byte_count": len(body),
            "url": url,
            "etag": self._header(headers, "etag"),
            "last_modified": self._header(headers, "last-modified"),
        }


class IngestionWorker:
    def __init__(
        self,
        *,
        mapper_service: MapperService | None = None,
        repository: TemporalMapRepository | None = None,
        archive: RawArchive | None = None,
        geolocation_source=None,
        poll_interval: float = 30.0,
    ):
        self.mapper_service = mapper_service or MapperService()
        self.repository = repository or TemporalMapRepository()
        self.archive = archive or RawArchive()
        self.geolocation_source = geolocation_source
        self.poll_interval = poll_interval
        self.stop_event = threading.Event()

    def setup(self, *, migrate: bool = True) -> None:
        if migrate:
            self.repository.database.migrate()
        if self.geolocation_source is None:
            self.geolocation_source = PostGISGeoConfirmedGeolocationsSource(
                database=self.repository.database, read_only=False
            )
        self.mapper_service.load_caches()
        for kind, source in self.mapper_service.iter_sources():
            if isinstance(source, ISWMapperSource):
                upstream_type = "arcgis"
                upstream_config = {"layers": source.LAYERS}
                refresh_policy = {
                    "default_seconds": source.refresh_interval,
                    "layers": source.LAYER_TTL,
                }
            elif isinstance(source, GoogleMyMapsMapperSource):
                upstream_type = "kml"
                upstream_config = {"url": source._kml_url}
                refresh_policy = {"default_seconds": source.refresh_interval}
            else:  # pragma: no cover - extension point for future sources
                upstream_type = source.__class__.__name__.lower()
                upstream_config = {}
                refresh_policy = {"default_seconds": source.refresh_interval}
            self.repository.register_source(
                source_id=source.id,
                kind=kind,
                display_name=source.display_name,
                source_url=source.source_url,
                attribution=source.attribution,
                upstream_type=upstream_type,
                upstream_config=upstream_config,
                refresh_policy=refresh_policy,
            )

    def import_baseline(self) -> list:
        """Import existing pickle caches as the first historical snapshots."""
        results = []
        for _kind, source in self.mapper_service.iter_sources():
            payload = source.get_overlay()
            if not payload.get("layers"):
                log.warning("Skipping empty baseline for %s", source.id)
                continue
            body = canonical_json(payload).encode("utf-8")
            raw = self.archive.store(
                source.id,
                f"legacy-cache://{source.id}",
                body,
                {"Content-Type": "application/json"},
            )
            captured_at = datetime.fromtimestamp(
                payload.get("last_updated") or time.time(), timezone.utc
            )
            result = self.repository.ingest_overlay(
                payload,
                captured_at=captured_at,
                raw_records=[raw],
            )
            results.append(result)
            log.info(
                "Baseline %s: %s snapshot=%s features=%d",
                source.id,
                result.status,
                result.snapshot_id,
                result.feature_count,
            )
        return results

    def _run_source(self, source):
        if not source.is_due():
            return None
        raw_records: list[dict] = []

        def capture(url, body, headers):
            raw_records.append(self.archive.store(source.id, url, body, headers))

        with capture_raw_responses(capture):
            source.refresh_if_due()
        payload = source.get_overlay()
        if payload.get("layers"):
            result = self.repository.ingest_overlay(
                payload,
                captured_at=datetime.now(timezone.utc),
                raw_records=raw_records,
            )
            log.info(
                "Ingested %s: %s snapshot=%s features=%d",
                source.id,
                result.status,
                result.snapshot_id,
                result.feature_count,
            )
            if payload.get("status") == "stale":
                self.repository.record_failure(
                    source.id,
                    source._last_error or "upstream refresh failed; retained stale layers",
                )
            return result
        error = source._last_error or "upstream returned no usable layers"
        self.repository.record_failure(source.id, error, raw_records=raw_records)
        log.warning("Ingest failed for %s: %s", source.id, error)
        return None

    def run_once(self) -> list:
        results = []
        for _kind, source in self.mapper_service.iter_sources():
            try:
                result = self._run_source(source)
                if result:
                    results.append(result)
            except Exception as exc:
                log.exception("Ingestion cycle failed for %s", source.id)
                try:
                    self.repository.record_failure(source.id, str(exc))
                except Exception:
                    log.exception("Could not record failed ingest run for %s", source.id)
        try:
            self.geolocation_source.refresh_if_due()
        except Exception:
            log.exception("GeoConfirmed ingestion cycle failed")
        return results

    def run_forever(self) -> None:
        log.info("Temporal ingestion worker started")
        while not self.stop_event.is_set():
            started = time.monotonic()
            self.run_once()
            elapsed = time.monotonic() - started
            self.stop_event.wait(max(0.0, self.poll_interval - elapsed))
        log.info("Temporal ingestion worker stopped")

    def stop(self, *_args) -> None:
        self.stop_event.set()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one due-source cycle and exit")
    parser.add_argument("--baseline", action="store_true", help="import pickle caches and exit")
    parser.add_argument("--no-migrate", action="store_true", help="do not apply pending schema migrations")
    parser.add_argument("--poll-interval", type=float, default=30.0)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    worker = IngestionWorker(poll_interval=max(1.0, args.poll_interval))
    worker.setup(migrate=not args.no_migrate)
    if args.baseline:
        worker.import_baseline()
        return
    if args.once:
        worker.run_once()
        return
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run_forever()


if __name__ == "__main__":
    main()
