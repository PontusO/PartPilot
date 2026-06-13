"""WooCommerce REST API v3 client — read-only product listing.

Auth is the standard WooCommerce consumer key/secret over HTTPS Basic auth. We only call
``GET /products`` (paged) and never write, so PartPilot can pull the shop catalogue without
any risk of mutating it. Modelled on the Mouser/Digi-Key clients in this package (httpx +
small retry loop + normalization to a plain dataclass).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterator

import httpx

API_ROOT = "/wp-json/wc/v3"
_PER_PAGE = 100


class WooError(RuntimeError):
    """A WooCommerce request failed (auth, network or HTTP error)."""


@dataclass
class WooProduct:
    """The slice of a Woo product PartPilot cares about. ``stock_quantity`` is None when the
    product doesn't manage stock (``manage_stock=false``) — i.e. quantity is unknown, not zero."""

    sku: str
    name: str
    description: str | None
    stock_quantity: float | None
    manage_stock: bool
    stock_status: str | None
    type: str


class WooClient:
    def __init__(
        self,
        base_url: str,
        consumer_key: str,
        consumer_secret: str,
        *,
        http: httpx.Client | None = None,
        max_retries: int = 3,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self._auth = httpx.BasicAuth(consumer_key or "", consumer_secret or "")
        self._http = http or httpx.Client(timeout=30)
        self.max_retries = max_retries

    # -- public API --------------------------------------------------------

    def ping(self) -> bool:
        """Fetch a single product to confirm the URL + credentials work. Raises WooError on
        failure; returns True otherwise (an empty shop is still a valid connection)."""
        self._get("/products", {"per_page": 1, "page": 1})
        return True

    def iter_products(self) -> Iterator[WooProduct]:
        """Yield every published product, paging until a short page is returned."""
        page = 1
        while True:
            batch = self._get("/products", {"per_page": _PER_PAGE, "page": page, "status": "publish"})
            if not isinstance(batch, list):
                raise WooError("Unexpected WooCommerce response (expected a list of products).")
            for raw in batch:
                product = _to_product(raw)
                if product is not None:
                    yield product
            if len(batch) < _PER_PAGE:
                return
            page += 1

    # -- transport ---------------------------------------------------------

    def _get(self, path: str, params: dict):
        url = f"{self.base_url}{API_ROOT}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self._http.get(url, params=params, auth=self._auth,
                                      headers={"Accept": "application/json"})
            except httpx.HTTPError as exc:
                last_exc = exc
                time.sleep(2**attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(2**attempt)
                continue
            if resp.status_code in (401, 403):
                raise WooError(
                    "WooCommerce rejected the credentials (HTTP "
                    f"{resp.status_code}). Check the consumer key/secret and that the key has "
                    "read access."
                )
            if resp.status_code >= 400:
                raise WooError(f"WooCommerce request failed: HTTP {resp.status_code} for {path}.")
            try:
                return resp.json()
            except ValueError as exc:
                raise WooError("WooCommerce returned a non-JSON response — check the store URL.") from exc
        if last_exc:
            raise WooError(f"Could not reach WooCommerce at {self.base_url}: {last_exc}") from last_exc
        raise WooError(f"WooCommerce request failed after {self.max_retries} retries: {path}")


# -- response normalization ------------------------------------------------


def _to_product(raw: dict) -> WooProduct | None:
    """Map a raw Woo product dict to WooProduct. Returns None for products with no SKU
    (nothing to match against ``parts.part_no``)."""
    if not isinstance(raw, dict):
        return None
    sku = (raw.get("sku") or "").strip()
    if not sku:
        return None
    manage_stock = bool(raw.get("manage_stock"))
    qty = raw.get("stock_quantity")
    stock_quantity = _num(qty) if manage_stock else None
    return WooProduct(
        sku=sku,
        name=(raw.get("name") or "").strip(),
        description=_clean(raw.get("short_description")) or _clean(raw.get("description")),
        stock_quantity=stock_quantity,
        manage_stock=manage_stock,
        stock_status=(raw.get("stock_status") or None),
        type=(raw.get("type") or "simple"),
    )


def _num(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _clean(html: str | None) -> str | None:
    """Woo descriptions are HTML; keep it lightweight — strip tags and collapse whitespace."""
    if not html:
        return None
    import re

    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None
