"""Work order queries + the build lifecycle (allocate -> issue -> finish).

Stock changes go through catalog.stock.post_movement so on-hand and the ledger stay in step:
ISSUE movements consume components when a WO is issued; a BUILD movement adds the finished
assembly to stock when it's completed.
"""

from __future__ import annotations

import math
from datetime import date

from ...core import add_workdays, iso, parse_date, ref_no, sub_workdays, workdays_between
from ...core.db import Database
from ..catalog import stock


def _spillage_settings(conn) -> tuple[float, float]:
    """(spillage_percent, min_margin_qty) from Setup → Production settings; (0, 0) if unset/absent."""
    try:
        rows = dict(conn.execute(
            "SELECT key, value FROM app_settings WHERE key IN "
            "('production.spillage_percent', 'production.min_margin_qty')").fetchall())
    except Exception:  # app_settings table not present (e.g. isolated tests)
        return 0.0, 0.0

    def _f(v):
        try:
            return float(v) if v not in (None, "") else 0.0
        except (TypeError, ValueError):
            return 0.0

    return _f(rows.get("production.spillage_percent")), _f(rows.get("production.min_margin_qty"))

STATUSES = ("allocated", "issued", "finished", "cancelled")

# Fallback build estimate (working days) when an assembly has no default and none is entered.
DEFAULT_BUILD_DAYS = 5


def explode_to_components(conn, assembly_id: int, qty: float) -> dict[int, float]:
    """Recursively explode an assembly's BOM to base components -> {part_id: total_qty}.

    Sub-assemblies are walked into their children (multi-level). A sub-assembly with no BOM,
    or one reached cyclically, is treated as a stocked line item instead of being exploded.
    """
    acc: dict[int, float] = {}

    def walk(pid: int, mult: float, seen: frozenset) -> None:
        rows = conn.execute(
            "SELECT b.child_id, b.qty_per, p.kind FROM bom_lines b JOIN parts p ON p.id = b.child_id "
            "WHERE b.parent_id = ?",
            (pid,),
        ).fetchall()
        for r in rows:
            cid, need, kind = r["child_id"], (r["qty_per"] or 0) * mult, r["kind"]
            has_bom = kind == "ASSY" and conn.execute(
                "SELECT 1 FROM bom_lines WHERE parent_id = ? LIMIT 1", (cid,)).fetchone()
            if has_bom and cid not in seen:
                walk(cid, need, seen | {cid})
            else:
                acc[cid] = acc.get(cid, 0.0) + need

    walk(assembly_id, qty, frozenset({assembly_id}))
    return acc


# ---- reads ----

def summary(db: Database) -> dict:
    with db.connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM work_orders").fetchone()[0]
        rows = dict(conn.execute(
            "SELECT status, COUNT(*) FROM work_orders GROUP BY status").fetchall())
    return {"total": total, "allocated": rows.get("allocated", 0),
            "issued": rows.get("issued", 0), "finished": rows.get("finished", 0)}


def assemblies(db: Database) -> list[dict]:
    """Assemblies that can be built (have at least one BOM line)."""
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT id, part_no, value, default_build_days FROM parts WHERE kind = 'ASSY'
               AND EXISTS (SELECT 1 FROM bom_lines b WHERE b.parent_id = parts.id)
               ORDER BY part_no""")]


def list_work_orders(db: Database, status: str | None = None, search: str | None = None) -> list[dict]:
    like = f"%{search}%" if search else None
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT w.id, w.wo_no, w.qty, w.status, w.build_date, w.planned_start, w.due_date,
                      p.part_no AS assembly_part_no, p.value AS assembly_value,
                      (SELECT COUNT(*) FROM work_order_lines l WHERE l.work_order_id = w.id) AS line_count
               FROM work_orders w JOIN parts p ON p.id = w.assembly_id
               WHERE (:status IS NULL OR w.status = :status)
                 AND (:search IS NULL OR w.wo_no LIKE :like OR p.part_no LIKE :like OR p.value LIKE :like)
               ORDER BY w.id DESC""",
            {"status": status, "search": search, "like": like},
        )]


def get_work_order(db: Database, wo_id: int) -> dict | None:
    with db.connect() as conn:
        head = conn.execute(
            """SELECT w.*, p.part_no AS assembly_part_no, p.value AS assembly_value,
                      co.id AS customer_order_id, co.order_ref AS customer_order_ref,
                      co.required_date AS customer_required_date
               FROM work_orders w JOIN parts p ON p.id = w.assembly_id
               LEFT JOIN customer_order_lines col ON col.id = w.customer_order_line_id
               LEFT JOIN customer_orders co ON co.id = col.order_id
               WHERE w.id = ?""",
            (wo_id,),
        ).fetchone()
        if head is None:
            return None
        lines = [dict(r) for r in conn.execute(
            """SELECT wl.id, wl.part_id, wl.qty_required, wl.qty_issued, wl.line_no,
                      p.part_no, p.value, p.kind, p.total_qty, p.total_alloc
               FROM work_order_lines wl JOIN parts p ON p.id = wl.part_id
               WHERE wl.work_order_id = ? ORDER BY COALESCE(wl.line_no, 1e9), wl.id""",
            (wo_id,),
        )]
    wo = dict(head)
    short_count = 0
    for d in lines:
        d["available"] = (d["total_qty"] or 0) - (d["total_alloc"] or 0)
        d["short"] = max(0.0, (d["qty_required"] or 0) - d["available"])
        if d["short"] > 0:
            short_count += 1
    wo["lines"] = lines
    wo["short_count"] = short_count
    return wo


# ---- create ----

def _resolve_schedule(conn, assembly_id: int, data: dict) -> tuple[str | None, str | None, int | None]:
    """The DUE DATE is the anchor: planned_start and the purchasing window are back-scheduled from
    it using the build duration. Duration = entered value, else the assembly's default, else
    DEFAULT_BUILD_DAYS (5 working days)."""
    duration = data.get("duration_days")
    if duration in (None, ""):
        row = conn.execute("SELECT default_build_days FROM parts WHERE id = ?", (assembly_id,)).fetchone()
        duration = row["default_build_days"] if row else None
    duration = int(duration) if duration not in (None, "") else DEFAULT_BUILD_DAYS
    start, due = parse_date(data.get("planned_start")), parse_date(data.get("due_date"))
    if due:
        start = sub_workdays(due, duration)          # back-schedule start from the due date
    elif start:
        due = add_workdays(start, duration)          # only if a start was given explicitly (rare)
    return iso(start), iso(due), duration


def create_work_order(db: Database, data: dict) -> int:
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM parts WHERE id = ? AND kind = 'ASSY'",
                        (data["assembly_id"],)).fetchone() is None:
            raise ValueError("Choose an assembly to build.")
        qty = data.get("qty") or 1
        planned_start, due_date, duration_days = _resolve_schedule(conn, data["assembly_id"], data)
        spillage, min_margin = _spillage_settings(conn)
        wo_id = conn.execute(
            """INSERT INTO work_orders (wo_no, assembly_id, qty, status, customer_order_line_id,
               location_id, build_date, notes, planned_start, due_date, duration_days,
               spillage_percent, min_margin_qty)
               VALUES (?, ?, ?, 'allocated', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (data.get("wo_no"), data["assembly_id"], qty, data.get("customer_order_line_id"),
             data.get("location_id"), data.get("build_date"), data.get("notes"),
             planned_start, due_date, duration_days, spillage, min_margin),
        ).lastrowid
        if not data.get("wo_no"):
            conn.execute("UPDATE work_orders SET wo_no = ? WHERE id = ?", (ref_no("WO", wo_id), wo_id))
        exploded = explode_to_components(conn, data["assembly_id"], qty)
        for i, (pid, need) in enumerate(sorted(exploded.items()), start=1):
            # per-component margin = max(percentage of need, minimum qty); round up to whole parts
            if spillage or min_margin:
                required = math.ceil(round(need + max(need * spillage / 100.0, min_margin), 6))
            else:
                required = need
            conn.execute(
                "INSERT INTO work_order_lines (work_order_id, part_id, qty_required, line_no) "
                "VALUES (?, ?, ?, ?)",
                (wo_id, pid, required, i),
            )
        pb = _critical_buy_by(conn, wo_id, parse_date(planned_start))   # now that lines exist
        if pb:
            conn.execute("UPDATE work_orders SET purchase_by = ? WHERE id = ?", (pb, wo_id))
        conn.commit()
    return wo_id


# ---- scheduling (planning calendar) ----

def reschedule_work_order(db: Database, wo_id: int, *, planned_start=None, due_date=None,
                          duration_days=None) -> None:
    """Re-plan a WO. The WO page is DUE-DRIVEN (due + build days → back-scheduled start). The
    calendar also supports drag (new start, keep duration) and resize (start + new end → duration)."""
    with db.connect() as conn:
        wo = conn.execute(
            "SELECT planned_start, due_date, duration_days FROM work_orders WHERE id = ?", (wo_id,)
        ).fetchone()
        if wo is None:
            raise ValueError("Work order not found.")
        cur_start, cur_due, cur_dur = (parse_date(wo["planned_start"]), parse_date(wo["due_date"]),
                                       wo["duration_days"])
        start = parse_date(planned_start) if planned_start is not None else None
        due = parse_date(due_date) if due_date is not None else None
        dur = int(duration_days) if duration_days not in (None, "") else None

        if due is not None and dur is not None:        # WO page: due + build days → back-schedule start
            start = sub_workdays(due, dur)
        elif start is not None and due is not None:    # calendar resize: start + new end → duration
            dur = workdays_between(start, due)
        elif start is not None:                        # calendar move: keep duration → recompute due
            dur = dur if dur is not None else cur_dur
            due = add_workdays(start, dur) if dur else cur_due
        elif due is not None:                          # due changed alone → back-schedule with current duration
            dur = dur if dur is not None else (cur_dur or DEFAULT_BUILD_DAYS)
            start = sub_workdays(due, dur)
        elif dur is not None:                          # duration changed alone
            start, due = cur_start, (add_workdays(cur_start, dur) if cur_start else cur_due)
        else:
            return
        purchase_by = _critical_buy_by(conn, wo_id, start)   # re-derive purchasing date from new start
        conn.execute(
            "UPDATE work_orders SET planned_start = ?, due_date = ?, duration_days = ?, "
            "purchase_by = ?, updated_at = datetime('now') WHERE id = ?",
            (iso(start), iso(due), dur, purchase_by, wo_id),
        )
        conn.commit()


def _buy_by_lines(conn, wo_id: int, start) -> list[dict]:
    """Per-component 'order by' dates (only components that have a supplier lead time)."""
    if start is None:
        return []
    lines = []
    for ln in conn.execute(
        """SELECT p.part_no,
                  (SELECT ps.lead_time FROM part_suppliers ps WHERE ps.part_id = wl.part_id
                   ORDER BY ps.is_default DESC, ps.id LIMIT 1) AS lead_time
           FROM work_order_lines wl JOIN parts p ON p.id = wl.part_id
           WHERE wl.work_order_id = ?""",
        (wo_id,),
    ):
        if ln["lead_time"]:
            lines.append({"part_no": ln["part_no"], "lead_time": ln["lead_time"],
                          "order_by": iso(sub_workdays(start, ln["lead_time"]))})
    return sorted(lines, key=lambda x: x["order_by"])


def _critical_buy_by(conn, wo_id: int, start) -> str | None:
    """The earliest order-by (when purchasing must start) for a build starting on ``start``."""
    lines = _buy_by_lines(conn, wo_id, start)
    return lines[0]["order_by"] if lines else None


def set_purchase_by(db: Database, wo_id: int, value: str | None) -> None:
    """Override the planned purchasing date (calendar drag, e.g. for hard-to-source parts)."""
    with db.connect() as conn:
        conn.execute("UPDATE work_orders SET purchase_by = ?, updated_at = datetime('now') WHERE id = ?",
                     (value or None, wo_id))
        conn.commit()


def buy_by_for_wo(db: Database, wo_id: int) -> dict | None:
    """The planned purchasing date (``purchase_by``) + the per-component lead-time breakdown.
    Returns None when nothing has a lead time (no purchasing deadline to surface)."""
    with db.connect() as conn:
        wo = conn.execute("SELECT planned_start, purchase_by FROM work_orders WHERE id = ?",
                          (wo_id,)).fetchone()
        if wo is None or not wo["purchase_by"]:
            return None
        start = parse_date(wo["planned_start"])
        lines = _buy_by_lines(conn, wo_id, start)
    return {"critical": wo["purchase_by"], "late": parse_date(wo["purchase_by"]) < date.today(),
            "planned_start": iso(start), "lines": lines}


# ---- lifecycle ----

def _wo_ref(wo) -> str:
    return wo["wo_no"] or ref_no("WO", wo["id"])


def issue_work_order(db: Database, wo_id: int, user: str | None = None) -> None:
    """Consume all components out of stock (ISSUE movements) and mark the WO issued/WIP."""
    with db.connect() as conn:
        wo = conn.execute("SELECT * FROM work_orders WHERE id = ?", (wo_id,)).fetchone()
        if wo is None:
            raise ValueError("Work order not found.")
        if wo["status"] != "allocated":
            raise ValueError(f"Only an allocated work order can be issued (this one is {wo['status']}).")
        for ln in conn.execute("SELECT * FROM work_order_lines WHERE work_order_id = ?", (wo_id,)):
            if ln["qty_required"]:
                stock.post_movement(conn, ln["part_id"], delta=-ln["qty_required"], mtype=stock.ISSUE,
                                    reference=_wo_ref(wo), note="work order issue", user=user,
                                    location_id=wo["location_id"])
            conn.execute("UPDATE work_order_lines SET qty_issued = qty_required WHERE id = ?", (ln["id"],))
        conn.execute("UPDATE work_orders SET status = 'issued', updated_at = datetime('now') WHERE id = ?",
                     (wo_id,))
        conn.commit()


def finish_work_order(db: Database, wo_id: int, user: str | None = None) -> None:
    """Put the finished assemblies into stock (BUILD movement) and mark the WO finished."""
    with db.connect() as conn:
        wo = conn.execute("SELECT * FROM work_orders WHERE id = ?", (wo_id,)).fetchone()
        if wo is None:
            raise ValueError("Work order not found.")
        if wo["status"] != "issued":
            raise ValueError(f"Only an issued work order can be finished (this one is {wo['status']}).")
        stock.post_movement(conn, wo["assembly_id"], delta=wo["qty"], mtype=stock.BUILD,
                            reference=_wo_ref(wo), note="work order completion", user=user,
                            location_id=wo["location_id"])
        conn.execute(
            "UPDATE work_orders SET status = 'finished', "
            "build_date = COALESCE(build_date, date('now')), updated_at = datetime('now') WHERE id = ?",
            (wo_id,),
        )
        conn.commit()


def flush_work_order(db: Database, wo_id: int, user: str | None = None) -> None:
    """Run a quick build straight through: issue then finish in one go."""
    issue_work_order(db, wo_id, user)
    finish_work_order(db, wo_id, user)


# ---- customer-order fulfilment (build to fulfil) ----

def order_header(db: Database, order_id: int) -> dict | None:
    """Light header for the build-review page (kept here to avoid a customer_orders import cycle)."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT o.id, o.order_ref, o.status, c.name AS customer_name "
            "FROM customer_orders o LEFT JOIN contacts c ON c.id = o.customer_id WHERE o.id = ?",
            (order_id,),
        ).fetchone()
    return dict(row) if row else None


def fulfilment_proposals(db: Database, order_id: int) -> list[dict]:
    """For each line on a customer order, decide whether a work order is needed.

    category: 'build'     — a buildable assembly short of stock (needs a WO);
              'covered'    — a buildable assembly already met by stock + open WOs;
              'component'  — not an assembly (would be purchased, not built).
    shortfall = ordered − free stock − qty already on open work orders for this line.
    """
    with db.connect() as conn:
        lines = conn.execute(
            """SELECT l.id AS line_id, l.ordered_qty, l.part_id,
                      p.part_no, p.value, p.kind, p.total_qty, p.total_alloc
               FROM customer_order_lines l JOIN parts p ON p.id = l.part_id
               WHERE l.order_id = ? ORDER BY COALESCE(l.line_no, 1e9), l.id""",
            (order_id,),
        ).fetchall()
        props = []
        for r in lines:
            d = dict(r)
            free = (d["total_qty"] or 0) - (d["total_alloc"] or 0)
            on_wo = conn.execute(
                "SELECT COALESCE(SUM(qty), 0) FROM work_orders "
                "WHERE customer_order_line_id = ? AND status IN ('allocated', 'issued')",
                (d["line_id"],),
            ).fetchone()[0]
            buildable = d["kind"] == "ASSY" and conn.execute(
                "SELECT 1 FROM bom_lines WHERE parent_id = ? LIMIT 1", (d["part_id"],)
            ).fetchone() is not None
            shortfall = max(0.0, (d["ordered_qty"] or 0) - free - on_wo)
            d.update(free=free, on_wo=on_wo, shortfall=shortfall,
                     category=("component" if not buildable else "build" if shortfall > 0 else "covered"))
            props.append(d)
    return props


def work_orders_for_order(db: Database, order_id: int) -> dict[int, list[dict]]:
    """Work orders linked to each line of a customer order: {line_id: [wo, ...]}."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT id, wo_no, status, qty, customer_order_line_id AS line_id FROM work_orders
               WHERE customer_order_line_id IN
                     (SELECT id FROM customer_order_lines WHERE order_id = ?)
               ORDER BY id""",
            (order_id,),
        ).fetchall()
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r["line_id"], []).append(dict(r))
    return out


def create_work_orders_for_order(db: Database, order_id: int, selections: dict[int, float],
                                 user: str | None = None) -> list[int]:
    """Create a work order per selected line ({line_id: build_qty}), linked to the order line."""
    with db.connect() as conn:
        head = conn.execute("SELECT order_ref, required_date FROM customer_orders WHERE id = ?",
                            (order_id,)).fetchone()
        ref = (head["order_ref"] if head and head["order_ref"] else f"CO{order_id}")
        required_date = head["required_date"] if head else None
        valid = {r["id"]: r["part_id"] for r in conn.execute(
            "SELECT l.id, l.part_id FROM customer_order_lines l JOIN parts p ON p.id = l.part_id "
            "WHERE l.order_id = ? AND p.kind = 'ASSY'", (order_id,))}

    created = []
    for line_id, qty in selections.items():
        if line_id in valid and qty and qty > 0:
            # auto-plan: due = customer required date; start back-scheduled from the assembly's build days
            created.append(create_work_order(db, {
                "assembly_id": valid[line_id], "qty": qty,   # wo_no auto-assigned (WO-NNNNN)
                "customer_order_line_id": line_id, "due_date": required_date,
                "notes": f"Build to fulfil customer order {ref}"}))
    return created


def cancel_work_order(db: Database, wo_id: int) -> None:
    """Cancel a work order that hasn't been issued yet (no stock has moved)."""
    with db.connect() as conn:
        wo = conn.execute("SELECT status FROM work_orders WHERE id = ?", (wo_id,)).fetchone()
        if wo is None:
            raise ValueError("Work order not found.")
        if wo["status"] != "allocated":
            raise ValueError("Only an allocated work order can be cancelled; stock has already moved.")
        conn.execute("UPDATE work_orders SET status = 'cancelled', updated_at = datetime('now') WHERE id = ?",
                     (wo_id,))
        conn.commit()
