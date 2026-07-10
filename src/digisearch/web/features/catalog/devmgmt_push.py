"""Assemble the devmgmt §5 payloads from DB rows and push them in referential order.

This is the seam between PartPilot's storage (``devmgmt_repo``) and the transport
(``digisearch.devmgmt.DevmgmtClient``). It shapes stored rows into the exact JSON the contract
specifies and drives the catalog-before-device ordering. Trigger wiring (push on catalog edit /
on work-order finish) will call these builders; for the first milestone the CLI does.
"""

from __future__ import annotations

from ....devmgmt import DevmgmtClient
from ...core.db import Database
from . import devmgmt_repo


def model_payload(model: dict) -> dict:
    """Shape a stored model (from ``devmgmt_repo.get_model``) into the §5.1 body."""
    return {
        "ref": model["ref"],
        "name": model["name"],
        "radio_capabilities": model["radio_capabilities"],
        "board_revisions": [
            {"ref": br["ref"], "rev": br["rev"]} for br in model.get("board_revisions", [])
        ],
        "retired": bool(model.get("retired_at")),   # §7 soft-retire flag (default false)
    }


def variant_payload(variant: dict) -> dict:
    """Shape a stored variant (from ``devmgmt_repo.get_variant``) into the §5.2 body.

    ``radio_config`` is omitted when null (it's optional in the contract)."""
    payload = {
        "ref": variant["ref"],
        "model_ref": variant["model_ref"],
        "sku": variant["sku"],
        "enabled_radios": variant["enabled_radios"],
        "flashable_targets": [
            {
                "component": t["component"],
                "factory_firmware_ref": t["factory_firmware_ref"],
                "update_method": t["update_method"],
            }
            for t in variant.get("flashable_targets", [])
        ],
        "retired": bool(variant.get("retired_at")),   # §7 soft-retire flag (default false)
    }
    if variant.get("radio_config") is not None:
        payload["radio_config"] = variant["radio_config"]
    return payload


def device_payload(device: dict) -> dict:
    """Shape a stored device (from ``devmgmt_repo.get_device``) into the §5.3 body."""
    return {
        "serial": device["serial"],
        "variant_ref": device["variant_ref"],
        "board_rev": device["board_rev"],
        "owner_token": device["owner_token"],
        "radios": device.get("radios", []),
    }


def build_payloads(db: Database, serial: str) -> tuple[dict, dict, dict]:
    """Load a device and everything it references, returning (model, variant, device) payloads.

    Raises ValueError if the device — or the variant/model it hangs off — can't be found."""
    device = devmgmt_repo.get_device(db, serial)
    if not device:
        raise ValueError(f"No device build with serial {serial!r}.")
    variant = devmgmt_repo.get_variant(db, device["variant_ref"])
    if not variant:
        raise ValueError(f"Device {serial!r} references unknown variant {device['variant_ref']!r}.")
    model = devmgmt_repo.get_model(db, variant["model_ref"])
    if not model:
        raise ValueError(f"Variant {variant['ref']!r} references unknown model {variant['model_ref']!r}.")
    return model_payload(model), variant_payload(variant), device_payload(device)


def push_device(db: Database, client: DevmgmtClient, serial: str) -> dict:
    """Push a device and its catalog dependencies to devmgmt, in order, then mark it pushed.

    Returns the three payloads that were sent (handy for logging / a dry-run preview). Any
    transport error propagates; because every upsert is idempotent the call is safe to retry."""
    model, variant, device = build_payloads(db, serial)
    client.push_all(model=model, variant=variant, device=device)
    devmgmt_repo.mark_device_pushed(db, serial)
    return {"model": model, "variant": variant, "device": device}
