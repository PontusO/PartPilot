"""The platform's SQLite database and a tiny migration runner.

This is the seed of the source-of-truth store that will, feature by feature, take over
the data currently in miniMRP. Each feature contributes ordered ``Migration`` steps; the
runner applies any not yet recorded in ``schema_migrations`` and is safe to run on every
startup. Deliberately minimal (no SQLAlchemy/Alembic yet) — just enough that every data
feature has a home for its schema.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .registry import FeatureRegistry


class Database:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def apply_migrations(self, registry: FeatureRegistry) -> list[tuple[str, int]]:
        """Apply every feature migration not yet recorded. Returns the ones applied."""
        applied: list[tuple[str, int]] = []
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    feature    TEXT    NOT NULL,
                    version    INTEGER NOT NULL,
                    name       TEXT    NOT NULL,
                    applied_at TEXT    NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (feature, version)
                )
                """
            )
            for feature_name, mig in registry.all_migrations():
                seen = conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE feature = ? AND version = ?",
                    (feature_name, mig.version),
                ).fetchone()
                if seen:
                    continue
                conn.executescript(mig.sql)
                conn.execute(
                    "INSERT INTO schema_migrations (feature, version, name) VALUES (?, ?, ?)",
                    (feature_name, mig.version, mig.name),
                )
                applied.append((feature_name, mig.version))
            conn.commit()
        return applied
