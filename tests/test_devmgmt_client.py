import httpx
import pytest
import respx

from digisearch.devmgmt import (
    BearerAuth,
    DevmgmtAuthError,
    DevmgmtClient,
    DevmgmtConfig,
    DevmgmtConflictError,
    DevmgmtError,
    DevmgmtPayloadError,
    DevmgmtReferentialError,
    MutualTLSAuth,
    NoAuth,
)
from digisearch.devmgmt.client import DEVICES_PATH, MODELS_PATH, VARIANTS_PATH

BASE = "https://devmgmt.example.com"


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    # The client backs off with time.sleep between retries; skip the real waits in tests.
    monkeypatch.setattr("digisearch.devmgmt.client.time.sleep", lambda *_: None)


def _client(**kw) -> DevmgmtClient:
    # Inject a plain httpx client so no real cert is needed; respx patches its transport.
    return DevmgmtClient(BASE, http=httpx.Client(timeout=5), max_retries=3, **kw)


@respx.mock
def test_upsert_model_posts_expected_body():
    route = respx.post(f"{BASE}{MODELS_PATH}").mock(return_value=httpx.Response(200, json={"ok": True}))
    body = {"ref": "PM-CONN840", "name": "Connectivity840"}
    assert _client().upsert_model(body) == {"ok": True}
    assert route.called
    import json
    assert json.loads(route.calls.last.request.content) == body


@respx.mock
def test_push_all_calls_endpoints_in_referential_order():
    seen = []
    for path in (MODELS_PATH, VARIANTS_PATH, DEVICES_PATH):
        respx.post(f"{BASE}{path}").mock(
            side_effect=lambda req, p=path: seen.append(p) or httpx.Response(200, json={}))
    _client().push_all(model={"ref": "m"}, variant={"ref": "v"}, device={"serial": "s"})
    assert seen == [MODELS_PATH, VARIANTS_PATH, DEVICES_PATH]


@respx.mock
def test_409_raises_referential_error():
    respx.post(f"{BASE}{VARIANTS_PATH}").mock(
        return_value=httpx.Response(409, json={"message": "unknown model"}))
    with pytest.raises(DevmgmtReferentialError, match="referential gap"):
        _client().upsert_variant({"ref": "v", "model_ref": "missing"})


@respx.mock
def test_400_raises_payload_error_and_does_not_retry():
    route = respx.post(f"{BASE}{MODELS_PATH}").mock(return_value=httpx.Response(400, json={"error": "bad"}))
    with pytest.raises(DevmgmtPayloadError):
        _client().upsert_model({"bad": "payload"})
    assert route.call_count == 1  # 400 is terminal, not retried


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors(status):
    respx.post(f"{BASE}{DEVICES_PATH}").mock(return_value=httpx.Response(status, json={}))
    with pytest.raises(DevmgmtAuthError):
        _client().provision_device({"serial": "s"})


@respx.mock
def test_5xx_is_retried_then_succeeds():
    route = respx.post(f"{BASE}{MODELS_PATH}").mock(side_effect=[
        httpx.Response(503),
        httpx.Response(500),
        httpx.Response(200, json={"ok": 1}),
    ])
    assert _client().upsert_model({"ref": "m"}) == {"ok": 1}
    assert route.call_count == 3


@respx.mock
def test_5xx_exhausts_retries_and_raises():
    respx.post(f"{BASE}{MODELS_PATH}").mock(return_value=httpx.Response(500))
    with pytest.raises(DevmgmtError, match="after 3 retries"):
        _client().upsert_model({"ref": "m"})


@respx.mock
def test_3xx_redirect_is_an_error_not_success():
    # httpx doesn't follow redirects; a redirected upsert was never delivered, so treating it as
    # success would mark outbox jobs done / stamp pushed_at while devmgmt received nothing.
    respx.post(f"{BASE}{MODELS_PATH}").mock(
        return_value=httpx.Response(301, headers={"Location": "https://elsewhere.example.com"}))
    with pytest.raises(DevmgmtError, match="HTTP 301"):
        _client().upsert_model({"ref": "m"})


@respx.mock
def test_network_error_is_retried_then_raises():
    route = respx.post(f"{BASE}{MODELS_PATH}").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(DevmgmtError, match="Could not reach"):
        _client().upsert_model({"ref": "m"})
    assert route.call_count == 3


# -- hard delete (docs §7) -------------------------------------------------

@respx.mock
def test_delete_variant_success():
    route = respx.delete(f"{BASE}{VARIANTS_PATH}/SKU-1").mock(return_value=httpx.Response(204))
    _client().delete_variant("SKU-1")
    assert route.called


@respx.mock
def test_delete_is_idempotent_on_404():
    respx.delete(f"{BASE}{VARIANTS_PATH}/gone").mock(
        return_value=httpx.Response(404, json={"error": "unknown"}))
    _client().delete_variant("gone")   # 404 == already deleted; must NOT raise


@respx.mock
def test_delete_409_raises_conflict():
    respx.delete(f"{BASE}{VARIANTS_PATH}/SKU-1").mock(
        return_value=httpx.Response(409, json={"error": "retire before delete"}))
    with pytest.raises(DevmgmtConflictError, match="retire before delete"):
        _client().delete_variant("SKU-1")


@respx.mock
def test_delete_5xx_retried_then_raises():
    route = respx.delete(f"{BASE}{MODELS_PATH}/m").mock(return_value=httpx.Response(500))
    with pytest.raises(DevmgmtError, match="after 3 retries"):
        _client().delete_model("m")
    assert route.call_count == 3


@respx.mock
def test_delete_device_uses_devices_path():
    route = respx.delete(f"{BASE}{DEVICES_PATH}/SN-1").mock(return_value=httpx.Response(200, json={}))
    _client().delete_device("SN-1")
    assert route.called


@respx.mock
def test_bearer_auth_sets_authorization_header():
    route = respx.post(f"{BASE}{MODELS_PATH}").mock(return_value=httpx.Response(200, json={}))
    client = DevmgmtClient(BASE, auth=BearerAuth("secret-token"), http=httpx.Client())
    client.upsert_model({"ref": "m"})
    assert route.calls.last.request.headers["Authorization"] == "Bearer secret-token"


# -- config / auth wiring --------------------------------------------------

def test_config_absent_without_base_url(monkeypatch):
    # Blank base URL disables the integration. Set it empty rather than delete it: from_env() calls
    # load_dotenv(), which would otherwise repopulate it from the developer's real .env file.
    monkeypatch.setenv("DEVMGMT_BASE_URL", "")
    assert DevmgmtConfig.from_env() is None


def test_config_mtls_builds_mutual_tls_auth():
    cfg = DevmgmtConfig(base_url=BASE, auth_mode="mtls",
                        client_cert="/c.pem", client_key="/k.pem", ca_cert="/ca.pem")
    auth = cfg.build_auth()
    assert isinstance(auth, MutualTLSAuth)
    assert auth.client_cert == "/c.pem" and auth.ca_cert == "/ca.pem"


def test_config_mtls_requires_cert_and_key():
    with pytest.raises(RuntimeError, match="DEVMGMT_CLIENT_CERT"):
        DevmgmtConfig(base_url=BASE, auth_mode="mtls").build_auth()


def test_config_bearer_requires_token():
    with pytest.raises(RuntimeError, match="DEVMGMT_BEARER_TOKEN"):
        DevmgmtConfig(base_url=BASE, auth_mode="bearer").build_auth()


def test_config_unknown_mode_raises():
    with pytest.raises(RuntimeError, match="Unknown DEVMGMT_AUTH_MODE"):
        DevmgmtConfig(base_url=BASE, auth_mode="carrier-pigeon").build_auth()


def test_noauth_contributes_no_headers():
    assert NoAuth().headers() == {}
