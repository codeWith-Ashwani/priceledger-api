"""User-requested public page lookup. No login, CAPTCHA solving or proxy rotation."""

import ipaddress
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from app.config import get_settings
from app.identity import mobile_attributes, model_attributes
from app.public_http import PublicFetchError, fetch_public

STORES = {
    "amazon": ("Amazon", "#a86416", "https://www.amazon.in/s?"),
    "flipkart": ("Flipkart", "#316dde", "https://www.flipkart.com/search?"),
    "meesho": ("Meesho", "#aa2878", "https://www.meesho.com/search?"),
    "croma": ("Croma", "#08776a", "https://www.croma.com/searchB?"),
    "reliance": ("Reliance Digital", "#cf3038", "https://www.reliancedigital.in/search?"),
    "iqoo": ("iQOO Official Store", "#986b08", "https://shop.iqoo.com/in/products/phone"),
    "sony": ("Sony Center", "#222222", "https://shopatsc.com/search?"),
    "boat": ("boAt Official Store", "#c52e37", "https://www.boat-lifestyle.com/search?"),
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
    "croma.com": "croma",
    "www.croma.com": "croma",
    "reliancedigital.in": "reliance",
    "www.reliancedigital.in": "reliance",
    "shop.iqoo.com": "iqoo",
    "shopatsc.com": "sony",
    "www.shopatsc.com": "sony",
    "boat-lifestyle.com": "boat",
    "www.boat-lifestyle.com": "boat",
}
SHORT_HOSTS = {"amzn.in", "amzn.to", "fkrt.it", "dl.flipkart.com"}
IMAGE_HOSTS = {
    "m.media-amazon.com",
    "images-eu.ssl-images-amazon.com",
    "rukmini1.flixcart.com",
    "rukminim1.flixcart.com",
    "rukminim2.flixcart.com",
    "images.meesho.com",
    "media-ik.croma.com",
    "media.croma.com",
    "www.reliancedigital.in",
    "assets.reliancedigital.in",
    "exstatic-in.iqoo.com",
    "contents.mediadecathlon.com",
    "cdn.jiostore.online",
    "cdn.shopify.com",
    "www.boat-lifestyle.com",
    "images.samsung.com",
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
    item_paise: int | None
    shipping_paise: int | None
    availability: str
    condition: str
    seller: str
    rating: dict | None = None


def discovery_retailers(category: str | None = None, brand: str | None = None) -> list[str]:
    configured = list(dict.fromkeys(get_settings().discovery_retailers))
    # Electronics stores are irrelevant to known fashion/grocery/beauty categories.
    non_electronics = category and re.search(
        r"fashion|clothing|grocery|beauty|personal-care|food", category, re.I
    )
    return [
        store
        for store in configured
        if (not non_electronics or store not in {"croma", "reliance", "iqoo", "sony", "boat"})
        and (store != "iqoo" or brand is None or brand.lower() == "iqoo")
        and (store != "sony" or brand is None or brand.lower() == "sony")
        and (store != "boat" or brand is None or brand.lower() == "boat")
    ]


def retailer_key(host: str) -> str:
    return HOSTS.get(host, "site-" + sha256(host.removeprefix("www.").encode()).hexdigest()[:12])


def store_info(target: Target) -> tuple[str, str]:
    if target.retailer in STORES:
        return STORES[target.retailer][:2]
    return urlsplit(target.url).hostname.removeprefix("www."), "#155c45"


def allowed_url(value: str, retailer: str | None = None) -> str:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 80, 443}
            or not parsed.hostname
            or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", parsed.hostname)
            or parsed.hostname.endswith(
                (".test", ".local", ".localhost", ".invalid", ".example", ".internal", ".onion")
            )
            or (retailer and retailer_key(parsed.hostname) != retailer)
        ):
            raise ValueError
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            pass
        else:
            raise ValueError
        return (
            "https://"
            + parsed.hostname
            + (parsed.path or "/")
            + ("?" + parsed.query if parsed.query else "")
        )
    except ValueError as exc:
        raise LookupError(
            "invalid_link",
            "Use a public ecommerce product link with a valid domain and standard HTTPS port.",
            400,
        ) from exc


def target_for(value: str) -> Target:
    url = allowed_url(value)
    parsed = urlsplit(url)
    retailer = retailer_key(parsed.hostname)
    if retailer.startswith("site-"):
        return Target(retailer, url, "url-" + sha256(url.encode()).hexdigest()[:24])
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
    elif retailer == "croma":
        match = re.search(r"/p/(\d{4,20})(?:/|$)", parsed.path)
        if match:
            return Target(retailer, "https://www.croma.com" + parsed.path.rstrip("/"), match[1])
    elif retailer == "iqoo":
        match = re.fullmatch(r"/in/product/(\d{1,12})/?", parsed.path)
        sku = parse_qs(parsed.query).get("skuId", [None])[0]
        if match and (sku is None or re.fullmatch(r"\d{1,12}", sku)):
            canonical = "https://shop.iqoo.com/in/product/" + match[1]
            return Target(
                retailer,
                canonical + ("?" + urlencode({"skuId": sku}) if sku else ""),
                match[1] + "-" + sku if sku else "spu-" + match[1],
            )
    elif retailer == "reliance":
        match = re.search(r"/product/([a-zA-Z0-9-]+)(?:/|$)", parsed.path)
        legacy = re.search(r"/p/(\d{4,20})(?:/|$)", parsed.path)
        if match or legacy:
            identity = (match or legacy)[1]
            return Target(
                retailer,
                "https://www.reliancedigital.in" + parsed.path.rstrip("/"),
                identity[-150:],
            )
    elif retailer in {"sony", "boat"}:
        match = re.search(r"/products/([a-zA-Z0-9-]+)/?$", parsed.path)
        variant = parse_qs(parsed.query).get("variant", [None])[0]
        if match and (variant is None or re.fullmatch(r"\d{1,20}", variant)):
            host = "shopatsc.com" if retailer == "sony" else "www.boat-lifestyle.com"
            canonical = f"https://{host}/products/{match[1]}"
            return Target(
                retailer,
                canonical + ("?" + urlencode({"variant": variant}) if variant else ""),
                (match[1][:100] + "-" + variant) if variant else match[1][:100],
            )
    raise LookupError(
        "invalid_link", "This is not a supported product page. Paste the product's full link.", 400
    )


def fetch_page(url: str, retailer: str) -> tuple[str, str]:
    """All stores use DNS-pinned verified TLS, retailer-bound redirects and bounded bodies."""
    try:
        return fetch_public(url, lambda candidate: allowed_url(candidate, retailer))
    except PublicFetchError as exc:
        raise LookupError(exc.code, str(exc), exc.status) from exc
    except OSError as exc:
        raise LookupError(
            "connection_failed", "The retailer could not be reached. Try again later."
        ) from exc


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


def consolidate_products(products: list[dict]) -> list[dict]:
    """Combine complementary schema blocks only with a shared identity and price."""
    result: list[dict] = []
    for product in products:
        for index, existing in enumerate(result):
            identifier = product.get("sku") or product.get("@id")
            if not identifier or identifier != (existing.get("sku") or existing.get("@id")):
                continue
            keys = ("name", "brand", "model", "color", "size", "mpn", "gtin13", "gtin14")
            if any(
                key in product and key in existing and product[key] != existing[key] for key in keys
            ):
                continue
            first, second = existing.get("offers", {}), product.get("offers", {})
            if not isinstance(first, dict) or not isinstance(second, dict):
                continue
            if any(
                key in first and key in second and first[key] != second[key]
                for key in ("price", "priceCurrency", "availability", "itemCondition", "seller")
            ):
                continue
            result[index] = {**existing, **product, "offers": {**first, **second}}
            break
        else:
            result.append(product)
    return result


def safe_image(value, product_host: str | None = None) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("url")
    if isinstance(value, str) and len(value) <= 2048:
        parsed = urlsplit(value)
        if (
            parsed.scheme == "https"
            and (parsed.hostname in IMAGE_HOSTS or parsed.hostname == product_host)
            and not parsed.username
            and not parsed.password
        ):
            return value
    return None


def product_rating(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    try:
        if any(
            isinstance(value.get(key), bool) for key in ("ratingValue", "bestRating", "worstRating")
        ):
            return None
        rating = Decimal(str(value.get("ratingValue")))
        best = Decimal(str(value.get("bestRating", 5)))
        worst = Decimal(str(value.get("worstRating", 1)))
        if (
            not all(number.is_finite() for number in (rating, best, worst))
            or not 0 <= worst < best <= 100
            or not worst <= rating <= best
        ):
            return None
        count = value.get("ratingCount", value.get("reviewCount"))
        count_text = str(count).replace(",", "")
        count = (
            int(count_text)
            if count_text.isascii() and count_text.isdigit() and len(count_text) <= 9
            else None
        )
        return {"value": float(rating), "best": float(best), "count": count}
    except (ValueError, InvalidOperation):
        return None


def amazon_condition(soup: BeautifulSoup, asin: str) -> str:
    for node in soup.select('script[type="a-state"][data-a-state]'):
        try:
            if json.loads(node["data-a-state"]).get("key") != "vas-base-vm":
                continue
            data = json.loads(node.get_text())
            if data.get("productAsin") != asin:
                continue
            return {("NEW",): "new", ("USED",): "used"}.get(
                tuple(data.get("buyingOptionTypes", [])), "unknown"
            )
        except (ValueError, TypeError, AttributeError):
            continue
    return "unknown"


def selected_offer(item: dict, soup: BeautifulSoup, target: Target) -> tuple[dict, dict, Target]:
    offers = item.get("offers", {})
    if not isinstance(offers, list):
        return item, offers if isinstance(offers, dict) else {}, target
    if len(offers) == 1:
        return item, offers[0] if isinstance(offers[0], dict) else {}, target
    requested = parse_qs(urlsplit(target.url).query).get("variant", [None])[0]
    options = soup.select('form[action*="cart/add"] select[name="id"] option[selected]')
    ids = {node.get("value") for node in options}
    ids.update(
        node.get("value") for node in soup.select('form[action*="cart/add"] input[name="id"]')
    )
    selected = requested or (next(iter(ids)) if len(ids) == 1 else None)
    if not selected:
        return item, {}, target
    candidates = [
        offer
        for offer in offers
        if isinstance(offer, dict)
        and isinstance(offer.get("url"), str)
        and parse_qs(urlsplit(offer["url"]).query).get("variant", [None])[0] == selected
    ]
    if len(candidates) != 1:
        raise LookupError(
            "missing_variant", "The store did not identify the selected variant price."
        )
    offer = dict(candidates[0])
    resolved = target_for(offer["url"])
    if (
        resolved.retailer != target.retailer
        or urlsplit(resolved.url).path.split("/products/")[-1]
        != urlsplit(target.url).path.split("/products/")[-1]
    ):
        raise LookupError("missing_variant", "The offer belongs to a different product.")
    option = next((node for node in options if node.get("value") == selected), None)
    name = offer.get("name") or (option.get_text(" ", strip=True) if option else "")
    if option and re.search(r"sold out|out of stock", option.get_text(), re.I):
        offer["availability"] = "https://schema.org/OutOfStock"
    # A product-level image can depict a different colour. Only retain an image
    # tied to the selected variant in literal Shopify product JSON.
    item = {**item, "sku": offer.get("sku", item.get("sku", "")), "image": None}
    handle = urlsplit(resolved.url).path.split("/products/")[-1]
    for script in soup.find_all("script"):
        text = script.get_text()
        try:
            if script.get("type") == "application/json" and str(script.get("id", "")).startswith(
                "ProductJson"
            ):
                data = json.loads(text)
            else:
                assignment = re.search(r"\bvar\s+json_product\s*=\s*(\{)", text)
                if not assignment:
                    continue
                data, _ = json.JSONDecoder().raw_decode(text, assignment.start(1))
        except (ValueError, RecursionError):
            continue
        if not isinstance(data, dict) or data.get("handle") != handle:
            continue
        variants = data.get("variants", [])
        if not isinstance(variants, list):
            continue
        matches = [v for v in variants if isinstance(v, dict) and str(v.get("id")) == selected]
        if len(matches) != 1 or matches[0].get("sku") != item["sku"]:
            continue
        image = matches[0].get("featured_image")
        if isinstance(image, dict) and isinstance(image.get("src"), str):
            item["image"] = urljoin(resolved.url, image["src"])
        break
    if name:
        name = re.split(r"\s+-\s+(?:Sold out|Rs\.)", str(name), flags=re.I)[0].strip()
        item = {**item, "color": name, "name": item["name"] + " (" + name + ")"}
    return item, offer, resolved


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
                if item.get("name")
                and (
                    item.get("@type") == "Product"
                    or (isinstance(item.get("@type"), list) and "Product" in item["@type"])
                )
            )
        except (ValueError, RecursionError):
            continue
    if (
        target.retailer == "flipkart"
        and target.external_id
        and not target.external_id.startswith("itm")
    ):
        products = [item for item in products if str(item.get("sku", "")) == target.external_id]
    products = consolidate_products(products)
    if len(products) == 1:
        item = products[0]
        if target.retailer == "iqoo":
            sku = str(item.get("sku", ""))
            requested = parse_qs(urlsplit(target.url).query).get("skuId", [None])[0]
            if not re.fullmatch(r"\d{1,12}", sku) or (requested and requested != sku):
                raise LookupError(
                    "missing_variant", "iQOO did not serve the requested product SKU."
                )
            target = target_for(target.url.split("?", 1)[0] + "?" + urlencode({"skuId": sku}))
        item, offers, target = selected_offer(item, soup, target)
        if isinstance(offers, dict) and offers.get("@type") != "AggregateOffer":
            price = paise(offers.get("price"))
            if price is not None and offers.get("priceCurrency") == "INR":
                attributes = {
                    key: str(item[key])[:200]
                    for key in (
                        "model",
                        "color",
                        "size",
                        "mpn",
                        "sku",
                        "gtin",
                        "gtin8",
                        "gtin12",
                        "gtin13",
                        "gtin14",
                    )
                    if isinstance(item.get(key), (str, int))
                }
                if isinstance(item.get("size"), list) and len(item["size"]) == 1:
                    attributes["size"] = str(item["size"][0])[:200]
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
                if target.retailer == "boat" and str(brand).lower() == "boatlifestylein":
                    brand = "boAt"
                identity_title = title
                heading = soup.select_one("h1") if target.retailer == "flipkart" else None
                if heading and title in heading.get_text(" ", strip=True):
                    identity_title = heading.get_text(" ", strip=True)
                if target.retailer == "iqoo":
                    heading = soup.select_one("h1.title")
                    color = soup.select_one('[role="radio"][aria-checked="true"] .color-name')
                    if heading and title in heading.get_text(" ", strip=True):
                        identity_title = heading.get_text(" ", strip=True)
                        title = str(brand) + " " + identity_title
                    if color:
                        attributes["color"] = color.get_text(" ", strip=True)
                attributes = model_attributes(identity_title, str(brand), attributes)
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
                    safe_image(item.get("image"), urlsplit(target.url).hostname),
                    price,
                    shipping_paise,
                    availability,
                    condition,
                    str(seller)[:150],
                    product_rating(item.get("aggregateRating")),
                )
        # A named product without offers can still start discovery. Record an
        # unpriced check, never a zero-price offer or a made-up historical price.
        if not offers:
            brand = item.get("brand", "")
            if isinstance(brand, dict):
                brand = brand.get("name", "")
            title = str(item.get("name", "")).strip()
            if urlsplit(target.url).hostname in {"samsung.com", "www.samsung.com"}:
                meta = soup.select_one('meta[name="title"]')
                if meta and "Samsung India" in meta.get("content", ""):
                    brand = "Samsung"
                    title = "Samsung " + re.sub(
                        r"^Buy\s+", "", meta["content"].split(" - Price", 1)[0]
                    )
            if isinstance(brand, str) and brand.strip() and title:
                attributes = model_attributes(
                    title,
                    brand,
                    {
                        **{
                            key: str(item[key])[:200]
                            for key in ("sku", "model", "mpn", "color", "size")
                            if isinstance(item.get(key), (str, int))
                        },
                        "listing_title": title[:250],
                    },
                )
                return Listing(
                    target,
                    title[:250],
                    brand[:100],
                    str(item.get("category", "uncategorized"))[:80],
                    attributes,
                    safe_image(item.get("image"), urlsplit(target.url).hostname),
                    None,
                    None,
                    "unknown",
                    "unknown",
                    "Seller not stated",
                    product_rating(item.get("aggregateRating")),
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
        if title and asin and asin.get("value") == target.external_id:
            text = title.get_text(" ", strip=True)[:250]
            availability = soup.select_one("#availability")
            state = (
                "in_stock"
                if availability and re.search(r"\bin stock\b", availability.get_text(), re.I)
                else "unknown"
            )
            if availability and "currently unavailable" in availability.get_text().lower():
                state = "out_of_stock"
            if amount is None and state == "in_stock":
                state = "unknown"
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
            rating_node = soup.select_one("#acrPopover .a-icon-alt")
            rating_match = (
                re.fullmatch(r"([0-9.]+) out of ([0-9.]+) stars", rating_node.get_text(strip=True))
                if rating_node
                else None
            )
            count_node = soup.select_one("#acrCustomerReviewText")
            count_match = (
                re.fullmatch(r"([0-9,]+) ratings?", count_node.get_text(strip=True))
                if count_node
                else None
            )
            rating = (
                product_rating(
                    {
                        "ratingValue": rating_match[1],
                        "bestRating": rating_match[2],
                        "ratingCount": count_match[1] if count_match else None,
                    }
                )
                if rating_match
                else None
            )
            return Listing(
                target,
                text,
                brand[:100],
                "uncategorized",
                model_attributes(text, brand, {"listing_title": text, "asin": target.external_id}),
                safe_image(image.get("data-old-hires") or image.get("src")) if image else None,
                amount,
                None,
                state,
                amazon_condition(soup, target.external_id),
                seller.get_text(" ", strip=True)[:150] if seller else "Seller not stated",
                rating,
            )
    raise LookupError(
        "price_unavailable",
        "The retailer page did not expose a verified INR product price. No price was recorded.",
    )


def load_listing(target: Target) -> Listing:
    html, final_url = fetch_page(target.url, target.retailer)
    resolved = target_for(final_url)
    if (
        target.external_id
        and not target.retailer.startswith("site-")
        and resolved.external_id != target.external_id
    ):
        raise LookupError(
            "changed_product",
            "The retailer redirected to a different product. Paste the new product link.",
        )
    return parse_listing(html, resolved)


def search_store(retailer: str, query: str) -> dict:
    if retailer == "iqoo":
        from app.iqoo import search_candidates

        search_url = STORES[retailer][2]
        try:
            items = search_candidates(query, lambda url: fetch_page(url, retailer)[0])
            return {
                "retailer": retailer,
                "retailer_name": STORES[retailer][0],
                "status": "ok" if items else "no_matches",
                "message": "Official catalog checked; exact SKU prices require verification.",
                "search_url": search_url,
                "items": items,
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
    search_url = STORES[retailer][2] + urlencode({"k" if retailer == "amazon" else "q": query})
    try:
        html, _ = fetch_page(search_url, retailer)
        soup = BeautifulSoup(html, "html.parser")

        def tokens(value: str):
            normalized = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", value.lower())
            return set(re.findall(r"[a-z0-9]+", normalized))

        terms = tokens(query)
        items = {}
        if retailer in {"sony", "boat"}:
            # Shopify can render search cards through JavaScript. Read only its
            # literal JSON catalog metadata, never execute scripts or trust prices.
            for script in soup.select("script"):
                content = script.get_text()
                match = re.search(r"\bvar meta\s*=\s*", content[:1_000_000])
                if not match:
                    continue
                try:
                    metadata, _ = json.JSONDecoder().raw_decode(content[match.end() :])
                except (ValueError, RecursionError):
                    continue
                if not isinstance(metadata, dict) or not isinstance(metadata.get("products"), list):
                    continue
                for product in metadata["products"][:50]:
                    if not isinstance(product, dict):
                        continue
                    handle = product.get("handle", "")
                    variants = product.get("variants", [])
                    if not re.fullmatch(r"[a-zA-Z0-9-]{1,100}", str(handle)) or not isinstance(
                        variants, list
                    ):
                        continue
                    for variant in variants[:2]:
                        if not isinstance(variant, dict):
                            continue
                        title, variant_id = variant.get("name", ""), str(variant.get("id", ""))
                        if (
                            not isinstance(title, str)
                            or not terms.issubset(tokens(title))
                            or not variant_id.isdigit()
                        ):
                            continue
                        target = target_for(
                            urljoin(search_url, "/products/" + handle)
                            + "?"
                            + urlencode({"variant": variant_id})
                        )
                        items[(target.external_id, target.url)] = {
                            "title": title[:250],
                            "url": target.url,
                            "variant_attributes": model_attributes(
                                title, str(product.get("vendor", "")), {}
                            ),
                            "retailer": retailer,
                            "retailer_name": STORES[retailer][0],
                        }
                    if len(items) >= 6:
                        break
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
                    "variant_attributes": mobile_attributes(
                        anchor.get_text(" ", strip=True)[:1500], "", {}
                    ),
                    "url": target.url,
                    "retailer": retailer,
                    "retailer_name": STORES[retailer][0],
                }
            if len(items) >= 12:
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
