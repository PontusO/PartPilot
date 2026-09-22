"""Read-only views over the Goods Received Notes (the tables are owned by purchase_orders)."""

from __future__ import annotations

from ...core.db import Database


def summary(db: Database) -> dict:
    with db.connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM goods_receipts").fetchone()[0]
        units = conn.execute("SELECT COALESCE(SUM(qty), 0) FROM goods_receipt_lines").fetchone()[0]
    return {"total": total, "units": units}


def list_receipts(db: Database, search: str | None = None) -> list[dict]:
    like = f"%{search}%" if search else None
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT g.id, g.grn_no, g.grn_date, g.advice_no, g.received_by,
                      s.name AS supplier_name, p.po_no,
                      (SELECT COUNT(*) FROM goods_receipt_lines l WHERE l.grn_id = g.id) AS line_count
               FROM goods_receipts g
               LEFT JOIN suppliers s ON s.id = g.supplier_id
               LEFT JOIN purchase_orders p ON p.id = g.po_id
               WHERE (:s IS NULL OR g.grn_no LIKE :like OR g.advice_no LIKE :like
                      OR s.name LIKE :like OR p.po_no LIKE :like)
               ORDER BY g.id DESC""",
            {"s": search, "like": like},
        )]


def get_receipt(db: Database, grn_id: int) -> dict | None:
    with db.connect() as conn:
        head = conn.execute(
            """SELECT g.*, s.name AS supplier_name, p.po_no
               FROM goods_receipts g LEFT JOIN suppliers s ON s.id = g.supplier_id
               LEFT JOIN purchase_orders p ON p.id = g.po_id WHERE g.id = ?""",
            (grn_id,),
        ).fetchone()
        if head is None:
            return None
        lines = [dict(r) for r in conn.execute(
            """SELECT l.id, l.qty, l.unit_price, l.part_id, pt.part_no, pt.value
               FROM goods_receipt_lines l LEFT JOIN parts pt ON pt.id = l.part_id
               WHERE l.grn_id = ? ORDER BY l.id""",
            (grn_id,),
        )]
    for ln in lines:
        ln["line_value"] = (ln["qty"] or 0) * (ln["unit_price"] or 0) if ln["unit_price"] is not None else None
    g = dict(head)
    g["lines"] = lines
    g["total_value"] = sum(ln["line_value"] for ln in lines if ln["line_value"] is not None)
    return g
