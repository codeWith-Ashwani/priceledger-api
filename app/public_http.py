"""Public HTTPS fetching with DNS-pinned connections and normal TLS verification."""

import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit


class PublicFetchError(Exception):
    def __init__(self, message: str, code: str = "invalid_page", status: int = 502):
        super().__init__(message)
        self.code, self.status = code, status


def public_addresses(host: str) -> list[str]:
    addresses = list(
        dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
    )
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (
            not ip.is_global
            or (getattr(ip, "ipv4_mapped", None) and not ip.ipv4_mapped.is_global)
            or (getattr(ip, "sixtofour", None) and not ip.sixtofour.is_global)
        ):
            raise PublicFetchError(
                "Private and reserved network addresses cannot be fetched.",
                "unsafe_destination",
                400,
            )
    if not addresses:
        raise PublicFetchError("The store address could not be resolved.")
    return addresses


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, timeout: float):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def fetch_public(url: str, validate_url) -> tuple[str, str]:
    deadline = time.monotonic() + 35
    for _ in range(5):
        url = validate_url(url)
        parsed = urlsplit(url)
        addresses = public_addresses(parsed.hostname)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PublicFetchError("The store lookup timed out.")
        connection = PinnedHTTPSConnection(parsed.hostname, addresses[0], min(10, remaining))
        try:
            connection.request(
                "GET",
                parsed.path + ("?" + parsed.query if parsed.query else ""),
                headers={
                    "User-Agent": "PriceLedger/0.3 (user-requested product lookup; no login)",
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Encoding": "identity",
                },
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise PublicFetchError("The store returned an invalid redirect.")
                url = validate_url(urljoin(url, location))
                continue
            if response.status in {403, 429, 503}:
                raise PublicFetchError(
                    "The store did not allow this lookup. No price was recorded.",
                    "retailer_blocked",
                )
            if response.status == 404:
                raise PublicFetchError(
                    "The store did not return this product page. Check the link.",
                    "listing_unavailable",
                    404,
                )
            if response.status != 200:
                raise PublicFetchError(
                    f"The store returned HTTP {response.status}. No price was recorded.",
                    "retailer_unavailable",
                )
            if "text/html" not in (response.getheader("Content-Type") or "").lower():
                raise PublicFetchError("This link did not return an HTML product page.")
            if (response.getheader("Content-Encoding") or "identity").lower() != "identity":
                raise PublicFetchError("The store returned an unsupported compressed response.")
            body = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PublicFetchError("The store lookup timed out.")
                if connection.sock:
                    connection.sock.settimeout(min(10, remaining))
                chunk = response.read(65536)
                if not chunk:
                    break
                if len(body) + len(chunk) > 3_000_000:
                    raise PublicFetchError(
                        "The store page exceeds the lookup size limit.", "page_too_large"
                    )
                body.extend(chunk)
            return bytes(body).decode("utf-8", errors="replace"), url
        except http.client.HTTPException as exc:
            raise PublicFetchError("The store returned an invalid HTTP response.") from exc
        finally:
            connection.close()
    raise PublicFetchError("The store redirected too many times.", "redirect_limit")
