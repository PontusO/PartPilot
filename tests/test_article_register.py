import pytest

from digisearch.web.core import FeatureRegistry
from digisearch.web.core.db import Database
from digisearch.web.features.article_register import feature as article_register_feature
from digisearch.web.features.article_register import importer, repo
from digisearch.web.features.article_register.codes import article_code
from digisearch.web.features.article_register.repo import DuplicateNumber
from digisearch.web.features.catalog import feature as catalog_feature


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "a.db")
    reg = FeatureRegistry()
    # catalog first (as in production) so `parts` exists for the soft code→part_no join.
    reg.register(catalog_feature, article_register_feature)
    database.apply_migrations(reg)
    return database


def test_prefixes_seeded_and_grouped(db):
    prefixes = repo.list_prefixes(db)
    assert len(prefixes) == 25
    by_code = {p["code"]: p for p in prefixes}
    assert by_code["98"]["label"] == "Assemblies" and by_code["98"]["category"] == "internal"
    assert by_code["05"]["label"] == "Brainlit" and by_code["05"]["category"] == "customer"
    groups = {g["category"]: len(g["prefixes"]) for g in repo.prefixes_grouped(db)}
    assert groups == {"customer": 8, "ic": 1, "document": 10, "internal": 6}


def test_allocation_and_code_format(db):
    assert repo.next_running_no(db) == 1
    eid = repo.create_entry(db, prefix="98", running_no=2, suffix=4, product="Enterprise PCBA")
    entry = repo.get_entry(db, eid)
    assert entry["code"] == "98-00002-4" == article_code("98", 2, 4)
    assert repo.next_running_no(db) == 3  # MAX(2)+1
    assert repo.next_suffix(db, "98", 2) == 5  # MAX(4)+1
    assert repo.next_suffix(db, "99", 2) == 1  # different prefix, empty


def test_prefix_is_zero_padded(db):
    eid = repo.create_entry(db, prefix="5", running_no=357, suffix=1)  # customer 5 → '05'
    assert repo.get_entry(db, eid)["code"] == "05-00357-1"


def test_duplicate_triplet_rejected(db):
    repo.create_entry(db, prefix="98", running_no=390, suffix=1)
    with pytest.raises(DuplicateNumber):
        repo.create_entry(db, prefix="98", running_no=390, suffix=1)


def test_create_product_makes_one_family(db):
    # A product = one running number with a line per ticked group, all suffix 1.
    running_no = repo.create_product(db, product="AddBox 200", prefixes=["98", "54", "99"],
                                     created_by="LO")
    assert running_no == 1
    family = repo.get_family(db, running_no)
    codes = [e["code"] for e in family]
    assert codes == ["54-00001-1", "98-00001-1", "99-00001-1"]  # ordered by prefix
    assert all(e["product"] == "AddBox 200" and e["created_by"] == "LO" for e in family)
    # Next product gets the next running number.
    assert repo.create_product(db, product="AddBox 300", prefixes=["98"]) == 2


def test_create_product_dedupes_and_requires_a_group(db):
    running_no = repo.create_product(db, product="X", prefixes=["98", "98", ""])
    assert len(repo.get_family(db, running_no)) == 1  # duplicate 98 collapsed to one line
    with pytest.raises(ValueError):
        repo.create_product(db, product="Y", prefixes=[])


def test_retire_flag_does_not_block_other_suffix(db):
    a = repo.create_entry(db, prefix="99", running_no=2, suffix=4, product="V1.2 PCB")
    repo.set_retired(db, a, True)
    assert repo.get_entry(db, a)["retired"] == 1
    # A different suffix in the same family is still allocatable.
    b = repo.create_entry(db, prefix="99", running_no=2, suffix=5)
    assert repo.get_entry(db, b)["code"] == "99-00002-5"
    repo.set_retired(db, a, False)
    assert repo.get_entry(db, a)["retired"] == 0


def test_family_view_gathers_the_running_number(db):
    repo.create_entry(db, prefix="97", running_no=2, suffix=4)
    repo.create_entry(db, prefix="98", running_no=2, suffix=4)
    repo.create_entry(db, prefix="99", running_no=2, suffix=1)
    repo.create_entry(db, prefix="98", running_no=3, suffix=1)  # different family
    codes = [e["code"] for e in repo.get_family(db, 2)]
    assert codes == ["97-00002-4", "98-00002-4", "99-00002-1"]


def test_soft_catalog_link(db):
    with db.connect() as conn:
        conn.execute("INSERT INTO parts (part_no, kind) VALUES ('98-00002-4', 'ASSY')")
        conn.commit()
    eid = repo.create_entry(db, prefix="98", running_no=2, suffix=4)
    entry = next(e for e in repo.list_entries(db) if e["id"] == eid)
    assert entry["part_id"] is not None
    # A code with no matching part has a null link.
    other = repo.create_entry(db, prefix="99", running_no=2, suffix=1)
    assert next(e for e in repo.list_entries(db) if e["id"] == other)["part_id"] is None


def test_list_filters_and_retired_visibility(db):
    good = repo.create_entry(db, prefix="98", running_no=2, suffix=4, product="Enterprise")
    dead = repo.create_entry(db, prefix="99", running_no=2, suffix=4, product="Old PCB")
    repo.set_retired(db, dead, True)
    active = {e["id"] for e in repo.list_entries(db)}
    assert good in active and dead not in active  # retired hidden by default
    assert dead in {e["id"] for e in repo.list_entries(db, include_retired=True)}
    assert {e["id"] for e in repo.list_entries(db, category="internal")} == {good}
    assert {e["id"] for e in repo.list_entries(db, search="enterprise")} == {good}


def _build_workbook(path):
    import openpyxl
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Artikellista"
    ws.append(["Prefix", "Artikelnummer", "Suffix", "Produkt", "Upplagd av", "Shortform"])
    data = [
        ("98", 2, 4, "Enterprise PCBA", "PO", "98-00002-4", False),
        ("99", 2, 4, "Enterprise PCB (old)", "PO", "99-00002-4", True),  # struck → retired
        ("", 50, "", "", "", "", False),                                 # reserved running number
        ("", "", "", "", "", "", False),                                 # empty → skipped
    ]
    for prefix, num, suffix, product, by, code, struck in data:
        row = ws.max_row + 1
        ws.cell(row=row, column=1, value=prefix)
        ws.cell(row=row, column=2, value=num)
        ws.cell(row=row, column=3, value=suffix)
        ws.cell(row=row, column=4, value=product)
        ws.cell(row=row, column=5, value=by)
        code_cell = ws.cell(row=row, column=6, value=code)
        if struck:
            code_cell.font = Font(strike=True)
    wb.save(path)


def test_importer_marks_retired_and_skips_reserved(db, tmp_path):
    xlsx = tmp_path / "reg.xlsx"
    _build_workbook(xlsx)
    stats = importer.import_register(db, xlsx)
    assert stats["article numbers"] == 2
    assert stats["reserved skipped"] == 1  # blank-prefix placeholder row is NOT imported
    assert stats["article retired"] == 1
    # Only the two assigned numbers land; no reserved rows.
    assert repo.summary(db) == {"total": 2, "reserved": 0, "retired": 1, "families": 1}

    fam = {e["code"]: e for e in repo.get_family(db, 2)}
    assert fam["99-00002-4"]["retired"] == 1
    assert fam["98-00002-4"]["retired"] == 0
    assert repo.get_family(db, 50) == []  # the reserved running number was skipped


def test_importer_is_idempotent(db, tmp_path):
    xlsx = tmp_path / "reg.xlsx"
    _build_workbook(xlsx)
    importer.import_register(db, xlsx)
    before = repo.summary(db)
    stats = importer.import_register(db, xlsx)
    # Re-run: both assigned rows already exist (skipped) + the empty row = 3 skipped.
    assert stats == {"article numbers": 0, "article retired": 0,
                     "reserved skipped": 1, "article skipped": 3}
    assert repo.summary(db) == before
