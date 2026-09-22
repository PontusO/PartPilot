"""Fortnox REST API v3 client with OAuth2 (authorization-code) auth.

Auth model (per the Fortnox developer docs): a user authorises the integration once; we exchange
the code for an access token (1 h) and a refresh token (45 days). The refresh token **rotates** —
each refresh returns a new one and invalidates the old — so whenever we refresh we persist the new
pair via the ``on_refresh`` callback. API calls send ``Authorization: Bearer <access_token>``.

Only the slice PartPilot needs is implemented: look up / create a customer, and create a (draft)
invoice. Modelled on the WooCommerce client in this project (httpx + small retry/backoff loop).
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

API_BASE = "https://api.fortnox.se/3"
AUTH_URL = "https://apps.fortnox.se/oauth-v1/auth"
TOKEN_URL = "https://apps.fortnox.se/oauth-v1/token"
DEFAULT_SCOPES = ("invoice", "customer", "companyinformation")
_REFRESH_SKEW = 60  # refresh this many seconds before the access token actually expires


class FortnoxError(RuntimeError):
    """A Fortnox request failed (auth, network, rate limit exhausted, or an API error)."""


class FortnoxAuthError(FortnoxError):
    """The tokens are invalid/expired and the integration must be reconnected by a user."""


@dataclass
class FortnoxTokens:
    access_token: str
    refresh_token: str
    expires_at: datetime  # tz-aware UTC

    def as_dict(self) -> dict:
        return {"access_token": self.access_token, "refresh_token": self.refresh_token,
                "expires_at": self.expires_at.isoformat()}

    @property
    def expired(self) -> bool:
        return _now() >= self.expires_at - timedelta(seconds=_REFRESH_SKEW)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tokens_from_response(data: dict) -> FortnoxTokens:
    return FortnoxTokens(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_at=_now() + timedelta(seconds=int(data.get("expires_in", 3600))),
    )


# -- OAuth (no instance/tokens needed) -------------------------------------

def authorize_url(client_id: str, redirect_uri: str, state: str,
                  scopes=DEFAULT_SCOPES) -> str:
    """The URL to send the user to so they can authorise the integration."""
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "access_type": "offline",   # required to receive a refresh token
        "response_type": "code",
        "account_type": "service",
    })


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str,
                  *, http: httpx.Client | None = None) -> FortnoxTokens:
    """Swap an authorization code for the initial access + refresh tokens."""
    return _token_request(client_id, client_secret, {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
    }, http=http)


def _token_request(client_id: str, client_secret: str, body: dict,
                   *, http: httpx.Client | None = None) -> FortnoxTokens:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    client = http or httpx.Client(timeout=30)
    try:
        resp = client.post(TOKEN_URL, data=body, headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        })
    except httpx.HTTPError as exc:
        raise FortnoxError(f"Could not reach Fortnox token endpoint: {exc}") from exc
    finally:
        if http is None:
            client.close()
    if resp.status_code >= 400:
        # invalid_grant on refresh => the refresh token is dead; needs a fresh user authorisation
        detail = _safe_json(resp)
        if body.get("grant_type") == "refresh_token":
            raise FortnoxAuthError(
                f"Fortnox refused to refresh the token ({detail}). Reconnect the integration.")
        raise FortnoxError(f"Fortnox token request failed: HTTP {resp.status_code} {detail}")
    return _tokens_from_response(resp.json())


# -- the client --------------------------------------------------------------

class FortnoxClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        tokens: FortnoxTokens,
        *,
        on_refresh=None,
        http: httpx.Client | None = None,
        max_retries: int = 3,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.tokens = tokens
        self._on_refresh = on_refresh          # called with FortnoxTokens after every refresh
        self._http = http or httpx.Client(timeout=30)
        self.max_retries = max_retries

    # --- customers ---

    def find_customer_by_orgno(self, org_no: str) -> dict | None:
        """The first Fortnox customer with this organisation number, or None."""
        org = (org_no or "").strip()
        if not org:
            return None
        data = self._request("GET", "/customers", params={"organisationnumber": org})
        customers = (data.get("Customers") or [])
        return customers[0] if customers else None

    def create_customer(self, customer: dict) -> dict:
        """Create a customer; returns the created Customer dict (incl. ``CustomerNumber``)."""
        data = self._request("POST", "/customers", json={"Customer": customer})
        return data.get("Customer") or {}

    # --- invoices ---

    def create_invoice(self, invoice: dict) -> dict:
        """Create an invoice (left as a draft); returns the Invoice dict (incl. ``DocumentNumber``)."""
        data = self._request("POST", "/invoices", json={"Invoice": invoice})
        return data.get("Invoice") or {}

    # --- transport ---

    def _ensure_token(self) -> None:
        if self.tokens.expired:
            self.tokens = _token_request(
                self.client_id, self.client_secret,
                {"grant_type": "refresh_token", "refresh_token": self.tokens.refresh_token},
                http=self._http,
            )
            if self._on_refresh:
                self._on_refresh(self.tokens)

    def _request(self, method: str, path: str, *, json: dict | None = None,
                 params: dict | None = None, _retried_auth: bool = False):
        self._ensure_token()
        url = f"{API_BASE}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self._http.request(
                    method, url, json=json, params=params,
                    headers={"Authorization": f"Bearer {self.tokens.access_token}",
                             "Accept": "application/json", "Content-Type": "application/json"},
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                time.sleep(2**attempt)
                continue
            if resp.status_code == 429:                       # rate limited — back off and retry
                time.sleep(_retry_after(resp, attempt))
                continue
            if resp.status_code >= 500:
                time.sleep(2**attempt)
                continue
            if resp.status_code == 401 and not _retried_auth:  # token died mid-flight — refresh once
                self.tokens = _token_request(
                    self.client_id, self.client_secret,
                    {"grant_type": "refresh_token", "refresh_token": self.tokens.refresh_token},
                    http=self._http,
                )
                if self._on_refresh:
                    self._on_refresh(self.tokens)
                return self._request(method, path, json=json, params=params, _retried_auth=True)
            if resp.status_code in (401, 403):
                raise FortnoxAuthError(
                    f"Fortnox rejected the request (HTTP {resp.status_code}). Reconnect the "
                    f"integration. {_error_message(resp)}")
            if resp.status_code >= 400:
                raise FortnoxError(f"Fortnox API error (HTTP {resp.status_code}): {_error_message(resp)}")
            return _safe_json(resp)
        if last_exc:
            raise FortnoxError(f"Could not reach Fortnox: {last_exc}") from last_exc
        raise FortnoxError(f"Fortnox request failed after {self.max_retries} retries: {path}")


# -- helpers ---------------------------------------------------------------

def _retry_after(resp: httpx.Response, attempt: int) -> float:
    try:
        return float(resp.headers.get("Retry-After", ""))
    except ValueError:
        return 2.0 * (attempt + 1)


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {}


def _error_message(resp: httpx.Response) -> str:
    info = _safe_json(resp).get("ErrorInformation") or {}
    return info.get("message") or info.get("Message") or resp.text[:200]
