"""Queries + writes for the Article Register (internal part-number allocation).

Raw sqlite3 via ``Database.connect()``; rows come back as ``dict`` (``row_factory`` is set on the
connection). Allocation follows the house ``MAX(...)+1`` convention used by PO/WO/CO. The catalog
link is a query-time string match (``parts.part_no = article_numbers.code``) — no FK, no coupling.
"""

from __future__ import annotations

import sqlite3

from ...core.db import Database
from .codes import article_code, compose_description, normalize_prefix

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


def search_unassigned(db: Database, query: str | None = None, *, prefix: str | None = None,
                      limit: int = 20) -> list[dict]:
    """Article numbers that have a code but no catalog part/assembly yet — for the part-number
    typeahead on the Add-component / New-assembly forms.

    "Unassigned" = a live (non-reserved, non-retired) ``code`` with no matching ``parts.part_no``
    (assemblies are parts with ``kind='ASSY'``, so this one check covers both). ``prefix`` optionally
    scopes to a single category (e.g. ``98`` for assemblies). Matches the typed text against the code
    or the product description; ordered so the most recent numbers surface first.
    """
    clauses = ["a.code IS NOT NULL", "a.retired = 0",
               "NOT EXISTS (SELECT 1 FROM parts p WHERE p.part_no = a.code)"]
    params: dict = {"limit": max(1, min(limit, 50))}
    if prefix:
        clauses.append("a.prefix = :prefix")
        params["prefix"] = normalize_prefix(prefix)
    if query:
        clauses.append("(a.code LIKE :like OR a.product LIKE :like)")
        params["like"] = f"%{query}%"
    where = "WHERE " + " AND ".join(clauses)
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            f"""SELECT a.code, a.product, a.prefix, a.running_no, a.suffix,
                       ap.label AS prefix_label, ap.category AS category
                {_JOINS} {where}
                ORDER BY a.running_no DESC, a.prefix, a.suffix
                LIMIT :limit""", params)]


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

def _first_free_running_no(conn) -> int:
    """Smallest running number >= 1 not present in ``article_numbers`` — the first *gap*, not MAX+1.

    The register grew with large gaps between legacy blocks (e.g. real numbers stop around 392 but a
    few high numbers push MAX past 1000); those free numbers in between are meant to be reused, so we
    fill the earliest gap. Retired and reserved rows still occupy their running number, so a number
    taken for any reason is never handed out again.
    """
    used = {r[0] for r in conn.execute("SELECT DISTINCT running_no FROM article_numbers")}
    n = 1
    while n in used:
        n += 1
    return n


def next_running_no(db: Database) -> int:
    with db.connect() as conn:
        return _first_free_running_no(conn)


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
        running_no = _first_free_running_no(conn)
        conn.executemany(
            """INSERT INTO article_numbers
                   (prefix, running_no, suffix, code, product, created_by, comment, source)
               VALUES (?, ?, 1, ?, ?, ?, ?, 'manual')""",
            [(pfx, running_no, article_code(pfx, running_no, 1), product, created_by, comment)
             for pfx in codes])
        conn.commit()
    return running_no


def duplicate_entry(db: Database, entry_id: int) -> int | None:
    """Copy an entry into the next free suffix within its prefix + running number.

    The clone keeps the prefix, running number, product name and metadata; only the suffix advances
    (``MAX(suffix) + 1`` for that prefix+running number, so it never collides). Returns the new
    entry id, or ``None`` if the source is missing or is a reserved row (no prefix → no suffix).
    """
    entry = get_entry(db, entry_id)
    if entry is None or not entry["prefix"]:
        return None
    suffix = next_suffix(db, entry["prefix"], entry["running_no"])
    return create_entry(db, prefix=entry["prefix"], running_no=entry["running_no"], suffix=suffix,
                        product=entry["product"], created_by=entry["created_by"],
                        comment=entry["comment"])


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


def delete_entry(db: Database, entry_id: int) -> None:
    """Hard-delete a single entry. Rare, admin-only, irreversible — retiring is the normal
    lifecycle action; this is for genuine mistakes."""
    with db.connect() as conn:
        conn.execute("DELETE FROM article_numbers WHERE id = ?", (entry_id,))
        conn.commit()


def set_retired(db: Database, entry_id: int, retired: bool) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE article_numbers SET retired = ?, updated_at = datetime('now') WHERE id = ?",
            (1 if retired else 0, entry_id))
        conn.commit()


# ---- templates (product-structure blueprints) ----

def list_templates(db: Database, *, active_only: bool = True) -> list[dict]:
    """Templates with their lines attached (small table — one round trip per template is fine)."""
    where = "WHERE active = 1" if active_only else ""
    with db.connect() as conn:
        ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM article_templates {where} ORDER BY name")]
    return [t for t in (get_template(db, i) for i in ids) if t]


def get_template(db: Database, template_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM article_templates WHERE id = ?", (template_id,)).fetchone()
        if not row:
            return None
        lines = conn.execute(
            """SELECT l.*, ap.label AS prefix_label, ap.category AS category
               FROM article_template_lines l
               LEFT JOIN article_prefixes ap ON ap.code = l.prefix
               WHERE l.template_id = ? ORDER BY l.sort_order, l.id""", (template_id,)).fetchall()
    tmpl = dict(row)
    tmpl["lines"] = [dict(r) for r in lines]
    return tmpl


def create_template(db: Database, *, name: str, notes: str | None = None) -> int:
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO article_templates (name, notes) VALUES (?, ?)", (name, notes))
        conn.commit()
        return cur.lastrowid


def save_template(db: Database, template_id: int, *, name: str, notes: str | None,
                  lines: list[dict]) -> None:
    """Update the header and replace all lines (the editor posts the full ordered set)."""
    rows = []
    for i, ln in enumerate(lines):
        prefix = normalize_prefix(ln.get("prefix"))
        if not prefix:
            continue
        rows.append((template_id, prefix, int(ln.get("suffix") or 1),
                     (ln.get("label") or "").strip(), i))
    with db.connect() as conn:
        conn.execute(
            "UPDATE article_templates SET name = ?, notes = ?, updated_at = datetime('now') WHERE id = ?",
            (name, notes, template_id))
        conn.execute("DELETE FROM article_template_lines WHERE template_id = ?", (template_id,))
        conn.executemany(
            """INSERT INTO article_template_lines (template_id, prefix, suffix, label, sort_order)
               VALUES (?, ?, ?, ?, ?)""", rows)
        conn.commit()


def delete_template(db: Database, template_id: int) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM article_template_lines WHERE template_id = ?", (template_id,))
        conn.execute("DELETE FROM article_templates WHERE id = ?", (template_id,))
        conn.commit()


def family_prefixes(db: Database, running_no: int) -> list[str]:
    """The distinct assigned prefixes already present in a running-number family (reserved rows have
    no prefix and are excluded)."""
    with db.connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT prefix FROM article_numbers "
            "WHERE running_no = ? AND prefix IS NOT NULL ORDER BY prefix", (running_no,))]


def list_family_documents(db: Database, running_no: int) -> list[dict]:
    """Documents allocated under this running-number family, for the detail-page panel. The
    ``documents`` table is owned by the (later-registered) Documents feature; guard for its absence
    so Article-Register-only unit tests still pass — same decoupled, query-time-join spirit as the
    ``parts`` soft link."""
    try:
        with db.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT d.id, d.code, d.title, d.prefix, d.storage_kind, d.external_url, d.retired, "
                "       cr.rev AS current_rev "
                "FROM documents d "
                "LEFT JOIN document_revisions cr ON cr.document_id = d.id AND cr.is_current = 1 "
                "WHERE d.running_no = ? ORDER BY d.code", (running_no,))]
    except sqlite3.OperationalError:
        return []


def family_codes(db: Database, running_no: int) -> list[str]:
    """The article codes already assigned in a running-number family (reserved rows have no code and
    are excluded). Drives the "already in family → skipped" tag when adding template lines to an
    existing family: the skip is by **exact code**, so a template can still add new suffixes under a
    prefix that's already present (e.g. add 99-…-2 "Stencil TOP" when only 99-…-1 "PCB" exists)."""
    with db.connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT code FROM article_numbers "
            "WHERE running_no = ? AND code IS NOT NULL ORDER BY code", (running_no,))]


def apply_template(db: Database, template_id: int, *, product: str | None,
                   created_by: str | None = None, comment: str | None = None,
                   running_no: int | None = None) -> int:
    """Generate a product family from a template.

    ``running_no=None`` allocates a fresh running number (New Product); otherwise the lines are
    appended to an existing family. When appending, a template line is skipped only if its **exact
    code** (prefix + running number + the template line's own suffix) already exists — so a template
    can still add new suffixes under a prefix that's already present (e.g. add the Stencil TOP/BOT
    lines 99-…-2/-3 to a family that so far only has the PCB line 99-…-1). Returns the running number
    written to. Raises ``ValueError`` if appending would add nothing (every code already exists).
    """
    tmpl = get_template(db, template_id)
    if not tmpl or not tmpl["lines"]:
        raise ValueError("That template has no lines.")
    with db.connect() as conn:
        appending = running_no is not None
        if running_no is None:
            running_no = _first_free_running_no(conn)
        existing = set(family_codes(db, running_no)) if appending else set()
        seen: set[str] = set()
        rows = []
        for ln in tmpl["lines"]:
            prefix = normalize_prefix(ln["prefix"])
            if not prefix:
                continue
            suffix = int(ln["suffix"] or 1)
            code = article_code(prefix, running_no, suffix)
            if code in existing or code in seen:  # already in the family (or a duplicate line) → skip
                continue
            seen.add(code)
            rows.append((prefix, running_no, suffix, code,
                         compose_description(product, ln["label"]), created_by, comment, "manual"))
        if appending and not rows:
            raise ValueError(
                f"Every line in this template is already in family {running_no:05d} — nothing to add.")
        try:
            conn.executemany(
                """INSERT INTO article_numbers
                       (prefix, running_no, suffix, code, product, created_by, comment, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", rows)
        except sqlite3.IntegrityError as exc:
            raise DuplicateNumber(str(exc)) from exc
        conn.commit()
    return running_no


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
