"""devmgmt push client — the three idempotent upserts PartPilot calls (docs §5).

PartPilot is authoritative for the catalog + per-device manufacturing record and *pushes* it to
devmgmt; it never reads catalog state back. All three endpoints are idempotent upserts keyed by
``ref``/``serial``, so every call is safe to retry. Modelled on the WooCommerce/Fortnox clients in
this package (httpx + a small retry/backoff loop). Auth is injected as an ``AuthStrategy`` so mTLS
(the chosen mechanism) and Bearer are interchangeable — see ``auth.py``.

Status handling per the contract:
  200            -> upserted (return parsed JSON, or {} for an empty body)
  400            -> DevmgmtPayloadError (bad payload — not retried)
  401 / 403      -> DevmgmtAuthError (auth/cert problem — not retried)
  409            -> DevmgmtReferentialError (referential gap, e.g. a device before its variant;
                    retry after the missing catalog object is pushed)
  429 / 5xx      -> retried with exponential backoff, then DevmgmtError
  network error  -> retried, then DevmgmtError
"""

from __future__ import annotations

import time

import httpx

from .auth import AuthStrategy, NoAuth

MODELS_PATH = "/api/v1/catalog/models"
VARIANTS_PATH = "/api/v1/catalog/variants"
DEVICES_PATH = "/api/v1/provisioning/devices"


class DevmgmtError(RuntimeError):
    """A devmgmt request failed (network, exhausted retries, or an unexpected HTTP error)."""


class DevmgmtPayloadError(DevmgmtError):
    """devmgmt rejected the payload (HTTP 400) — a client bug; retrying won't help."""


class DevmgmtAuthError(DevmgmtError):
    """devmgmt rejected our identity (HTTP 401/403) — bad token or client certificate."""


class DevmgmtReferentialError(DevmgmtError):
    """A referenced object doesn't exist yet in devmgmt (HTTP 409) — push the catalog first."""


class DevmgmtConflictError(DevmgmtError):
    """A guarded DELETE was refused (HTTP 409) — e.g. "retire before delete" or still referenced.
    Terminal: retrying won't help until the guard is cleared (docs §7)."""


class DevmgmtClient:
    def __init__(
        self,
        base_url: str,
        *,
        auth: AuthStrategy | None = None,
        http: httpx.Client | None = None,
        max_retries: int = 3,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self._auth = auth or NoAuth()
        # Let the auth strategy build the client (mTLS bakes the client cert into it). Tests inject
        # their own ``http`` so no real certificate is needed.
        self._http = http or self._auth.build_client(timeout=30)
        self.max_retries = max_retries

    # -- the three push endpoints (docs §5) --------------------------------

    def upsert_model(self, model: dict) -> dict:
        """§5.1 — upsert a product model (with its board revisions)."""
        return self._post(MODELS_PATH, model)

    def upsert_variant(self, variant: dict) -> dict:
        """§5.2 — upsert a variant/SKU. 409 if its model isn't pushed yet."""
        return self._post(VARIANTS_PATH, variant)

    def provision_device(self, device: dict) -> dict:
        """§5.3 — provision a manufactured device. 409 if its variant/board_rev isn't pushed yet."""
        return self._post(DEVICES_PATH, device)

    def push_all(self, *, model: dict, variant: dict, device: dict) -> None:
        """Push a model, variant and device in referential order (catalog before device).

        Sequencing the calls this way is exactly what avoids a 409: the model exists before the
        variant references it, and the variant exists before the device references it. Any error
        propagates (all three upserts are idempotent, so the whole call is safe to re-run)."""
        self.upsert_model(model)
        self.upsert_variant(variant)
        self.provision_device(device)

    # -- hard delete (docs §7) ---------------------------------------------

    def delete_variant(self, ref: str) -> None:
        """DELETE a variant. Idempotent (404 = already gone). 409 if it isn't retired / is still
        referenced — raised as DevmgmtConflictError."""
        self._delete(f"{VARIANTS_PATH}/{ref}")

    def delete_model(self, ref: str) -> None:
        """DELETE a model (devmgmt cascades its board revisions + variants). Same guards as above."""
        self._delete(f"{MODELS_PATH}/{ref}")

    def delete_device(self, serial: str) -> None:
        """DELETE a released device. Idempotent; 409 if it isn't released yet."""
        self._delete(f"{DEVICES_PATH}/{serial}")

    # -- transport ---------------------------------------------------------

    def _post(self, path: str, body: dict) -> dict:
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   **self._auth.headers()}
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self._http.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                last_exc = exc
                time.sleep(2**attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(2**attempt)
                continue
            if resp.status_code == 400:
                raise DevmgmtPayloadError(
                    f"devmgmt rejected the payload (HTTP 400) for {path}: {_detail(resp)}")
            if resp.status_code in (401, 403):
                raise DevmgmtAuthError(
                    f"devmgmt rejected the request (HTTP {resp.status_code}) for {path}. "
                    f"Check the client certificate / token. {_detail(resp)}")
            if resp.status_code == 409:
                raise DevmgmtReferentialError(
                    f"devmgmt reports a referential gap (HTTP 409) for {path}: {_detail(resp)}. "
                    "Push the referenced model/variant before this object.")
            if 200 <= resp.status_code < 300:
                return _safe_json(resp)
            # Anything else (3xx included — httpx doesn't follow redirects, and an upsert that got
            # redirected was NOT delivered) is a failure; treating it as success would mark outbox
            # jobs done and stamp pushed_at while devmgmt never received the data.
            raise DevmgmtError(
                f"devmgmt request failed: HTTP {resp.status_code} for {path}: {_detail(resp)}")
        if last_exc:
            raise DevmgmtError(f"Could not reach devmgmt at {self.base_url}: {last_exc}") from last_exc
        raise DevmgmtError(f"devmgmt request failed after {self.max_retries} retries: {path}")

    def _delete(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json", **self._auth.headers()}
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self._http.request("DELETE", url, headers=headers)
            except httpx.HTTPError as exc:
                last_exc = exc
                time.sleep(2**attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(2**attempt)
                continue
            # 404 is success: deleting an already-gone entity is fine (idempotent, docs §7).
            if resp.status_code == 404 or 200 <= resp.status_code < 300:
                return _safe_json(resp)
            if resp.status_code == 409:
                raise DevmgmtConflictError(
                    f"devmgmt refused the delete (HTTP 409) for {path}: {_detail(resp)}")
            if resp.status_code in (401, 403):
                raise DevmgmtAuthError(
                    f"devmgmt rejected the request (HTTP {resp.status_code}) for {path}. "
                    f"Check the client certificate / token. {_detail(resp)}")
            raise DevmgmtError(
                f"devmgmt delete failed: HTTP {resp.status_code} for {path}: {_detail(resp)}")
        if last_exc:
            raise DevmgmtError(f"Could not reach devmgmt at {self.base_url}: {last_exc}") from last_exc
        raise DevmgmtError(f"devmgmt delete failed after {self.max_retries} retries: {path}")


def _safe_json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {"result": data}


def _detail(resp: httpx.Response) -> str:
    """A short error detail for logs — a JSON ``message``/``error`` field, else the body head."""
    body = _safe_json(resp)
    return str(body.get("message") or body.get("error") or resp.text[:200] or "(no body)")
