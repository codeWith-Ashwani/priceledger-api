"""Fire-Boltt's public theme contract; no script execution or extra requests."""

import json
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from app.identity import fireboltt_model, normalized
from app.retail import (
    Listing,
    LookupError,
    paise,
    product_rating,
    safe_image,
    schema_objects,
    shopify_product,
    target_for,
)


def mask_js_literals(content: str) -> str:
    """Keep offsets while excluding assignments inside comments and string literals."""
    return re.sub(
        r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|/\*[\s\S]*?\*/|//[^\n]*""",
        lambda match: re.sub(r"[^\n]", " ", match[0]),
        content,
    )


def theme_objects(soup):
    """Recover only the theme's known extra itemReviewed property, never arbitrary JSON."""
    for script in soup.select('script[type="application/ld+json"]'):
        content = script.get_text().strip()
        try:
            value = json.loads(content)
        except (ValueError, RecursionError):
            try:
                value, end = json.JSONDecoder().raw_decode(content)
                tail = content[end:].strip()
                if not tail.startswith(","):
                    continue
                extra = json.loads("{" + tail[1:] + "}")
                if not isinstance(extra, dict) or set(extra) != {"itemReviewed"}:
                    continue
            except (ValueError, RecursionError):
                continue
        yield from schema_objects(value)


def parse(soup, target) -> Listing:
    def reject(code="price_unavailable"):
        raise LookupError(
            code,
            "The official store did not expose agreeing product, variant and INR price evidence. "
            "No price was recorded.",
        )

    def same_page(value):
        if not isinstance(value, str):
            return False
        try:
            other = target_for(urljoin(target.url, value))
            return other.retailer == target.retailer and urlsplit(other.url).path.rstrip(
                "/"
            ) == urlsplit(target.url).path.rstrip("/")
        except (LookupError, ValueError):
            return False

    def official_organization(value):
        if value.get("@type") != "Organization" or normalized(value.get("name")) != "fireboltt":
            return False
        try:
            origin = target_for(str(value.get("url", "")))
            return origin.retailer == target.retailer
        except LookupError:
            return False

    metadata = shopify_product(soup, target)
    canonical = soup.select_one('link[rel="canonical"]')
    headings = {node.get_text(" ", strip=True) for node in soup.select("h1")}
    objects = list(theme_objects(soup))
    if (
        not metadata
        or not canonical
        or not same_page(canonical.get("href"))
        or normalized(metadata.get("vendor")) not in {"bolttgames", "fireboltt"}
        or not any(official_organization(value) for value in objects)
    ):
        reject()
    # Ignore commented assignments and unrelated recommendation data. The full
    # native product must agree with analytics and the visible page identity.
    natives = []
    for script in soup.select("script"):
        content = script.get_text()
        if "snapProdDetail" not in content:
            continue
        for match in re.finditer(
            r"(?m)^\s*var snapProdDetail\s*=\s*(\{)", mask_js_literals(content)
        ):
            try:
                data, _ = json.JSONDecoder().raw_decode(content, match.start(1))
            except (ValueError, RecursionError):
                continue
            if isinstance(data, dict):
                natives.append(data)
    if len(natives) != 1:
        reject()
    native = natives[0]
    if (
        any(native.get(key) != metadata.get(key) for key in ("id", "handle", "vendor"))
        or not isinstance(native.get("id"), int)
        or isinstance(native["id"], bool)
        or not isinstance(native.get("title"), str)
        or native["title"] not in headings
        or not isinstance(native.get("variants"), list)
        or len(native["title"]) > 240
    ):
        reject()
    requested = parse_qs(urlsplit(target.url).query).get("variant", [])
    if len(requested) > 1 or (requested and not requested[0].isdigit()):
        reject("missing_variant")
    selected_ids = {
        node.get("value")
        for node in soup.select(
            'form[action*="cart/add"] input[name="id"], '
            'form[action*="cart/add"] select[name="id"] option[selected]'
        )
        if node.get("value")
    }
    variants = native["variants"][:500]
    selected = (
        requested[0] if requested else next(iter(selected_ids)) if len(selected_ids) == 1 else None
    )
    if selected is None and len(variants) == 1 and isinstance(variants[0], dict):
        selected = str(variants[0].get("id"))
    matches = [v for v in variants if isinstance(v, dict) and str(v.get("id")) == selected]
    analytics = [
        v
        for v in metadata["variants"][:500]
        if isinstance(v, dict) and str(v.get("id")) == selected
    ]
    products = [v for v in objects if v.get("@type") == "Product" and same_page(v.get("url"))]
    if len(matches) != 1 or len(analytics) != 1 or len(products) != 1:
        reject("missing_variant")
    variant, analytic, product = matches[0], analytics[0], products[0]
    price = variant.get("price")
    offers = product.get("offers")
    offers = offers if isinstance(offers, list) else [offers]
    offers = [
        offer
        for offer in offers
        if isinstance(offer, dict)
        and same_page(offer.get("url"))
        and parse_qs(urlsplit(offer["url"]).query).get("variant") == [selected]
    ]
    if len(offers) != 1:
        reject("missing_variant")
    offer = offers[0]
    available = variant.get("available")
    if (
        product.get("name") != native["title"]
        or not isinstance(price, int)
        or isinstance(price, bool)
        or not 0 <= price <= 2_000_000_000
        or price != analytic.get("price")
        or type(analytic.get("price")) is not int
        or type(variant.get("id")) is not int
        or variant["id"] <= 0
        or offer.get("priceCurrency") != "INR"
        or paise(offer.get("price")) != price
        or not isinstance(variant.get("sku"), str)
        or not variant["sku"]
        or len(variant["sku"]) > 200
        or any(value.get("sku") != variant["sku"] for value in (analytic, offer))
        or type(available) is not bool
        or str(offer.get("availability", "")).rsplit("/", 1)[-1]
        != ("InStock" if available else "OutOfStock")
    ):
        reject("ambiguous_price")
    brand = product.get("brand", {})
    brand = brand.get("name") if isinstance(brand, dict) else brand
    if normalized(brand) not in {"bolttgames", "fireboltt"}:
        reject()
    attributes = {"sku": variant["sku"], "listing_title": native["title"]}
    model = fireboltt_model(native["title"])
    if model and native.get("type") == "Smart Watch":
        attributes["model"] = model
    options = native.get("options", [])
    if isinstance(options, list):
        for index, name in enumerate(options[:3], start=1):
            value = variant.get(f"option{index}")
            if (
                str(name).casefold() in {"color", "colour"}
                and isinstance(value, str)
                and len(value) <= 200
            ):
                attributes["color"] = value
            elif name == "Strap Style" and isinstance(value, str) and len(value) <= 200:
                attributes["strap_style"] = value
    image = variant.get("featured_image")
    image = image.get("src") if isinstance(image, dict) else None
    resolved = target_for(
        urljoin(urljoin(target.url, canonical["href"]), "/products/" + native["handle"])
        + "?"
        + urlencode({"variant": selected})
    )
    return Listing(
        resolved,
        "Fire-Boltt " + native["title"][:240],
        "Fire-Boltt",
        str(native.get("type", ""))[:100] or "Uncategorised",
        attributes,
        safe_image(urljoin(resolved.url, image), urlsplit(resolved.url).hostname)
        if isinstance(image, str)
        else None,
        price,
        None,
        "in_stock" if available else "out_of_stock",
        "unknown",
        "Seller not stated",
        rating=product_rating(product.get("aggregateRating")),
        discovery_kind="shopify_html",
    )
