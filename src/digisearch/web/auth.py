"""User store, password hashing and role gating.

This SQLite ``users`` table is deliberately the *first* piece of the source-of-truth
database that will, over time, grow to hold the catalog/inventory/BOM data currently
living in miniMRP. For now it only authenticates people and assigns them a role so the
right operators see the right screens.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path

# Roles the app understands. ``admin``/``purchasing`` use the purchasing tool today;
# warehouse/shipping are placeholders for the screens to come.
ROLES = ("admin", "purchasing", "warehouse", "shipping")

# Which roles may run the purchasing tool.
PURCHASE_ROLES = frozenset({"admin", "purchasing"})

_PBKDF2_ROUNDS = 200_000


@dataclass(frozen=True)
class User:
    id: int
    username: str
    role: str


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS).hex()


class UserStore:
    """Thin SQLite-backed user table. One connection, created on demand."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    username      TEXT NOT NULL UNIQUE,
                    salt          TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role          TEXT NOT NULL DEFAULT 'purchasing',
                    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def create_user(self, username: str, password: str, role: str = "purchasing") -> User:
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")
        salt = secrets.token_bytes(16)
        pw_hash = _hash_password(password, salt)
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, salt, password_hash, role) VALUES (?, ?, ?, ?)",
                (username, salt.hex(), pw_hash, role),
            )
            return User(id=cur.lastrowid, username=username, role=role)

    def verify(self, username: str, password: str) -> User | None:
        """Return the User on correct credentials, else None (constant-ish time)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, username, salt, password_hash, role FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            # Hash anyway to avoid leaking whether the username exists via timing.
            _hash_password(password, secrets.token_bytes(16))
            return None
        expected = row["password_hash"]
        actual = _hash_password(password, bytes.fromhex(row["salt"]))
        if not secrets.compare_digest(expected, actual):
            return None
        return User(id=row["id"], username=row["username"], role=row["role"])

    def get(self, user_id: int) -> User | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, username, role FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return User(id=row["id"], username=row["username"], role=row["role"]) if row else None
