"""OAuth2 client-credentials (2-legged) token management for Digi-Key v4."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from ..config import DigiKeyCredentials

TOKEN_PATH = "/v1/oauth2/token"
_TOKEN_CACHE_FILE = Path(".digisearch_cache") / "token.json"


class TokenManager:
    """Fetches and caches a client-credentials access token.

    No browser/user login is required: the client id + secret are exchanged
    directly for a bearer token used on every product request.
    """

    def __init__(self, creds: DigiKeyCredentials, client: httpx.Client | None = None):
        self.creds = creds
        self._client = client or httpx.Client(timeout=30)
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._load_cached()

    def _cache_key(self) -> str:
        return f"{self.creds.base_url}:{self.creds.client_id}"

    def _load_cached(self) -> None:
        if not _TOKEN_CACHE_FILE.exists():
            return
        try:
            data = json.loads(_TOKEN_CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return
        if data.get("key") == self._cache_key() and data.get("expires_at", 0) > time.time() + 60:
            self._token = data["access_token"]
            self._expires_at = data["expires_at"]

    def _store_cached(self) -> None:
        _TOKEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_CACHE_FILE.write_text(
            json.dumps(
                {
                    "key": self._cache_key(),
                    "access_token": self._token,
                    "expires_at": self._expires_at,
                }
            )
        )

    def get_token(self) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        return self._refresh()

    def _refresh(self) -> str:
        resp = self._client.post(
            self.creds.base_url + TOKEN_PATH,
            data={
                "client_id": self.creds.client_id,
                "client_secret": self.creds.client_secret,
                "grant_type": "client_credentials",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._expires_at = time.time() + int(payload.get("expires_in", 600))
        self._store_cached()
        return self._token
