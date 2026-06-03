"""Mouser Search API v1 client (keyword search + normalization).

Used as a second-choice source behind Digi-Key. Auth is a single API key passed as
a query parameter; no OAuth. Get a Search API key from https://www.mouser.com/api-hub/.
"""

from __future__ import annotations

import re
import time

import httpx

from ..config import MouserCredentials
from ..digikey.cache import DiskCache
from ..models import Candidate

KEYWORD_SEARCH_PATH = "/search/keyword"


class MouserClient:
    def __init__(
        self,
        creds: MouserCredentials,
        cache: DiskCache | None = None,
        http: httpx.Client | None = None,
        max_retries: int = 3,
    ):
        self.creds = creds
        self._http = http or httpx.Client(timeout=30)
        self._cache = cache if cache is not None else DiskCache()
        self.max_retries = max_retries

    def keyword_search(self, keywords: str, limit: int = 5) -> list[Candidate]:
        raw = self._keyword_search_raw(keywords, limit)
        parts = (raw.get("SearchResults") or {}).get("Parts") or []
        return [c for p in parts if (c := part_to_candidate(p)) is not None]

    def _keyword_search_raw(self, keywords: str, limit: int) -> dict:
        cache_key = f"mouser-kw::{keywords}::{limit}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        body = {
            "SearchByKeywordRequest": {
                "keyword": keywords,
                "records": limit,
                "startingRecord": 0,
                "searchOptions": "None",
                "searchWithYourSignUpLanguage": "en",
            }
        }
        data = self._post(KEYWORD_SEARCH_PATH, body)
        errors = data.get("Errors") or []
        if errors:
            raise RuntimeError(f"Mouser API error: {errors}")
        self._cache.set(cache_key, data)
        return data

    def _post(self, path: str, body: dict) -> dict:
        url = f"{self.creds.base_url}{path}?apiKey={self.creds.api_key}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self._http.post(
                    url, json=body,
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                time.sleep(2**attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(2**attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        if last_exc:
            raise last_exc
        raise RuntimeError(f"Mouser request failed after {self.max_retries} retries: {path}")


# -- response normalization ----------------------------------------------


def parse_price(text: str | None) -> float | None:
    """Parse a Mouser price string like '$0.10', '0,10 €', '1.234,56 kr'."""
    if not text:
        return None
    s = re.sub(r"[^\d.,]", "", text)
    if not s:
        return None
    if "," in s and "." in s:
        # The right-most separator is the decimal point.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")  # EU decimal comma
    try:
        return float(s)
    except ValueError:
        return None


def parse_availability(text: str | None) -> int:
    """'5000 In Stock' / '1,234 In Stock' -> integer; 'Quote'/'' -> 0."""
    if not text:
        return 0
    digits = re.sub(r"[^\d]", "", text.split()[0]) if text.split() else ""
    return int(digits) if digits else 0


def _attributes(part: dict) -> dict[str, str]:
    params: dict[str, str] = {}
    for a in part.get("ProductAttributes") or []:
        name, value = a.get("AttributeName"), a.get("AttributeValue")
        if name and value:
            params[str(name)] = str(value)
    return params


def part_to_candidate(part: dict) -> Candidate | None:
    if not part:
        return None
    breaks: list[tuple[int, float]] = []
    for pb in part.get("PriceBreaks") or []:
        qty, price = pb.get("Quantity"), parse_price(pb.get("Price"))
        if qty is not None and price is not None:
            breaks.append((int(qty), price))
    breaks.sort()
    currency = next((pb.get("Currency") for pb in part.get("PriceBreaks") or []), None)
    return Candidate(
        supplier="Mouser",
        mpn=part.get("ManufacturerPartNumber"),
        manufacturer=part.get("Manufacturer"),
        dk_part_number=part.get("MouserPartNumber"),
        description=part.get("Description"),
        datasheet_url=part.get("DataSheetUrl"),
        product_url=part.get("ProductDetailUrl"),
        quantity_available=parse_availability(part.get("Availability")),
        lifecycle=part.get("LifecycleStatus"),
        unit_price=breaks[0][1] if breaks else None,
        currency=currency,
        price_breaks=breaks,
        parameters=_attributes(part),
    )
