"""Canonical data models shared across the pipeline."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class LineKind(str, Enum):
    """How a BOM line should be sourced."""

    GENERIC_PASSIVE = "generic_passive"  # R/C/L specified by value + package
    CRYSTAL = "crystal"  # frequency + package, semi-generic
    MPN = "mpn"  # a real (or preferred) manufacturer part number
    LOOKUP = "lookup"  # generic device resolved via the device-lookup table
    DNP = "dnp"  # do-not-mount / do-not-populate
    NON_ORDERABLE = "non_orderable"  # testpoint, mounting hole, fiducial, etc.


class CompType(str, Enum):
    RESISTOR = "resistor"
    CAPACITOR = "capacitor"
    INDUCTOR = "inductor"
    CRYSTAL = "crystal"
    OTHER = "other"


class BomLine(BaseModel):
    """A normalized line read from a source BOM, before resolution."""

    refdes: list[str] = Field(default_factory=list)
    qty: int = 0  # per-board count (derived from refdes when possible)
    value: str | None = None
    device: str | None = None
    package: str | None = None
    description: str | None = None
    comment: str | None = None
    row_index: int = 0
    raw: dict = Field(default_factory=dict)

    @property
    def refdes_str(self) -> str:
        return ", ".join(self.refdes)


class PartSpec(BaseModel):
    """Structured interpretation of a generic (parametric) line."""

    comp_type: CompType = CompType.OTHER
    value_raw: str | None = None
    value_si: float | None = None  # ohms / farads / henries / hertz
    value_display: str | None = None
    package_imperial: str | None = None  # EIA chip size, e.g. "0402" (parametric-matched)
    package_note: str | None = None  # free-form package hint for the query, e.g. crystal "3.2 x 2.5 mm"
    package_code: str | None = None  # 4-digit outline code for crystals, e.g. "3225"
    tolerance: str | None = None
    voltage: str | None = None
    dielectric: str | None = None
    assumed: list[str] = Field(default_factory=list)  # attrs we defaulted (=> flag)
    value_warning: str | None = None  # e.g. non-standard E-series value (possible BOM typo)


class Candidate(BaseModel):
    """A single distributor product normalized from an API response."""

    supplier: str | None = None  # "Digi-Key" or "Mouser"
    mpn: str | None = None
    manufacturer: str | None = None
    dk_part_number: str | None = None
    description: str | None = None
    datasheet_url: str | None = None
    product_url: str | None = None
    quantity_available: int = 0
    lifecycle: str | None = None
    unit_price: float | None = None  # qty 1
    currency: str | None = None
    price_breaks: list[tuple[int, float]] = Field(default_factory=list)
    # Full-reel (Tape & Reel) packaging, when offered: reel size + its price breaks.
    reel_qty: int | None = None
    reel_price_breaks: list[tuple[int, float]] = Field(default_factory=list)
    reel_part_number: str | None = None  # distributor P/N for the reel variation
    parameters: dict[str, str] = Field(default_factory=dict)

    def order_part_number(self, packaging: str | None) -> str | None:
        """The distributor P/N to order for a given packaging decision."""
        if packaging == "Full reel" and self.reel_part_number:
            return self.reel_part_number
        return self.dk_part_number

    @staticmethod
    def _price_from(breaks: list[tuple[int, float]], quantity: int) -> float | None:
        applicable = [p for (bq, p) in breaks if bq <= quantity]
        if applicable:
            return applicable[-1]
        return breaks[0][1] if breaks else None

    def price_at(self, quantity: int) -> float | None:
        """Cut-tape unit price for the highest break quantity <= ``quantity``."""
        price = self._price_from(self.price_breaks, quantity)
        return price if price is not None else self.unit_price

    def reel_price_at(self, quantity: int) -> float | None:
        """Full-reel unit price for the highest reel break <= ``quantity``."""
        return self._price_from(self.reel_price_breaks, quantity)


class Status(str, Enum):
    RESOLVED = "resolved"  # confident automatic pick
    REVIEW = "review"  # picked but low confidence -> flag
    IN_STOCK = "in_stock"  # already covered by our own free stock; no purchase needed
    NOT_FOUND = "not_found"  # searched but nothing matched
    MANUAL = "manual"  # under-specified (no value/MPN) -> can't auto-resolve, needs a human
    DNP = "dnp"
    NON_ORDERABLE = "non_orderable"
    ERROR = "error"


class ResolvedLine(BaseModel):
    """A BOM line after Digi-Key resolution."""

    line: BomLine
    kind: LineKind
    spec: PartSpec | None = None
    query: str | None = None
    chosen: Candidate | None = None
    alternates: list[Candidate] = Field(default_factory=list)
    confidence: float = 0.0
    status: Status = Status.NOT_FOUND
    flag_reason: str | None = None
    # Our own catalog stock cross-reference (populated when the stock pre-check runs)
    stock_on_hand: float | None = None
    stock_free: float | None = None
    need_to_buy: int | None = None
    stock_match: str | None = None
    # Purchasing decision (full reel vs cut tape)
    packaging: str | None = None
    purchase_qty: int | None = None
    purchase_unit_price: float | None = None
    line_cost: float | None = None

    @property
    def flagged(self) -> bool:
        return self.status in (Status.REVIEW, Status.NOT_FOUND, Status.MANUAL, Status.ERROR)

    def order_qty(self, total_required: int) -> int:
        """How many to actually purchase: the shortfall if stock was checked, else all."""
        return self.need_to_buy if self.need_to_buy is not None else total_required

    def unit_price(self) -> float | None:
        return self.chosen.unit_price if self.chosen else None

    def build_unit_price(self, total_qty: int) -> float | None:
        return self.chosen.price_at(total_qty) if self.chosen else None
