"""Configuration: environment credentials, settings file, and column mappings."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

PKG_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PKG_ROOT.parent.parent
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"

PROD_BASE = "https://api.digikey.com"
SANDBOX_BASE = "https://sandbox-api.digikey.com"


class DigiKeyCredentials(BaseModel):
    client_id: str
    client_secret: str
    sandbox: bool = False
    locale_site: str = "SE"
    locale_language: str = "en"
    locale_currency: str = "SEK"

    @property
    def base_url(self) -> str:
        return SANDBOX_BASE if self.sandbox else PROD_BASE

    @classmethod
    def from_env(cls, *, sandbox: bool | None = None) -> "DigiKeyCredentials":
        load_dotenv()
        client_id = os.getenv("DIGIKEY_CLIENT_ID", "")
        client_secret = os.getenv("DIGIKEY_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            raise RuntimeError(
                "Missing Digi-Key credentials. Copy .env.example to .env and set "
                "DIGIKEY_CLIENT_ID and DIGIKEY_CLIENT_SECRET."
            )
        env_sandbox = os.getenv("DIGIKEY_SANDBOX", "false").lower() in ("1", "true", "yes")
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            sandbox=env_sandbox if sandbox is None else sandbox,
            locale_site=os.getenv("DIGIKEY_LOCALE_SITE", "SE"),
            locale_language=os.getenv("DIGIKEY_LOCALE_LANGUAGE", "en"),
            locale_currency=os.getenv("DIGIKEY_LOCALE_CURRENCY", "SEK"),
        )


class MouserCredentials(BaseModel):
    api_key: str
    base_url: str = "https://api.mouser.com/api/v1.0"

    @classmethod
    def from_env(cls) -> "MouserCredentials | None":
        load_dotenv()
        api_key = os.getenv("MOUSER_API_KEY", "")
        return cls(api_key=api_key) if api_key else None


class MatchWeights(BaseModel):
    value: float = 0.40
    package: float = 0.25
    in_stock: float = 0.20
    lifecycle: float = 0.10
    type: float = 0.05


class Settings(BaseModel):
    # --- Operational defaults (overridden by CLI flags when given) ---
    build_qty: int = 1  # default for --build-qty
    currency: str | None = None  # default for --currency (else .env locale currency)
    output_dir: str | None = None  # where to write reports (default: next to the input)

    # --- Matching / purchasing behaviour ---
    confidence_threshold: float = 0.75  # >= -> auto-resolved, else flagged for review
    # Buy a full reel when the whole reel costs under this (locale currency); else cut tape.
    reel_threshold: float = 10000.0
    candidates_per_line: int = 5
    alternates_kept: int = 3
    weights: MatchWeights = Field(default_factory=MatchWeights)
    # Defaults assumed for under-specified passives (each assumption flags the line).
    default_resistor_tolerance: str = "1%"
    default_capacitor_dielectric_small: str = "C0G"  # for <= 1nF
    default_capacitor_dielectric_bulk: str = "X7R"
    default_capacitor_voltage: str = "16V"
    dnp_values: list[str] = Field(
        default_factory=lambda: ["DNM", "DNP", "DNI", "NF", "NOTFITTED", "NOPOP", "NA"]
    )

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Settings":
        candidate = Path(path) if path else DEFAULT_CONFIG_DIR / "settings.yaml"
        if candidate.exists():
            data = yaml.safe_load(candidate.read_text()) or {}
            return cls.model_validate(data)
        return cls()


def load_column_mappings(path: str | Path | None = None) -> dict:
    candidate = Path(path) if path else DEFAULT_CONFIG_DIR / "column_mappings.yaml"
    if candidate.exists():
        return yaml.safe_load(candidate.read_text()) or {}
    return {}
