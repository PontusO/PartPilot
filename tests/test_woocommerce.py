import httpx
import pytest
import respx

from digisearch.woocommerce import WooClient, WooError

BASE = "https://shop.example.com"
PRODUCTS = f"{BASE}/wp-json/wc/v3/products"


def _client() -> WooClient:
    return WooClient(BASE, "ck_test", "cs_test", max_retries=1)


_next_id = [1000]


def _product(sku, name="Widget", qty=5, manage=True, **extra):
    _next_id[0] += 1
    p = {"id": _next_id[0], "sku": sku, "name": name, "manage_stock": manage,
         "stock_quantity": qty, "stock_status": "instock", "type": "simple",
         "price": "12.50", "regular_price": "12.50", "sale_price": "", "on_sale": False}
    p.update(extra)
    return p


@respx.mock
def test_iter_products_normalizes_fields():
    respx.get(PRODUCTS).mock(return_value=httpx.Response(
        200, json=[_product("99-100", name="Resistor", qty=42,
                            short_description="<p>1k 0402</p>")]))
    products = list(_client().iter_products())
    assert len(products) == 1
    p = products[0]
    assert p.sku == "99-100" and p.name == "Resistor"
    assert p.stock_quantity == 42.0 and p.manage_stock is True
    assert p.description == "1k 0402"  # HTML stripped
    assert p.price == 12.50            # parsed from the shop price string


@respx.mock
def test_unmanaged_stock_is_none_not_zero():
    respx.get(PRODUCTS).mock(return_value=httpx.Response(
        200, json=[_product("99-200", manage=False, qty=None)]))
    p = list(_client().iter_products())[0]
    assert p.manage_stock is False and p.stock_quantity is None


@respx.mock
def test_products_without_sku_are_dropped():
    respx.get(PRODUCTS).mock(return_value=httpx.Response(
        200, json=[_product(""), _product("98-1")]))
    skus = [p.sku for p in _client().iter_products()]
    assert skus == ["98-1"]


@respx.mock
def test_pagination_stops_on_short_page():
    full = [_product(f"99-{i}") for i in range(100)]
    route = respx.get(PRODUCTS)
    route.side_effect = [
        httpx.Response(200, json=full),          # page 1: full -> fetch more
        httpx.Response(200, json=[_product("99-x")]),  # page 2: short -> stop
    ]
    products = list(_client().iter_products())
    assert len(products) == 101
    assert route.call_count == 2


@respx.mock
def test_auth_failure_raises_wooerror():
    respx.get(PRODUCTS).mock(return_value=httpx.Response(401, json={"message": "nope"}))
    with pytest.raises(WooError):
        _client().ping()


@respx.mock
def test_ping_ok_on_empty_shop():
    respx.get(PRODUCTS).mock(return_value=httpx.Response(200, json=[]))
    assert _client().ping() is True


@respx.mock
def test_product_carries_woo_id():
    respx.get(PRODUCTS).mock(return_value=httpx.Response(
        200, json=[{"id": 77, "sku": "99-1", "name": "R", "manage_stock": True,
                    "stock_quantity": 5, "type": "simple"}]))
    assert list(_client().iter_products())[0].id == 77


@respx.mock
def test_price_prefers_stored_base_price_over_converted():
    # A multi-currency plugin converted `price` to EUR, but regular_price stays base SEK.
    respx.get(PRODUCTS).mock(return_value=httpx.Response(
        200, json=[_product("99-00230-1", price="6.27", regular_price="65", on_sale=False)]))
    assert list(_client().iter_products())[0].price == 65.0


@respx.mock
def test_sale_price_used_when_on_sale():
    respx.get(PRODUCTS).mock(return_value=httpx.Response(
        200, json=[_product("99-1", price="80", regular_price="100",
                            sale_price="80", on_sale=True)]))
    assert list(_client().iter_products())[0].price == 80.0


@respx.mock
def test_price_falls_back_to_price_when_no_regular():
    respx.get(PRODUCTS).mock(return_value=httpx.Response(
        200, json=[_product("99-1", price="9.99", regular_price="", on_sale=False)]))
    assert list(_client().iter_products())[0].price == 9.99


@respx.mock
def test_currency_override_is_sent_on_reads():
    route = respx.get(PRODUCTS).mock(return_value=httpx.Response(200, json=[]))
    WooClient(BASE, "ck", "cs", currency="SEK", max_retries=1).ping()
    assert route.calls.last.request.url.params["currency"] == "SEK"


@respx.mock
def test_no_currency_param_by_default():
    route = respx.get(PRODUCTS).mock(return_value=httpx.Response(200, json=[]))
    _client().ping()
    assert "currency" not in route.calls.last.request.url.params


@respx.mock
def test_update_stock_batch_chunks_and_counts():
    route = respx.post(f"{BASE}/wp-json/wc/v3/products/batch")
    # 150 updates -> two chunks (100 + 50); echo back the update arrays
    def _reply(request):
        import json
        body = json.loads(request.content)
        return httpx.Response(200, json={"update": body["update"]})
    route.side_effect = _reply

    updates = [(i, float(i)) for i in range(1, 151)]
    written = _client().update_stock_batch(updates)
    assert written == 150 and route.call_count == 2


@respx.mock
def test_update_stock_batch_write_denied_raises():
    respx.post(f"{BASE}/wp-json/wc/v3/products/batch").mock(
        return_value=httpx.Response(401, json={"message": "read-only key"}))
    with pytest.raises(WooError, match="write"):
        _client().update_stock_batch([(1, 5.0)])
