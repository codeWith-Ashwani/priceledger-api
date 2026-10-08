"""Conservative cross-retailer identity: valid GTINs or complete mobile variants."""

import re


def normalized(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def mobile_attributes(title: str, brand: str, attributes: dict) -> dict:
    result = dict(attributes)
    ram = re.search(r"(\d+)\s*GB\s*RAM", title, re.I)
    storage = re.search(r"(\d+)\s*GB\s*(?:Storage|ROM)", title, re.I)
    combo = re.search(r"(\d+)\s*GB\s*[,/+]?\s*(\d+)\s*GB", title, re.I)
    if ram:
        result["ram_gb"] = int(ram[1])
    if storage:
        result["storage_gb"] = int(storage[1])
    elif combo:
        result["ram_gb"], result["storage_gb"] = int(combo[1]), int(combo[2])
    else:
        sizes = [int(match[1]) for match in re.finditer(r"(\d+)\s*GB(?!\s*RAM)", title, re.I)]
        if len(sizes) == 1 and sizes[0] >= 32:
            result["storage_gb"] = sizes[0]
    if "color" not in result:
        colors = re.findall(
            r"\b(blue|black|white|green|pink|red|silver|grey|gray|yellow|purple)\b", title.lower()
        )
        if len(set(colors)) == 1:
            result["color"] = colors[0]
    # Phones only: never apply these heuristics to clothing, packs or accessories.
    if ("phone" in title.lower() or "iphone" in title.lower()) and not re.search(
        r"\b(?:cover|case|charger|protector|cable)\b", title, re.I
    ):
        base = re.split(
            r"\((?=\s*(?:\d+\s*GB|blue|black|white|green|pink|red))|\b\d+\s*GB|\b(?:blue|black|white|green|pink|red|silver|grey|gray)\b",
            title,
            maxsplit=1,
            flags=re.I,
        )[0]
        if brand and normalized(brand) != "brandnotstated":
            base = re.sub(r"^" + re.escape(brand) + r"\s*", "", base, flags=re.I)
        result.setdefault("model", base.strip(" (,|-"))
        result["identity_category"] = "mobile"
    return result


def valid_gtin(value) -> str | None:
    text = str(value)
    if not text.isdigit() or len(text) not in {8, 12, 13, 14} or set(text) == {"0"}:
        return None
    total = sum(
        int(digit) * (3 if index % 2 == 0 else 1) for index, digit in enumerate(reversed(text[:-1]))
    )
    return text.zfill(14) if (10 - total % 10) % 10 == int(text[-1]) else None


def match_variant(left, right) -> dict:
    a, b = left.attributes, right.attributes
    brand_a, brand_b = normalized(left.brand), normalized(right.brand)
    conflicts = []
    for key in ("model", "color", "size", "ram_gb", "storage_gb", "pack", "mpn"):
        if key in a and key in b and normalized(a[key]) != normalized(b[key]):
            conflicts.append(key)
    if brand_a != brand_b or brand_a == "brandnotstated":
        return {"status": "review", "reason": "Brand identity is not confirmed."}
    if conflicts or (
        left.condition != "unknown"
        and right.condition != "unknown"
        and left.condition != right.condition
    ):
        return {
            "status": "different_variant",
            "reason": "Different "
            + ", ".join(
                {"ram_gb": "RAM", "storage_gb": "storage", "color": "colour"}.get(key, key)
                for key in (conflicts or ["condition"])
            )
            + ".",
        }
    gtins_a = {
        valid_gtin(a[key]) for key in ("gtin", "gtin8", "gtin12", "gtin13", "gtin14") if key in a
    } - {None}
    gtins_b = {
        valid_gtin(b[key]) for key in ("gtin", "gtin8", "gtin12", "gtin13", "gtin14") if key in b
    } - {None}
    if gtins_a and gtins_b and not gtins_a.intersection(gtins_b):
        return {"status": "different_variant", "reason": "Different product barcodes."}
    if left.condition == "unknown" or right.condition == "unknown":
        return {"status": "review", "reason": "Product condition is not confirmed."}
    if gtins_a.intersection(gtins_b):
        return {
            "status": "verified",
            "method": "gtin",
            "reason": "Matching barcode, brand and condition; no conflicting variant attributes.",
        }
    required = ("model", "color", "ram_gb", "storage_gb")
    if a.get("identity_category") == b.get("identity_category") == "mobile" and all(
        key in a and key in b for key in required
    ):
        return {
            "status": "verified",
            "method": "mobile_variant",
            "reason": "Matching brand, phone model, RAM, storage, colour and condition.",
        }
    return {
        "status": "review",
        "reason": "Not enough variant identifiers to combine these listings.",
    }
