# PartPilot ⇄ devmgmt integration — PartPilot-side specification

Audience: whoever builds the devmgmt integration **inside PartPilot**. This is the
contract PartPilot implements the *calling* side of; devmgmt implements the
*receiving* side (its phases B2 + D). Both can be built in parallel against this doc,
each stubbing the other. Full platform context: `device-platform.md`.

## 1. Roles

- **PartPilot is authoritative** for the product catalog and the per-device
  manufacturing record. It **pushes** that data to devmgmt over HTTPS.
- **devmgmt** receives it, hosts firmware, and runs claiming/ownership + rollout.
  devmgmt's catalog is a read-only projection of PartPilot — PartPilot never reads
  catalog *back* from devmgmt (except optional firmware-ref validation, §6).

PartPilot pushes on two triggers:
1. **Catalog change** — a model / board revision / SKU is created or edited.
2. **Device provisioned** — the tester finishes a unit (secure element written,
   identities + keys known).

## 2. What PartPilot must own & model

| Entity | Fields PartPilot must hold and send |
|---|---|
| **Product model** | stable `ref`, name, `radio_capabilities[]`, its **board revisions** |
| **Board revision** | stable `ref`, `rev` label (e.g. "C") |
| **Variant / SKU** | stable `ref`, `model_ref`, `sku` string, `enabled_radios[]`, optional `radio_config`, **flashable targets** (component + factory firmware ref + update method) |
| **Device build record** | `serial`, `variant_ref`, `board_rev`, per-radio `identity` + `secrets`, an `owner_token` |

## 3. Shared identifiers (PartPilot assigns; both sides must match exactly)

| Identifier | Notes |
|---|---|
| model / variant / board-rev `ref` | Any stable, opaque string PartPilot controls (UUID or a coded id). Never reused. |
| `sku` | Human SKU string; the catalog join key. Same model + different firmware ⇒ **different SKU**. |
| `serial` | Per-device, globally unique; becomes the device's primary handle in devmgmt. |
| `firmware_ref` | An agreed string naming a firmware build (see §6). Must match an image hosted in devmgmt. |
| radio identities | DevEUI/JoinEUI/IMEI/ICCID/MAC/BLE-addr — from the hardware; globally unique. |

## 4. Auth — mutual TLS (settled)

The API is served on a dedicated, mTLS-gated endpoint, **fully isolated from the
device plane**:

- **Base URL: `https://devmgmt.ilabs.se:8443/api/v1/…`** (a separate nginx server
  block on port 8443; the device plane on :443 is untouched and never asked for a
  client cert).
- **PartPilot presents a client certificate.** nginx verifies it against a pinned
  trust store; unverified requests are rejected at the TLS layer (HTTP 400).
- **Cert exchange:** PartPilot generates its own keypair and sends devmgmt only the
  **public `.crt`**, which we pin. (A test client cert is pinned today so the
  endpoint is live; PartPilot's cert is appended when it arrives.)
- **Firewall:** port 8443 must be reachable (the Hetzner cloud firewall is being
  opened for it).

The endpoint is live now — reachable once the firewall is open and PartPilot's cert
is pinned.

## 5. The push contract (endpoints devmgmt exposes; PartPilot calls)

All are **idempotent upserts** keyed by `ref`/`serial` — safe to retry. Expected
responses: `200` (upserted), `400` (bad payload), `401/403` (auth), `409`
(referential gap, e.g. variant before its model), `5xx` (retry with backoff).

### 5.1 Upsert a product model (with board revisions)
```
POST /api/v1/catalog/models
{
  "ref": "PM-CONN840",
  "name": "Connectivity840",
  "radio_capabilities": ["ble", "lorawan", "cellular"],
  "board_revisions": [
    { "ref": "PM-CONN840-B", "rev": "B" },
    { "ref": "PM-CONN840-C", "rev": "C" }
  ]
}
```

### 5.2 Upsert a variant / SKU
```
POST /api/v1/catalog/variants
{
  "ref": "SKU-CONN840-WEBSHOP",
  "model_ref": "PM-CONN840",
  "sku": "CONN840-WEBSHOP",
  "enabled_radios": ["ble", "lorawan", "cellular"],
  "radio_config": {
    "lorawan": { "profile_id": "0100000A", "lns_default": "ttn" }
  },
  "flashable_targets": [
    { "component": "mcu",         "factory_firmware_ref": "MCU-CONN840-1.2.0", "update_method": "ota_via_mcu" },
    { "component": "lte_modem",   "factory_firmware_ref": "ADRASTEA-06.006",   "update_method": "local_serial" },
    { "component": "wifi_module", "factory_firmware_ref": "ESP-2.1",           "update_method": "ota_via_mcu" }
  ],
  "owner_customer_slug": "acme"   // OPTIONAL — customer-specific SKU. Omit/null = generic.
}
```
`owner_customer_slug` marks a variant as **customer-specific**: it's resolved to a
devmgmt customer by slug, and then only that customer's users (and iLabs staff) can
see the SKU in the catalog. An unknown slug is accepted but treated as generic (the
customer/slug mapping is open decision #3 — for now it's a devmgmt customer slug).
`update_method ∈ { ota_via_mcu, local_serial, local_usb }` — a property of the
component on this board (PartPilot knows it from the design); devmgmt projects it.

### 5.3 Provision a manufactured device
```
POST /api/v1/provisioning/devices
{
  "serial": "CONN840-000042",
  "variant_ref": "SKU-CONN840-WEBSHOP",
  "board_rev": "C",
  "owner_token": "9f2c…(high-entropy, per device)",
  "radios": [
    { "tech": "lorawan",  "identity": { "dev_eui": "0011223344556677", "join_eui": "0102030405060708" },
                          "secrets":  { "app_key": "00112233445566778899AABBCCDDEEFF" } },
    { "tech": "cellular", "identity": { "imei": "350000000000017", "iccid": "8934071100000000017" } },
    { "tech": "ble",      "identity": { "ble_addr": "AABBCCDDEEFF" } }
  ]
}
```
- **Secrets** (e.g. LoRaWAN `app_key`) travel over TLS; devmgmt encrypts them at rest
  and never returns them. Send only what devmgmt needs to provision the LNS.
- **`owner_token`** is stored **hashed** by devmgmt and is what the customer's claim
  must present (see §7).
- Referential rule: `variant_ref` and `board_rev` must already exist (push catalog
  first) — else `409`; retry after the catalog call succeeds.

## 6. Firmware references (§ the one coordination point)

PartPilot references firmware by `firmware_ref` strings; the **binaries live in
devmgmt**. To keep them in sync:
- Agree a **naming convention** up front, e.g. `<COMPONENT>-<MODEL>-<VERSION>`
  (`MCU-CONN840-1.2.0`) or vendor-native for modules (`ADRASTEA-06.006`).
- devmgmt will expose a read-only `GET /api/v1/firmware/images` so PartPilot can
  **validate** a `firmware_ref` exists before referencing it (optional but
  recommended — avoids referencing firmware that hasn't been published yet).
- A device/variant referencing an unknown `firmware_ref` is accepted but **flagged**
  by devmgmt until the image is published.

## 7. Lifecycle — retire (soft) + delete (hard)

Removals originate in PartPilot (devmgmt never deletes catalog by hand). Every
resource follows the same lifecycle: **active → retired → deleted**.

- **Active** — normal, visible.
- **Retired (soft)** — `retired_at` set. Hidden from default listings and from
  customer visibility, but **kept** and **still resolvable** for anything that
  already references it (a device pointing at a retired SKU keeps working). Reversible.
- **Deleted (hard)** — the row is removed. Permitted only once retired and no longer
  referenced.

### Retire — the flag
Two equivalent ways, both from PartPilot; both set the same `retired_at`:

1. **On upsert** — the model/variant payload accepts `"retired": true|false`
   (default `false`). `true` stamps `retired_at`; `false` clears it (un-retire).
   Retiring is just publishing with the flag — it flows through the existing
   trigger → outbox → loop.
2. **Dedicated endpoints** — for a "Retire" button that shouldn't re-send the payload:
   ```
   POST /api/v1/catalog/models/{ref}/retire      POST .../unretire
   POST /api/v1/catalog/variants/{ref}/retire    POST .../unretire
   ```
Idempotent. `200` with the entity; `404` if the ref is unknown.

### Delete — hard, guarded
```
DELETE /api/v1/catalog/variants/{ref}
DELETE /api/v1/catalog/models/{ref}     # cascades board revisions + its variants
```
Guards (they enforce the retire→delete discipline):
- **Must be retired first** → `409 { "error": "retire before delete" }` if still active.
- **Not referenced** → `409 { "error": "N devices still reference this" }` — reassign /
  remove / retire those devices first. (No `force` flag — deletes stay safe.)
Success → `200`/`204`. Unknown ref → `404` (idempotent: deleting a gone entity is fine).

### devmgmt behaviour
- Retired models/variants are **excluded from the default Products list** and from
  `visibleVariants` (customer scoping). A staff **"show retired"** filter surfaces
  them with a *Retired* badge + `retired_at`.
- A retired variant **still resolves** for devices that reference it — retire hides it
  from browsing, it never severs live links or firmware resolution.

### The general pattern (all resources)
This `active → retired → deleted` lifecycle is **base functionality for every
resource**, not just the catalog — notably **devices**: `state = released` is the
device's retire-equivalent (RMA / decommission — soft, reversible, kept), and
`DELETE /api/v1/provisioning/devices/{serial}` hard-removes a released device,
guarded the same way. New resource APIs ship with `retired_at` + a guarded DELETE.

## 8. Owner token + QR / label generation (PartPilot's job)

PartPilot (or the tester via PartPilot) has all the identities at provision time, so it
**generates the QR/label**:
- Generate a **high-entropy `owner_token`** per device; store it, send it to devmgmt
  (§5.3), and include it in the QR.
- **LoRaWAN** boards: emit a **TR005** QR (`LW:D0:<JoinEUI>:<DevEUI>:<ProfileID>:O<owner_token>…`).
- **Non-LoRaWAN / multi-radio** boards: emit the iLabs device QR (universal link keyed on
  `serial` + owner_token; identity in the URL fragment). Format sample in `device-platform.md` §7.
- Optional AES-GCM envelope for identifier privacy — if used, it's **decrypted by
  devmgmt** (PartPilot just encrypts with the shared/issuer key; never ship a key in the app).

## 9. What PartPilot does NOT do

- Host firmware binaries (devmgmt does).
- Manage device **ownership / claiming** or **rollout** (devmgmt + customers do).
- Read catalog state back from devmgmt (push-only, except firmware-ref validation).

## 10. PartPilot build checklist

1. **Data model**: models, board_revisions, variants (+ flashable_targets & firmware
   refs), device build records (serial, radios, identities, secrets, owner_token).
2. **Tester intake**: record the provisioning result (secure-element keys, radio
   identities, serial, board_rev) from the test station; generate `owner_token`.
3. **QR/label generation** (§7).
4. **devmgmt client**: the three POSTs (§5) behind an auth wrapper, with idempotent
   retry/backoff and referential ordering (catalog before devices).
5. **Trigger wiring**: push on catalog edit; push on device-provision completion.

### First milestone (enables end-to-end testing with devmgmt)
Push **one** model + one variant + one device to devmgmt's endpoints (real once B2/D
land; a stub before then). That alone lets the whole claim flow be exercised end to end.

## 11. Open decisions to settle jointly

1. ~~Auth mechanism~~ — **settled: mTLS** (§4).
2. **Identifier formats** — the exact shape of `ref`, `sku`, `serial`, `firmware_ref`
   (and the firmware naming convention, §6). Doesn't block: devmgmt stores opaque strings.
3. **"Customer" mapping** — does a PartPilot customer (who ordered a build) correspond
   to a devmgmt customer account? Affects whether a variant may hint an intended owner.
4. **Secrets transport** — plain-over-TLS (recommended) vs additionally wrapped.

## 12. devmgmt receiving side — status

**Live (2026-07-06):** all three endpoints + the two GET read-backs are deployed and
mTLS-verified on prod `:8443`. Secrets are encrypted at rest (libsodium). Idempotent
upserts confirmed. Milestone 1 can be verified against the live endpoint as soon as
the firewall opens and PartPilot's client cert is pinned.
