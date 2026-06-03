"""Digi-Key Product Information API v4 client (keyword search + normalization)."""

from __future__ import annotations

import time

import httpx

from ..config import DigiKeyCredentials
from ..models import Candidate
from .auth import TokenManager
from .cache import DiskCache

KEYWORD_SEARCH_PATH = "/products/v4/search/keyword"


class DigiKeyClient:
    def __init__(
        self,
        creds: DigiKeyCredentials,
        cache: DiskCache | None = None,
        http: httpx.Client | None = None,
        max_retries: int = 3,
    ):
        self.creds = creds
        self._http = http or httpx.Client(timeout=30)
        self._tokens = TokenManager(creds, self._http)
        self._cache = cache if cache is not None else DiskCache()
        self.max_retries = max_retries

    # -- public API -------------------------------------------------------

    def keyword_search(self, keywords: str, limit: int = 5) -> list[Candidate]:
        """Search Digi-Key by keyword, returning normalized candidates."""
        raw = self._keyword_search_raw(keywords, limit)
        products = raw.get("Products") or []
        return [c for p in products if (c := product_to_candidate(p)) is not None]

    def _keyword_search_raw(self, keywords: str, limit: int) -> dict:
        cache_key = f"kw::{self.creds.locale_currency}::{keywords}::{limit}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        body = {"Keywords": keywords, "Limit": limit, "Offset": 0}
        data = self._post(KEYWORD_SEARCH_PATH, body)
        self._cache.set(cache_key, data)
        return data

    # -- transport --------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._tokens.get_token()}",
            "X-DIGIKEY-Client-Id": self.creds.client_id,
            "X-DIGIKEY-Locale-Site": self.creds.locale_site,
            "X-DIGIKEY-Locale-Language": self.creds.locale_language,
            "X-DIGIKEY-Locale-Currency": self.creds.locale_currency,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _post(self, path: str, body: dict) -> dict:
        url = self.creds.base_url + path
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self._http.post(url, json=body, headers=self._headers())
            except httpx.HTTPError as exc:  # network-level
                last_exc = exc
                time.sleep(2**attempt)
                continue
            if resp.status_code == 429:  # rate limited
                wait = int(resp.headers.get("Retry-After", 2**attempt))
                time.sleep(max(wait, 1))
                continue
            if resp.status_code >= 500:
                time.sleep(2**attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        if last_exc:
            raise last_exc
        raise RuntimeError(f"Digi-Key request failed after {self.max_retries} retries: {path}")


# -- response normalization ----------------------------------------------


def _first(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _pick_variation(product: dict) -> dict:
    """Choose a representative packaging variation (lowest MOQ, prefer with pricing)."""
    variations = product.get("ProductVariations") or []
    if not variations:
        return {}
    scored = sorted(
        variations,
        key=lambda v: (
            0 if (v.get("StandardPricing") or v.get("StandardPackaging")) else 1,
            v.get("MinimumOrderQuantity", 10**9) or 10**9,
        ),
    )
    return scored[0]


def _extract_price_breaks(variation: dict, product: dict) -> list[tuple[int, float]]:
    pricing = variation.get("StandardPricing") or product.get("StandardPricing") or []
    breaks: list[tuple[int, float]] = []
    for entry in pricing:
        bq = entry.get("BreakQuantity")
        up = entry.get("UnitPrice")
        if bq is not None and up is not None:
            breaks.append((int(bq), float(up)))
    breaks.sort()
    return breaks


def _extract_parameters(product: dict) -> dict[str, str]:
    params: dict[str, str] = {}
    for p in product.get("Parameters") or []:
        name = _first(p, "ParameterText", "Parameter")
        value = _first(p, "ValueText", "Value")
        if name and value:
            params[str(name)] = str(value)
    return params


def _find_reel_variation(product: dict) -> dict:
    """The full-reel (Tape & Reel) variation, if the part offers one."""
    for v in product.get("ProductVariations") or []:
        name = ((v.get("PackageType") or {}).get("Name") or "").lower()
        if "tape & reel" in name or "(tr)" in name:
            return v
    return {}


def product_to_candidate(product: dict) -> Candidate | None:
    if not product:
        return None
    variation = _pick_variation(product)
    breaks = _extract_price_breaks(variation, product)
    reel_var = _find_reel_variation(product)
    reel_qty = reel_var.get("MinimumOrderQuantity") if reel_var else None
    reel_breaks = _extract_price_breaks(reel_var, {}) if reel_var else []
    reel_pn = _first(reel_var, "DigiKeyProductNumber", "DigiKeyPartNumber") if reel_var else None
    manufacturer = product.get("Manufacturer") or {}
    description = product.get("Description") or {}
    status = product.get("ProductStatus") or {}
    unit_price = _first(product, "UnitPrice")
    if unit_price is None and breaks:
        unit_price = breaks[0][1]
    return Candidate(
        supplier="Digi-Key",
        mpn=_first(product, "ManufacturerProductNumber", "ManufacturerPartNumber"),
        manufacturer=manufacturer.get("Name") if isinstance(manufacturer, dict) else manufacturer,
        dk_part_number=_first(variation, "DigiKeyProductNumber", "DigiKeyPartNumber")
        or _first(product, "DigiKeyProductNumber"),
        description=description.get("ProductDescription")
        if isinstance(description, dict)
        else description,
        datasheet_url=_first(product, "DatasheetUrl"),
        product_url=_first(product, "ProductUrl"),
        quantity_available=int(_first(product, "QuantityAvailable", default=0) or 0),
        lifecycle=status.get("Status") if isinstance(status, dict) else status,
        unit_price=float(unit_price) if unit_price is not None else None,
        price_breaks=breaks,
        reel_qty=int(reel_qty) if reel_qty else None,
        reel_price_breaks=reel_breaks,
        reel_part_number=reel_pn,
        parameters=_extract_parameters(product),
    )
