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
    elif combo and not ram:
        result["ram_gb"], result["storage_gb"] = int(combo[1]), int(combo[2])
    else:
        sizes = [int(match[1]) for match in re.finditer(r"(\d+)\s*GB(?!\s*RAM)", title, re.I)]
        if len(sizes) == 1 and sizes[0] >= 32:
            result["storage_gb"] = sizes[0]
    variant = re.search(r"\(([^()]*(?:\d+\s*GB|RAM|Storage)[^()]*)\)", title, re.I)
    if "color" not in result and variant:
        first = variant[1].split(",", 1)[0].strip()
        if re.fullmatch(r"[A-Za-z][A-Za-z -]{1,45}", first) and not re.search(
            r"\b(?:RAM|Storage|ROM|GB)\b", first, re.I
        ):
            result["color"] = first
    if "color" not in result:
        colors = re.findall(
            r"\b(blue|black|white|green|pink|red|silver|grey|gray|yellow|purple)\b", title.lower()
        )
        if len(set(colors)) == 1:
            result["color"] = colors[0]
    # Phones only: never apply these heuristics to clothing, packs or accessories.
    phone_brand = normalized(brand) in {
        "iqoo",
        "vivo",
        "oppo",
        "oneplus",
        "realme",
        "poco",
        "redmi",
        "samsung",
        "motorola",
        "nothing",
        "apple",
        "google",
        "infinix",
        "tecno",
        "nokia",
        "honor",
    }
    if (
        re.search(r"\bphone\b", title, re.I)
        or (phone_brand and "storage_gb" in result and re.search(r"\d", title))
    ) and not re.search(
        r"\b(?:cover|case|charger|protector|cable|laptop|notebook|tablet|tab|pad|book|ssd|headphones?|earbuds?|headset)\b",
        title,
        re.I,
    ):
        model_title = title[: variant.start()] if variant else title
        base = re.split(
            r"\((?=\s*(?:\d+\s*GB|blue|black|white|green|pink|red))|\b\d+\s*GB|\b(?:blue|black|white|green|pink|red|silver|grey|gray)\b",
            model_title.split("|", 1)[0],
            maxsplit=1,
            flags=re.I,
        )[0]
        if brand and normalized(brand) != "brandnotstated":
            base = re.sub(r"^" + re.escape(brand) + r"\s*", "", base, flags=re.I)
        result.setdefault("model", base.strip(" (,|-"))
        result["identity_category"] = "mobile"
    return result


def model_attributes(title: str, brand: str, attributes: dict) -> dict:
    """Extract a model identifier, keeping colour/capacity as variant metadata."""
    result = {**mobile_attributes(title, brand, {}), **attributes}
    if result.get("identity_category") == "mobile" and re.search(
        r"\b(?:headphones?|earbuds?|headset)\b", title, re.I
    ):
        result.pop("identity_category", None)
        result.pop("model", None)
    if result.get("identity_category") == "mobile":
        return result
    base = re.sub(r"^" + re.escape(brand) + r"\s*", "", title, flags=re.I)
    # Schema model fields can contain the entire marketing title (Reliance).
    supplied = str(result.get("model", ""))
    if supplied:
        result["model"] = re.split(r"[,|]", supplied, maxsplit=1)[0].strip()
    prefix = re.split(r"[,|()]", base, maxsplit=1)[0].strip()
    code = re.match(
        r"((?:[A-Za-z][A-Za-z -]{0,35}\s+)?[A-Za-z]{1,24}(?:-[A-Za-z]{1,8})?[- ]?\d{1,5}[A-Za-z]?(?:[-/]\d{1,4})?(?:\s+(?:Pro|Plus|Lite|Ultra|Max|ANC|[2345]G|v\d+|Gen(?:eration)?[- ]?\d+|Mark[- ]?\d+))?)\b",
        prefix,
    )
    if code and not re.search(r"\b(?:size|pack|of|hours?|mah|watts?)\s*\d", code[1], re.I):
        derived = code[1].strip()
        unqualified = re.sub(
            r"\s+(?:Pro|Plus|Lite|Ultra|Max|ANC|[2345]G|v\d+|Gen(?:eration)?[- ]?\d+|Mark[- ]?\d+)$",
            "",
            derived,
            flags=re.I,
        )
        # Older parsers omitted generation qualifiers. Restore them in matching
        # metadata without rewriting historical observation snapshots.
        if not supplied or normalized(result["model"]) == normalized(unqualified):
            result["model"] = derived
    return result


def model_key(value) -> str:
    # Charging wattage is a retailer naming addition, not a model generation.
    return normalized(re.sub(r"\b\d{1,3}W\b", "", str(value), flags=re.I))


def match_model(left, right) -> dict:
    """Model comparisons may contain variants; they never create history links."""
    exact = match_variant(left, right)
    if exact["status"] == "verified":
        return {**exact, "differences": []}
    brand = normalized(left.brand)
    if not brand or brand == "brandnotstated" or brand != normalized(right.brand):
        return {"status": "review", "reason": "Brand identity is not confirmed."}
    a = model_attributes(left.title, left.brand, left.attributes)
    b = model_attributes(right.title, right.brand, right.attributes)
    accessory = r"\b(?:case|cover|protector|replacement|earpads|skin|sleeve)\b"
    if bool(re.search(accessory, left.title, re.I)) != bool(
        re.search(accessory, right.title, re.I)
    ):
        return {"status": "different_variant", "reason": "An accessory is a different product."}
    if not a.get("model") or not b.get("model"):
        return exact
    if model_key(a["model"]) != model_key(b["model"]):
        return {"status": "different_variant", "reason": "Different model."}
    # Do not rank used/refurbished stock against new or unknown condition.
    if left.condition != right.condition and {left.condition, right.condition} & {
        "used",
        "refurbished",
    }:
        return {"status": "different_variant", "reason": "Different product condition."}
    differences = [
        {"attribute": key, "requested": a[key], "offered": b[key]}
        for key in ("color", "size", "ram_gb", "storage_gb", "pack", "mpn")
        if key in a and key in b and normalized(a[key]) != normalized(b[key])
    ]
    return {
        "status": "model_match",
        "method": "brand_model",
        "reason": "Same brand and model. Variant details may differ; history stays separate.",
        "differences": differences,
    }


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
    if conflicts == ["model"] and normalized(
        re.sub(r"\b\d{1,3}W\b", "", str(a["model"]), flags=re.I)
    ) == normalized(re.sub(r"\b\d{1,3}W\b", "", str(b["model"]), flags=re.I)):
        return {
            "status": "review",
            "reason": "Retailer model names differ in their charging label; a shared manufacturer identifier is needed.",
        }
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
