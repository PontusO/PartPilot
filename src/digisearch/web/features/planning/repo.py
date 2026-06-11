"""Calendar event aggregation for the planning board.

Two events per scheduled work order: the **build bar** (planned_start → planned finish/due, purple —
our plan, ending on the due date) and a **purchasing marker** (the planned 'order materials by' date).
Both are draggable: moving the build bar shifts the whole build; the purchasing marker moves on its
own. The customer's requested date is NOT a calendar marker — it's retained on the order/WO page.
"""

from __future__ import annotations

from datetime import date, timedelta

from ...core import iso, parse_date
from ...core.db import Database

# Build-bar colour: purple = a planned build (its end is the due date); green = finished.
_STATUS_COLOR = {"allocated": "#8b5cf6", "issued": "#8b5cf6", "finished": "#22c55e"}


def calendar_events(db: Database, start: str | None = None, end: str | None = None) -> list[dict]:
    events: list[dict] = []
    with db.connect() as conn:
        wos = conn.execute(
            """SELECT w.id, w.wo_no, w.qty, w.status, w.planned_start, w.due_date, w.purchase_by,
                      p.part_no AS assembly_part_no
               FROM work_orders w JOIN parts p ON p.id = w.assembly_id
               WHERE w.planned_start IS NOT NULL AND w.status != 'cancelled'"""
        ).fetchall()

    today = date.today()
    for w in wos:
        due = parse_date(w["due_date"]) or parse_date(w["planned_start"])
        movable = w["status"] != "finished"
        events.append({
            "id": f"wo-{w['id']}",
            "title": f"{w['wo_no']} · {w['assembly_part_no']} ×{w['qty']:g} · due {w['due_date']} · {w['status']}",
            "start": w["planned_start"],
            "end": iso(due + timedelta(days=1)),       # FullCalendar end is exclusive
            "allDay": True,
            "color": _STATUS_COLOR.get(w["status"], "#8b5cf6"),
            "url": f"/work-orders/{w['id']}",
            "editable": movable,
            "extendedProps": {"type": "wo", "wo_id": w["id"], "status": w["status"]},
        })
        if w["purchase_by"] and movable:
            late = parse_date(w["purchase_by"]) < today
            events.append({
                "id": f"buy-{w['id']}",
                "title": ("⚠ " if late else "") + f"Order materials: {w['wo_no']}",
                "start": w["purchase_by"],
                "allDay": True,
                "color": "#ef4444" if late else "#0ea5e9",
                "editable": True,
                "display": "block",
                "url": "/purchase-orders/suggestions",
                "extendedProps": {"type": "buyby", "wo_id": w["id"], "late": late},
            })
    return events
