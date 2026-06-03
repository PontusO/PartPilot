"""Simple on-disk JSON cache for Digi-Key responses (free tier is rate-limited)."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

DEFAULT_DIR = Path(".digisearch_cache")


class DiskCache:
    def __init__(self, directory: str | Path = DEFAULT_DIR, ttl_seconds: int = 7 * 24 * 3600):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_seconds

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self.dir / f"{digest}.json"

    def get(self, key: str) -> dict | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        if self.ttl and time.time() - payload.get("_cached_at", 0) > self.ttl:
            return None
        return payload.get("data")

    def set(self, key: str, data: dict) -> None:
        path = self._path(key)
        path.write_text(json.dumps({"_cached_at": time.time(), "data": data}))
