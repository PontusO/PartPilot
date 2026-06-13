import httpx
import pytest
import respx

from digisearch.woocommerce import WooClient, WooError

BASE = "https://shop.example.com"
PRODUCTS = f"{BASE}/wp-json/wc/v3/products"


def _client() -> WooClient:
    return WooClient(BASE, "ck_test", "cs_test", max_retries=1)


def _product(sku, name="Widget", qty=5, manage=True, **extra):
    p = {"sku": sku, "name": name, "manage_stock": manage, "stock_quantity": qty,
         "stock_status": "instock", "type": "simple"}
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
