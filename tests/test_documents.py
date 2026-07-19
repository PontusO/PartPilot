import pytest

from digisearch.web.core import FeatureRegistry
from digisearch.web.core.db import Database
from digisearch.web.features.article_register import feature as article_register_feature
from digisearch.web.features.article_register import repo as ar_repo
from digisearch.web.features.article_register.codes import article_code
from digisearch.web.features.catalog import feature as catalog_feature
from digisearch.web.features.documents import feature as documents_feature
from digisearch.web.features.documents import repo


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "a.db")
    reg = FeatureRegistry()
    # production order: catalog + article_register own the tables documents soft-joins by code.
    reg.register(catalog_feature, article_register_feature, documents_feature)
    database.apply_migrations(reg)
    return database


def _alloc(db, prefix, *, product="Doc"):
    """Allocate an article code the way the router does, returning (running_no, code)."""
    rn = ar_repo.next_running_no(db)
    ar_repo.create_entry(db, prefix=prefix, running_no=rn, suffix=1, product=product)
    return rn, article_code(prefix, rn, 1)


def test_backfill_stub_documents_for_existing_article_numbers(tmp_path):
    """The documents v2 migration turns pre-existing document-class article numbers into stub
    documents. Simulated by allocating numbers BEFORE the Documents feature is registered, then
    applying its migrations."""
    db = Database(tmp_path / "backfill.db")
    reg1 = FeatureRegistry()
    reg1.register(catalog_feature, article_register_feature)     # no documents table yet
    db.apply_migrations(reg1)

    def alloc(prefix, product, *, retired=False):
        rn = ar_repo.next_running_no(db)
        ar_repo.create_entry(db, prefix=prefix, running_no=rn, suffix=1, product=product)
        code = article_code(prefix, rn, 1)
        if retired:
            with db.connect() as conn:
                conn.execute("UPDATE article_numbers SET retired = 1 WHERE code = ?", (code,))
                conn.commit()
        return code

    doc = alloc("54", "Old Schematic")          # document class → stub file document
    soft = alloc("95", "Firmware repo")         # software → stub link document
    part = alloc("99", "A component")           # not a document → left alone
    gone = alloc("54", "Retired drawing", retired=True)  # retired → left alone

    # Now register the Documents feature and apply its migrations → v2 back-fills.
    reg2 = FeatureRegistry()
    reg2.register(catalog_feature, article_register_feature, documents_feature)
    db.apply_migrations(reg2)

    d = repo.document_for_code(db, doc)
    assert d and d["storage_kind"] == "file" and d["title"] == "Old Schematic"
    s = repo.document_for_code(db, soft)
    assert s and s["storage_kind"] == "link"
    assert repo.document_for_code(db, part) is None   # 99 component is not a document
    assert repo.document_for_code(db, gone) is None   # retired numbers are not back-filled

    # Idempotent: re-applying does not duplicate.
    db.apply_migrations(reg2)
    assert repo.document_for_code(db, doc) is not None


def test_document_prefixes_are_document_class_plus_software(db):
    codes = [p["code"] for p in repo.document_prefixes(db)]
    assert codes == ["50", "51", "52", "53", "54", "55", "56", "57", "58", "59", "95"]


def test_create_file_document_and_first_revision(db):
    rn, code = _alloc(db, "54", product="Schematic")
    doc_id = repo.create_document(db, code=code, running_no=rn, prefix="54", title="Schematic",
                                  storage_kind="file")
    rev_id = repo.add_file_revision(db, doc_id, rev="A", filename="sch.pdf", rel_path=f"{doc_id}/x.pdf",
                                    byte_size=123, content_type="application/pdf", uploaded_by="PO")
    doc = repo.get_document(db, doc_id)
    assert doc["code"] == code and doc["storage_kind"] == "file"
    assert doc["current_revision_id"] == rev_id
    assert len(doc["revisions"]) == 1 and doc["revisions"][0]["is_current"] == 1


def test_create_link_document_95_source(db):
    rn, code = _alloc(db, "95", product="Firmware")
    doc_id = repo.create_document(db, code=code, running_no=rn, prefix="95", title="Firmware",
                                  storage_kind="link")
    repo.add_link_revision(db, doc_id, rev="A", external_url="https://github.com/org/repo",
                           ext_ref="main")
    doc = repo.get_document(db, doc_id)
    assert doc["storage_kind"] == "link"
    assert doc["external_url"] == "https://github.com/org/repo" and doc["ext_ref"] == "main"
    # a link revision stores no file
    assert doc["revisions"][0]["rel_path"] is None


def test_upload_new_revision_appends_and_flips_current(db):
    rn, code = _alloc(db, "54")
    doc_id = repo.create_document(db, code=code, running_no=rn, prefix="54", title="Drawing",
                                  storage_kind="file")
    a = repo.add_file_revision(db, doc_id, rev="A", filename="a.pdf", rel_path=f"{doc_id}/a.pdf",
                               byte_size=10)
    b = repo.add_file_revision(db, doc_id, rev="B", filename="b.pdf", rel_path=f"{doc_id}/b.pdf",
                               byte_size=20)
    doc = repo.get_document(db, doc_id)
    current = [r for r in doc["revisions"] if r["is_current"]]
    assert len(current) == 1 and current[0]["id"] == b  # exactly one current, the newest
    assert doc["revisions"][-1]["id"] == a and doc["revisions"][-1]["is_current"] == 0
    # B supersedes A
    assert next(r for r in doc["revisions"] if r["id"] == b)["supersedes_id"] == a


def test_link_revision_history_retained(db):
    rn, code = _alloc(db, "95")
    doc_id = repo.create_document(db, code=code, running_no=rn, prefix="95", title="FW",
                                  storage_kind="link")
    repo.add_link_revision(db, doc_id, rev="A", external_url="https://github.com/org/repo")
    repo.add_link_revision(db, doc_id, rev="B", external_url="https://github.com/org/repo2")
    doc = repo.get_document(db, doc_id)
    assert doc["external_url"] == "https://github.com/org/repo2"  # live URL follows the newest
    urls = {r["external_url"] for r in doc["revisions"]}
    assert urls == {"https://github.com/org/repo", "https://github.com/org/repo2"}  # both kept


def test_set_current_reverts_to_older_revision(db):
    rn, code = _alloc(db, "54")
    doc_id = repo.create_document(db, code=code, running_no=rn, prefix="54", title="D",
                                  storage_kind="file")
    a = repo.add_file_revision(db, doc_id, rev="A", filename="a", rel_path=f"{doc_id}/a", byte_size=1)
    repo.add_file_revision(db, doc_id, rev="B", filename="b", rel_path=f"{doc_id}/b", byte_size=1)
    assert repo.set_current_revision(db, doc_id, a) is True
    doc = repo.get_document(db, doc_id)
    assert doc["current_revision_id"] == a
    assert [r for r in doc["revisions"] if r["is_current"]][0]["id"] == a
    # a foreign revision id is rejected
    assert repo.set_current_revision(db, doc_id, 9999) is False


def test_family_documents_by_running_no(db):
    rn, code = _alloc(db, "54", product="Board")
    d1 = repo.create_document(db, code=code, running_no=rn, prefix="54", title="Sch",
                              storage_kind="file")
    ar_repo.create_entry(db, prefix="95", running_no=rn, suffix=1, product="FW")
    d2 = repo.create_document(db, code=article_code("95", rn, 1), running_no=rn, prefix="95",
                              title="FW", storage_kind="link")
    codes = {d["code"] for d in repo.family_documents(db, rn)}
    assert codes == {article_code("54", rn, 1), article_code("95", rn, 1)}
    # the Article Register guarded read sees the same set
    assert {d["code"] for d in ar_repo.list_family_documents(db, rn)} == codes


def test_retire_and_restore(db):
    rn, code = _alloc(db, "54")
    doc_id = repo.create_document(db, code=code, running_no=rn, prefix="54", title="D",
                                  storage_kind="file")
    repo.set_retired(db, doc_id, True)
    assert repo.get_document(db, doc_id)["retired"] == 1
    assert repo.list_documents(db) == []                       # hidden by default
    assert len(repo.list_documents(db, include_retired=True)) == 1
    repo.set_retired(db, doc_id, False)
    assert repo.get_document(db, doc_id)["retired"] == 0


def test_delete_returns_file_paths_and_cascades(db):
    rn, code = _alloc(db, "54")
    doc_id = repo.create_document(db, code=code, running_no=rn, prefix="54", title="D",
                                  storage_kind="file")
    repo.add_file_revision(db, doc_id, rev="A", filename="a", rel_path=f"{doc_id}/a", byte_size=1)
    repo.add_file_revision(db, doc_id, rev="B", filename="b", rel_path=f"{doc_id}/b", byte_size=1)
    paths = repo.delete_document(db, doc_id)
    assert set(paths) == {f"{doc_id}/a", f"{doc_id}/b"}
    assert repo.get_document(db, doc_id) is None
    with db.connect() as conn:  # revisions cascaded
        assert conn.execute("SELECT COUNT(*) FROM document_revisions").fetchone()[0] == 0
