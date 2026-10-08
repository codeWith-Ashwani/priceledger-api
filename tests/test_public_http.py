import socket

import pytest

from app.public_http import PinnedHTTPSConnection, PublicFetchError, public_addresses
from app.retail import LookupError, fetch_page, parse_listing, target_for


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
    response = client.post("/api/v1/lookup", json={"query": target.url})
    assert response.status_code == 200
    product = client.get(f"/api/v1/products/{response.json()['product_id']}").json()
    assert product["offers"][0]["retailer_name"] == "store.example.org"
    assert product["offers"][0]["item_paise"] == 12345
    assert product["offers"][0]["source_name"] == "store.example.org product page"


@pytest.fixture
def pinned_transport(monkeypatch):
    connections = []
    responses = []

    class Response:
        def __init__(self, status=200, headers=None, body=b"<html>real transport contract</html>"):
            self.status = status
            self.headers = headers or {"Content-Type": "text/html"}
            self.body = body

        def getheader(self, name):
            return self.headers.get(name)

        def read(self, maximum):
            chunk, self.body = self.body[:maximum], self.body[maximum:]
            return chunk

    class Connection:
        sock = None

        def __init__(self, host, address, timeout):
            self.host, self.address, self.timeout = host, address, timeout
            self.closed = False
            self.response = responses.pop(0)
            connections.append(self)

        def request(self, method, path, headers):
            self.method, self.path, self.headers = method, path, headers

        def getresponse(self):
            return self.response

        def close(self):
            self.closed = True

    monkeypatch.setattr("app.public_http.public_addresses", lambda host: ["93.184.216.34"])
    monkeypatch.setattr("app.public_http.PinnedHTTPSConnection", Connection)
    return Response, responses, connections


@pytest.mark.parametrize(
    "retailer,url",
    [
        ("amazon", "https://www.amazon.in/dp/B012345678"),
        ("flipkart", "https://www.flipkart.com/test/p/itm123"),
        ("meesho", "https://www.meesho.com/test/p/abc123"),
    ],
)
def test_all_dedicated_stores_use_pinned_public_transport(pinned_transport, retailer, url):
    Response, responses, connections = pinned_transport
    responses.append(Response())
    html, final = fetch_page(url, retailer)
    assert html == "<html>real transport contract</html>" and final == url
    connection = connections[0]
    assert connection.address == "93.184.216.34"
    assert connection.headers["Accept-Encoding"] == "identity"
    assert connection.method == "GET" and connection.closed


@pytest.mark.parametrize(
    "status,code,result_status",
    [
        (403, "retailer_blocked", 502),
        (429, "retailer_blocked", 502),
        (503, "retailer_blocked", 502),
        (404, "listing_unavailable", 404),
        (500, "retailer_unavailable", 502),
    ],
)
def test_pinned_retailer_errors_keep_collection_classification(
    pinned_transport, status, code, result_status
):
    Response, responses, connections = pinned_transport
    responses.append(Response(status=status))
    with pytest.raises(LookupError) as failure:
        fetch_page("https://www.flipkart.com/test/p/itm123", "flipkart")
    assert failure.value.code == code and failure.value.status == result_status
    assert connections[0].closed


def test_retailer_redirect_revalidates_and_pins_each_host(pinned_transport, monkeypatch):
    Response, responses, connections = pinned_transport
    hosts = []
    monkeypatch.setattr(
        "app.public_http.public_addresses", lambda host: hosts.append(host) or ["93.184.216.34"]
    )
    responses.extend(
        [
            Response(status=302, headers={"Location": "https://flipkart.com/test/p/itm123"}),
            Response(),
        ]
    )
    _, url = fetch_page("https://www.flipkart.com/test/p/itm123", "flipkart")
    assert url == "https://flipkart.com/test/p/itm123"
    assert hosts == ["www.flipkart.com", "flipkart.com"]
    assert all(connection.closed for connection in connections)


def test_dedicated_store_private_dns_is_rejected_before_connection(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (0, 0, 0, "", ("93.184.216.34", 443)),
            (0, 0, 0, "", ("127.0.0.1", 443)),
        ],
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Private DNS must never create a connection")

    monkeypatch.setattr("app.public_http.PinnedHTTPSConnection", forbidden)
    with pytest.raises(LookupError) as failure:
        fetch_page("https://www.flipkart.com/test/p/itm123", "flipkart")
    assert failure.value.code == "unsafe_destination" and failure.value.status == 400


@pytest.mark.parametrize(
    "headers,body,code",
    [
        ({"Content-Type": "application/json"}, b"{}", "invalid_page"),
        ({"Content-Type": "text/html", "Content-Encoding": "gzip"}, b"compressed", "invalid_page"),
        ({"Content-Type": "text/html"}, b"x" * 3_000_001, "page_too_large"),
    ],
    ids=["wrong-media-type", "compressed-response", "oversized-response"],
)
def test_pinned_transport_rejects_invalid_or_unbounded_pages(pinned_transport, headers, body, code):
    Response, responses, connections = pinned_transport
    responses.append(Response(headers=headers, body=body))
    with pytest.raises(LookupError) as failure:
        fetch_page("https://www.flipkart.com/test/p/itm123", "flipkart")
    assert failure.value.code == code and connections[0].closed
