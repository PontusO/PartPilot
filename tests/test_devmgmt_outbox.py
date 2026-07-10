import pytest

from digisearch.devmgmt import DevmgmtPayloadError, DevmgmtReferentialError
from digisearch.web.core import FeatureRegistry
from digisearch.web.core.db import Database
from digisearch.web.features.assemblies import feature as assemblies_feature
from digisearch.web.features.assemblies import repo as asmrepo
from digisearch.web.features.catalog import feature as catalog_feature
from digisearch.web.features.catalog import devmgmt_outbox as outbox
from digisearch.web.features.catalog import devmgmt_repo as repo
from digisearch.web.features.contacts import feature as contacts_feature
from digisearch.web.features.customer_orders import feature as co_feature
from digisearch.web.features.work_orders import feature as wo_feature
from digisearch.web.features.work_orders import repo as wo_repo


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "o.db")
    reg = FeatureRegistry()
    reg.register(catalog_feature, assemblies_feature, contacts_feature, co_feature, wo_feature)
    database.apply_migrations(reg)
    return database


MODEL = dict(ref="PM-X", name="X", radio_capabilities=["ble"],
             board_revisions=[{"ref": "PM-X-C", "rev": "C"}])


def _rows(db, status=None):
    q = "SELECT kind, ref, status, attempts FROM devmgmt_outbox"
    if status:
        q += f" WHERE status = '{status}'"
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(q + " ORDER BY id")]


def _clear_outbox(db):
    with db.connect() as conn:
        conn.execute("DELETE FROM devmgmt_outbox")
        conn.commit()


def _variant(db, assembly_id=None):
    repo.upsert_model(db, **MODEL)
    repo.upsert_variant(db, ref="SKU-X", model_ref="PM-X", sku="X", enabled_radios=["ble"],
                        radio_config=None, assembly_id=assembly_id, flashable_targets=[])


# -- catalog-edit trigger --------------------------------------------------

def test_upsert_model_enqueues_a_model_job(db):
    repo.upsert_model(db, **MODEL)
    assert _rows(db) == [{"kind": "model", "ref": "PM-X", "status": "pending", "attempts": 0}]


def test_upsert_variant_enqueues_a_variant_job(db):
    _variant(db)
    kinds = {(r["kind"], r["ref"]) for r in _rows(db)}
    assert ("model", "PM-X") in kinds and ("variant", "SKU-X") in kinds


def test_re_editing_coalesces_and_resets_to_pending(db):
    repo.upsert_model(db, **MODEL)
    # Simulate the job already having been pushed (done, some attempts), then edit the model again.
    with db.connect() as conn:
        conn.execute("UPDATE devmgmt_outbox SET status = 'done', attempts = 3")
        conn.commit()
    repo.upsert_model(db, ref="PM-X", name="X v2", radio_capabilities=["ble"],
                      board_revisions=[{"ref": "PM-X-C", "rev": "C"}])
    rows = _rows(db)
    assert len(rows) == 1  # still one row (UNIQUE kind,ref)
    assert rows[0]["status"] == "pending" and rows[0]["attempts"] == 0  # reset for re-push


# -- work-order-finish trigger ---------------------------------------------

def _finished_wo_with_device(db, *, link_variant: bool):
    """Build an assembly, optionally a variant on it + a device on the WO, then run the WO to
    finished. Returns nothing — the point is the side effect on the outbox."""
    top = asmrepo.create_assembly(db, {"part_no": "TOP-DEV"})
    if link_variant:
        _variant(db, assembly_id=top)
    wo_id = wo_repo.create_work_order(db, {"assembly_id": top, "qty": 2})
    if link_variant:
        repo.create_device(db, serial="SN-1", variant_ref="SKU-X", board_rev="C",
                           radios=[{"tech": "ble", "identity": {"ble_addr": "AA"}}],
                           work_order_id=wo_id)
    _clear_outbox(db)  # isolate what *finishing* enqueues from what the setup edits enqueued
    wo_repo.issue_work_order(db, wo_id)
    wo_repo.finish_work_order(db, wo_id)


def test_finish_enqueues_variant_and_linked_device(db):
    _finished_wo_with_device(db, link_variant=True)
    got = {(r["kind"], r["ref"]) for r in _rows(db, status="pending")}
    assert got == {("variant", "SKU-X"), ("device", "SN-1")}


def test_finish_of_plain_assembly_enqueues_nothing(db):
    _finished_wo_with_device(db, link_variant=False)
    assert _rows(db) == []  # no variant maps to the assembly → nothing to push


# -- reads / ordering ------------------------------------------------------

def test_pending_jobs_are_ordered_model_then_variant_then_device(db):
    with db.connect() as conn:
        outbox.enqueue(conn, "device", "SN-9")
        outbox.enqueue(conn, "variant", "SKU-X")
        outbox.enqueue(conn, "model", "PM-X")
        conn.commit()
    assert [j["kind"] for j in outbox.pending_jobs(db)] == ["model", "variant", "device"]


def test_has_pending(db):
    assert outbox.has_pending(db) is False
    repo.upsert_model(db, **MODEL)
    assert outbox.has_pending(db) is True


# -- flush -----------------------------------------------------------------

class FakeClient:
    def __init__(self, error=None):
        self.calls = []
        self._error = error

    def _maybe_fail(self):
        if self._error is not None:
            raise self._error

    def upsert_model(self, payload):
        self.calls.append(("model", payload["ref"]))
        self._maybe_fail()

    def upsert_variant(self, payload):
        self.calls.append(("variant", payload["ref"]))
        self._maybe_fail()

    def provision_device(self, payload):
        self.calls.append(("device", payload["serial"]))
        self._maybe_fail()

    def push_all(self, *, model, variant, device):
        self.upsert_model(model)
        self.upsert_variant(variant)
        self.provision_device(device)

    def delete_variant(self, ref):
        self.calls.append(("delete-variant", ref))
        self._maybe_fail()

    def delete_model(self, ref):
        self.calls.append(("delete-model", ref))
        self._maybe_fail()


def _seed_all_kinds(db):
    _variant(db)  # enqueues model + variant
    repo.create_device(db, serial="SN-1", variant_ref="SKU-X", board_rev="C",
                       radios=[{"tech": "ble", "identity": {"ble_addr": "AA"}}])
    with db.connect() as conn:
        outbox.enqueue(conn, "device", "SN-1")  # device trigger normally does this on WO-finish
        conn.commit()


def test_flush_pushes_all_and_marks_done_and_stamps_device(db):
    _seed_all_kinds(db)
    client = FakeClient()
    report = outbox.flush(db, client)
    assert report == {"pushed": 3, "retry": 0, "errored": 0}
    assert all(r["status"] == "done" for r in _rows(db))
    # Device push stamps pushed_at (push_device -> mark_device_pushed).
    assert repo.get_device(db, "SN-1")["pushed_at"] is not None
    # The model was pushed before the device job ran (referential order held).
    assert client.calls[0] == ("model", "PM-X")


def test_flush_leaves_transient_failures_pending_for_retry(db):
    repo.upsert_model(db, **MODEL)
    report = outbox.flush(db, FakeClient(error=DevmgmtReferentialError("no model yet")))
    assert report == {"pushed": 0, "retry": 1, "errored": 0}
    row = _rows(db)[0]
    assert row["status"] == "pending" and row["attempts"] == 1  # will try again next tick


def test_flush_marks_payload_errors_permanent(db):
    repo.upsert_model(db, **MODEL)
    report = outbox.flush(db, FakeClient(error=DevmgmtPayloadError("bad")))
    assert report == {"pushed": 0, "retry": 0, "errored": 1}
    assert _rows(db)[0]["status"] == "error"  # not retried


def test_transient_failures_are_never_abandoned(db):
    repo.upsert_model(db, **MODEL)
    with db.connect() as conn:  # even a job that has already failed many times stays pending
        conn.execute("UPDATE devmgmt_outbox SET attempts = 50")
        conn.commit()
    report = outbox.flush(db, FakeClient(error=DevmgmtReferentialError("still no model")))
    assert report == {"pushed": 0, "retry": 1, "errored": 0}
    row = _rows(db)[0]
    assert row["status"] == "pending" and row["attempts"] == 51  # durable across any outage


def test_failed_job_is_backed_off_until_due(db):
    repo.upsert_model(db, **MODEL)
    outbox.flush(db, FakeClient(error=DevmgmtReferentialError("no model yet")))
    # The retry is scheduled in the future, so the job isn't due on the very next tick...
    assert outbox.pending_jobs(db) == [] and outbox.has_pending(db) is False
    with db.connect() as conn:
        row = conn.execute("SELECT status, next_attempt_at FROM devmgmt_outbox").fetchone()
        assert row["status"] == "pending" and row["next_attempt_at"] is not None
        conn.execute("UPDATE devmgmt_outbox SET next_attempt_at = datetime('now', '-1 second')")
        conn.commit()
    # ...but is picked up again once the backoff has elapsed.
    assert outbox.has_pending(db) is True and len(outbox.pending_jobs(db)) == 1


def test_re_edit_resets_backoff_to_due_now(db):
    repo.upsert_model(db, **MODEL)
    outbox.flush(db, FakeClient(error=DevmgmtReferentialError("down")))
    assert outbox.pending_jobs(db) == []      # backed off
    repo.upsert_model(db, **MODEL)            # a fresh edit re-enqueues...
    assert len(outbox.pending_jobs(db)) == 1  # ...and is due immediately


def test_done_does_not_clobber_a_reenqueue_during_push(db):
    repo.upsert_model(db, **MODEL)

    class EditsDuringPush(FakeClient):
        def upsert_model(self, payload):
            super().upsert_model(payload)
            # A user edit lands while the push is in flight → resets the row to pending. The
            # flushed payload predates the edit, so 'done' must not overwrite the fresh job.
            repo.upsert_model(db, ref="PM-X", name="edited mid-push",
                              radio_capabilities=["ble"],
                              board_revisions=[{"ref": "PM-X-C", "rev": "C"}])

    outbox.flush(db, EditsDuringPush())
    row = _rows(db)[0]
    assert row["status"] == "pending" and row["attempts"] == 0  # the edit survives for a re-send


def test_recreating_an_object_supersedes_its_queued_delete(db):
    with db.connect() as conn:
        outbox.enqueue(conn, "delete-variant", "SKU-X")
        conn.commit()
    _variant(db)  # re-creates SKU-X — the old hard-delete must not replay on top of it
    rows = {(r["kind"], r["status"]) for r in _rows(db)}
    assert ("delete-variant", "pending") not in rows
    assert ("variant", "pending") in rows


# -- delete jobs -----------------------------------------------------------

def test_flush_delete_variant_calls_client_and_marks_done(db):
    with db.connect() as conn:
        outbox.enqueue(conn, "delete-variant", "SKU-GONE")
        conn.commit()
    client = FakeClient()
    report = outbox.flush(db, client)
    assert ("delete-variant", "SKU-GONE") in client.calls
    assert report["pushed"] == 1 and _rows(db)[0]["status"] == "done"


def test_flush_delete_conflict_is_terminal(db):
    from digisearch.devmgmt import DevmgmtConflictError
    with db.connect() as conn:
        outbox.enqueue(conn, "delete-variant", "SKU-X")
        conn.commit()
    report = outbox.flush(db, FakeClient(error=DevmgmtConflictError("still referenced")))
    assert report == {"pushed": 0, "retry": 0, "errored": 1}   # guard failure isn't retried
    assert _rows(db)[0]["status"] == "error"
