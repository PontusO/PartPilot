# Generic Parts Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An admin tool under Setup & Tools that imports catalog parts from any CSV/Excel BOM via a user-confirmed column mapping, resolver enrichment, and a tick-to-create review.

**Architecture:** Lift the part-creating half of the assembly BOM importer into a shared catalog module; the assembly importer becomes a wrapper that adds BOM lines. Give `resolve_bom_file` a per-run `mappings` argument. The Setup tool is four routes + four templates + a small helper module (header reading, mapping key, pre-fill, validation, memory in `app_settings`).

**Tech Stack:** FastAPI + Jinja (hot-reloading templates), raw sqlite3 via `Database`, pandas readers in `partpilot.bom.readers`, pytest + `TestClient`, `FakeSearcher`-style monkeypatching (no live APIs).

**Spec:** `docs/superpowers/specs/2026-10-15-generic-parts-import-design.md`

## Global Constraints

- All Setup routes gate with `require_role(request, SETUP_ROLES)` (`frozenset({"admin"})`).
- Raw SQL only, no ORM. No new migration: mapping memory uses `app_settings` via `repo.get_setting/set_setting`.
- Never edit a shipped migration.
- UI follows `docs/design-rules.md`: `.panel`, `.note`, `.cards`, `.badge {ok|warn|bad|stock|muted}`, `button` / `button.ghost` / `button.quiet`, no raw hex except `#0d1219`.
- Tests never hit live APIs; monkeypatch `resolve_bom_file` in web tests.
- Commit after every task. Restart `partpilot.service` is the operator's job (needs sudo).
- Job ids are `uuid4().hex`, validated with `re.fullmatch(r"[0-9a-f]+", job_id)` before any path use.

## Review Focus

1. A file whose header row is not the first row (title rows above): `read_table` already detects the header row; the mapping screen must show *those* headers, not the title cells. Test in Task 3 (`test_read_headers_skips_title_rows`).
2. Two source columns mapped to the same target: server must reject, keep choices. Test in Task 3 (`test_parse_mapping_rejects_duplicate_target`) and Task 4 (web round-trip).
3. Header set differing only in column order must NOT reuse the remembered mapping. Test in Task 3 (`test_header_key_order_sensitive`).
4. A column the user set to "ignore" must not be fuzzy-matched by the resolver's detection. Test in Task 2 (`test_resolve_bom_file_uses_only_given_mappings`).
5. Same MPN twice in one upload must create one part and link the second. Test in Task 1 (`test_apply_parts_plan_dedupes_same_mpn`).

---

### Task 1: Shared parts-import engine in the catalog feature

**Files:**
- Create: `src/partpilot/web/features/catalog/import_parts.py`
- Modify: `src/partpilot/web/features/assemblies/import_bom.py` (becomes a wrapper)
- Test: `tests/test_import_parts_engine.py`

**Interfaces:**
- Produces: `IN_INVENTORY, NEW, UNRESOLVED, MANUAL, SKIP` str constants; `build_import_plan(db, run: ResolvedRun) -> list[dict]`; `create_part_from_item(db, item: dict) -> int`; `apply_parts_plan(db, plan: list[dict], accepted: set[int]) -> dict` returning `{"created": int, "linked": int, "skipped": int, "review": list[dict], "part_ids": list[int | None]}` where `part_ids[i]` is the catalog part for plan row `i` (`None` when nothing was created/linked for that row).
- `assemblies.import_bom.apply_import_plan(db, assembly_id, plan, accepted) -> {"created","lines_added","skipped","review"}` keeps its existing contract.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_import_parts_engine.py
import pytest

from partpilot.web.core.db import Database
from partpilot.web.core.registry import FeatureRegistry
from partpilot.web.features.assemblies.feature import FEATURE as assemblies_feature
from partpilot.web.features.catalog.feature import FEATURE as catalog_feature
from partpilot.web.features.catalog import import_parts
from partpilot.web.features.catalog import repo as catrepo


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "a.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature, assemblies_feature)
    database.apply_migrations(reg)
    return database


def test_apply_parts_plan_creates_links_and_reports(db):
    existing = catrepo.create_part(db, part={"part_no": "EXIST-MPN"}, supplier_lines=[])
    plan = [
        {"status": "in_inventory", "part_id": existing, "qty": 3, "refdes": "R1"},
        {"status": "new", "part_no": "NEW-1", "value": "4k7", "category": "RESISTOR",
         "mfr_pno": "NEW-1", "supplier_name": "Digi-Key", "supplier_pno": "NEW-1-ND",
         "unit_cost": 0.1, "reel_qty": 5000, "qty": 2, "refdes": "R2"},
        {"status": "unresolved", "part_no": "WEIRD", "value": "weird", "qty": 1, "refdes": "R3"},
        {"status": "new", "part_no": "SKIP-NEW", "qty": 1, "refdes": "R4"},   # not accepted
        {"status": "skip", "qty": 1, "refdes": "R5"},
        {"status": "manual", "part_no": "???", "qty": 1, "refdes": "L1"},
    ]
    res = import_parts.apply_parts_plan(db, plan, accepted={1, 2})
    assert res["created"] == 2 and res["linked"] == 1 and res["skipped"] == 3
    assert res["part_ids"][0] == existing
    assert res["part_ids"][1] == catrepo.find_part_id_by_mpn(db, "NEW-1")
    assert res["part_ids"][2] == catrepo.find_part_id_by_mpn(db, "WEIRD")
    assert res["part_ids"][3] is None and res["part_ids"][4] is None and res["part_ids"][5] is None


def test_apply_parts_plan_dedupes_same_mpn(db):
    plan = [
        {"status": "new", "part_no": "DUP-1", "mfr_pno": "DUP-1", "qty": 1, "refdes": "C1"},
        {"status": "new", "part_no": "DUP-1", "mfr_pno": "DUP-1", "qty": 1, "refdes": "C2"},
    ]
    res = import_parts.apply_parts_plan(db, plan, accepted={0, 1})
    assert res["created"] == 1 and res["linked"] == 1
    assert res["part_ids"][0] == res["part_ids"][1]


def test_apply_parts_plan_reports_incomplete_values(db):
    plan = [{"status": "new", "part_no": "P-1", "value": "10k//0402",
             "value_missing": ["tolerance"], "qty": 1, "refdes": "R1"}]
    res = import_parts.apply_parts_plan(db, plan, accepted={0})
    assert res["review"] == [{"part_id": res["part_ids"][0], "part_no": "P-1",
                              "value": "10k//0402", "missing": ["tolerance"]}]


def test_assembly_wrapper_still_exposes_engine_names():
    from partpilot.web.features.assemblies import import_bom
    assert import_bom.build_import_plan is import_parts.build_import_plan
    assert import_bom.NEW == "new"
```

- [ ] **Step 2: Run tests, expect ImportError**

Run: `uv run pytest tests/test_import_parts_engine.py -q`
Expected: FAIL — `No module named 'partpilot.web.features.catalog.import_parts'`

- [ ] **Step 3: Create the shared module**

Move the constants, `_target_mpn`, `_category`, `_value_for`, `build_import_plan`, `_create_part` (renamed `create_part_from_item`) verbatim from `assemblies/import_bom.py` into `catalog/import_parts.py`, adjusting imports (`from . import repo as catalog_repo`, `from ..purchasing.service import ResolvedRun`). Add:

```python
def apply_parts_plan(db: Database, plan: list[dict], accepted: set[int]) -> dict:
    """Create the accepted NEW/UNRESOLVED rows and link IN_INVENTORY rows.

    Returns counts plus ``review`` (created passives with an incomplete iLabs value) and
    ``part_ids``: one entry per plan row, the catalog part id that row now points at, or None
    for rows that were skipped / not accepted / manual.
    """
    created = linked = skipped = 0
    review: list[dict] = []
    part_ids: list[int | None] = []
    for i, item in enumerate(plan):
        status = item.get("status")
        if status == IN_INVENTORY:
            part_ids.append(item["part_id"])
            linked += 1
            continue
        if status in (NEW, UNRESOLVED) and i in accepted:
            # Re-use an existing catalog part if its MPN already landed (avoids duplicates,
            # including the same MPN appearing twice in one file).
            pid = catalog_repo.find_part_id_by_mpn(db, item.get("part_no"))
            if pid is None:
                pid = create_part_from_item(db, item)
                created += 1
                if item.get("value_missing"):
                    review.append({"part_id": pid, "part_no": item.get("part_no"),
                                   "value": item.get("value"), "missing": item["value_missing"]})
            else:
                linked += 1
            part_ids.append(pid)
            continue
        skipped += 1          # SKIP, MANUAL, or not accepted
        part_ids.append(None)
    return {"created": created, "linked": linked, "skipped": skipped,
            "review": review, "part_ids": part_ids}
```

- [ ] **Step 4: Rewrite `assemblies/import_bom.py` as the wrapper**

```python
"""Import a CSV BOM onto an assembly, reusing the shared parts-import engine.

The part-level work (classify resolved lines, create accepted parts) lives in
``catalog/import_parts.py`` so the Setup "Import parts" tool shares it. This module adds the
assembly step: one BOM line per row that now has a catalog part.
"""

from __future__ import annotations

from ...core.db import Database
from ..catalog.import_parts import (  # noqa: F401  (re-exported for callers/tests)
    IN_INVENTORY, MANUAL, NEW, SKIP, UNRESOLVED, build_import_plan, apply_parts_plan,
)
from . import repo as asm_repo


def apply_import_plan(db: Database, assembly_id: int, plan: list[dict], accepted: set[int]) -> dict:
    """Create accepted parts and add a BOM line per row that has a part."""
    res = apply_parts_plan(db, plan, accepted)
    lines_added = 0
    for item, child_id in zip(plan, res["part_ids"]):
        if child_id is None:
            continue
        asm_repo.add_bom_line(db, assembly_id, child_id, item.get("qty") or 1, item.get("refdes"))
        lines_added += 1
    return {"created": res["created"], "lines_added": lines_added,
            "skipped": res["skipped"], "review": res["review"]}
```

Note the old `skipped` counted SKIP rows + unaccepted rows; `apply_parts_plan` also counts MANUAL rows as skipped. `test_apply_import_plan_creates_and_links` asserts `created == 2 and lines_added == 3` only, so this is compatible.

- [ ] **Step 5: Run new + existing tests**

Run: `uv run pytest tests/test_import_parts_engine.py tests/test_assemblies.py -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/partpilot/web/features/catalog/import_parts.py src/partpilot/web/features/assemblies/import_bom.py tests/test_import_parts_engine.py
git commit -m "Lift parts-import engine out of the assembly importer into catalog/import_parts"
```

---

### Task 2: Per-run column mappings on `resolve_bom_file`

**Files:**
- Modify: `src/partpilot/web/features/purchasing/service.py:62-85`
- Test: `tests/test_purchasing_service_mappings.py`

**Interfaces:**
- Produces: `resolve_bom_file(bom_path, *, ..., mappings: dict | None = None)`. When `mappings` is given it replaces the yaml aliases entirely.

- [ ] **Step 1: Write failing test**

```python
# tests/test_purchasing_service_mappings.py
from types import SimpleNamespace

import partpilot.web.features.purchasing.service as service


def _stub_clients(monkeypatch):
    monkeypatch.setattr(service.DigiKeyCredentials, "from_env",
                        classmethod(lambda cls, sandbox=False: SimpleNamespace(locale_currency="SEK")))
    monkeypatch.setattr(service.MouserCredentials, "from_env", classmethod(lambda cls: None))
    import partpilot.digikey.client as dk
    monkeypatch.setattr(dk, "DigiKeyClient", lambda creds: object())


def test_resolve_bom_file_uses_only_given_mappings(tmp_path, monkeypatch):
    _stub_clients(monkeypatch)
    captured = {}
    def fake_resolve_bom(lines, client, settings, build_qty, lookup, stock, mouser, reel):
        captured["lines"] = lines
        return []
    monkeypatch.setattr(service, "resolve_bom", fake_resolve_bom)

    bom = tmp_path / "b.csv"
    # "Value" would normally be detected as value; the user mapped only Wert -> value and
    # deliberately ignored "Value" and "Designator".
    bom.write_text("Designator,Wert,Value,Menge\nR1,10k,IGNORED,2\n")
    service.resolve_bom_file(bom, build_qty=1, check_stock=False,
                             mappings={"value": ["Wert"], "qty": ["Menge"]})
    [line] = captured["lines"]
    assert line.value == "10k" and line.qty == 2
    assert line.refdes == [] and line.device is None


def test_resolve_bom_file_defaults_to_yaml_mappings(tmp_path, monkeypatch):
    _stub_clients(monkeypatch)
    captured = {}
    monkeypatch.setattr(service, "resolve_bom",
                        lambda lines, *a: captured.setdefault("lines", lines) and [])
    bom = tmp_path / "b.csv"
    bom.write_text("Designator,Value,Qty\nR1,10k,1\n")
    service.resolve_bom_file(bom, build_qty=1, check_stock=False)
    assert captured["lines"][0].refdes == ["R1"]
```

- [ ] **Step 2: Run, expect TypeError on unexpected kwarg**

Run: `uv run pytest tests/test_purchasing_service_mappings.py -q`
Expected: first test FAILS with `unexpected keyword argument 'mappings'`.

- [ ] **Step 3: Implement**

In `resolve_bom_file` signature add `mappings: dict | None = None,` after `database`. Replace `mappings = load_column_mappings(None)` with `mappings = mappings if mappings is not None else load_column_mappings(None)`. Extend the docstring: "Pass ``mappings`` (``{field: [header, ...]}``) to override the yaml column aliases for this run — only the given fields are detected, so an omitted field is never fuzzy-matched."

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_purchasing_service_mappings.py tests/test_pipeline_report.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/partpilot/web/features/purchasing/service.py tests/test_purchasing_service_mappings.py
git commit -m "resolve_bom_file: accept per-run column mappings"
```

---

### Task 3: Mapping helper module (headers, key, pre-fill, validation, memory)

**Files:**
- Create: `src/partpilot/web/features/setup/import_parts.py`
- Test: `tests/test_setup_import_parts_mapping.py`

**Interfaces:**
- Produces:
  - `TARGETS: list[tuple[str, str]]` = `[("refdes","RefDes"),("qty","Qty"),("value","Value"),("device","Device / MPN"),("package","Package"),("description","Description"),("comment","Comment")]`
  - `read_headers(path: Path) -> tuple[list[str], list[list[str]]]` — headers and up to 3 sample values per column (parallel list). Raises `ValueError` on unreadable/empty.
  - `header_key(headers: list[str]) -> str` — `"bom_import.mapping." + sha1`.
  - `guess_mapping(headers) -> dict[str, str]` — alias guess via `detect_columns(headers, load_column_mappings())`.
  - `remembered_mapping(db, headers) -> dict[str, str] | None`
  - `remember_mapping(db, headers, mapping) -> None`
  - `prefill_mapping(db, headers) -> dict[str, str]` — remembered else guess.
  - `parse_mapping(form_items: dict[str, str], headers) -> dict[str, str]` — form keys `map_<index>` with target values; returns `{field: header}`; raises `ValueError` with a user message on duplicate target or missing value/device.
  - `run_mappings(mapping: dict[str, str]) -> dict[str, list[str]]` — `{field: [header]}`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_setup_import_parts_mapping.py
import pytest

from partpilot.web.core.db import Database
from partpilot.web.core.registry import FeatureRegistry
from partpilot.web.features.setup import import_parts as ip
from partpilot.web.features.setup.feature import FEATURE as setup_feature


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "s.db")
    reg = FeatureRegistry()
    reg.register(setup_feature)
    database.apply_migrations(reg)
    return database


def test_read_headers_skips_title_rows(tmp_path):
    p = tmp_path / "bom.csv"
    p.write_text("My Project BOM,,,\nexported 2026-10-15,,,\n"
                 "Designator,Value,Footprint,Qty\nR1,10k,0402,1\nC1,100n,0402,1\nU1,MCU,QFN,1\nX1,,,1\n")
    headers, samples = ip.read_headers(p)
    assert headers == ["Designator", "Value", "Footprint", "Qty"]
    assert samples[1] == ["10k", "100n", "MCU"]         # max 3, non-empty only
    assert samples[0] == ["R1", "C1", "U1"]


def test_read_headers_rejects_unknown_suffix(tmp_path):
    p = tmp_path / "bom.pdf"
    p.write_bytes(b"%PDF")
    with pytest.raises(ValueError):
        ip.read_headers(p)


def test_header_key_normalises_but_is_order_sensitive():
    a = ip.header_key(["Designator", "Value", "Qty"])
    assert a == ip.header_key([" designator", "VALUE ", "qty"])
    assert a != ip.header_key(["Value", "Designator", "Qty"])
    assert a != ip.header_key(["Designator", "Value", "Qty", "Extra"])
    assert a.startswith("bom_import.mapping.")


def test_guess_mapping_uses_aliases():
    m = ip.guess_mapping(["Designator", "Value", "Footprint", "Qty", "Weird"])
    assert m == {"refdes": "Designator", "value": "Value", "package": "Footprint", "qty": "Qty"}


def test_prefill_prefers_remembered(db):
    headers = ["Designator", "Wert", "Value"]
    assert ip.remembered_mapping(db, headers) is None
    assert ip.prefill_mapping(db, headers)["value"] == "Value"      # alias guess
    ip.remember_mapping(db, headers, {"refdes": "Designator", "value": "Wert"})
    assert ip.prefill_mapping(db, headers) == {"refdes": "Designator", "value": "Wert"}
    assert ip.remembered_mapping(db, ["Wert", "Designator", "Value"]) is None   # reordered


def test_parse_mapping_ok():
    headers = ["Designator", "Value", "Notes"]
    m = ip.parse_mapping({"map_0": "refdes", "map_1": "value", "map_2": "ignore"}, headers)
    assert m == {"refdes": "Designator", "value": "Value"}
    assert ip.run_mappings(m) == {"refdes": ["Designator"], "value": ["Value"]}


def test_parse_mapping_rejects_duplicate_target():
    with pytest.raises(ValueError, match="Value.*twice|twice.*Value|more than once"):
        ip.parse_mapping({"map_0": "value", "map_1": "value"}, ["A", "B"])


def test_parse_mapping_requires_value_or_device():
    with pytest.raises(ValueError, match="Value.*Device"):
        ip.parse_mapping({"map_0": "refdes", "map_1": "qty"}, ["A", "B"])


def test_parse_mapping_ignores_unknown_targets_and_indexes():
    m = ip.parse_mapping({"map_0": "device", "map_1": "bogus", "map_9": "value", "x": "y"}, ["A", "B"])
    assert m == {"device": "A"}
```

- [ ] **Step 2: Run, expect ImportError**

Run: `uv run pytest tests/test_setup_import_parts_mapping.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# src/partpilot/web/features/setup/import_parts.py
"""Column mapping for the Setup "Import parts from BOM" tool.

Reads a file's headers + sample values, pre-fills a mapping (remembered for this exact header
set, else the yaml alias guess), validates the user's choices, and remembers them in
``app_settings`` so the next upload of the same layout is pre-filled.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from partpilot.bom.normalize import _norm, detect_columns
from partpilot.bom.readers import read_table
from partpilot.config import load_column_mappings

from ...core.db import Database
from . import repo

# (field, label) — the BomLine fields the resolver understands.
TARGETS: list[tuple[str, str]] = [
    ("refdes", "RefDes"), ("qty", "Qty"), ("value", "Value"), ("device", "Device / MPN"),
    ("package", "Package"), ("description", "Description"), ("comment", "Comment"),
]
TARGET_FIELDS = {f for f, _ in TARGETS}
IGNORE = "ignore"
SAMPLES = 3


def read_headers(path: Path) -> tuple[list[str], list[list[str]]]:
    """Headers and up to SAMPLES non-empty values per column. Raises ValueError if unreadable."""
    df = read_table(path)          # raises ValueError on unsupported suffix
    headers = [str(c) for c in df.columns]
    if not headers:
        raise ValueError("No header row found in the file.")
    samples: list[list[str]] = []
    for col in df.columns:
        vals = [str(v).strip() for v in df[col].tolist()]
        samples.append([v for v in vals if v][:SAMPLES])
    return headers, samples


def header_key(headers: list[str]) -> str:
    joined = "\x1f".join(_norm(h) for h in headers)
    return "bom_import.mapping." + hashlib.sha1(joined.encode("utf-8")).hexdigest()


def guess_mapping(headers: list[str]) -> dict[str, str]:
    return detect_columns(list(headers), load_column_mappings())


def remembered_mapping(db: Database, headers: list[str]) -> dict[str, str] | None:
    raw = repo.get_setting(db, header_key(headers))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    mapping = data.get("mapping") or {}
    # Only keep entries that still point at a real header + known field.
    return {f: h for f, h in mapping.items() if f in TARGET_FIELDS and h in headers} or None


def remember_mapping(db: Database, headers: list[str], mapping: dict[str, str]) -> None:
    payload = {"headers": list(headers), "mapping": dict(mapping),
               "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    repo.set_setting(db, header_key(headers), json.dumps(payload))


def prefill_mapping(db: Database, headers: list[str]) -> dict[str, str]:
    return remembered_mapping(db, headers) or guess_mapping(headers)


def parse_mapping(form_items: dict[str, str], headers: list[str]) -> dict[str, str]:
    """Turn ``map_<i> -> target`` form fields into ``{field: header}``.

    Raises ValueError with a message for the user when a target is used twice or neither
    Value nor Device / MPN is mapped.
    """
    labels = dict(TARGETS)
    mapping: dict[str, str] = {}
    for key, target in form_items.items():
        if not key.startswith("map_") or not key[4:].isdigit():
            continue
        idx = int(key[4:])
        if idx >= len(headers) or target not in TARGET_FIELDS:
            continue
        if target in mapping:
            raise ValueError(
                f"'{labels[target]}' is mapped more than once ({mapping[target]} and {headers[idx]}). "
                "Each PartPilot field can take one column.")
        mapping[target] = headers[idx]
    if "value" not in mapping and "device" not in mapping:
        raise ValueError("Map at least one column to Value or Device / MPN.")
    return mapping


def run_mappings(mapping: dict[str, str]) -> dict[str, list[str]]:
    """The per-run ``mappings`` for ``resolve_bom_file``: only the fields the user mapped."""
    return {field: [header] for field, header in mapping.items()}
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_setup_import_parts_mapping.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/partpilot/web/features/setup/import_parts.py tests/test_setup_import_parts_mapping.py
git commit -m "Setup import-parts: header reading, mapping key/pre-fill/validation/memory"
```

---

### Task 4: Routes, templates, Setup index entry

**Files:**
- Modify: `src/partpilot/web/features/setup/router.py` (imports, `TOOLS`, 4 routes)
- Create: `src/partpilot/web/features/setup/templates/import_parts_upload.html`, `import_parts_map.html`, `import_parts_review.html`, `import_parts_result.html`
- Test: `tests/test_setup_import_parts_web.py`

**Interfaces:**
- Consumes: Task 1 `build_import_plan`, `apply_parts_plan`; Task 2 `resolve_bom_file(..., mappings=)`; Task 3 helpers.
- Routes: `GET /setup/import-parts`, `POST /setup/import-parts`, `POST /setup/import-parts/resolve`, `POST /setup/import-parts/apply`.
- Form field names: upload `file`, `build_qty`; map `job_id`, `map_<i>`, `build_qty`; apply `job_id`, `accept` (multi).
- Files in `jobs_dir`: `pimp-<job>.<suffix>` (upload), `pimeta-<job>.json` (`{"source": name, "headers": [...], "build_qty": int|null}`), `piplan-<job>.json`.

- [ ] **Step 1: Write failing web tests**

```python
# tests/test_setup_import_parts_web.py
import json

import pytest
from fastapi.testclient import TestClient

import partpilot.web.app as web_app
import partpilot.web.features.setup.router as setup_router
from partpilot.bom.normalize import load_bom
from partpilot.models import Candidate, LineKind, ResolvedLine, Status
from partpilot.web.features.purchasing.service import ResolvedRun


@pytest.fixture
def app(tmp_path):
    application = web_app.create_app(
        db_path=tmp_path / "partpilot.db", data_dir=tmp_path / "data", secret_key="test-secret")
    application.state.store.create_user("buyer1", "pw", role="purchasing")
    return application


def _admin(app):
    c = TestClient(app)
    user = app.state.store.ensure_admin("admin", "pw")  # see note below
    c.post("/login", data={"username": "admin", "password": "pw"})
    return c


def _fake_resolve(bom_path, *, build_qty=None, database=None, mappings=None, **kw):
    """Parse the file with the given mappings (so the test proves the mapping reached the
    resolver), then hand back canned outcomes keyed on the value text."""
    lines = load_bom(bom_path, mappings)
    out = []
    for ln in lines:
        v = (ln.value or ln.device or "")
        if v == "EXIST":
            out.append(ResolvedLine(line=ln, kind=LineKind.MPN, status=Status.RESOLVED, confidence=1,
                                    chosen=Candidate(supplier="Digi-Key", mpn="EXIST", dk_part_number="E-ND")))
        elif v == "10k":
            out.append(ResolvedLine(line=ln, kind=LineKind.GENERIC_PASSIVE, status=Status.RESOLVED,
                                    confidence=0.9, chosen=Candidate(supplier="Digi-Key", mpn="RES-10K",
                                    manufacturer="Yageo", dk_part_number="R-ND", unit_price=0.01)))
        elif v == "":
            out.append(ResolvedLine(line=ln, kind=LineKind.MPN, status=Status.MANUAL))
        else:
            out.append(ResolvedLine(line=ln, kind=LineKind.MPN, status=Status.NOT_FOUND))
    return ResolvedRun(resolved=out, build_qty=build_qty or 1, currency="SEK",
                       stock_checked=False, mouser_enabled=False, warnings=["fake warning"])


CSV = "Title row,,,\nDesignator,Wert,Value,Qty\nR1,10k,junk,1\nU1,EXIST,junk,1\nX1,odd,junk,1\nL1,,junk,1\n"


def test_import_parts_requires_admin(app):
    c = TestClient(app)
    c.post("/login", data={"username": "buyer1", "password": "pw"})
    assert c.get("/setup/import-parts").status_code == 403


def test_upload_shows_headers_samples_and_guesses(app):
    c = _admin(app)
    r = c.post("/setup/import-parts", files={"file": ("bom.csv", CSV, "text/csv")})
    assert r.status_code == 200
    html = r.text
    for h in ("Designator", "Wert", "Value", "Qty"):
        assert h in html
    assert "Title row" not in html.split("<table")[1]   # header detection skipped the title
    assert "10k" in html and "EXIST" in html             # sample values
    # alias guesses pre-selected: Designator->refdes, Value->value, Qty->qty; Wert -> ignore
    assert 'name="map_0"' in html and 'value="refdes" selected' in html
    assert 'value="value" selected' in html


def test_full_flow_maps_resolves_reviews_and_applies(app, monkeypatch):
    monkeypatch.setattr(setup_router, "resolve_bom_file", _fake_resolve)
    from partpilot.web.features.catalog import repo as catrepo
    existing = catrepo.create_part(app.state.database, part={"part_no": "EXIST"}, supplier_lines=[])

    c = _admin(app)
    r = c.post("/setup/import-parts", files={"file": ("bom.csv", CSV, "text/csv")})
    job_id = r.text.split('name="job_id" value="')[1].split('"')[0]

    # Map Wert -> value (the user corrects the guess), ignore Value.
    r = c.post("/setup/import-parts/resolve", data={
        "job_id": job_id, "map_0": "refdes", "map_1": "value", "map_2": "ignore", "map_3": "qty"})
    assert r.status_code == 200, r.text
    html = r.text
    assert "fake warning" in html
    assert "in inventory" in html and "RES-10K" in html
    assert html.count('name="accept"') == 2            # new (10k) + unresolved (odd)
    assert "manual" in html

    # Remembered for next time.
    r2 = c.post("/setup/import-parts", files={"file": ("bom2.csv", CSV, "text/csv")})
    assert 'name="map_1"' in r2.text
    sel = r2.text.split('name="map_1"')[1].split("</select>")[0]
    assert 'value="value" selected' in sel

    # Apply: accept only the new row (index 0 in plan order R1, U1, X1, L1).
    r = c.post("/setup/import-parts/apply", data={"job_id": job_id, "accept": ["0"]})
    assert r.status_code == 200
    assert catrepo.find_part_id_by_mpn(app.state.database, "RES-10K") is not None
    assert catrepo.find_part_id_by_mpn(app.state.database, "odd") is None
    assert "1</b><small>parts created" in r.text.replace("\n", "")
    assert "RES-10K" in r.text


def test_mapping_validation_round_trips(app):
    c = _admin(app)
    r = c.post("/setup/import-parts", files={"file": ("bom.csv", CSV, "text/csv")})
    job_id = r.text.split('name="job_id" value="')[1].split('"')[0]
    r = c.post("/setup/import-parts/resolve", data={
        "job_id": job_id, "map_0": "value", "map_1": "value", "map_2": "ignore", "map_3": "qty"})
    assert r.status_code == 400
    assert "more than once" in r.text
    assert 'name="map_1"' in r.text          # back on the mapping screen, file still there
    r = c.post("/setup/import-parts/resolve", data={
        "job_id": job_id, "map_0": "refdes", "map_1": "ignore", "map_2": "ignore", "map_3": "qty"})
    assert r.status_code == 400 and "Value or Device" in r.text


def test_resolve_failure_returns_to_mapping(app, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no creds")
    monkeypatch.setattr(setup_router, "resolve_bom_file", boom)
    c = _admin(app)
    r = c.post("/setup/import-parts", files={"file": ("bom.csv", CSV, "text/csv")})
    job_id = r.text.split('name="job_id" value="')[1].split('"')[0]
    r = c.post("/setup/import-parts/resolve", data={"job_id": job_id, "map_1": "value"})
    assert r.status_code == 500 and "Resolve failed: no creds" in r.text
    assert 'value="value" selected' in r.text.split('name="map_1"')[1].split("</select>")[0]


def test_bad_upload_rejected(app):
    c = _admin(app)
    r = c.post("/setup/import-parts", files={"file": ("x.pdf", b"%PDF", "application/pdf")})
    assert r.status_code == 400 and "Unsupported" in r.text
    r = c.post("/setup/import-parts", data={})
    assert r.status_code == 400


def test_expired_job_handled(app):
    c = _admin(app)
    r = c.post("/setup/import-parts/resolve", data={"job_id": "deadbeef", "map_0": "value"})
    assert r.status_code == 400 and "expired" in r.text.lower()
    r = c.post("/setup/import-parts/apply", data={"job_id": "deadbeef"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup/import-parts"
```

Note on `_admin`: check `tests/test_web.py` for how an admin logs in (the `create_app` initial admin is created from `PARTPILOT_ADMIN_USER/PASSWORD` or generated). Use whatever helper existing setup tests use (e.g. `test_company_details_settings`); replace `ensure_admin` with that mechanism.

- [ ] **Step 2: Run, expect 404s**

Run: `uv run pytest tests/test_setup_import_parts_web.py -q`
Expected: FAIL (404 on `/setup/import-parts`).

- [ ] **Step 3: Router — imports, TOOLS entry, routes**

Add imports: `import json, re`, `from partpilot.config import Settings` (exists), `from ..catalog.import_parts import apply_parts_plan, build_import_plan`, `from ..purchasing.service import resolve_bom_file`, `from . import import_parts as ip`.

Prepend to `TOOLS`:
```python
    {"label": "Import parts from BOM", "url": "/setup/import-parts", "icon": "📥",
     "description": "Upload any CSV/Excel BOM or part list, map its columns to PartPilot fields, "
                    "resolve each row against stock and Digi-Key/Mouser, and tick the parts to "
                    "create in the catalog. Mappings are remembered per file layout."},
```

Routes (append before the part-cleanup section):

```python
# ---- Import parts from any BOM ------------------------------------------------------------

_JOB_RE = re.compile(r"[0-9a-f]+")


def _ip_paths(jobs_dir: Path, job_id: str, suffix: str = "") -> tuple[Path, Path, Path]:
    return (jobs_dir / f"pimp-{job_id}{suffix}", jobs_dir / f"pimeta-{job_id}.json",
            jobs_dir / f"piplan-{job_id}.json")


def _ip_upload_page(request, error=None, status=200):
    return request.app.state.templates.TemplateResponse(
        request, "import_parts_upload.html", {"error": error}, status_code=status)


def _ip_map_page(request, *, job_id, meta, samples, mapping, error=None, status=200):
    return request.app.state.templates.TemplateResponse(
        request, "import_parts_map.html",
        {"job_id": job_id, "source": meta["source"], "headers": meta["headers"],
         "samples": samples, "mapping": mapping, "targets": ip.TARGETS,
         "build_qty": meta.get("build_qty"), "error": error}, status_code=status)


@router.get("/import-parts", response_class=HTMLResponse)
def import_parts_form(request: Request):
    require_role(request, SETUP_ROLES)
    return _ip_upload_page(request)


@router.post("/import-parts", response_class=HTMLResponse)
async def import_parts_upload(request: Request):
    require_role(request, SETUP_ROLES)
    db = request.app.state.database
    jobs_dir: Path = request.app.state.jobs_dir
    form = await request.form()
    upload = form.get("file")
    if not upload or not getattr(upload, "filename", ""):
        return _ip_upload_page(request, "Choose a CSV or Excel file.", 400)
    build_qty = (form.get("build_qty") or "").strip()
    build_qty = int(build_qty) if build_qty.isdigit() and int(build_qty) > 0 else None

    job_id = uuid4().hex
    suffix = Path(upload.filename).suffix.lower()
    bom_path, meta_path, _ = _ip_paths(jobs_dir, job_id, suffix)
    bom_path.write_bytes(await upload.read())
    try:
        headers, samples = ip.read_headers(bom_path)
    except ValueError as exc:
        bom_path.unlink(missing_ok=True)
        return _ip_upload_page(request, str(exc), 400)
    meta = {"source": Path(upload.filename).name, "headers": headers, "build_qty": build_qty,
            "suffix": suffix}
    meta_path.write_text(json.dumps(meta))
    return _ip_map_page(request, job_id=job_id, meta=meta, samples=samples,
                        mapping=ip.prefill_mapping(db, headers))


def _ip_load_job(jobs_dir: Path, job_id: str):
    """(bom_path, meta_path, plan_path, meta) or None if the job is unknown/expired."""
    if not _JOB_RE.fullmatch(job_id or ""):
        return None
    _, meta_path, plan_path = _ip_paths(jobs_dir, job_id)
    if not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text())
    bom_path = _ip_paths(jobs_dir, job_id, meta.get("suffix", ""))[0]
    return bom_path, meta_path, plan_path, meta


@router.post("/import-parts/resolve", response_class=HTMLResponse)
async def import_parts_resolve(request: Request):
    require_role(request, SETUP_ROLES)
    db = request.app.state.database
    jobs_dir: Path = request.app.state.jobs_dir
    form = await request.form()
    job_id = (form.get("job_id") or "").strip()
    job = _ip_load_job(jobs_dir, job_id)
    if job is None or not job[0].exists():
        return _ip_upload_page(request, "Upload expired — please upload the file again.", 400)
    bom_path, meta_path, plan_path, meta = job
    headers = meta["headers"]
    _, samples = ip.read_headers(bom_path)
    chosen = {k: v for k, v in form.items() if k.startswith("map_")}
    # What the user picked, as {field: header}, for re-rendering on error.
    picked = {v: headers[int(k[4:])] for k, v in chosen.items()
              if k[4:].isdigit() and int(k[4:]) < len(headers) and v in ip.TARGET_FIELDS}
    try:
        mapping = ip.parse_mapping(chosen, headers)
    except ValueError as exc:
        return _ip_map_page(request, job_id=job_id, meta=meta, samples=samples,
                            mapping=picked, error=str(exc), status=400)
    ip.remember_mapping(db, headers, mapping)

    try:
        run = await run_in_threadpool(
            resolve_bom_file, bom_path, build_qty=meta.get("build_qty"), database=db,
            mappings=ip.run_mappings(mapping))
    except Exception as exc:  # missing creds, network, bad rows
        return _ip_map_page(request, job_id=job_id, meta=meta, samples=samples,
                            mapping=mapping, error=f"Resolve failed: {exc}", status=500)
    finally:
        pass  # keep the upload until apply so a failed resolve can be retried from the map screen

    plan = build_import_plan(db, run)
    plan_path.write_text(json.dumps(plan))
    return request.app.state.templates.TemplateResponse(
        request, "import_parts_review.html",
        {"plan": plan, "job_id": job_id, "warnings": run.warnings, "source": meta["source"]})


@router.post("/import-parts/apply")
async def import_parts_apply(request: Request):
    require_role(request, SETUP_ROLES)
    db = request.app.state.database
    jobs_dir: Path = request.app.state.jobs_dir
    form = await request.form()
    job_id = (form.get("job_id") or "").strip()
    accepted = {int(v) for v in form.getlist("accept") if str(v).isdigit()}
    job = _ip_load_job(jobs_dir, job_id)
    if job is None or not job[2].exists():
        return RedirectResponse("/setup/import-parts", status_code=303)
    bom_path, meta_path, plan_path, meta = job
    plan = json.loads(plan_path.read_text())
    result = apply_parts_plan(db, plan, accepted)
    for p in (bom_path, meta_path, plan_path):
        p.unlink(missing_ok=True)
    created = [{"part_id": pid, "part_no": it.get("part_no"), "value": it.get("value")}
               for it, pid in zip(plan, result["part_ids"])
               if pid is not None and it.get("status") != "in_inventory"]
    return request.app.state.templates.TemplateResponse(
        request, "import_parts_result.html",
        {"result": result, "created": created, "source": meta["source"]})
```

Spec deviation, deliberate: the spec said to delete the upload after a failed resolve. Keeping it lets the user fix the mapping and retry without re-uploading, which is what the "mapping retained" error path is for. Files are removed at apply. Note this in the spec's error table when committing.

- [ ] **Step 4: Templates**

`import_parts_upload.html`:
```html
{% extends "base.html" %}
{% block title %}Import parts · PartPilot{% endblock %}
{% block content %}
<div class="panel">
  <p class="note"><a href="/setup">← Setup &amp; Tools</a></p>
  <h1>Import parts from BOM</h1>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <p class="note">Upload any CSV or Excel BOM / part list. You'll map its columns to PartPilot
    fields next; the rows are then resolved against stock and Digi-Key/Mouser (the same engine the
    purchasing tool uses) and you choose which parts to create in the catalog. Nothing is attached
    to an assembly.</p>
  <form method="post" action="/setup/import-parts" enctype="multipart/form-data">
    <label for="file">BOM file (.csv / .tsv / Excel)</label>
    <input id="file" name="file" type="file" accept=".csv,.tsv,.txt,.xlsx,.xlsm,.xls" required>
    <label for="build_qty">Build quantity for stock/pricing check (optional)</label>
    <input id="build_qty" name="build_qty" type="number" min="1" step="1" placeholder="settings default">
    <button type="submit">Next: map columns</button>
  </form>
</div>
{% endblock %}
```

`import_parts_map.html`:
```html
{% extends "base.html" %}
{% block title %}Map columns · PartPilot{% endblock %}
{% block content %}
<style>
  #colmap td, #colmap th { padding:.4rem .5rem; vertical-align:top; }
  #colmap select { margin:0; min-width:11rem; }
  #colmap .sample { color:var(--muted); font-size:.82rem; }
</style>
<div class="panel">
  <p class="note"><a href="/setup/import-parts">← Import parts</a></p>
  <h1>Map columns <span class="flag" style="font-size:.9rem;">— {{ source }}</span></h1>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <p class="note">Each column of the file is listed with a few sample values. Choose which
    PartPilot field it feeds, or leave it as <b>ignore</b>. At least <b>Value</b> or
    <b>Device / MPN</b> must be mapped. The mapping is remembered for files with this header
    layout.</p>
  <form method="post" action="/setup/import-parts/resolve" onsubmit="showOverlay()">
    <input type="hidden" name="job_id" value="{{ job_id }}">
    {% set rev = {} %}{% for f, h in mapping.items() %}{% set _ = rev.update({h: f}) %}{% endfor %}
    <div style="overflow-x:auto;">
      <table id="colmap">
        <thead><tr><th>File column</th><th>Sample values</th><th>PartPilot field</th></tr></thead>
        <tbody>
          {% for h in headers %}
          {% set cur = rev.get(h) %}
          <tr>
            <td><b>{{ h }}</b></td>
            <td class="sample">{% for s in samples[loop.index0] %}<code>{{ s }}</code>{% if not loop.last %} · {% endif %}{% endfor %}</td>
            <td>
              <select name="map_{{ loop.index0 }}">
                <option value="ignore" {{ 'selected' if not cur else '' }}>— ignore —</option>
                {% for f, label in targets %}
                <option value="{{ f }}" {{ 'selected' if cur == f else '' }}>{{ label }}</option>
                {% endfor %}
              </select>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    <div style="margin-top:1.2rem; display:flex; gap:.8rem;">
      <button type="submit" style="margin-top:0;">Resolve rows</button>
      <button type="button" class="quiet" onclick="window.location.href='/setup/import-parts'">Cancel</button>
    </div>
  </form>
</div>
<div id="overlay">
  <div class="spin"></div>
  <div>Resolving against Digi-Key/Mouser… this can take a minute.</div>
</div>
<script>function showOverlay(){ document.getElementById('overlay').style.display = 'flex'; }</script>
{% endblock %}
```

`import_parts_review.html`: copy `assembly_import_review.html`, change back-link to `/setup/import-parts`, title "Review parts", form action `/setup/import-parts/apply`, note text ("Tick the parts to create in the catalog…"), submit label "Create ticked parts", cancel → `/setup/import-parts`. For `in_inventory` rows render the resolved part cell as a link to `/catalog/{{ it.part_id }}`.

`import_parts_result.html`: copy `assembly_import_result.html`; cards: `result.created` "parts created", `result.linked` "already in catalog", `result.skipped` "skipped"; add a "Created parts" table (`created` list: part_no linking to `/catalog/{{ p.part_id }}`, value); keep the "Needs checking" table; buttons: "Import another file" → `/setup/import-parts`, "Back to Setup & Tools" (`quiet`) → `/setup`.

- [ ] **Step 5: Run web tests, iterate until green**

Run: `uv run pytest tests/test_setup_import_parts_web.py -q`
Expected: PASS. Check `#overlay` / `.spin` exist in `base.html` (they're used by the assembly importer, so they do).

- [ ] **Step 6: Full suite**

Run: `uv run pytest -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/partpilot/web/features/setup tests/test_setup_import_parts_web.py
git commit -m "Setup & Tools: Import parts from any BOM (map columns -> resolve -> review -> create)"
```

---

### Task 5: Docs

**Files:**
- Modify: `docs/design-rules.md` (change log), `README.md` (Setup & Tools section), spec error table (upload kept until apply).

- [ ] **Step 1: design-rules change log entry**

Append under the change log, dated 2026-10-15: "Setup → Import parts from BOM: four-screen wizard (upload → map → review → result). The **column-mapping table** pattern: one row per source column, header in bold, sample values as `<code>` separated by `·` in `--muted`, a `<select>` per row with an `— ignore —` first option. Review/result screens reuse the assembly-import badge set (`ok` in inventory, `warn` new, `bad` unresolved/manual, `muted` skip) and stat `.cards`."

- [ ] **Step 2: README**

In the Setup & Tools feature description, add a bullet: "**Import parts from BOM** — upload any CSV/Excel part list, map its columns to PartPilot fields (remembered per layout), resolve rows against stock and Digi-Key/Mouser, tick the parts to create. Catalog only; use an assembly's Import BOM to build a BOM."

- [ ] **Step 3: Spec error-table fix**

Change the "Resolver raises" row: upload file is kept so the user can retry from the mapping screen; all job files are deleted at apply.

- [ ] **Step 4: Commit**

```bash
git add docs/design-rules.md README.md docs/superpowers/specs/2026-10-15-generic-parts-import-design.md
git commit -m "Docs: Import parts tool in README, design-rules change log, spec error table"
```
