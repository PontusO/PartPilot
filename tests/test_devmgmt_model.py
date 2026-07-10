import pytest

from digisearch.web.core import FeatureRegistry
from digisearch.web.core.db import Database
from digisearch.web.features.catalog import feature as catalog_feature
from digisearch.web.features.catalog import devmgmt_outbox
from digisearch.web.features.catalog import devmgmt_push as push
from digisearch.web.features.catalog import devmgmt_repo as repo


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "c.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature)
    database.apply_migrations(reg)
    return database


MODEL = dict(
    ref="PM-CONN840", name="Connectivity840",
    radio_capabilities=["ble", "lorawan", "cellular"],
    board_revisions=[{"ref": "PM-CONN840-B", "rev": "B"}, {"ref": "PM-CONN840-C", "rev": "C"}],
)
VARIANT = dict(
    ref="SKU-CONN840-WEBSHOP", model_ref="PM-CONN840", sku="CONN840-WEBSHOP",
    enabled_radios=["ble", "lorawan", "cellular"],
    radio_config={"lorawan": {"profile_id": "0100000A", "lns_default": "ttn"}},
    flashable_targets=[
        {"component": "mcu", "factory_firmware_ref": "MCU-CONN840-1.2.0", "update_method": "ota_via_mcu"},
        {"component": "lte_modem", "factory_firmware_ref": "ADRASTEA-06.006", "update_method": "local_serial"},
    ],
)
DEVICE = dict(
    serial="CONN840-000042", variant_ref="SKU-CONN840-WEBSHOP", board_rev="C",
    radios=[{"tech": "lorawan", "identity": {"dev_eui": "00", "join_eui": "01"},
             "secrets": {"app_key": "FF"}}],
)


def _seed(db):
    repo.upsert_model(db, **MODEL)
    repo.upsert_variant(db, **VARIANT)
    return repo.create_device(db, **DEVICE)


def test_model_roundtrip_parses_json_and_board_revs(db):
    repo.upsert_model(db, **MODEL)
    got = repo.get_model(db, "PM-CONN840")
    assert got["name"] == "Connectivity840"
    assert got["radio_capabilities"] == ["ble", "lorawan", "cellular"]  # JSON parsed to a list
    assert [b["rev"] for b in got["board_revisions"]] == ["B", "C"]


def test_model_upsert_is_idempotent_and_resyncs_board_revs(db):
    repo.upsert_model(db, **MODEL)
    # Re-upsert with a renamed model and one fewer board revision.
    repo.upsert_model(db, ref="PM-CONN840", name="Connectivity840 v2",
                      radio_capabilities=["ble"], board_revisions=[{"ref": "PM-CONN840-C", "rev": "C"}])
    got = repo.get_model(db, "PM-CONN840")
    assert got["name"] == "Connectivity840 v2"
    assert [b["rev"] for b in got["board_revisions"]] == ["C"]  # dropped revision B


def test_variant_requires_existing_model(db):
    with pytest.raises(ValueError, match="Unknown model_ref"):
        repo.upsert_variant(db, **VARIANT)


def test_variant_roundtrip(db):
    repo.upsert_model(db, **MODEL)
    repo.upsert_variant(db, **VARIANT)
    got = repo.get_variant(db, "SKU-CONN840-WEBSHOP")
    assert got["model_ref"] == "PM-CONN840"
    assert got["enabled_radios"] == ["ble", "lorawan", "cellular"]
    assert got["radio_config"]["lorawan"]["lns_default"] == "ttn"
    assert [t["component"] for t in got["flashable_targets"]] == ["mcu", "lte_modem"]


def test_create_device_generates_owner_token(db):
    repo.upsert_model(db, **MODEL)
    repo.upsert_variant(db, **VARIANT)
    token = repo.create_device(db, **DEVICE)
    assert len(token) == 64  # 32 bytes hex
    got = repo.get_device(db, "CONN840-000042")
    assert got["owner_token"] == token
    assert got["radios"][0]["identity"]["dev_eui"] == "00"


def test_create_device_keeps_token_across_reprovision(db):
    repo.upsert_model(db, **MODEL)
    repo.upsert_variant(db, **VARIANT)
    first = repo.create_device(db, **DEVICE)
    # Re-run tester intake for the same serial — the QR is already printed, so the token must hold.
    second = repo.create_device(db, **DEVICE)
    assert first == second


def test_reprovision_of_pushed_device_clears_pushed_at_and_requeues(db):
    _seed(db)
    push.push_device(db, _FakeClient(), "CONN840-000042")   # pushed_at stamped
    repo.create_device(db, **DEVICE)   # tester re-runs intake (e.g. corrected radio identities)
    # devmgmt now holds stale data: the device must count as un-pushed and a re-push be queued.
    assert repo.get_device(db, "CONN840-000042")["pushed_at"] is None
    assert devmgmt_outbox.status_for(db, "device", "CONN840-000042")["status"] == "pending"


def test_upsert_model_rejects_dropping_a_rev_devices_reference(db):
    _seed(db)   # the seeded device sits on rev C
    with pytest.raises(ValueError, match="board revision"):
        repo.upsert_model(db, ref="PM-CONN840", name="Connectivity840",
                          radio_capabilities=["ble"],
                          board_revisions=[{"ref": "PM-CONN840-B", "rev": "B"}])   # drops C
    # The rejected edit rolled back — both revisions are still there.
    assert [b["rev"] for b in repo.get_model(db, "PM-CONN840")["board_revisions"]] == ["B", "C"]


def test_create_device_rejects_unknown_board_rev(db):
    repo.upsert_model(db, **MODEL)
    repo.upsert_variant(db, **VARIANT)
    bad = {**DEVICE, "board_rev": "Z"}
    with pytest.raises(ValueError, match="board_rev"):
        repo.create_device(db, **bad)


def test_build_payloads_matches_contract_shapes(db):
    _seed(db)
    model, variant, device = push.build_payloads(db, "CONN840-000042")
    # §5.1
    assert model == {
        "ref": "PM-CONN840", "name": "Connectivity840",
        "radio_capabilities": ["ble", "lorawan", "cellular"],
        "board_revisions": [{"ref": "PM-CONN840-B", "rev": "B"}, {"ref": "PM-CONN840-C", "rev": "C"}],
        "retired": False,
    }
    # §5.2
    assert variant["ref"] == "SKU-CONN840-WEBSHOP" and variant["model_ref"] == "PM-CONN840"
    assert variant["sku"] == "CONN840-WEBSHOP"
    assert variant["radio_config"]["lorawan"]["profile_id"] == "0100000A"
    assert variant["flashable_targets"][0]["update_method"] == "ota_via_mcu"
    # §5.3
    assert device["serial"] == "CONN840-000042" and device["board_rev"] == "C"
    assert device["variant_ref"] == "SKU-CONN840-WEBSHOP"
    assert len(device["owner_token"]) == 64
    assert device["radios"][0]["tech"] == "lorawan"


class _FakeClient:
    def __init__(self):
        self.calls = []

    def push_all(self, *, model, variant, device):
        self.calls.append((model["ref"], variant["ref"], device["serial"]))


def test_push_device_pushes_then_marks_pushed(db):
    _seed(db)
    assert db.connect().execute(
        "SELECT pushed_at FROM device_builds WHERE serial = ?", ("CONN840-000042",)
    ).fetchone()[0] is None
    client = _FakeClient()
    push.push_device(db, client, "CONN840-000042")
    assert client.calls == [("PM-CONN840", "SKU-CONN840-WEBSHOP", "CONN840-000042")]
    pushed_at = db.connect().execute(
        "SELECT pushed_at FROM device_builds WHERE serial = ?", ("CONN840-000042",)
    ).fetchone()[0]
    assert pushed_at is not None  # stamped after a successful push


def test_build_payloads_unknown_serial_raises(db):
    with pytest.raises(ValueError, match="No device build"):
        push.build_payloads(db, "NOPE-1")


def test_payloads_carry_retired_flag(db):
    _seed(db)
    model, variant, _ = push.build_payloads(db, "CONN840-000042")
    assert model["retired"] is False and variant["retired"] is False   # §7 default
    repo.set_variant_retired(db, "SKU-CONN840-WEBSHOP", True)
    _, variant2, _ = push.build_payloads(db, "CONN840-000042")
    assert variant2["retired"] is True                                 # retire flows into the push
    repo.set_variant_retired(db, "SKU-CONN840-WEBSHOP", False)
    _, variant3, _ = push.build_payloads(db, "CONN840-000042")
    assert variant3["retired"] is False                                # un-retire clears it


def test_delete_variant_guard_count(db):
    _seed(db)   # seeds a device on the variant
    assert repo.variant_device_count(db, "SKU-CONN840-WEBSHOP") == 1


def test_variant_payload_omits_null_radio_config(db):
    repo.upsert_model(db, **MODEL)
    repo.upsert_variant(db, ref="SKU-BARE", model_ref="PM-CONN840", sku="BARE",
                        enabled_radios=["ble"], radio_config=None, flashable_targets=[])
    variant = push.variant_payload(repo.get_variant(db, "SKU-BARE"))
    assert "radio_config" not in variant
