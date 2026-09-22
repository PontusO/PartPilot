"""Auth strategies for talking to devmgmt.

The devmgmt auth mechanism is an open decision (docs/partpilot-integration.md §8): mutual TLS
vs a system Bearer token. Both are built behind this small ``AuthStrategy`` protocol so the
transport can swap without touching ``DevmgmtClient``. mTLS is the chosen first mechanism; the
Bearer strategy is here so the swap is a one-line config change once devmgmt settles it.

A strategy does two things: build the httpx client (mTLS needs a client-cert-bearing SSL
context) and contribute request headers (Bearer needs an Authorization header). A strategy that
does neither (``NoAuth``) exists for local stubs and tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import httpx


class AuthStrategy(Protocol):
    """How PartPilot authenticates to devmgmt. Kept tiny so mTLS/Bearer are interchangeable."""

    def build_client(self, *, timeout: float) -> httpx.Client:
        """Return the httpx client the transport should use (mTLS bakes the cert in here)."""

    def headers(self) -> dict[str, str]:
        """Per-request headers this strategy contributes (Bearer sets Authorization here)."""


class NoAuth:
    """No client cert, no auth header. For local stubs and tests only."""

    def build_client(self, *, timeout: float) -> httpx.Client:
        return httpx.Client(timeout=timeout)

    def headers(self) -> dict[str, str]:
        return {}


class MutualTLSAuth:
    """Client-certificate (mTLS) auth — the chosen first mechanism (docs §4/§8).

    PartPilot presents ``client_cert``/``client_key`` on every call; ``ca_cert`` (optional) is the
    CA bundle used to verify devmgmt's server certificate. The cert files must exist when a request
    is actually made (httpx builds the SSL context lazily on first use)."""

    def __init__(self, client_cert: str | Path, client_key: str | Path,
                 ca_cert: str | Path | None = None):
        self.client_cert = str(client_cert)
        self.client_key = str(client_key)
        self.ca_cert = str(ca_cert) if ca_cert else None

    def build_client(self, *, timeout: float) -> httpx.Client:
        return httpx.Client(
            cert=(self.client_cert, self.client_key),
            verify=self.ca_cert if self.ca_cert else True,
            timeout=timeout,
        )

    def headers(self) -> dict[str, str]:
        return {}


class BearerAuth:
    """System Bearer-token auth (docs §4) — the alternative to mTLS, kept for an easy swap."""

    def __init__(self, token: str):
        self.token = token

    def build_client(self, *, timeout: float) -> httpx.Client:
        return httpx.Client(timeout=timeout)

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}
