"""Queries + writes for the Article Register (internal part-number allocation).

Raw sqlite3 via ``Database.connect()``; rows come back as ``dict`` (``row_factory`` is set on the
connection). Allocation follows the house ``MAX(...)+1`` convention used by PO/WO/CO. The catalog
link is a query-time string match (``parts.part_no = article_numbers.code``) — no FK, no coupling.
"""

from __future__ import annotations

import sqlite3

from ...core.db import Database
from .codes import article_code, normalize_prefix

# Display order + human labels for the prefix categories (used to group the allocator dropdown).
CATEGORIES = (
    ("customer", "Customer products"),
    ("ic", "iLabs ICs"),
    ("document", "Documents"),
    ("internal", "Internal"),
)
CATEGORY_LABELS = dict(CATEGORIES)


class DuplicateNumber(Exception):
    """Raised when a code / (prefix, running_no, suffix) triplet already exists."""


# ---- prefixes (reference table) ----

def list_prefixes(db: Database, *, active_only: bool = True) -> list[dict]:
    where = "WHERE active = 1" if active_only else ""
    order = ("ORDER BY CASE category "
             "WHEN 'customer' THEN 0 WHEN 'ic' THEN 1 WHEN 'document' THEN 2 ELSE 3 END, code")
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT code, label, category, active FROM article_prefixes {where} {order}")]


def prefixes_grouped(db: Database) -> list[dict]:
    """Active prefixes grouped for the allocator dropdown: [{category, label, prefixes:[...]}]."""
    rows = list_prefixes(db, active_only=True)
    groups: dict[str, list[dict]] = {key: [] for key, _ in CATEGORIES}
    for r in rows:
        groups.setdefault(r["category"], []).append(r)
    return [{"category": key, "label": CATEGORY_LABELS.get(key, key.title()), "prefixes": groups[key]}
            for key, _ in CATEGORIES if groups.get(key)]


# ---- entries ----

_JOINS = """
    FROM article_numbers a
    LEFT JOIN article_prefixes ap ON ap.code = a.prefix
    LEFT JOIN parts p ON p.part_no = a.code
"""


def list_entries(db: Database, *, search: str | None = None, prefix: str | None = None,
                 category: str | None = None, include_retired: bool = False) -> list[dict]:
    like = f"%{search}%" if search else None
    clauses = []
    if not include_retired:
        clauses.append("a.retired = 0")
    if prefix:
        clauses.append("a.prefix = :prefix")
    if category:
        clauses.append("ap.category = :category")
    if search:
        clauses.append("(a.code LIKE :like OR a.product LIKE :like OR a.created_by LIKE :like "
                       "OR CAST(a.running_no AS TEXT) LIKE :like)")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            f"""SELECT a.*, ap.label AS prefix_label, ap.category AS category, p.id AS part_id
                {_JOINS} {where}
                ORDER BY a.running_no DESC, a.prefix, a.suffix""",
            {"prefix": prefix, "category": category, "like": like},
        )]


def get_family(db: Database, running_no: int) -> list[dict]:
    """All entries (assigned + reserved) sharing a running number — the family view."""
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            f"""SELECT a.*, ap.label AS prefix_label, ap.category AS category, p.id AS part_id
                {_JOINS} WHERE a.running_no = :n
                ORDER BY a.prefix IS NULL, a.prefix, a.suffix""",
            {"n": running_no},
        )]


def get_entry(db: Database, entry_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            f"""SELECT a.*, ap.label AS prefix_label, ap.category AS category, p.id AS part_id
                {_JOINS} WHERE a.id = :id""", {"id": entry_id}).fetchone()
    return dict(row) if row else None


# ---- allocation ----

def next_running_no(db: Database) -> int:
    with db.connect() as conn:
        return conn.execute(
            "SELECT COALESCE(MAX(running_no), 0) + 1 FROM article_numbers").fetchone()[0]


def next_suffix(db: Database, prefix: str, running_no: int) -> int:
    with db.connect() as conn:
        return conn.execute(
            "SELECT COALESCE(MAX(suffix), 0) + 1 FROM article_numbers "
            "WHERE prefix = ? AND running_no = ?", (prefix, running_no)).fetchone()[0]


def create_entry(db: Database, *, prefix: str, running_no: int, suffix: int,
                 product: str | None = None, created_by: str | None = None,
                 comment: str | None = None, source: str = "manual") -> int:
    prefix = normalize_prefix(prefix)
    code = article_code(prefix, running_no, suffix)
    with db.connect() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO article_numbers
                       (prefix, running_no, suffix, code, product, created_by, comment, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (prefix, running_no, suffix, code, product, created_by, comment, source))
        except sqlite3.IntegrityError as exc:
            raise DuplicateNumber(f"{code} already exists.") from exc
        conn.commit()
        return cur.lastrowid


def create_product(db: Database, *, product: str | None, prefixes: list[str],
                   created_by: str | None = None, comment: str | None = None) -> int:
    """Allocate one new running number and create a line under it for each selected group.

    This is how a product is created: the running number is the product's shared identity, and
    each ticked group (e.g. 98 assembly, 54 drawing, 99 component) becomes a line ``PREFIX-NNNNN-1``
    sharing that number, product name and metadata. Returns the new running number.
    """
    codes = list(dict.fromkeys(  # de-dupe, preserve tick order
        normalize_prefix(p) for p in prefixes if normalize_prefix(p)))
    if not codes:
        raise ValueError("Pick at least one group.")
    with db.connect() as conn:
        running_no = conn.execute(
            "SELECT COALESCE(MAX(running_no), 0) + 1 FROM article_numbers").fetchone()[0]
        conn.executemany(
            """INSERT INTO article_numbers
                   (prefix, running_no, suffix, code, product, created_by, comment, source)
               VALUES (?, ?, 1, ?, ?, ?, ?, 'manual')""",
            [(pfx, running_no, article_code(pfx, running_no, 1), product, created_by, comment)
             for pfx in codes])
        conn.commit()
    return running_no


def update_entry(db: Database, entry_id: int, *, product: str | None = None,
                 created_by: str | None = None, comment: str | None = None) -> None:
    """Edit the descriptive fields only — the identity (prefix/running/suffix) is immutable once set."""
    with db.connect() as conn:
        conn.execute(
            """UPDATE article_numbers
               SET product = ?, created_by = ?, comment = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (product, created_by, comment, entry_id))
        conn.commit()


def set_retired(db: Database, entry_id: int, retired: bool) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE article_numbers SET retired = ?, updated_at = datetime('now') WHERE id = ?",
            (1 if retired else 0, entry_id))
        conn.commit()


# ---- summary ----

def summary(db: Database) -> dict:
    with db.connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM article_numbers").fetchone()[0]
        reserved = conn.execute(
            "SELECT COUNT(*) FROM article_numbers WHERE prefix IS NULL").fetchone()[0]
        retired = conn.execute(
            "SELECT COUNT(*) FROM article_numbers WHERE retired = 1").fetchone()[0]
        families = conn.execute(
            "SELECT COUNT(DISTINCT running_no) FROM article_numbers").fetchone()[0]
    return {"total": total, "reserved": reserved, "retired": retired, "families": families}
