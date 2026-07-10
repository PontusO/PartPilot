"""devmgmt integration — PartPilot pushes its catalog + per-device build records to devmgmt.

See docs/partpilot-integration.md for the full contract. PartPilot is the authoritative,
push-only side; this package is the *calling* client (auth wrapper + the three upserts).
"""

from __future__ import annotations

from .auth import AuthStrategy, BearerAuth, MutualTLSAuth, NoAuth
from .client import (
    DevmgmtAuthError,
    DevmgmtClient,
    DevmgmtConflictError,
    DevmgmtError,
    DevmgmtPayloadError,
    DevmgmtReferentialError,
)
from .config import DevmgmtConfig

__all__ = [
    "AuthStrategy",
    "BearerAuth",
    "MutualTLSAuth",
    "NoAuth",
    "DevmgmtClient",
    "DevmgmtConfig",
    "DevmgmtError",
    "DevmgmtPayloadError",
    "DevmgmtAuthError",
    "DevmgmtReferentialError",
    "DevmgmtConflictError",
]
