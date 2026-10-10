"""Read bounded SKU metadata from iQOO's public Nuxt catalog, without running JS."""

import json
import re
from itertools import islice

from bs4 import BeautifulSoup


def split_values(text: str) -> list[str]:
    values, start, depth, quote, escaped = [], 0, 0, None, False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in {'"', "'"}:
            quote = char
        elif char in "{[(":
            depth += 1
            if depth > 100:
                raise ValueError("Catalog nesting exceeds the supported format")
        elif char in "}])":
            depth -= 1
            if depth < 0:
                raise ValueError("Unbalanced catalog data")
        elif char == "," and depth == 0:
            values.append(text[start:index])
            if len(values) > 5000:
                raise ValueError("Too many catalog values")
            start = index + 1
    if depth or quote:
        raise ValueError("Incomplete catalog data")
    return [*values, text[start:]]


def literal(value: str, aliases: dict):
    value = value.strip()
    if value in aliases:
        return aliases[value]
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, (str, int, float, bool)) or parsed is None else None
    except (ValueError, RecursionError):
        return None


def state(html: str) -> tuple[str, dict]:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script:not([src])"):
        script = node.get_text().strip()
        signature = re.match(r"window\.__NUXT__=\(function\(([A-Za-z0-9_$,]+)\)\{", script)
        if not signature or len(script) > 1_000_000:
            continue
        marker = script.rfind("}(")
        if marker < signature.end() or not script.endswith("));"):
            continue
        try:
            arguments = split_values(script[marker + 2 : -3])
            names = signature[1].split(",")
            if len(arguments) != len(names) or len(names) > 2000:
                continue
            return script, {
                name: literal(value, {}) for name, value in zip(names, arguments, strict=True)
            }
        except ValueError:
            continue
    return "", {}


def flat_fields(body: str, aliases: dict) -> dict:
    result = {}
    for field in split_values(body):
        key, separator, value = field.partition(":")
        if separator and key.strip() in {"spuId", "skuId", "skuName", "spuName", "shortName"}:
            result[key.strip()] = literal(value, aliases)
    return result


def catalog_rows(html: str) -> list[dict]:
    script, aliases = state(html)
    # Public catalog cards have a flat identity prefix followed by nested metadata.
    rows = []
    for match in islice(re.finditer(r'\{shortName:[^,{}]+,skuName:"(?:\\.|[^"\\])*"', script), 50):
        tail = script[match.end() :]
        # spuId and skuId occur before the first nested energy/promotion object.
        boundary = re.search(r"[{}\[\]]", tail)
        prefix = (
            script[match.start() + 1 : match.end()] + tail[: boundary.start() if boundary else 0]
        )
        try:
            row = flat_fields(prefix.rstrip(","), aliases)
        except ValueError:
            continue
        if (
            type(row.get("spuId")) is int
            and type(row.get("skuId")) is int
            and isinstance(row.get("skuName"), str)
        ):
            rows.append(row)
    return rows[:50]


def variant_rows(html: str) -> list[dict]:
    script, aliases = state(html)
    rows = []
    pattern = r'\{skuId:([^,{}]+),shortName:[^,{}]+,skuName:("(?:\\.|[^"\\])*")'
    for match in islice(re.finditer(pattern, script), 50):
        identifier = literal(match[1], aliases)
        title = literal(match[2], {})
        if type(identifier) is int and 0 < identifier < 10**12 and isinstance(title, str):
            rows.append({"skuId": identifier, "title": title.strip()})
    return rows[:50]


def tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z]+|\d+", value.lower()))


def search_candidates(query: str, fetch) -> list[dict]:
    # At most two search reads: one catalog and one matching model's SKU selector.
    catalog = fetch("https://shop.iqoo.com/in/products/phone")
    required = tokens(query) - {"iqoo"}
    models = [row for row in catalog_rows(catalog) if required.issubset(tokens(row["skuName"]))]
    if not models:
        return []
    model = models[0]
    url = f"https://shop.iqoo.com/in/product/{model['spuId']}"
    variants = variant_rows(fetch(url))
    variants.append({"skuId": model["skuId"], "title": model["skuName"]})
    items = {}
    for row in variants:
        sku_url = url + f"?skuId={row['skuId']}"
        items[sku_url] = {
            "title": row["title"][:250],
            "url": sku_url,
            "retailer": "iqoo",
            "retailer_name": "iQOO Official Store",
        }
    return list(items.values())[:12]
