"""Shared fixtures: Digi-Key response samples and a fake searcher."""

from __future__ import annotations

import pytest

from digisearch.digikey.client import product_to_candidate
from digisearch.models import Candidate


def make_product(
    mpn="CL05B104KO5NNNC",
    manufacturer="Samsung",
    dkpn="1276-1000-1-ND",
    qty=50000,
    status="Active",
    params=None,
    breaks=((1, 0.10), (10, 0.05), (100, 0.02), (1000, 0.012)),
):
    """Build a Product dict in the shape of the v4 keyword-search response."""
    return {
        "ManufacturerProductNumber": mpn,
        "Manufacturer": {"Name": manufacturer},
        "Description": {"ProductDescription": "CAP CER 0.1UF 16V X7R 0402"},
        "DatasheetUrl": "https://example.com/ds.pdf",
        "ProductUrl": "https://www.digikey.com/x",
        "QuantityAvailable": qty,
        "ProductStatus": {"Status": status},
        "UnitPrice": breaks[0][1] if breaks else None,
        "Parameters": [
            {"ParameterText": k, "ValueText": v} for k, v in (params or {}).items()
        ],
        "ProductVariations": [
            {
                "DigiKeyProductNumber": dkpn,
                "MinimumOrderQuantity": 1,
                "StandardPricing": [
                    {"BreakQuantity": bq, "UnitPrice": up} for bq, up in breaks
                ],
            }
        ],
    }


def make_mouser_part(
    mpn="APS6404L-3SQR",
    manufacturer="AP Memory",
    mpn2="81-APS6404L-3SQR",
    availability="5000 In Stock",
    status="Active",
    breaks=(("1", "$1.50"), ("100", "$1.20")),
    attributes=None,
):
    """Build a Part dict in the shape of the Mouser keyword-search response."""
    return {
        "ManufacturerPartNumber": mpn,
        "Manufacturer": manufacturer,
        "MouserPartNumber": mpn2,
        "Description": "IC SRAM 64MBIT QSPI 8SOP",
        "DataSheetUrl": "https://example.com/ds.pdf",
        "ProductDetailUrl": "https://www.mouser.com/x",
        "Availability": availability,
        "LifecycleStatus": status,
        "PriceBreaks": [{"Quantity": int(q), "Price": p, "Currency": "USD"} for q, p in breaks],
        "ProductAttributes": [
            {"AttributeName": k, "AttributeValue": v} for k, v in (attributes or {}).items()
        ],
    }


CAP_PARAMS = {"Capacitance": "0.1 µF", "Package / Case": "0402 (1005 Metric)",
              "Tolerance": "±10%", "Temperature Coefficient": "X7R", "Voltage - Rated": "16V"}
RES_PARAMS = {"Resistance": "10 kOhms", "Package / Case": "0402 (1005 Metric)",
              "Tolerance": "±1%"}


class FakeSearcher:
    """Returns canned candidates keyed loosely by query content."""

    def __init__(self, mapping: dict[str, list[Candidate]] | None = None, default=None):
        self.mapping = mapping or {}
        self.default = default if default is not None else []
        self.calls: list[str] = []

    def keyword_search(self, keywords: str, limit: int = 5):
        self.calls.append(keywords)
        for needle, candidates in self.mapping.items():
            if needle.lower() in keywords.lower():
                return candidates[:limit]
        return list(self.default)[:limit]


@pytest.fixture
def cap_candidate() -> Candidate:
    return product_to_candidate(make_product(params=CAP_PARAMS))


@pytest.fixture
def res_candidate() -> Candidate:
    return product_to_candidate(
        make_product(mpn="RC0402FR-0710KL", manufacturer="Yageo", params=RES_PARAMS)
    )
