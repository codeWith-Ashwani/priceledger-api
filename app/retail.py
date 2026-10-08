"""User-requested public page lookup. No login, CAPTCHA solving or proxy rotation."""

import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

STORES = {
    "amazon": ("Amazon", "#a86416", "https://www.amazon.in/s?"),
    "flipkart": ("Flipkart", "#316dde", "https://www.flipkart.com/search?"),
    "meesho": ("Meesho", "#aa2878", "https://www.meesho.com/search?"),
}
HOSTS = {
    "amazon.in": "amazon",
    "www.amazon.in": "amazon",
    "m.amazon.in": "amazon",
    "amzn.in": "amazon",
    "amzn.to": "amazon",
    "flipkart.com": "flipkart",
    "www.flipkart.com": "flipkart",
    "dl.flipkart.com": "flipkart",
    "fkrt.it": "flipkart",
    "meesho.com": "meesho",
    "www.meesho.com": "meesho",
}
SHORT_HOSTS = {"amzn.in", "amzn.to", "fkrt.it", "dl.flipkart.com"}
IMAGE_HOSTS = {
    "m.media-amazon.com",
    "images-eu.ssl-images-amazon.com",
    "rukmini1.flixcart.com",
    "rukminim1.flixcart.com",
    "rukminim2.flixcart.com",
    "images.meesho.com",
}


class LookupError(Exception):
    def __init__(self, code: str, message: str, status: int = 502):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


@dataclass(frozen=True)
class Target:
    retailer: str
    url: str
    external_id: str | None


@dataclass(frozen=True)
class Listing:
    target: Target
    title: str
    brand: str
    category: str
    attributes: dict
    image_url: str | None
    item_paise: int
    shipping_paise: int | None
    availability: str
    condition: str
    seller: str


def allowed_url(value: str, retailer: str | None = None) -> str:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 80, 443}
            or parsed.hostname not in HOSTS
            or (retailer and HOSTS[parsed.hostname] != retailer)
        ):
            raise ValueError
        return (
            "https://"
            + parsed.hostname
            + (parsed.path or "/")
            + ("?" + parsed.query if parsed.query else "")
        )
    except ValueError as exc:
        raise LookupError(
            "invalid_link", "Use an Amazon India, Flipkart or Meesho product link.", 400
        ) from exc


def target_for(value: str) -> Target:
    url = allowed_url(value)
    parsed = urlsplit(url)
    retailer = HOSTS[parsed.hostname]
    if parsed.hostname in SHORT_HOSTS:
        return Target(retailer, url, None)
    if retailer == "amazon":
        match = re.search(r"/(?:dp|gp/product)/([A-Za-z0-9]{10})(?:/|$)", parsed.path)
        if match:
            asin = match[1].upper()
            return Target(retailer, f"https://www.amazon.in/dp/{asin}", asin)
    elif retailer == "flipkart":
        match = re.search(r"/p/(itm[A-Za-z0-9]+)(?:/|$)", parsed.path)
        if match:
            pid = parse_qs(parsed.query).get("pid", [""])[0]
            if pid and not re.fullmatch(r"[A-Za-z0-9]{5,50}", pid):
                raise LookupError("invalid_link", "The Flipkart product ID is invalid.", 400)
            canonical = "https://www.flipkart.com" + parsed.path.rstrip("/")
            if pid:
                canonical += "?" + urlencode({"pid": pid})
            return Target(retailer, canonical, pid or match[1])
    elif retailer == "meesho":
        match = re.search(r"/p/([a-zA-Z0-9]+)(?:/|$)", parsed.path)
        if match:
            return Target(retailer, "https://www.meesho.com" + parsed.path.rstrip("/"), match[1])
    raise LookupError(
        "invalid_link", "This is not a supported product page. Paste the product's full link.", 400
    )


def check_public_dns(host: str):
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise LookupError(
            "unsafe_destination", "The retailer address could not be safely reached.", 400
        )


def fetch_page(url: str, retailer: str) -> tuple[str, str]:
    """Allowlisted HTTPS only, validated redirects, bounded timeout and decoded body size."""
    headers = {
        "User-Agent": "PriceLedger/0.2 (user-requested product lookup; no login)",
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        with httpx.Client(
            timeout=httpx.Timeout(12, connect=5),
            headers=headers,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            for _ in range(5):
                url = allowed_url(url, retailer)
                check_public_dns(urlsplit(url).hostname)
                with client.stream("GET", url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            break
                        url = allowed_url(urljoin(url, location), retailer)
                        continue
                    if response.status_code in {403, 429, 503}:
                        raise LookupError(
                            "retailer_blocked",
                            f"{STORES[retailer][0]} did not allow this lookup. No price was recorded.",
                        )
                    if response.status_code == 404:
                        raise LookupError(
                            "listing_unavailable",
                            "The retailer did not return this product page. Check the link or try later.",
                            404,
                        )
                    if response.status_code != 200:
                        raise LookupError(
                            "retailer_unavailable",
                            "The retailer is temporarily unavailable. Try again later.",
                        )
                    if "text/html" not in response.headers.get("content-type", "").lower():
                        raise LookupError(
                            "invalid_page", "The retailer returned an unsupported page."
                        )
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 3_000_000:
                            raise LookupError(
                                "page_too_large", "The retailer page exceeds the lookup size limit."
                            )
                    return bytes(body).decode(response.encoding or "utf-8", errors="replace"), url
    except (httpx.HTTPError, OSError) as exc:
        raise LookupError(
            "connection_failed", "The retailer could not be reached. Try again later."
        ) from exc
    raise LookupError("redirect_limit", "The retailer redirected too many times.")


def paise(value) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        amount = Decimal(str(value).replace("₹", "").replace(",", "").strip())
        converted = amount * 100
        if (
            not converted.is_finite()
            or converted != converted.to_integral_value()
            or not 0 <= converted <= 2_000_000_000
        ):
            return None
        return int(converted)
    except (InvalidOperation, ValueError):
        return None


def schema_objects(value):
    if isinstance(value, list):
        for item in value:
            yield from schema_objects(item)
    elif isinstance(value, dict):
        yield value
        for item in value.values():
            if isinstance(item, (dict, list)):
                yield from schema_objects(item)


def safe_image(value) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("url")
    if isinstance(value, str) and len(value) <= 2048:
        parsed = urlsplit(value)
        if (
            parsed.scheme == "https"
            and parsed.hostname in IMAGE_HOSTS
            and not parsed.username
            and not parsed.password
        ):
            return value
    return None


def parse_listing(html: str, target: Target) -> Listing:
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one('form[action*="validateCaptcha"], #captchacharacters'):
        raise LookupError(
            "retailer_blocked", "The retailer requested a CAPTCHA. No price was recorded."
        )
    products = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            products.extend(
                item
                for item in schema_objects(json.loads(script.get_text()))
                if item.get("@type") == "Product"
                or (isinstance(item.get("@type"), list) and "Product" in item["@type"])
            )
        except (ValueError, RecursionError):
            continue
    if (
        target.retailer == "flipkart"
        and target.external_id
        and not target.external_id.startswith("itm")
    ):
        products = [item for item in products if str(item.get("sku", "")) == target.external_id]
    if len(products) == 1:
        item = products[0]
        offers = item.get("offers", {})
        if isinstance(offers, list):
            offers = offers[0] if len(offers) == 1 else {}
        if isinstance(offers, dict) and offers.get("@type") != "AggregateOffer":
            price = paise(offers.get("price"))
            if price is not None and offers.get("priceCurrency") == "INR":
                attributes = {
                    key: str(item[key])[:200]
                    for key in ("model", "color", "size", "mpn", "sku", "gtin13")
                    if isinstance(item.get(key), (str, int))
                }
                title = str(item.get("name", "")).strip()
                if not title:
                    raise LookupError("missing_product", "The page did not identify a product.")
                if target.retailer == "flipkart" and target.external_id.startswith("itm"):
                    sku = str(item.get("sku", ""))
                    if not re.fullmatch(r"[A-Za-z0-9]{5,50}", sku):
                        raise LookupError(
                            "missing_variant", "The retailer did not identify this product variant."
                        )
                    target = Target("flipkart", target.url + "?" + urlencode({"pid": sku}), sku)
                # Preserve the retailer's complete variant title; never infer cross-store identity.
                attributes["listing_title"] = title[:250]
                brand = item.get("brand", "")
                if isinstance(brand, dict):
                    brand = brand.get("name", "")
                availability = str(offers.get("availability", "")).split("/")[-1]
                availability = {
                    "InStock": "in_stock",
                    "OutOfStock": "out_of_stock",
                    "SoldOut": "out_of_stock",
                }.get(availability, "unknown")
                shipping = offers.get("shippingDetails", {})
                if isinstance(shipping, list):
                    shipping = shipping[0] if len(shipping) == 1 else {}
                rate = shipping.get("shippingRate", {}) if isinstance(shipping, dict) else {}
                destination = (
                    shipping.get("shippingDestination", {}) if isinstance(shipping, dict) else {}
                )
                shipping_paise = (
                    paise(rate.get("value"))
                    if isinstance(rate, dict)
                    and rate.get("currency") == "INR"
                    and isinstance(destination, dict)
                    and destination.get("addressCountry") == "IN"
                    and not destination.get("addressRegion")
                    and not destination.get("postalCode")
                    else None
                )
                seller = offers.get("seller", {})
                seller = (
                    seller.get("name", "Seller not stated")
                    if isinstance(seller, dict)
                    else "Seller not stated"
                )
                condition = str(offers.get("itemCondition", "")).split("/")[-1]
                condition = {
                    "NewCondition": "new",
                    "UsedCondition": "used",
                    "RefurbishedCondition": "refurbished",
                }.get(condition, "unknown")
                return Listing(
                    target,
                    title[:250],
                    str(brand)[:100] or "Brand not stated",
                    str(item.get("category", "uncategorized"))[:80],
                    attributes,
                    safe_image(item.get("image")),
                    price,
                    shipping_paise,
                    availability,
                    condition,
                    str(seller)[:150],
                )
    if target.retailer == "amazon":
        title = soup.select_one("#productTitle")
        asin = soup.select_one("input#ASIN")
        price = soup.select_one(
            "#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price) .a-offscreen, #corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen"
        )
        amount = paise(price.get_text()) if price else None
        if amount is None:
            # The current main-price widget may leave its accessible text empty.
            # Read only the visible price-to-pay, never the MRP or recommendations.
            widget = soup.select_one("#corePriceDisplay_desktop_feature_div .priceToPay")
            symbol = widget.select_one(".a-price-symbol") if widget else None
            whole = widget.select_one(".a-price-whole") if widget else None
            fraction = widget.select_one(".a-price-fraction") if widget else None
            if symbol and symbol.get_text(strip=True) == "₹" and whole:
                visible = whole.get_text(strip=True).rstrip(".")
                if fraction:
                    visible += "." + fraction.get_text(strip=True)
                amount = paise(visible)
        if title and asin and asin.get("value") == target.external_id and amount is not None:
            text = title.get_text(" ", strip=True)[:250]
            availability = soup.select_one("#availability")
            state = (
                "in_stock"
                if availability and re.search(r"\bin stock\b", availability.get_text(), re.I)
                else "unknown"
            )
            if availability and "currently unavailable" in availability.get_text().lower():
                state = "out_of_stock"
            byline = soup.select_one("#bylineInfo")
            brand_match = (
                re.fullmatch(
                    r"Visit the (.+) Store|Brand:\s*(.+)", byline.get_text(" ", strip=True)
                )
                if byline
                else None
            )
            brand = (
                next((value for value in brand_match.groups() if value), "Brand not stated")
                if brand_match
                else "Brand not stated"
            )
            image = soup.select_one("#landingImage")
            seller = soup.select_one("#sellerProfileTriggerId")
            return Listing(
                target,
                text,
                brand[:100],
                "uncategorized",
                {"listing_title": text, "asin": target.external_id},
                safe_image(image.get("data-old-hires") or image.get("src")) if image else None,
                amount,
                None,
                state,
                "unknown",
                seller.get_text(" ", strip=True)[:150] if seller else "Seller not stated",
            )
    raise LookupError(
        "price_unavailable",
        "The retailer page did not expose a verified INR product price. No price was recorded.",
    )


def load_listing(target: Target) -> Listing:
    html, final_url = fetch_page(target.url, target.retailer)
    resolved = target_for(final_url)
    if target.external_id and resolved.external_id != target.external_id:
        raise LookupError(
            "changed_product",
            "The retailer redirected to a different product. Paste the new product link.",
        )
    return parse_listing(html, resolved)


def search_store(retailer: str, query: str) -> dict:
    search_url = STORES[retailer][2] + urlencode({"k" if retailer == "amazon" else "q": query})
    try:
        html, _ = fetch_page(search_url, retailer)
        soup = BeautifulSoup(html, "html.parser")

        def tokens(value: str):
            normalized = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", value.lower())
            return set(re.findall(r"[a-z0-9]+", normalized))

        terms = tokens(query)
        items = {}
        for anchor in soup.select("a[href]"):
            try:
                target = target_for(urljoin(search_url, anchor["href"]))
            except LookupError:
                continue
            if target.retailer != retailer or not target.external_id:
                continue
            image = anchor.find("img")
            title = (
                anchor.get("title") or anchor.get("aria-label") or anchor.get_text(" ", strip=True)
            )
            if not title or len(title) < 8:
                title = image.get("alt", "") if image else ""
            if image and len(image.get("alt", "")) >= 8 and terms.issubset(tokens(image["alt"])):
                title = image["alt"]
            if not terms.issubset(tokens(title)):
                continue
            key = (target.external_id, target.url)
            if key not in items:
                items[key] = {
                    "title": title[:250],
                    "url": target.url,
                    "retailer": retailer,
                    "retailer_name": STORES[retailer][0],
                }
            if len(items) >= 6:
                break
        return {
            "retailer": retailer,
            "retailer_name": STORES[retailer][0],
            "status": "ok" if items else "no_matches",
            "message": "Choose the exact model, colour, size and pack. Prices are checked after selection."
            if items
            else "No readable matches. Paste a product link instead.",
            "search_url": search_url,
            "items": list(items.values()),
        }
    except LookupError as exc:
        return {
            "retailer": retailer,
            "retailer_name": STORES[retailer][0],
            "status": exc.code,
            "message": exc.message,
            "search_url": search_url,
            "items": [],
        }
