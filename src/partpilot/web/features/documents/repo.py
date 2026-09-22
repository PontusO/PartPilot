"""Queries + writes for the Documents feature.

Raw sqlite3 via ``Database.connect()``; rows come back as ``dict``. Identity/numbering is NOT owned
here — a document's ``code`` is allocated through the Article Register allocator
(``article_register.repo``); this module only stores the document's metadata, its file/link revisions
(append-only), and which revision is current. The catalog link is the usual query-time string match
(``parts.part_no = documents.code``) — no FK.
"""

from __future__ import annotations

from ...core.db import Database

# The document classes a document may be filed under: the Article Register 'document' category
# (prefixes 50–59) plus '95' (software / source code, which is always a link).
SOFTWARE_PREFIX = "95"


def document_prefixes(db: Database) -> list[dict]:
    """Prefixes a document can be filed under, for the create form: the 'document' category plus the
    software prefix. Reuses the Article Register reference table so the two stay in sync."""
    from ..article_register import repo as ar_repo

    rows = ar_repo.list_prefixes(db, active_only=True)
    picked = [r for r in rows if r["category"] == "document" or r["code"] == SOFTWARE_PREFIX]
    picked.sort(key=lambda r: r["code"])
    return picked


# ---- reads ----

_LIST_SELECT = """
    SELECT d.*, cr.rev AS current_rev, cr.filename AS current_filename,
           cr.byte_size AS current_byte_size, cr.uploaded_at AS current_uploaded_at,
           ap.label AS prefix_label, p.id AS part_id
    FROM documents d
    LEFT JOIN document_revisions cr ON cr.document_id = d.id AND cr.is_current = 1
    LEFT JOIN article_prefixes ap ON ap.code = d.prefix
    LEFT JOIN parts p ON p.part_no = d.code
"""


def list_documents(db: Database, *, search: str | None = None, prefix: str | None = None,
                   kind: str | None = None, include_retired: bool = False) -> list[dict]:
    clauses, params = [], {}
    if not include_retired:
        clauses.append("d.retired = 0")
    if prefix:
        clauses.append("d.prefix = :prefix")
        params["prefix"] = prefix
    if kind in ("file", "link"):
        clauses.append("d.storage_kind = :kind")
        params["kind"] = kind
    if search:
        clauses.append("(d.code LIKE :like OR d.title LIKE :like OR d.doc_type LIKE :like)")
        params["like"] = f"%{search}%"
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            f"{_LIST_SELECT} {where} ORDER BY d.running_no DESC, d.code", params)]


def get_document(db: Database, document_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(f"{_LIST_SELECT} WHERE d.id = ?", (document_id,)).fetchone()
        if row is None:
            return None
        doc = dict(row)
        doc["revisions"] = [dict(r) for r in conn.execute(
            "SELECT * FROM document_revisions WHERE document_id = ? "
            "ORDER BY uploaded_at DESC, id DESC", (document_id,))]
    return doc


def get_revision(db: Database, document_id: int, revision_id: int) -> dict | None:
    """A single revision, scoped to its document (so a mismatched pair can't leak another doc's file)."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM document_revisions WHERE id = ? AND document_id = ?",
            (revision_id, document_id)).fetchone()
        return dict(row) if row else None


def article_code_exists(db: Database, code: str | None) -> bool:
    """True if ``code`` is an allocated Article Register number — used when creating a document bound
    to an existing number (no new allocation). Reads the AR-owned table by the usual soft-link spirit."""
    if not code:
        return False
    with db.connect() as conn:
        return conn.execute(
            "SELECT 1 FROM article_numbers WHERE code = ?", (code.strip(),)).fetchone() is not None


def document_for_code(db: Database, code: str | None) -> dict | None:
    """The document whose article code matches (used to link from a catalog part)."""
    if not code:
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM documents WHERE code = ?", (code.strip(),)).fetchone()
        return dict(row) if row else None


def family_documents(db: Database, running_no: int) -> list[dict]:
    """Documents belonging to a running-number family — drives the panel on the Article Register
    detail page."""
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT d.id, d.code, d.title, d.prefix, d.storage_kind, d.external_url, d.retired, "
            "       cr.rev AS current_rev "
            "FROM documents d "
            "LEFT JOIN document_revisions cr ON cr.document_id = d.id AND cr.is_current = 1 "
            "WHERE d.running_no = ? ORDER BY d.code", (running_no,))]


# ---- writes ----

def create_document(db: Database, *, code: str, running_no: int, prefix: str, title: str,
                    storage_kind: str, doc_type: str | None = None, created_by: str | None = None,
                    notes: str | None = None) -> int:
    """Insert the document metadata row (identity already allocated via the Article Register)."""
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO documents
                   (code, running_no, prefix, title, doc_type, storage_kind, created_by, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (code, running_no, prefix, title, doc_type, storage_kind, created_by, notes))
        conn.commit()
        return cur.lastrowid


def _make_current(conn, document_id: int, revision_id: int) -> None:
    """Flip ``is_current`` to exactly ``revision_id`` for its document, and point the document at it.
    Clearing first keeps the partial unique index (one current per document) satisfied."""
    conn.execute("UPDATE document_revisions SET is_current = 0 WHERE document_id = ?", (document_id,))
    conn.execute("UPDATE document_revisions SET is_current = 1 WHERE id = ?", (revision_id,))
    conn.execute(
        "UPDATE documents SET current_revision_id = ?, updated_at = datetime('now') WHERE id = ?",
        (revision_id, document_id))


def add_file_revision(db: Database, document_id: int, *, rev: str, filename: str, rel_path: str,
                      byte_size: int, content_type: str | None = None, uploaded_by: str | None = None,
                      notes: str | None = None) -> int:
    """Append a new file revision and make it current. ``rel_path`` is relative to data/documents/."""
    with db.connect() as conn:
        prev = conn.execute(
            "SELECT id FROM document_revisions WHERE document_id = ? AND is_current = 1",
            (document_id,)).fetchone()
        cur = conn.execute(
            """INSERT INTO document_revisions
                   (document_id, rev, filename, rel_path, byte_size, content_type,
                    supersedes_id, notes, uploaded_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (document_id, rev, filename, rel_path, byte_size, content_type,
             prev["id"] if prev else None, notes, uploaded_by))
        rev_id = cur.lastrowid
        _make_current(conn, document_id, rev_id)
        conn.commit()
        return rev_id


def add_link_revision(db: Database, document_id: int, *, rev: str, external_url: str,
                      ext_ref: str | None = None, ext_path: str | None = None,
                      uploaded_by: str | None = None, notes: str | None = None) -> int:
    """Append a new link revision (URL history) and make it current; mirror the URL onto the document."""
    with db.connect() as conn:
        prev = conn.execute(
            "SELECT id FROM document_revisions WHERE document_id = ? AND is_current = 1",
            (document_id,)).fetchone()
        cur = conn.execute(
            """INSERT INTO document_revisions
                   (document_id, rev, external_url, ext_ref, ext_path, supersedes_id, notes, uploaded_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (document_id, rev, external_url, ext_ref, ext_path,
             prev["id"] if prev else None, notes, uploaded_by))
        rev_id = cur.lastrowid
        _make_current(conn, document_id, rev_id)
        conn.execute(
            "UPDATE documents SET external_url = ?, ext_ref = ?, ext_path = ? WHERE id = ?",
            (external_url, ext_ref, ext_path, document_id))
        conn.commit()
        return rev_id


def set_current_revision(db: Database, document_id: int, revision_id: int) -> bool:
    """Mark an older revision current again. Returns False if the revision isn't part of the document.
    For a link document the document's mirrored URL follows the chosen revision."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT external_url, ext_ref, ext_path FROM document_revisions "
            "WHERE id = ? AND document_id = ?", (revision_id, document_id)).fetchone()
        if row is None:
            return False
        _make_current(conn, document_id, revision_id)
        if row["external_url"] is not None:
            conn.execute(
                "UPDATE documents SET external_url = ?, ext_ref = ?, ext_path = ? WHERE id = ?",
                (row["external_url"], row["ext_ref"], row["ext_path"], document_id))
        conn.commit()
        return True


def update_document(db: Database, document_id: int, *, title: str, doc_type: str | None,
                    notes: str | None) -> None:
    """Edit descriptive fields only — identity (code/running_no/prefix/storage_kind) is immutable."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE documents SET title = ?, doc_type = ?, notes = ?, updated_at = datetime('now') "
            "WHERE id = ?", (title, doc_type, notes, document_id))
        conn.commit()


def set_retired(db: Database, document_id: int, retired: bool) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE documents SET retired = ?, updated_at = datetime('now') WHERE id = ?",
                     (1 if retired else 0, document_id))
        conn.commit()


def delete_document(db: Database, document_id: int) -> list[str]:
    """Hard-delete the document (revisions cascade). Returns the ``rel_path`` of every file revision
    so the caller can unlink the bytes on disk."""
    with db.connect() as conn:
        paths = [r["rel_path"] for r in conn.execute(
            "SELECT rel_path FROM document_revisions WHERE document_id = ? AND rel_path IS NOT NULL",
            (document_id,))]
        conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        conn.commit()
    return paths
