"""Create a Fortnox invoice (draft) from a despatch — one invoice per despatch.

Flow per despatch:
  * resolve the Fortnox customer for the order's contact — use the stored link, else match by
    organisation number, else (only when ``confirm_customer`` is set) create it; without
    confirmation we stop and report ``needs_customer`` so the UI can ask first;
  * build a free-text invoice (no article mapping) from the despatch lines and POST it as a draft;
  * record the returned Fortnox invoice number on the despatch via ``mark_invoiced``.

Shipping has already happened and committed by the time we get here, so every failure is captured
on ``despatches.invoice_error`` (retryable) and never rolls back a despatch.
"""

from __future__ import annotations

from datetime import date

from digisearch.fortnox import FortnoxClient, FortnoxError

from ..contacts import repo as contacts_repo
from ..setup import repo as setup_repo
from . import repo as despatch_repo

# Free-text country names → ISO codes Fortnox expects. Best-effort; unknown → omitted.
_COUNTRY_CODES = {
    "sweden": "SE", "sverige": "SE", "norway": "NO", "norge": "NO", "denmark": "DK",
    "danmark": "DK", "finland": "FI", "germany": "DE", "tyskland": "DE", "usa": "US",
    "united states": "US", "united kingdom": "GB", "uk": "GB",
}


class _NeedsConfirmation(Exception):
    """The Fortnox customer must be created, but creation wasn't confirmed yet."""


def build_client(db) -> FortnoxClient | None:
    """A FortnoxClient from stored config + tokens, or None if not connected/configured."""
    cfg = setup_repo.get_fortnox(db)
    tokens = setup_repo.load_fortnox_tokens(db)
    if not (cfg["configured"] and tokens):
        return None
    return FortnoxClient(cfg["client_id"], cfg["client_secret"], tokens,
                         on_refresh=lambda t: setup_repo.save_fortnox_tokens(db, t))


def invoice_despatch(db, despatch_id: int, *, confirm_customer: bool = False,
                     client: FortnoxClient | None = None) -> dict:
    """Create (or report what's blocking) the Fortnox invoice for a despatch.

    Returns a dict with ``status`` in {invoiced, needs_customer, error} and a ``message``.
    For ``needs_customer`` it includes ``customer_preview`` (what would be created)."""
    d = despatch_repo.get_despatch(db, despatch_id)
    if d is None:
        return {"status": "error", "message": "Despatch not found."}
    if d.get("invoice_no"):
        return {"status": "invoiced", "invoice_no": d["invoice_no"], "message": "Already invoiced."}

    client = client or build_client(db)
    if client is None:
        return _fail(db, despatch_id, "Fortnox isn't connected — connect it in Setup → Fortnox.")
    if not d.get("customer_id"):
        return _fail(db, despatch_id, "The order has no customer to invoice.")
    contact = contacts_repo.get_contact(db, d["customer_id"])
    if contact is None:
        return _fail(db, despatch_id, "The order's customer was not found.")

    cfg = setup_repo.get_fortnox(db)
    try:
        customer_number = _ensure_customer(db, client, contact, d, confirm_customer)
    except _NeedsConfirmation:
        msg = "Awaiting confirmation to create this customer in Fortnox."
        _set_error(db, despatch_id, msg)
        return {"status": "needs_customer", "message": msg,
                "customer_preview": _customer_payload(db, contact, d)}
    except FortnoxError as exc:
        return _fail(db, despatch_id, str(exc))

    try:
        invoice = client.create_invoice(_invoice_payload(db, d, contact, customer_number, cfg))
    except FortnoxError as exc:
        return _fail(db, despatch_id, str(exc))

    invoice_no = str(invoice.get("DocumentNumber") or "")
    despatch_repo.mark_invoiced(db, despatch_id, invoice_no, date.today().isoformat())
    _set_error(db, despatch_id, None)
    return {"status": "invoiced", "invoice_no": invoice_no,
            "message": f"Created Fortnox draft invoice {invoice_no}."}


def pending_customer_preview(db, despatch_id: int) -> dict | None:
    """The customer that would be set up in Fortnox for a despatch that is waiting on confirmation
    (not yet invoiced, and its contact isn't linked to a Fortnox customer). None otherwise. Makes
    no API calls — used to render the confirm prompt on page load."""
    d = despatch_repo.get_despatch(db, despatch_id)
    if d is None or d.get("invoice_no") or not d.get("customer_id"):
        return None
    contact = contacts_repo.get_contact(db, d["customer_id"])
    if contact is None or contact.get("fortnox_customer_number"):
        return None
    return _customer_payload(db, contact, d)


# --- customer resolution ---

def _ensure_customer(db, client, contact, despatch, confirm) -> str:
    if contact.get("fortnox_customer_number"):
        return str(contact["fortnox_customer_number"])

    org = contact.get("org_no")
    if org:
        found = client.find_customer_by_orgno(org)
        if found and found.get("CustomerNumber"):
            number = str(found["CustomerNumber"])
            _link_customer(db, contact["id"], number)
            return number

    if not confirm:
        raise _NeedsConfirmation()

    created = client.create_customer(_customer_payload(db, contact, despatch))
    number = str(created.get("CustomerNumber") or "")
    if not number:
        raise FortnoxError("Fortnox did not return a customer number.")
    _link_customer(db, contact["id"], number)
    return number


# --- payload builders ---

def _customer_payload(db, contact, despatch) -> dict:
    inv_addr, _ = _order_addresses(db, despatch.get("order_id"))
    payload = {
        "Name": (inv_addr or {}).get("company") or contact.get("name"),
        "Type": "COMPANY",
        "Currency": contact.get("currency") or "SEK",
        "VATType": "SEVAT",
        "OrganisationNumber": contact.get("org_no"),
        "Email": contact.get("email"),
        "Phone1": contact.get("phone"),
    }
    src = inv_addr or {"line1": contact.get("address"), "postcode": contact.get("postcode"),
                       "country": contact.get("country")}
    payload.update({"Address1": src.get("line1"), "Address2": src.get("line2"),
                    "ZipCode": src.get("postcode"), "City": src.get("city")})
    code = _country_code(src.get("country"))
    if code:
        payload["CountryCode"] = code
    return {k: v for k, v in payload.items() if v not in (None, "")}


def _invoice_payload(db, d, contact, customer_number, cfg) -> dict:
    _, delivery = _order_addresses(db, d.get("order_id"))
    vat = _num(cfg.get("default_vat"))
    vat = int(vat) if vat is not None else 25
    account = (cfg.get("default_account") or "").strip()

    rows = []
    for ln in d.get("lines", []):
        desc = " ".join(x for x in [ln.get("part_no"), ln.get("value")] if x) or "Item"
        row = {"Description": desc[:200], "DeliveredQuantity": ln.get("qty") or 0,
               "Price": ln.get("unit_price") or 0, "VAT": vat, "Unit": "st"}
        if account:
            row["AccountNumber"] = int(account) if account.isdigit() else account
        rows.append(row)

    payload = {
        "CustomerNumber": customer_number,
        "InvoiceDate": d.get("despatch_date") or date.today().isoformat(),
        "Currency": contact.get("currency") or "SEK",
        "VATIncluded": False,
        "YourOrderNumber": d.get("order_ref") or "",
        "Remarks": f"Despatch {d.get('despatch_no') or d['id']}",
        "InvoiceRows": rows,
    }
    if delivery:
        extra = {"DeliveryName": delivery.get("company"),
                 "DeliveryAddress1": delivery.get("line1"),
                 "DeliveryAddress2": delivery.get("line2"),
                 "DeliveryZipCode": delivery.get("postcode"),
                 "DeliveryCity": delivery.get("city")}
        payload.update({k: v for k, v in extra.items() if v})
        code = _country_code(delivery.get("country"))
        if code:
            payload["DeliveryCountryCode"] = code
    return payload


# --- small helpers ---

def _order_addresses(db, order_id):
    """(invoice_address, delivery_address) dicts for the order, or (None, None)."""
    if not order_id:
        return None, None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT invoice_address_id, delivery_address_id FROM customer_orders WHERE id = ?",
            (order_id,),
        ).fetchone()
    if row is None:
        return None, None
    inv = contacts_repo.get_address(db, row["invoice_address_id"]) if row["invoice_address_id"] else None
    dlv = contacts_repo.get_address(db, row["delivery_address_id"]) if row["delivery_address_id"] else None
    return inv, dlv


def _link_customer(db, contact_id, number) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE contacts SET fortnox_customer_number = ? WHERE id = ?",
                     (number, contact_id))
        conn.commit()


def _set_error(db, despatch_id, message) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE despatches SET invoice_error = ?, updated_at = datetime('now') "
                     "WHERE id = ?", (message, despatch_id))
        conn.commit()


def _fail(db, despatch_id, message) -> dict:
    _set_error(db, despatch_id, message)
    return {"status": "error", "message": message}


def _country_code(name):
    return _COUNTRY_CODES.get((name or "").strip().lower())


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
