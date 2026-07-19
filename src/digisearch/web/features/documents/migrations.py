"""Documents feature schema.

Two tables. ``documents`` is one row per controlled document; its identity is its Article Register
``code`` (a 5x-class document number, or ``95`` for source code), soft-linked to the register and the
catalog by the house convention ``documents.code = article_numbers.code = parts.part_no`` — no FK, so
the feature stays decoupled (mirrors the Article Register's own decision). ``document_revisions`` is
an append-only history: every upload (or link change) is a new row, exactly one flagged current.

A document is either a stored **file** (bytes live on disk under ``data/documents/``; the DB keeps
the relative path, never a BLOB) or an external **link** (a URL, e.g. a GitHub repo — source code is
never copied in). Link documents keep the live URL denormalized on ``documents`` and append a
revision row per URL change so their history is retained too.
"""

from __future__ import annotations

from ...core import Migration

MIGRATIONS = [
    Migration(
        version=1,
        name="documents",
        sql="""
        CREATE TABLE documents (
            id                  INTEGER PRIMARY KEY,
            -- allocated Article Register code 'PREFIX-NNNNN-S'; soft link (no FK) to
            --   article_numbers.code and parts.part_no.
            code                TEXT NOT NULL UNIQUE,
            -- running_no + prefix are denormalized from `code` at creation (immutable): running_no
            --   drives the family-documents panel on the Article Register page; prefix drives the
            --   class filter/badge without re-parsing the code.
            running_no          INTEGER NOT NULL,
            prefix              TEXT NOT NULL,
            title               TEXT NOT NULL,
            doc_type            TEXT,                       -- free tag: datasheet / manual / source …
            storage_kind        TEXT NOT NULL CHECK (storage_kind IN ('file', 'link')),
            -- link payload (NULL for a file document); mirrors the current link revision.
            external_url        TEXT,
            ext_ref             TEXT,                        -- git branch/tag/commit
            ext_path            TEXT,                        -- path within the repo
            -- denormalized fast-path pointer to the current revision; NOT a FK (would be circular).
            --   document_revisions.is_current + ux_docrev_current are the source of truth.
            current_revision_id INTEGER,
            notes               TEXT,
            created_by          TEXT,
            retired             INTEGER NOT NULL DEFAULT 0,
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX ix_documents_running ON documents(running_no);
        CREATE INDEX ix_documents_prefix  ON documents(prefix);

        CREATE TABLE document_revisions (
            id            INTEGER PRIMARY KEY,
            document_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            rev           TEXT NOT NULL,                     -- label 'A','B','1.0','2026-07-19' …
            -- file payload (NULL for a link revision)
            filename      TEXT,                              -- original name, used as the download name
            rel_path      TEXT,                              -- path under data/documents/, never absolute
            byte_size     INTEGER,
            content_type  TEXT,
            -- link payload (NULL for a file revision)
            external_url  TEXT,
            ext_ref       TEXT,
            ext_path      TEXT,
            supersedes_id INTEGER REFERENCES document_revisions(id),
            notes         TEXT,
            is_current    INTEGER NOT NULL DEFAULT 0,
            uploaded_by   TEXT,
            uploaded_at   TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX ix_docrev_document ON document_revisions(document_id, uploaded_at);
        -- at most one current revision per document
        CREATE UNIQUE INDEX ux_docrev_current ON document_revisions(document_id) WHERE is_current = 1;
        """,
    ),
    Migration(
        version=2,
        name="back-fill stub documents for existing document-class article numbers",
        # Every existing document-class Article Register number (50–59 documents, 95 software) becomes
        # a bare stub document (no revision yet — attach the file/link later), so it shows under the
        # product's Documents section instead of only offering "Create document". This catches numbers
        # allocated before the Documents feature existed; new families get their stubs at creation via
        # article_register._create_stub_documents. Idempotent (skips codes that already have a doc) and
        # matches _is_document_line (prefix category 'document', or prefix 95). 95 → link, else file.
        # Active numbers only — retired ones stay "Create document" until restored. Runs after
        # article_register (registered first), so article_numbers/_prefixes exist.
        sql="""
        INSERT INTO documents (code, running_no, prefix, title, storage_kind, created_by)
        SELECT a.code, a.running_no, a.prefix,
               COALESCE(NULLIF(TRIM(a.product), ''), a.code),
               CASE WHEN a.prefix = '95' THEN 'link' ELSE 'file' END,
               a.created_by
        FROM article_numbers a
        LEFT JOIN article_prefixes ap ON ap.code = a.prefix
        WHERE a.code IS NOT NULL
          AND a.retired = 0
          AND (ap.category = 'document' OR a.prefix = '95')
          AND NOT EXISTS (SELECT 1 FROM documents d WHERE d.code = a.code);
        """,
    ),
]
