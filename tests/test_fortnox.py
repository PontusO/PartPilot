from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from digisearch.fortnox import FortnoxClient, FortnoxError, FortnoxTokens
from digisearch.fortnox.client import (
    AUTH_URL, TOKEN_URL, FortnoxAuthError, authorize_url, exchange_code,
)

API = "https://api.fortnox.se/3"


def _tokens(access="acc", refresh="ref", *, ttl=3600):
    return FortnoxTokens(access, refresh,
                         datetime.now(timezone.utc) + timedelta(seconds=ttl))


def _client(tokens=None, on_refresh=None):
    return FortnoxClient("cid", "secret", tokens or _tokens(), on_refresh=on_refresh,
                         max_retries=2)


def _token_response(access="acc2", refresh="ref2", expires_in=3600):
    return httpx.Response(200, json={"access_token": access, "refresh_token": refresh,
                                     "token_type": "Bearer", "expires_in": expires_in})


def test_authorize_url_has_required_params():
    url = authorize_url("cid", "https://pp.local/cb", "xyz")
    assert url.startswith(AUTH_URL)
    assert "client_id=cid" in url and "access_type=offline" in url
    assert "response_type=code" in url and "state=xyz" in url
    assert "scope=invoice+customer+companyinformation" in url


@respx.mock
def test_exchange_code_uses_basic_auth_and_returns_tokens():
    route = respx.post(TOKEN_URL).mock(return_value=_token_response("A", "R", 3600))
    toks = exchange_code("cid", "secret", "the-code", "https://pp.local/cb")
    assert toks.access_token == "A" and toks.refresh_token == "R"
    assert toks.expires_at > datetime.now(timezone.utc)
    req = route.calls.last.request
    assert req.headers["Authorization"].startswith("Basic ")
    assert b"grant_type=authorization_code" in req.content and b"the-code" in req.content


@respx.mock
def test_expired_access_token_is_refreshed_before_request():
    saved = []
    client = _client(_tokens(ttl=-10), on_refresh=lambda t: saved.append(t))  # already expired
    respx.post(TOKEN_URL).mock(return_value=_token_response("newacc", "newref"))
    cust = respx.get(f"{API}/customers").mock(
        return_value=httpx.Response(200, json={"Customers": [{"CustomerNumber": "42"}]}))

    found = client.find_customer_by_orgno("556677-8899")
    assert found["CustomerNumber"] == "42"
    # the refreshed token was used and persisted via on_refresh
    assert cust.calls.last.request.headers["Authorization"] == "Bearer newacc"
    assert saved and saved[0].refresh_token == "newref"


@respx.mock
def test_find_customer_returns_none_when_empty():
    respx.get(f"{API}/customers").mock(return_value=httpx.Response(200, json={"Customers": []}))
    assert _client().find_customer_by_orgno("000") is None


@respx.mock
def test_create_customer_wraps_and_unwraps():
    route = respx.post(f"{API}/customers").mock(
        return_value=httpx.Response(201, json={"Customer": {"CustomerNumber": "100", "Name": "Acme"}}))
    out = _client().create_customer({"Name": "Acme", "OrganisationNumber": "556"})
    assert out["CustomerNumber"] == "100"
    import json
    assert json.loads(route.calls.last.request.content) == {"Customer": {"Name": "Acme",
                                                                         "OrganisationNumber": "556"}}


@respx.mock
def test_create_invoice_returns_document_number():
    respx.post(f"{API}/invoices").mock(
        return_value=httpx.Response(201, json={"Invoice": {"DocumentNumber": "2001"}}))
    out = _client().create_invoice({"CustomerNumber": "100", "InvoiceRows": []})
    assert out["DocumentNumber"] == "2001"


@respx.mock
def test_401_midflight_triggers_one_refresh_and_retry():
    client = _client(on_refresh=lambda t: None)
    respx.post(TOKEN_URL).mock(return_value=_token_response("fresh", "ref3"))
    route = respx.post(f"{API}/invoices")
    route.side_effect = [
        httpx.Response(401, json={"ErrorInformation": {"message": "expired"}}),
        httpx.Response(201, json={"Invoice": {"DocumentNumber": "9"}}),
    ]
    out = client.create_invoice({"CustomerNumber": "1"})
    assert out["DocumentNumber"] == "9"
    assert route.calls.last.request.headers["Authorization"] == "Bearer fresh"


@respx.mock
def test_dead_refresh_token_raises_auth_error():
    client = _client(_tokens(ttl=-10))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        400, json={"error": "invalid_grant", "error_description": "expired"}))
    with pytest.raises(FortnoxAuthError):
        client.find_customer_by_orgno("556")


@respx.mock
def test_api_error_surfaces_message():
    respx.post(f"{API}/invoices").mock(return_value=httpx.Response(
        400, json={"ErrorInformation": {"message": "Customer not found"}}))
    with pytest.raises(FortnoxError, match="Customer not found"):
        _client().create_invoice({"CustomerNumber": "nope"})


@respx.mock
def test_429_is_retried_then_succeeds():
    route = respx.post(f"{API}/invoices")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(201, json={"Invoice": {"DocumentNumber": "7"}}),
    ]
    assert _client().create_invoice({})["DocumentNumber"] == "7"
