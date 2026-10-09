"""Parse rendered Ozon pages without inventing missing product facts."""

from __future__ import annotations

import html as html_lib
import json
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit


def _text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_lib.unescape(value or ""))).strip()


def _number(value: object) -> float | None:
    match = re.search(r"\d+(?:[.,]\d+)?", str(value).replace("\u2009", "").replace("\xa0", "").replace(" ", ""))
    if not match:
        return None
    try:
        return float(match.group().replace(",", "."))
    except ValueError:
        return None


def _jsonld(html: str):
    for raw in re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S):
        try:
            value = json.loads(html_lib.unescape(raw.strip()))
        except (json.JSONDecodeError, TypeError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict) and isinstance(item.get("@graph"), list):
                yield from (part for part in item["@graph"] if isinstance(part, dict))
            elif isinstance(item, dict):
                yield item


def _photo(url: object) -> str | None:
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    host = (urlsplit(url).hostname or "").lower()
    if not (host.endswith(".ozon.ru") or host.endswith(".ozone.ru") or host.endswith(".ozonusercontent.com") or host.endswith(".ozonstatic.cn")):
        return None
    return re.sub(r"/(?:c|wc)\d+/", "/wc1000/", url)


def _currencies(value: object) -> list[str]:
    text = str(value or "")
    found = [currency for currency, pattern in {
        "RUB": r"₽|\bруб\b|\bRUB\b",
        "CHF": r"\b(?:CHF|Fr)\b",
        "CNY": r"\b(?:CNY|RMB)\b|人民币|元",
        "KZT": r"\bKZT\b|₸",
        "JPY": r"\bJPY\b|円",
        "USD": r"\bUSD\b", "EUR": r"\bEUR\b|€", "BYN": r"\bBYN\b",
    }.items() if re.search(pattern, text, re.I)]
    return found


def _currency(value: object) -> str | None:
    found = _currencies(value)
    # The yen/yuan symbol alone does not establish CNY.
    return found[0] if len(found) == 1 else None


def _price_evidence(sku: str, html: str, single_product: bool) -> tuple:
    candidates = []
    raw_states = []
    class States(HTMLParser):
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if attrs.get("data-state"):
                raw_states.append((attrs["data-state"], attrs.get("id", "")))
    States().feed(html)
    raw_states += [(html_lib.unescape(raw), "") for raw in re.findall(r'<script\b[^>]*>(.*?)</script>', html, re.I | re.S)]
    for raw, widget_id in raw_states:
        if "cardPrice" not in raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        pending, prices, identities = [data], [], set()
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                identity = str(value.get("sku", ""))
                if identity:
                    identities.add(identity)
                if "cardPrice" in value:
                    price = value
                    if value is data and re.fullmatch(r"state-webPrice-\d+-default-\d+", widget_id):
                        # Observed CNY widget: its bank link binds the header and price to this SKU.
                        try:
                            link = urlsplit(str(value.get("link", "")))
                            ids = parse_qs(link.query).get("product_id", [])
                            linked = ids[0] if not link.scheme and not link.netloc and link.path == "/modal/pdpListOfBanks" and len(ids) == 1 and re.fullmatch(r"\d{6,14}", ids[0]) else "ambiguous"
                        except ValueError:
                            linked = "ambiguous"
                        identity = linked if not identity or identity == linked else "ambiguous"
                        header = _text(str(value.get("disclaimerPriceHeader", "")))
                        if re.fullmatch(r"(?:总计|Итого|Всего|Total)(?:\s*[:：]\s*|\s+)CNY", header, re.I):
                            declarations = [value[key] for key in ("currency", "priceCurrency") if key in value]
                            declared = "CNY" if all(_currency(item) == "CNY" for item in declarations) else ""
                            price = {**value, "priceCurrency": declared}
                        elif _currency(header) and _currency(header) != _currency(value.get("cardPrice")):
                            price = {**value, "priceCurrency": ""}
                    prices.append((identity, price))
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        for identity, price in prices:
            # No guessed widget ID: a single price in a same-SKU JSON object is evidence.
            if not identity and identities:
                identity = next(iter(identities)) if len(prices) == 1 and len(identities) == 1 else "ambiguous"
            candidates.append((identity, price, widget_id))
    selected = [(identity, price, widget) for identity, price, widget in candidates if identity == sku]
    if not selected and len(candidates) == 1 and not candidates[0][0] and single_product:
        # Existing single-product snapshots have one unlabelled price state.
        selected = candidates
    values = set()
    evidence = []
    for identity, price, widget in selected:
        raw = price.get("cardPrice")
        currency = _currency(raw)
        declarations = [_currency(price[key]) for key in ("priceCurrency", "currency") if key in price]
        if declarations:
            explicit = declarations[0] if all(item == declarations[0] for item in declarations) else None
            bare_amount = re.fullmatch(r"[¥￥]?\s*[\d.,\s]+\s*[¥￥]?", str(raw))
            currency = explicit if explicit and (currency == explicit or not currency and bare_amount) else None
        # A stale CNY declaration must not relabel a RUB price object.
        for key in ('price', 'originalPrice'):
            amount = price.get(key)
            if amount is not None:
                codes = _currencies(amount)
                if codes and codes != [currency]:
                    currency = None
        values.add((_number(raw), _number(price.get("price")), currency or ""))
        if identity == sku and currency == 'CNY':
            evidence.append({'sku': sku, 'currency': currency, 'raw': str(raw),
                             'amount': _number(raw), 'widget_id': widget})
    if len(values) != 1:
        return None, None, '', None
    return (*next(iter(values)), evidence[0] if evidence else None)


def parse_pdp(sku: str, html: str) -> dict:
    product = {
        "sku": sku,
        "title": "",
        "description": "",
        "source_brand": "",
        "green_price": None,
        "regular_price": None,
        "currency": "",
        "photos": [],
        "category_path": "",
    }
    products = [item for item in _jsonld(html) if item.get("@type") in ("Product", "IndividualProduct")]
    single_product = len(products) == 1
    matched = [item for item in products if str(item.get("sku", "")) == sku]
    products = matched or (products if len(products) == 1 and not products[0].get("sku") else [])
    brands = set()
    for item in products:
        product["title"] = product["title"] or _text(str(item.get("name", "")))
        product["description"] = product["description"] or _text(str(item.get("description", "")))
        brand = item.get("brand")
        if isinstance(brand, dict):
            product["source_brand"] = _text(str(brand.get("name", "")))
        elif brand:
            product["source_brand"] = _text(str(brand))
        if product["source_brand"]:
            brands.add(product["source_brand"].casefold())
        images = item.get("image", [])
        for candidate in [images] if isinstance(images, str) else images if isinstance(images, list) else []:
            if url := _photo(candidate):
                product["photos"].append(url)
        offers = item.get("offers")
        if isinstance(offers, dict):
            product["regular_price"] = product["regular_price"] or _number(offers.get("price"))

    product["source_brand_conflict"] = len(brands) > 1
    green, regular, currency, evidence = _price_evidence(sku, html, single_product and len(products) == 1)
    product["green_price"] = green
    product["regular_price"] = regular or product["regular_price"]
    product["currency"] = currency
    product['green_price_evidence'] = evidence

    if not product["title"]:
        for pattern in (
            r"<h1[^>]*>(.*?)</h1>",
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']',
            r"<title>(.*?)</title>",
        ):
            match = re.search(pattern, html, re.I | re.S)
            if match and len(_text(match.group(1))) >= 4:
                product["title"] = re.sub(r"\s*[-–]?\s*купить на OZON.*$", "", _text(match.group(1)), flags=re.I)
                break

    crumbs = [_text(value) for value in re.findall(r'<a[^>]+href=["\']/category/[^"\']+["\'][^>]*>(.*?)</a>', html, re.I | re.S)]
    product["category_path"] = " > ".join(value for value in crumbs if value)[:500]

    # Only images in this SKU's Product record are product evidence. Whole-page
    # URL scans mix recommendation cards, logos and review thumbnails into WB.
    product["photos"] = list(dict.fromkeys(product["photos"]))

    description = product["description"]
    description = re.sub(rf"(?i)\b(?:ozon|озон)\b|\b{re.escape(sku)}\b", "", description)
    product["description"] = re.sub(r"\s+", " ", description).strip()[:1900]
    product["photos"] = product["photos"][:30]
    return product


def _properties(html: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for _, value in re.findall(r'data-state=(["\'])(.*?)\1', html, re.I | re.S):
        if "characteristics" not in value:
            continue
        try:
            data = json.loads(html_lib.unescape(value))
        except (json.JSONDecodeError, TypeError):
            continue
        for group in data.get("characteristics", []) if isinstance(data, dict) else []:
            if not isinstance(group, dict):
                continue
            for item in group.get("short", []) + group.get("long", []):
                if not isinstance(item, dict):
                    continue
                name = _text(str(item.get("name", "")))
                values = [_text(str(part.get("text", ""))) for part in item.get("values", []) if isinstance(part, dict)]
                if name and any(values):
                    result[name] = ", ".join(value for value in values if value)
    return result


def package_units(label, value, dimension=False):
    text = (label + ' ' + value).casefold()
    patterns = {'mm': r'\bмм\b|\bmm\b|毫米', 'cm': r'\bсм\b|\bcm\b|厘米', 'm': r'\bм\b|\bm\b|(?<!厘|毫)米'} if dimension else {
        'kg': r'\bкг\b|\bkg\b|公斤|千克', 'g': r'\bг\b|\bg\b|(?<!千)克'}
    units = [u for u, pattern in patterns.items() if re.search(pattern, text)]
    return units[0] if len(units) == 1 else None


def parse_features(html: str) -> dict:
    props = _properties(html)
    result = {
        "length_cm": None,
        "width_cm": None,
        "height_cm": None,
        "weight_g": None,
        "dimension_source": None,
        "weight_source": None,
        "tnved": "",
        "properties": props,
    }
    for name, value in props.items():
        lower = name.lower()
        if "размер упаковки" in lower or "габариты упаковки" in lower or "包装尺寸" in name:
            numbers = re.findall(r"\d+(?:[.,]\d+)?", value)
            unit = package_units(name, value, dimension=True)
            if len(numbers) == 3 and unit:
                factor = {'mm': .1, 'cm': 1, 'm': 100}[unit]
                result["length_cm"], result["width_cm"], result["height_cm"] = [round(float(number.replace(",", ".")) * factor, 6) for number in numbers]
                result["dimension_source"] = name
        elif "вес с упаковкой" in lower or "вес брутто" in lower or any(label in name for label in ("毛重(单位)", "包装毛重", "含包装重量")):
            value_number = _number(value)
            unit = package_units(name, value)
            if value_number and unit:
                result["weight_g"] = round(value_number * (1000 if unit == 'kg' else 1))
                result["weight_source"] = name
        elif "тн вэд" in lower or "тнвэд" in lower:
            result["tnved"] = value

    rendered = _text(html)
    if result["length_cm"] is None:
        match = re.search(r"((?:Размер упаковки|Габариты упаковки|包装尺寸)[^:：<>]{0,50})[:：]\s*([\d.,]+)\s*[xхX*×]\s*([\d.,]+)\s*[xхX*×]\s*([\d.,]+)(\s*(?:см|мм|cm|mm|厘米|毫米|м|m)\b)?", rendered, re.I)
        if match:
            unit = package_units(match.group(1), match.group(5) or '', dimension=True)
            if unit:
                factor = {'mm': .1, 'cm': 1, 'm': 100}[unit]
                result["length_cm"], result["width_cm"], result["height_cm"] = [round(float(number.replace(",", ".")) * factor, 6) for number in match.groups()[1:4]]
                result["dimension_source"] = "rendered_features"
    if result["weight_g"] is None:
        # Some native descriptions put the unit in the label, not after the value.
        match = re.search(r"(?:Вес с упаковкой|Вес брутто)\s*,?\s*\(\s*(кг|г)\.?\s*\)\s*[:：]\s*(\d+(?:[.,]\d+)?)(?![\d.,])", rendered, re.I)
        if match:
            suffix = re.match(r"\s*(кг|г)\b", rendered[match.end():], re.I)
            if not suffix or suffix.group(1).lower() == match.group(1).lower():
                value_number = _number(match.group(2))
                result["weight_g"] = round(value_number * 1000) if match.group(1).lower() == "кг" else round(value_number)
                result["weight_source"] = "rendered_features"
        else:
            match = re.search(r"(?:Вес с упаковкой|Вес брутто|毛重\s*\(单位\)|包装毛重|含包装重量)[^:：]*[:：]\s*([\d.,\s]+)\s*(г|кг|克|公斤|千克)", rendered, re.I)
            if match and (value_number := _number(match.group(1))):
                result["weight_g"] = round(value_number * 1000) if match.group(2).lower() in ("кг", "公斤", "千克") else round(value_number)
                result["weight_source"] = "rendered_features"
    return result


def parse_product(sku: str, pdp_html: str, features_html: str) -> dict:
    product = parse_pdp(sku, pdp_html)
    features = parse_features(features_html)
    pdp_features = parse_features(pdp_html)
    for key in ("length_cm", "width_cm", "height_cm", "weight_g", "dimension_source", "weight_source", "tnved"):
        features[key] = features[key] or pdp_features[key]
    features["properties"].update(pdp_features["properties"])
    product.update({key: features[key] for key in ("length_cm", "width_cm", "height_cm", "weight_g", "tnved")})
    product["source_evidence"] = {
        "dimensions": features["dimension_source"],
        "weight": features["weight_source"],
    }
    product["properties"] = features["properties"]
    blockers = []
    if not product["title"]:
        blockers.append("missing_title")
    if not product["green_price"]:
        blockers.append("missing_green_price")
    if product["currency"] != "CNY":
        blockers.append("unsupported_price_currency")
    if not product["photos"]:
        blockers.append("missing_photos")
    if not all(product[key] and product[key] > 0 for key in ("length_cm", "width_cm", "height_cm")):
        blockers.append("missing_package_dimensions")
    if not product["weight_g"] or product["weight_g"] <= 0:
        blockers.append("missing_package_weight")
    return {"ok": True, "ready_for_listing": not blockers, "blockers": blockers, "product": product}
