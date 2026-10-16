"""The platform's SQLite database and a tiny migration runner.

This is the seed of the source-of-truth store that will, feature by feature, take over
the data currently in miniMRP. Each feature contributes ordered ``Migration`` steps; the
runner applies any not yet recorded in ``schema_migrations`` and is safe to run on every
startup. Deliberately minimal (no SQLAlchemy/Alembic yet) — just enough that every data
feature has a home for its schema.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path

from .registry import FeatureRegistry


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def rebuild_table(conn: sqlite3.Connection, table: str, *, sql_replacements: dict[str, str],
                  column_exprs: dict[str, str]) -> None:
    """Recreate ``table`` with its CREATE SQL textually edited (SQLite can't ALTER a foreign key),
    copying every row with selected columns rewritten by ``column_exprs`` (column -> SQL expr).

    Foreign-key enforcement is switched off for the duration: dropping the old table with it on
    would run ON DELETE actions on child tables. Runs in one explicit transaction and rolls back
    on any failure, including a non-empty ``foreign_key_check`` afterwards.
    """
    conn.commit()                                   # the pragma is a no-op inside a transaction
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if row is None:
            raise RuntimeError(f"rebuild_table: no table {table!r}")
        create_sql = row[0]
        for old, new in sql_replacements.items():
            create_sql = create_sql.replace(old, new)
        tmp = f"{table}__new"
        create_sql = re.sub(rf'CREATE TABLE\s+"?{table}"?', f"CREATE TABLE {tmp}", create_sql, count=1)
        indexes = [r[0] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (table,))]
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        select = ", ".join(column_exprs.get(c, c) for c in cols)
        conn.execute(create_sql)
        conn.execute(f"INSERT INTO {tmp} ({', '.join(cols)}) SELECT {select} FROM {table}")
        n_old = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        n_new = conn.execute(f"SELECT COUNT(*) FROM {tmp}").fetchone()[0]
        if n_old != n_new:
            raise RuntimeError(f"rebuild_table: {table} copied {n_new} of {n_old} rows")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
        for ix in indexes:
            conn.execute(ix)
        bad = conn.execute(f"PRAGMA foreign_key_check({table})").fetchall()
        if bad:
            raise RuntimeError(
                f"rebuild_table: {len(bad)} dangling reference(s) in {table} after rebuild")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


class Database:
    def __init__(self, db_path: str | Path, backup_dir: str | Path | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Where a copy of the database is written before migrations run. None disables (tests).
        self.backup_dir = Path(backup_dir) if backup_dir else None

    def connect(self) -> sqlite3.Connection:
        # timeout = how long a write waits for the lock before raising "database is locked".
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # WAL lets readers and a single writer run concurrently (writes don't block reads), which
        # matters once several people use the app at once. busy_timeout makes overlapping writers
        # wait rather than fail. NORMAL is the safe, recommended sync level under WAL.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _backup(self, conn: sqlite3.Connection, pending: int) -> Path:
        """Online copy (WAL-safe) of the database into backup_dir; raises if it can't be written."""
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        dest = self.backup_dir / f"partpilot-{datetime.now():%Y%m%d-%H%M%S}.db"
        with sqlite3.connect(dest) as copy:
            conn.backup(copy)
        print(f"[partpilot] backed up database to {dest} before applying {pending} migration(s)")
        return dest

    def apply_migrations(self, registry: FeatureRegistry) -> list[tuple[str, int]]:
        """Apply every feature migration not yet recorded. Returns the ones applied.

        When migrations are pending on an existing database (one that already has a
        ``schema_migrations`` table) and ``backup_dir`` is set, the database is backed up first;
        a backup failure aborts before anything is applied.
        """
        applied: list[tuple[str, int]] = []
        with self.connect() as conn:
            first_run = not table_exists(conn, "schema_migrations")
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
            seen = {(r[0], r[1]) for r in conn.execute("SELECT feature, version FROM schema_migrations")}
            pending = [(f, m) for f, m in registry.all_migrations() if (f, m.version) not in seen]
            if pending and self.backup_dir and not first_run:
                conn.commit()
                self._backup(conn, len(pending))
            for feature_name, mig in pending:
                if callable(mig.sql):
                    mig.sql(conn)
                else:
                    conn.executescript(mig.sql)
                conn.execute(
                    "INSERT INTO schema_migrations (feature, version, name) VALUES (?, ?, ?)",
                    (feature_name, mig.version, mig.name),
                )
                applied.append((feature_name, mig.version))
            conn.commit()
        return applied
