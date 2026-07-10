"""devmgmt connection config, loaded from the environment (.env / Setup & Tools).

Blank ``DEVMGMT_BASE_URL`` disables the integration (mirrors how a blank MOUSER_API_KEY disables
Mouser). Auth mode defaults to mTLS — the chosen first mechanism (docs §8) — and reads the client
cert/key (+ optional CA bundle) from paths. ``bearer`` and ``none`` are here so the mechanism can
swap without code changes; ``none`` targets a local stub for the first end-to-end milestone.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from pydantic import BaseModel

from .auth import AuthStrategy, BearerAuth, MutualTLSAuth, NoAuth

_dotenv_loaded = False


def _load_dotenv_once() -> None:
    """Parse .env into os.environ once per process. ``from_env`` is called on every sync-loop tick
    and on panel renders; re-reading the file each time is wasted disk I/O — and pointless anyway,
    since deployment doctrine (CLAUDE.md) is that .env changes require a service restart. Already-
    set environment variables always win (load_dotenv never overrides), so tests that monkeypatch
    DEVMGMT_* env vars behave the same."""
    global _dotenv_loaded
    if not _dotenv_loaded:
        load_dotenv()
        _dotenv_loaded = True


class DevmgmtConfig(BaseModel):
    base_url: str
    auth_mode: str = "mtls"          # mtls | bearer | none
    client_cert: str | None = None
    client_key: str | None = None
    ca_cert: str | None = None
    bearer_token: str | None = None

    @classmethod
    def from_env(cls) -> "DevmgmtConfig | None":
        """Build config from the environment, or None if devmgmt isn't configured (no base URL)."""
        _load_dotenv_once()
        base_url = os.getenv("DEVMGMT_BASE_URL", "").strip()
        if not base_url:
            return None
        return cls(
            base_url=base_url,
            auth_mode=os.getenv("DEVMGMT_AUTH_MODE", "mtls").strip().lower() or "mtls",
            client_cert=os.getenv("DEVMGMT_CLIENT_CERT", "").strip() or None,
            client_key=os.getenv("DEVMGMT_CLIENT_KEY", "").strip() or None,
            ca_cert=os.getenv("DEVMGMT_CA_CERT", "").strip() or None,
            bearer_token=os.getenv("DEVMGMT_BEARER_TOKEN", "").strip() or None,
        )

    def build_auth(self) -> AuthStrategy:
        """Resolve the configured auth mode into a concrete strategy (raises on misconfiguration)."""
        mode = self.auth_mode
        if mode == "mtls":
            if not self.client_cert or not self.client_key:
                raise RuntimeError(
                    "devmgmt mTLS needs DEVMGMT_CLIENT_CERT and DEVMGMT_CLIENT_KEY set."
                )
            return MutualTLSAuth(self.client_cert, self.client_key, self.ca_cert)
        if mode == "bearer":
            if not self.bearer_token:
                raise RuntimeError("devmgmt bearer auth needs DEVMGMT_BEARER_TOKEN set.")
            return BearerAuth(self.bearer_token)
        if mode == "none":
            return NoAuth()
        raise RuntimeError(f"Unknown DEVMGMT_AUTH_MODE {mode!r} (expected mtls, bearer or none).")
