"""Pull the WooCommerce catalogue into PartPilot (Woo is authoritative; we only read it).

For each shop product we match ``WooProduct.sku`` against ``parts.part_no``:
  * matched  -> set on-hand to the shop's stock (via an ADJUST stock movement, so the ledger
               and ``total_qty`` stay consistent and every webshop-driven change is auditable);
  * unmatched -> create the part, classified by SKU prefix (``99-`` component, ``98-`` assembly).

``sync_from_woo`` takes an *iterable of products* rather than a client so it's fully testable
without the network, and so a future daily scheduler can feed it the same way the manual tool
does. Pass ``dry_run=True`` to get the same report with no database writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import repo, stock
from ..assemblies import repo as assemblies_repo
from ...core.db import Database

PART_PREFIX = "99-"   # components
ASSY_PREFIX = "98-"   # assemblies
SYNC_REFERENCE = "woo-sync"


@dataclass
class SyncReport:
    created_parts: int = 0
    created_assemblies: int = 0
    updated: int = 0          # on-hand changed to match Woo
    unchanged: int = 0        # matched, stock already equal
    unmanaged: int = 0        # matched, but Woo doesn't manage the product's stock
    skipped: int = 0          # SKU prefix not 98-/99-
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


def sync_from_woo(db: Database, products, *, user: str | None = None,
                  dry_run: bool = False) -> SyncReport:
    report = SyncReport()
    for product in products:
        try:
            _sync_one(db, product, user=user, dry_run=dry_run, report=report)
        except Exception as exc:  # one bad product must not abort the whole run
            report.errors.append({"sku": getattr(product, "sku", "?"), "error": str(exc)})
    return report


def _sync_one(db, product, *, user, dry_run, report: SyncReport) -> None:
    sku = product.sku
    kind = kind_for_sku(sku)
    if kind is None:
        report.skipped += 1
        report._note(sku, "skipped", "SKU prefix is not 98- or 99-")
        return

    existing = repo.find_part_by_part_no(db, sku)
    if existing is not None:
        _update_stock(db, existing, product, user=user, dry_run=dry_run, report=report)
    else:
        _create(db, product, kind=kind, dry_run=dry_run, report=report)


def _update_stock(db, part, product, *, user, dry_run, report: SyncReport) -> None:
    sku = product.sku
    if product.stock_quantity is None:
        report.unmanaged += 1
        report._note(sku, "unmanaged", "Woo does not manage this product's stock")
        return
    current = part.get("total_qty") or 0.0
    delta = product.stock_quantity - current
    if abs(delta) < 1e-9:
        report.unchanged += 1
        report._note(sku, "unchanged", f"stock already {current:g}")
        return
    if not dry_run:
        stock.adjust_stock(db, part["id"], delta=delta, mtype=stock.ADJUST,
                           reference=SYNC_REFERENCE, note="WooCommerce sync",
                           user=user)
    report.updated += 1
    report._note(sku, "updated", f"{current:g} -> {product.stock_quantity:g}")


def _create(db, product, *, kind, dry_run, report: SyncReport) -> None:
    sku = product.sku
    qty = product.stock_quantity
    if dry_run:
        if kind == "ASSY":
            report.created_assemblies += 1
        else:
            report.created_parts += 1
        report._note(sku, "would create",
                     f"{kind}{f', opening stock {qty:g}' if qty is not None else ''}")
        return

    if kind == "ASSY":
        part_id = assemblies_repo.create_assembly(db, {
            "part_no": sku, "value": product.name or None, "description": product.description})
        if qty:
            stock.adjust_stock(db, part_id, delta=qty, mtype=stock.OPENING,
                               reference=SYNC_REFERENCE, note="WooCommerce sync (new assembly)")
        report.created_assemblies += 1
    else:
        repo.create_part(
            db,
            part={"part_no": sku, "value": product.name or None,
                  "description": product.description},
            supplier_lines=[],
            opening={"qty": qty or 0.0},
        )
        report.created_parts += 1
    report._note(sku, "created", f"{kind}{f', opening stock {qty:g}' if qty else ''}")
