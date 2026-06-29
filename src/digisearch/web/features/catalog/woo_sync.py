"""Two-way stock reconcile between PartPilot and the WooCommerce webshop.

The shop and the MRP share one physical stock pool but each moves it independently: the
webshop **sells** (stock down), PartPilot **builds/receives** (stock up). A plain overwrite
in either direction loses one side's change, so instead we reconcile *changes since the last
sync* against a stored per-part baseline (``parts.webshop_synced_qty`` = the Woo quantity as
of the last sync):

    woo_delta = W - B           # how Woo moved (sales are negative)
    R         = P + woo_delta   # PartPilot's reconciled on-hand (its own builds are in P)

We post ``woo_delta`` into PartPilot (WOOSALE for a sale, ADJUST for a manual webshop increase)
and push ``R`` back to the shop, then set the baseline to whatever Woo actually ends up at.
After a sync ``PartPilot on-hand == Woo qty == baseline`` so drift reconciles every cycle with
no double-counting.

Cold start (no baseline yet) / a SKU not in PartPilot: the webshop wins absolutely — adopt
Woo's value and record it as the baseline; nothing is pushed. ``sync_from_woo`` takes an
iterable of products (network-free testable) plus the client used for the push.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import repo, stock
from ..assemblies import repo as assemblies_repo
from ...core.db import Database

PART_PREFIX = "99-"   # components
ASSY_PREFIX = "98-"   # assemblies
SALE_REFERENCE = "woo-sale"
SYNC_REFERENCE = "woo-sync"
_EPS = 1e-9


@dataclass
class SyncReport:
    created_parts: int = 0
    created_assemblies: int = 0
    updated: int = 0          # PartPilot on-hand changed to reflect the webshop
    unchanged: int = 0        # nothing moved on either side
    unmanaged: int = 0        # matched, but Woo doesn't manage the product's stock
    skipped: int = 0          # SKU prefix not 98-/99-
    sold: float = 0.0         # units issued from webshop sales
    pushed: int = 0           # products whose Woo stock was set (or would be, in a preview)
    pending_push: int = 0     # pull-only run: products PartPilot is ahead on, push deferred
    log: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    @property
    def matched(self) -> int:
        return self.updated + self.unchanged + self.unmanaged

    @property
    def created(self) -> int:
        return self.created_parts + self.created_assemblies

    def _note(self, sku, action, detail=""):
        self.log.append({"sku": sku, "action": action, "detail": detail})


def kind_for_sku(sku: str) -> str | None:
    """'PART' for 99-… , 'ASSY' for 98-… , else None (unknown — caller skips)."""
    if sku.startswith(PART_PREFIX):
        return "PART"
    if sku.startswith(ASSY_PREFIX):
        return "ASSY"
    return None


def sync_from_woo(db: Database, products, *, client=None, user: str | None = None,
                  dry_run: bool = False, push: bool = True) -> SyncReport:
    """Reconcile PartPilot against the webshop's products.

    ``push=False`` makes this a **pull-only** run: webshop sales still flow into PartPilot and
    prices/new parts are adopted, but PartPilot's own stock is never written back to Woo (the
    deferred quantities are reported as ``pending_push``). The scheduled auto-sync uses this so
    an unattended timer never mutates the live shop; the manual button keeps ``push=True``.
    """
    report = SyncReport()
    pushes: list[tuple[int, int, float]] = []   # (part_id, woo_product_id, new_qty)
    for product in products:
        try:
            _reconcile_one(db, product, user=user, dry_run=dry_run, report=report, pushes=pushes)
        except Exception as exc:  # one bad product must not abort the whole run
            report.errors.append({"sku": getattr(product, "sku", "?"), "error": str(exc)})

    report.pushed = len(pushes)
    if pushes and not dry_run and not push:
        report.pending_push = report.pushed   # pulled only — leave Woo (and the baseline) alone
        report.pushed = 0
    elif pushes and not dry_run:
        if client is None:
            raise ValueError("A WooCommerce client is required to push stock updates.")
        try:
            client.update_stock_batch([(woo_id, qty) for _pid, woo_id, qty in pushes])
            for part_id, _woo_id, qty in pushes:   # Woo now holds qty — advance the baseline
                _set_baseline(db, part_id, qty)
        except Exception as exc:
            report.errors.append({"sku": "(push)", "error": str(exc)})
            report.pushed = 0   # PartPilot adjustments stand; Woo is unchanged, baselines stay at W
    return report


def _reconcile_one(db, product, *, user, dry_run, report: SyncReport, pushes) -> None:
    sku = product.sku
    kind = kind_for_sku(sku)
    if kind is None:
        report.skipped += 1
        report._note(sku, "skipped", "SKU prefix is not 98- or 99-")
        return

    existing = repo.find_part_by_part_no(db, sku)
    if existing is None:
        _create(db, product, kind=kind, user=user, dry_run=dry_run, report=report)
        return

    # Copy the shop price into external_price (book-keeping), independent of stock.
    if not dry_run and product.price is not None:
        _set_external_price(db, existing["id"], product.price)

    if product.stock_quantity is None:
        report.unmanaged += 1
        report._note(sku, "unmanaged", "Woo does not manage this product's stock")
        return

    woo_qty = product.stock_quantity
    on_hand = existing.get("total_qty") or 0.0
    baseline = existing.get("webshop_synced_qty")

    # Cold start: no baseline yet — the webshop wins, and we record the baseline. No push.
    if baseline is None:
        delta = woo_qty - on_hand
        if not dry_run:
            _apply(db, existing["id"], delta=delta, mtype=stock.ADJUST, reference=SYNC_REFERENCE,
                   note="WooCommerce sync (initial baseline)", user=user, baseline=woo_qty)
        if abs(delta) > _EPS:
            report.updated += 1
            report._note(sku, "adopted", f"stock set to {woo_qty:g} (first sync)")
        else:
            report.unchanged += 1
            report._note(sku, "unchanged", f"stock already {woo_qty:g}")
        return

    woo_delta = woo_qty - baseline          # webshop movement since last sync (sales < 0)
    reconciled = on_hand + woo_delta        # PartPilot's new on-hand (its own builds are in on_hand)
    changed_pp = abs(woo_delta) > _EPS
    need_push = abs(reconciled - woo_qty) > _EPS

    if changed_pp:
        if woo_delta < 0:
            mtype, ref, note = stock.WOOSALE, SALE_REFERENCE, "WooCommerce sale"
            report.sold += -woo_delta
        else:
            mtype, ref, note = stock.ADJUST, SYNC_REFERENCE, "WooCommerce stock increase"
        if not dry_run:
            # Set the provisional baseline to W (what Woo currently holds), atomic with the move.
            _apply(db, existing["id"], delta=woo_delta, mtype=mtype, reference=ref,
                   note=note, user=user, baseline=woo_qty)
        report.updated += 1
        report._note(sku, "sold" if woo_delta < 0 else "raised",
                     f"{on_hand:g} -> {reconciled:g} (Woo {baseline:g} -> {woo_qty:g})")

    if need_push:
        pushes.append((existing["id"], product.id, reconciled))
        report._note(sku, "push", f"set Woo to {reconciled:g}")
    elif not changed_pp:
        report.unchanged += 1
        report._note(sku, "unchanged", f"in sync at {woo_qty:g}")


def _create(db, product, *, kind, user, dry_run, report: SyncReport) -> None:
    sku = product.sku
    qty = product.stock_quantity
    if dry_run:
        if kind == "ASSY":
            report.created_assemblies += 1
        else:
            report.created_parts += 1
        report._note(sku, "would create",
                     f"{kind}{f', stock {qty:g}' if qty is not None else ''}")
        return

    if kind == "ASSY":
        part_id = assemblies_repo.create_assembly(db, {
            "part_no": sku, "value": product.name or None, "description": product.description})
        if qty:
            stock.adjust_stock(db, part_id, delta=qty, mtype=stock.OPENING,
                               reference=SYNC_REFERENCE, note="WooCommerce sync (new assembly)",
                               user=user)
        report.created_assemblies += 1
    else:
        repo.create_part(
            db,
            part={"part_no": sku, "value": product.name or None,
                  "description": product.description},
            supplier_lines=[],
            opening={"qty": qty or 0.0},
        )
        part_id = repo.find_part_by_part_no(db, sku)["id"]
        report.created_parts += 1

    # Woo already holds qty, so the baseline starts there and nothing is pushed.
    _set_baseline(db, part_id, qty)
    if product.price is not None:
        _set_external_price(db, part_id, product.price)
    report._note(sku, "created", f"{kind}{f', stock {qty:g}' if qty else ''}")


# --- stock + baseline writes (kept atomic per part) ---

def _apply(db, part_id, *, delta, mtype, reference, note, user, baseline) -> None:
    """Post a stock movement (if non-zero) and set the webshop baseline in one transaction."""
    with db.connect() as conn:
        if abs(delta) > _EPS:
            stock.post_movement(conn, part_id, delta=delta, mtype=mtype, reference=reference,
                                note=note, user=user)
        conn.execute("UPDATE parts SET webshop_synced_qty = ? WHERE id = ?", (baseline, part_id))
        conn.commit()


def _set_baseline(db, part_id, qty) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE parts SET webshop_synced_qty = ? WHERE id = ?", (qty, part_id))
        conn.commit()


def _set_external_price(db, part_id, price) -> None:
    """Copy the webshop price onto the part (book-keeping field, separate from unit_cost)."""
    with db.connect() as conn:
        conn.execute("UPDATE parts SET external_price = ? WHERE id = ?", (price, part_id))
        conn.commit()
