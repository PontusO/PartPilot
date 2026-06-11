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
    full_name: str = ""
    active: bool = True


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
            # Additive columns for named-user management; applied in place because the
            # users table predates (and is independent of) the feature migration system.
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
            if "full_name" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN full_name TEXT NOT NULL DEFAULT ''")
            if "active" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
            if "last_login_at" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN last_login_at TEXT")

    @staticmethod
    def _user(row: sqlite3.Row) -> User:
        return User(
            id=row["id"], username=row["username"], role=row["role"],
            full_name=row["full_name"] or "", active=bool(row["active"]),
        )

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def count_active_admins(self) -> int:
        """Active admins — used to refuse the last-admin lockout."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1"
            ).fetchone()[0]

    def list_users(self) -> list[User]:
        """All users, including inactive ones, ordered by username."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, username, role, full_name, active, last_login_at "
                "FROM users ORDER BY username"
            ).fetchall()
        return [
            User(id=r["id"], username=r["username"], role=r["role"],
                 full_name=r["full_name"] or "", active=bool(r["active"]))
            for r in rows
        ]

    def has_logged_in(self, user_id: int) -> bool:
        """True if the user has ever logged in (gates whether hard delete is offered)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_login_at FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return bool(row and row["last_login_at"])

    def create_user(self, username: str, password: str, role: str = "purchasing",
                    full_name: str = "") -> User:
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")
        salt = secrets.token_bytes(16)
        pw_hash = _hash_password(password, salt)
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, salt, password_hash, role, full_name) "
                "VALUES (?, ?, ?, ?, ?)",
                (username, salt.hex(), pw_hash, role, full_name),
            )
            return User(id=cur.lastrowid, username=username, role=role, full_name=full_name)

    def verify(self, username: str, password: str) -> User | None:
        """Return the User on correct credentials, else None (constant-ish time).

        Inactive accounts never verify, so deactivating a user blocks login immediately.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, username, salt, password_hash, role, full_name, active "
                "FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if row is None:
                # Hash anyway to avoid leaking whether the username exists via timing.
                _hash_password(password, secrets.token_bytes(16))
                return None
            expected = row["password_hash"]
            actual = _hash_password(password, bytes.fromhex(row["salt"]))
            if not secrets.compare_digest(expected, actual) or not row["active"]:
                return None
            conn.execute(
                "UPDATE users SET last_login_at = datetime('now') WHERE id = ?", (row["id"],)
            )
        return self._user(row)

    def get(self, user_id: int) -> User | None:
        """Return an *active* user by id, else None (a deactivated session resolves to None)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, username, role, full_name, active FROM users "
                "WHERE id = ? AND active = 1",
                (user_id,),
            ).fetchone()
        return self._user(row) if row else None

    def get_any(self, user_id: int) -> User | None:
        """Return a user by id regardless of active state — for admin management screens."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, username, role, full_name, active FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return self._user(row) if row else None

    def update_role(self, user_id: int, role: str) -> None:
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")
        with self._connect() as conn:
            conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))

    def set_password(self, user_id: int, password: str) -> None:
        salt = secrets.token_bytes(16)
        pw_hash = _hash_password(password, salt)
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET salt = ?, password_hash = ? WHERE id = ?",
                (salt.hex(), pw_hash, user_id),
            )

    def set_active(self, user_id: int, active: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, user_id)
            )

    def set_full_name(self, user_id: int, full_name: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET full_name = ? WHERE id = ?", (full_name, user_id)
            )

    def delete(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
