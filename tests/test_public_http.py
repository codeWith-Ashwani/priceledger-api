import socket

import pytest

from app.public_http import PinnedHTTPSConnection, PublicFetchError, public_addresses
from app.retail import LookupError, parse_listing, target_for


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fe80::1", "::ffff:127.0.0.1"]
)
def test_generic_store_dns_cannot_reach_private_addresses(monkeypatch, address):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *args, **kwargs: [(0, 0, 0, "", (address, 443))]
    )
    with pytest.raises(PublicFetchError):
        public_addresses("store.example.org")


def test_connection_pins_ip_and_keeps_original_tls_hostname(monkeypatch):
    connections = []
    raw = object()
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda destination, timeout: connections.append(destination) or raw,
    )

    class Context:
        def wrap_socket(self, sock, server_hostname):
            assert sock is raw
            assert server_hostname == "store.example.org"
            return "verified-tls-socket"

    connection = PinnedHTTPSConnection("store.example.org", "93.184.216.34", 10)
    connection._context = Context()
    connection.connect()
    assert connections == [("93.184.216.34", 443)]
    assert connection.sock == "verified-tls-socket"


def test_arbitrary_store_url_with_structured_product_data_is_supported():
    target = target_for("https://store.example.org/products/item?variant=blue")
    assert target.retailer.startswith("site-")
    html = """<script type="application/ld+json">{"@type":"Product","name":"Real source contract","brand":{"name":"Test"},"gtin13":"1234567890128","offers":{"@type":"Offer","priceCurrency":"INR","price":123.45,"availability":"https://schema.org/InStock","itemCondition":"https://schema.org/NewCondition"}}</script>"""
    listing = parse_listing(html, target)
    assert listing.item_paise == 12345
    assert listing.attributes["gtin13"] == "1234567890128"
    assert listing.condition == "new"


def test_generic_url_is_not_treated_as_a_product_without_product_evidence():
    with pytest.raises(LookupError):
        parse_listing(
            "<html><h1>Welcome to our shop</h1></html>", target_for("https://store.example.org/")
        )


def test_generic_product_lookup_records_the_actual_domain_source(client, monkeypatch):
    target = target_for("https://store.example.org/items/123?variant=blue")
    html = '<script type="application/ld+json">{"@type":"Product","name":"Source contract item","brand":{"name":"Test"},"offers":{"@type":"Offer","price":123.45,"priceCurrency":"INR","availability":"https://schema.org/InStock","itemCondition":"https://schema.org/NewCondition"}}</script>'
    listing = parse_listing(html, target)
    monkeypatch.setattr("app.lookup.load_listing", lambda ignored: listing)
    from app.lookup import _requests

    _requests.clear()
    response = client.post("/api/v1/lookup", json={"query": target.url})
    assert response.status_code == 200
    product = client.get(f"/api/v1/products/{response.json()['product_id']}").json()
    assert product["offers"][0]["retailer_name"] == "store.example.org"
    assert product["offers"][0]["item_paise"] == 12345
    assert product["offers"][0]["source_name"] == "store.example.org product page"
