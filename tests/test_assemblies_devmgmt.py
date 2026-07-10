import pytest
from fastapi.testclient import TestClient

import digisearch.web.app as web_app
from digisearch.web.features.assemblies import repo as asmrepo
from digisearch.web.features.catalog import devmgmt_outbox, devmgmt_repo


@pytest.fixture
def app(tmp_path):
    application = web_app.create_app(
        db_path=tmp_path / "p.db", data_dir=tmp_path / "data", secret_key="s")
    application.state.store.create_user("buyer1", "pw", role="purchasing")
    return application


@pytest.fixture
def db(app):
    return app.state.database


PUBLISH = {
    "editing": "0",
    "model_ref": "CONN840", "model_name": "Connectivity840",
    "radio_capabilities": "ble, lorawan, cellular", "board_revs": "B, C",
    "sku": "CONN840-WEBSHOP",   # variant ref is derived from this — no separate field
    "enabled_radios": "ble, lorawan",
    "radio_config": '{"lorawan": {"lns_default": "ttn"}}',
    "ft_component": ["mcu", "lte_modem"],
    "ft_firmware": ["MCU-CONN840-1.2.0", "ADRASTEA-06.006"],
    "ft_method": ["ota_via_mcu", "local_serial"],
}


def _setup(app):
    """Logged-in client + a fresh assembly to publish."""
    client = TestClient(app)
    client.post("/login", data={"username": "buyer1", "password": "pw"}, follow_redirects=False)
    aid = asmrepo.create_assembly(
        app.state.database, {"part_no": "CONN840-MAIN", "value": "Connectivity840 board", "rev": "C"})
    return client, aid


def _publish(client, aid, **overrides):
    return client.post(f"/assemblies/{aid}/devmgmt", data={**PUBLISH, **overrides},
                       follow_redirects=False)


# -- publish ---------------------------------------------------------------

def test_publish_creates_model_variant_and_enqueues(app, db):
    client, aid = _setup(app)
    r = _publish(client, aid)
    assert r.status_code == 303 and r.headers["location"] == f"/assemblies/{aid}"

    product = devmgmt_repo.product_for_assembly(db, aid)
    assert product["variant"]["sku"] == "CONN840-WEBSHOP"
    assert product["variant"]["ref"] == "CONN840-WEBSHOP"     # ref IS the SKU (no separate ref)
    assert product["variant"]["assembly_id"] == aid          # linked back to the assembly
    assert product["variant"]["enabled_radios"] == ["ble", "lorawan"]
    assert product["variant"]["radio_config"]["lorawan"]["lns_default"] == "ttn"
    assert [t["component"] for t in product["variant"]["flashable_targets"]] == ["mcu", "lte_modem"]
    assert product["model"]["ref"] == "CONN840"              # model ref = part number, no prefix
    assert [b["rev"] for b in product["model"]["board_revisions"]] == ["B", "C"]

    # Both were queued for pushing by the catalog-edit trigger.
    assert devmgmt_outbox.status_for(db, "model", "CONN840")["status"] == "pending"
    assert devmgmt_outbox.status_for(db, "variant", "CONN840-WEBSHOP")["status"] == "pending"


def test_detail_panel_shows_published_state(app):
    client, aid = _setup(app)
    _publish(client, aid)
    page = client.get(f"/assemblies/{aid}").text
    assert "Published as SKU" in page and "CONN840-WEBSHOP" in page
    assert "Push pending" in page                     # sync badge reflects the queued push


def test_unpublished_assembly_shows_publish_cta(app):
    client, aid = _setup(app)
    page = client.get(f"/assemblies/{aid}").text
    assert "isn’t published to devmgmt" in page
    assert f"/assemblies/{aid}/devmgmt" in page        # link to the publish form


def test_publish_form_defaults_to_part_number_without_prefixes(app):
    client, aid = _setup(app)
    form = client.get(f"/assemblies/{aid}/devmgmt").text
    assert 'name="variant_ref"' not in form             # no separate variant-ref field
    assert 'value="CONN840-MAIN"' in form               # model ref defaults to the part number
    assert "PM-" not in form and "SKU-" not in form     # prefixes gone


def test_edit_form_prefills_and_locks_model_ref(app):
    client, aid = _setup(app)
    _publish(client, aid)
    form = client.get(f"/assemblies/{aid}/devmgmt").text
    assert "CONN840" in form and "CONN840-WEBSHOP" in form   # model ref + SKU prefilled
    assert 'name="variant_ref"' not in form                  # still no variant-ref field
    assert "readonly" in form                                # model ref locked once created


def test_edit_updates_sku_but_keeps_stable_ref(app, db):
    client, aid = _setup(app)
    _publish(client, aid)
    original_ref = devmgmt_repo.product_for_assembly(db, aid)["variant"]["ref"]
    _publish(client, aid, editing="1", sku="CONN840-EU", enabled_radios="ble")
    product = devmgmt_repo.product_for_assembly(db, aid)
    assert product["variant"]["sku"] == "CONN840-EU"        # human SKU renamed
    assert product["variant"]["ref"] == original_ref        # ...but the devmgmt id is stable
    assert product["variant"]["enabled_radios"] == ["ble"]
    # Still exactly one variant for the assembly (upsert on the stable ref, not a second row).
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM variants WHERE assembly_id = ?", (aid,)).fetchone()[0] == 1


# -- validation ------------------------------------------------------------

def test_missing_sku_rejected_and_nothing_created(app, db):
    client, aid = _setup(app)
    r = _publish(client, aid, sku="")
    assert r.status_code == 400 and "required" in r.text
    assert devmgmt_repo.product_for_assembly(db, aid) is None


def test_enabled_radio_outside_capabilities_rejected(app):
    client, aid = _setup(app)
    r = _publish(client, aid, enabled_radios="ble, wifi")   # wifi not in capabilities
    assert r.status_code == 400 and "wifi" in r.text


def test_no_board_revision_rejected(app):
    client, aid = _setup(app)
    r = _publish(client, aid, board_revs="")
    assert r.status_code == 400 and "board revision" in r.text


def test_invalid_radio_config_json_rejected(app):
    client, aid = _setup(app)
    r = _publish(client, aid, radio_config="{not json")
    assert r.status_code == 400 and "valid JSON" in r.text


def test_firmware_target_needs_component_and_ref(app):
    client, aid = _setup(app)
    r = _publish(client, aid, ft_component=["mcu"], ft_firmware=[""], ft_method=["ota_via_mcu"])
    assert r.status_code == 400 and "component and a firmware ref" in r.text


# -- push now --------------------------------------------------------------

def test_push_now_reenqueues(app, db):
    client, aid = _setup(app)
    _publish(client, aid)
    with db.connect() as conn:                          # pretend the earlier push completed
        conn.execute("UPDATE devmgmt_outbox SET status = 'done'")
        conn.commit()
    r = client.post(f"/assemblies/{aid}/devmgmt/push", follow_redirects=False)
    assert r.status_code == 303
    assert devmgmt_outbox.status_for(db, "variant", "CONN840-WEBSHOP")["status"] == "pending"


def test_push_now_without_product_errors(app):
    client, aid = _setup(app)
    r = client.post(f"/assemblies/{aid}/devmgmt/push", follow_redirects=False)
    assert r.status_code == 400 and "Publish this assembly" in r.text


# -- retire / delete lifecycle (§7) ----------------------------------------

def test_retire_sets_retired_and_requeues(app, db):
    client, aid = _setup(app)
    _publish(client, aid)
    r = client.post(f"/assemblies/{aid}/devmgmt/retire", follow_redirects=False)
    assert r.status_code == 303
    assert devmgmt_repo.product_for_assembly(db, aid)["variant"]["retired_at"] is not None
    assert devmgmt_outbox.status_for(db, "variant", "CONN840-WEBSHOP")["status"] == "pending"


def test_unretire_clears_retired(app, db):
    client, aid = _setup(app)
    _publish(client, aid)
    client.post(f"/assemblies/{aid}/devmgmt/retire")
    client.post(f"/assemblies/{aid}/devmgmt/unretire")
    assert devmgmt_repo.product_for_assembly(db, aid)["variant"]["retired_at"] is None


def test_delete_requires_retire_first(app, db):
    client, aid = _setup(app)
    _publish(client, aid)
    r = client.post(f"/assemblies/{aid}/devmgmt/delete", follow_redirects=False)
    assert r.status_code == 400 and "Retire" in r.text
    assert devmgmt_repo.product_for_assembly(db, aid) is not None    # still there


def test_delete_retired_variant_removes_row_and_queues_delete(app, db, monkeypatch):
    # Unconfigured devmgmt: no remote state to converge with, so a still-queued retire push
    # doesn't block the delete. (Pinned because the developer's .env may configure devmgmt.)
    monkeypatch.setenv("DEVMGMT_BASE_URL", "")
    client, aid = _setup(app)
    _publish(client, aid)
    client.post(f"/assemblies/{aid}/devmgmt/retire")
    r = client.post(f"/assemblies/{aid}/devmgmt/delete", follow_redirects=False)
    assert r.status_code == 303
    assert devmgmt_repo.product_for_assembly(db, aid) is None        # local row gone
    assert devmgmt_outbox.status_for(db, "delete-variant", "CONN840-WEBSHOP")["status"] == "pending"
    # the now-superseded upsert job was dropped
    assert devmgmt_outbox.status_for(db, "variant", "CONN840-WEBSHOP") is None


def test_delete_blocked_while_retire_push_is_queued(app, db, monkeypatch):
    # Configured devmgmt + the retire's outbox job not flushed yet: deleting now would drop the
    # queued retire push and devmgmt would refuse the DELETE forever (retire-before-delete guard).
    monkeypatch.setenv("DEVMGMT_BASE_URL", "https://devmgmt.example.com")
    client, aid = _setup(app)
    _publish(client, aid)
    client.post(f"/assemblies/{aid}/devmgmt/retire")
    r = client.post(f"/assemblies/{aid}/devmgmt/delete", follow_redirects=False)
    assert r.status_code == 409 and "queued" in r.text
    assert devmgmt_repo.product_for_assembly(db, aid) is not None    # not deleted


def test_delete_blocked_by_referencing_device(app, db, monkeypatch):
    monkeypatch.setenv("DEVMGMT_BASE_URL", "")   # isolate this guard from the sync-pending one
    client, aid = _setup(app)
    _publish(client, aid)
    client.post(f"/assemblies/{aid}/devmgmt/retire")
    devmgmt_repo.create_device(db, serial="SN-1", variant_ref="CONN840-WEBSHOP", board_rev="C",
                               radios=[{"tech": "ble", "identity": {"ble_addr": "AA"}}])
    r = client.post(f"/assemblies/{aid}/devmgmt/delete", follow_redirects=False)
    assert r.status_code == 400 and "device" in r.text
    assert devmgmt_repo.product_for_assembly(db, aid) is not None    # not deleted


def test_panel_shows_retire_then_unretire_and_delete(app):
    client, aid = _setup(app)
    _publish(client, aid)
    active = client.get(f"/assemblies/{aid}").text
    assert "/devmgmt/retire" in active and "/devmgmt/delete" not in active
    client.post(f"/assemblies/{aid}/devmgmt/retire")
    retired = client.get(f"/assemblies/{aid}").text
    assert "Retired" in retired
    assert "/devmgmt/unretire" in retired and "/devmgmt/delete" in retired
