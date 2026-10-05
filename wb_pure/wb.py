"""Checkpointed direct WB operations; credentials never returned in results."""
from __future__ import annotations

import json
import math
import re
import uuid
import time
import threading
from contextvars import ContextVar
from decimal import Decimal
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .pricing import build_price_plan, minimum_sale, number, POLICY_VERSION

CONTENT = "https://content-api.wildberries.ru"
PRICES = "https://discounts-prices-api.wildberries.ru"
MARKET = "https://marketplace-api.wildberries.ru"
COMMON = "https://common-api.wildberries.ru"
CATEGORY_POLICY = 2


def clean_title_and_text(text, sku=""):
    text = str(text or "")
    text = re.sub(r"(?i)(?<!\w)(ozon|озон)(?!\w)", "", text)
    text = re.sub(r'https?://[^\s<>\"]+', '', text)
    text = re.sub(r'(?i)(?:Код товара(?: Ozon)?|Артикул(?: Ozon)?|SKU|Ozon ID)\s*[:：#]?\s*\d+', '', text)
    if sku:
        text = re.sub(rf'(?<!\d){re.escape(str(sku))}(?!\d)', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def strip_brand(text, brand):
    # Remove known brand tokens from copy, retaining factual WB brand metadata.
    if category_name(brand) not in ('нет бренда', 'без бренда', 'no brand', '无品牌'):
        text = re.sub(r'(?<!\w)' + re.escape(brand) + r'(?!\w)', '', text, flags=re.I)
    return re.sub(r'\s+', ' ', text).strip(' ,;:-')


def category_name(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip().casefold().replace('ё', 'е')


def source_category(product):
    # Only explicit source taxonomy is evidence; title substrings are not categories.
    types = {category_name(v) for k, v in product.get('properties', {}).items()
             if category_name(k) in ('тип', '类型', 'type') and isinstance(v, str) and v.strip()}
    if len(types) > 1:
        raise BusinessError('category_mismatch')
    if types:
        return next(iter(types))
    raise BusinessError('category_required')


def category_schema(token, subject, expected_name):
    schema = api(token, 'GET', CONTENT, f'/content/v2/object/charcs/{subject}').get('data', [])
    if not isinstance(schema, list) or not schema or any(not isinstance(f, dict) or type(f.get('subjectID')) is not int or f['subjectID'] != subject or
                         category_name(f.get('subjectName')) != expected_name or
                         type(f.get('charcID')) is not int for f in schema):
        raise BusinessError('category_unverified')
    return schema


def resolve_category(token, product, subject):
    source = source_category(product)
    mapping = product.get('category_mapping')
    if mapping:
        if (not isinstance(mapping, dict) or category_name(mapping.get('source_type')) != source
                or not mapping.get('evidence') or not mapping.get('confirmed_by')
                or type(mapping.get('subject_id')) is not int or not mapping.get('subject_name')):
            raise BusinessError('category_mapping_unverified')
        if subject is not None and subject != mapping['subject_id']:
            raise BusinessError('category_mismatch')
        subject = mapping['subject_id']
        label = category_name(mapping['subject_name'])
    else:
        label = source
    result = api(token, 'GET', CONTENT, '/content/v2/object/all?' + urlencode({'name': label, 'limit': 1000, 'offset': 0, 'locale': 'ru'}))
    rows = result.get('data', [])
    # No first-result, fuzzy or numeric-ID fallback. Broad/truncated queries require review.
    if not isinstance(rows, list) or len(rows) >= 1000:
        raise BusinessError('category_unverified')
    matches = {r['subjectID']: r for r in rows if isinstance(r, dict) and type(r.get('subjectID')) is int and
               category_name(r.get('subjectName')) == label}
    if len(matches) != 1:
        raise BusinessError('category_required')
    selected = next(iter(matches))
    if subject is not None and (type(subject) is not int or subject != selected):
        raise BusinessError('category_mismatch')
    return selected, label, category_schema(token, selected, label)


def required(field):
    return field.get('required') is True or field.get('isRequiredForCreate') is True


def validate_characteristics(schema, chars):
    allowed = {f['charcID']: f for f in schema}
    supplied = {c['id']: c['value'] for c in chars}
    if len(supplied) != len(chars) or set(supplied) - set(allowed):
        raise BusinessError('characteristics_mismatch')
    for field_id, field in allowed.items():
        if field_id not in supplied:
            if required(field):
                raise BusinessError('required_characteristics_missing')
            continue
        value = supplied[field_id]
        if field.get('charcType') == 4:
            valid = type(value) in (float, int) and math.isfinite(value)
        else:
            maximum = field.get('maxCount', 0)
            valid = isinstance(value, list) and bool(value) and all(isinstance(v, str) and 0 < len(v) <= 500 for v in value)
            valid = valid and (not maximum or type(maximum) is int and len(value) <= maximum)
        if not valid:
            raise BusinessError('characteristics_mismatch')


def validate_category_plan(plan):
    if plan.get('category_policy_version') != CATEGORY_POLICY or not plan.get('category_source') or type(plan.get('subjectID')) is not int:
        raise BusinessError('category_recheck_required')


def verify_card_category(card, plan):
    if not card or card.get('subjectID') != plan['subjectID']:
        raise BusinessError('category_readback_mismatch')


def source_brand(product):
    values = [product.get('source_brand')] + [v for k, v in product.get('properties', {}).items()
                                            if category_name(k) in ('бренд', 'brand', '品牌')]
    brands = {category_name(v): str(v).strip() for v in values if isinstance(v, str) and v.strip()}
    if product.get('source_brand_conflict') or len(brands) != 1 or len(next(iter(brands.values()), '')) > 100:
        raise BusinessError('brand_unverified')
    # Missing evidence must never be converted to an invented unbranded product.
    return next(iter(brands.values()))


def photos_ready(card, plan):
    photos = card.get('photos', []) if card else []
    urls = [p.get('big') for p in photos if isinstance(p, dict)]
    return len(urls) == len(plan['photos']) and all(isinstance(u, str) for u in urls) and len(set(urls)) == len(urls) and all(
        urlsplit(u).scheme == 'https' and urlsplit(u).hostname for u in urls)


def verify_card_content(card, plan):
    verify_card_category(card, plan)
    if not plan.get('brand') or category_name(card.get('brand')) != category_name(plan['brand']):
        raise BusinessError('brand_readback_mismatch')
    if any(card.get(k) != plan[k] for k in ('title', 'description')):
        raise BusinessError('text_readback_mismatch')
    if not photos_ready(card, plan):
        raise BusinessError('media_readback_mismatch')
    if any(card.get('dimensions', {}).get(k) != v for k, v in plan['dimensions'].items()):
        raise BusinessError('package_readback_mismatch')
    actual = {c.get('id'): c.get('value') for c in card.get('characteristics', [])}
    if any(actual.get(c['id']) != c['value'] for c in plan['characteristics']):
        raise BusinessError('characteristics_mismatch')


SELLER = ContextVar("seller", default="unknown")
PRICE_LOCK = threading.Lock()
PRICE_NEXT = {}


class BusinessError(Exception):
    def __init__(self, code, retry_after=0):
        super().__init__(code)
        self.retry_after = retry_after


BEFORE_WRITE = ContextVar("before_write", default=None)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise BusinessError("upstream_redirect")


def api(token, method, base, path, body=None, extra=None, raw=None):
    assert base in (CONTENT, PRICES, MARKET, COMMON)
    if base in (PRICES, CONTENT):
        # ponytail: one local worker per store; shared limiter needed only for multi-host operation.
        with PRICE_LOCK:
            seller = (SELLER.get(), base)
            delay = max(0, PRICE_NEXT.get(seller, 0) - time.monotonic())
            if delay > 2:
                raise BusinessError('wb_rate_limited', retry_after=delay)
            time.sleep(delay)
            PRICE_NEXT[seller] = time.monotonic() + .65
    data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": token, "Content-Type": "application/json", "Accept": "application/json"}
    headers.update(extra or {})
    hook = BEFORE_WRITE.get()
    if hook:
        hook(method, path)
    try:
        with build_opener(NoRedirect()).open(Request(base + path, data=data, headers=headers, method=method), timeout=18) as response:
            result = response.read(8 * 1024 * 1024)
            result = json.loads(result) if result else {}
    except HTTPError as error:
        if error.code == 429:
            try:
                retry = max(1, min(3600, float(error.headers.get('Retry-After', 30))))
            except (ValueError, TypeError):
                retry = 30
            with PRICE_LOCK:
                PRICE_NEXT[(SELLER.get(), base)] = time.monotonic() + retry
        raise BusinessError("wb_unauthorized" if error.code == 401 else "wb_permission_denied" if error.code == 403 else "wb_rate_limited" if error.code == 429 else "wb_request_rejected", retry_after=retry if error.code == 429 else 0) from None
    except (URLError, TimeoutError, ValueError, OSError):
        raise BusinessError("wb_unavailable") from None
    if isinstance(result, dict) and result.get("error"):
        raise BusinessError("wb_request_rejected")
    return result


def bind(token, warehouse):
    if type(warehouse) is not int or warehouse <= 0:
        raise BusinessError("invalid_warehouse")
    seller = api(token, "GET", COMMON, "/api/v1/seller-info")
    if not isinstance(seller.get("sid"), str) or not seller["sid"]:
        raise BusinessError("seller_unverified")
    warehouses = api(token, "GET", MARKET, "/api/v3/warehouses")
    match = next((w for w in warehouses if w.get("id") == warehouse), None)
    if not match:
        raise BusinessError("warehouse_mismatch")
    return {"seller_id": seller["sid"], "store_name": seller.get("name", ""), "warehouse_id": warehouse, "warehouse_name": match.get("name", "")}


def positive(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise BusinessError("missing_product_facts")
    return value


def image_url(url):
    p = urlsplit(url)
    host = p.hostname or ""
    if p.scheme != "https" or p.username or p.password or p.port not in (None, 443) or not any(host.endswith(s) for s in (".ozon.ru", ".ozone.ru", ".ozonusercontent.com", ".ozonstatic.cn")):
        raise BusinessError("invalid_image_url")
    return url


def prepare(token, product, stock, subject=None, multiplier=5, costs=None, policy_version=POLICY_VERSION):
    if policy_version != POLICY_VERSION:
        raise BusinessError("update_required")
    if type(stock) is not int or not 0 <= stock <= 100000:
        raise BusinessError("invalid_stock")
    if product.get("currency") != "CNY":
        raise BusinessError("switch_ozon_to_cny")
    price = positive(product.get("green_price"))
    store_goods = api(token, "GET", PRICES, "/api/v2/list/goods/filter?limit=1").get("data", {}).get("listGoods", [])
    if store_goods and any(g.get("currencyIsoCode4217") != "CNY" for g in store_goods):
        raise BusinessError("store_currency_mismatch")
    if not product.get("source_evidence", {}).get("dimensions") or not product.get("source_evidence", {}).get("weight"):
        raise BusinessError("missing_product_evidence")
    dims = {k: positive(product.get(k + "_cm")) for k in ("length", "width", "height")}
    weight = positive(product.get("weight_g")) / 1000
    brand = source_brand(product)
    title = strip_brand(clean_title_and_text(product.get("title", ""), product["sku"]), brand)[:60].strip()
    if len(title) < 5 or not product.get("photos"):
        raise BusinessError("missing_product_facts")
    photos = list(dict.fromkeys(image_url(u) for u in product["photos"]))[:30]
    subject, category_source, schema = resolve_category(token, product, subject)
    props = {re.sub(r"\s+", " ", k.strip().lower()): v for k, v in product.get("properties", {}).items()}
    chars = []
    for field in schema:
        value = props.get(str(field.get("name", "")).strip().lower())
        if value is None or value == "":
            continue
        if field.get("charcType") == 4:
            # Unit conversion or guessing a numeric attribute is not allowed.
            if not re.fullmatch(r"\d+(?:[.,]\d+)?", str(value).strip()):
                continue
            value = float(str(value).replace(",", "."))
        else:
            value = value if isinstance(value, list) else [str(value)]
        chars.append({"id": field["charcID"], "value": value})
    overrides = product.get('wb_characteristics', [])
    if overrides:
        if not product.get('mapping_evidence'):
            raise BusinessError('characteristics_evidence_required')
        merged = {c['id']: c for c in chars}
        for c in overrides:
            merged[c['id']] = c
        chars = list(merged.values())
    size = product.get('wb_size', {})
    if not isinstance(size, dict) or set(size) - {'techSize', 'wbSize'} or any(not isinstance(v, str) or not v for v in size.values()):
        raise BusinessError('invalid_size')
    validate_characteristics(schema, chars)
    try:
        pricing = build_price_plan(price, multiplier, costs)
    except ValueError as exc:
        raise BusinessError(str(exc)) from None
    description = strip_brand(clean_title_and_text(product.get("description") or title, product["sku"]), brand)[:1900]
    return {"sku": product["sku"], "stock": stock, "photos": photos, **pricing,
            "subjectID": subject, "category_policy_version": CATEGORY_POLICY, "category_source": category_source,
            "title": title, "description": description, "brand": brand,
            "dimensions": {**{k: math.ceil(v) for k, v in dims.items()}, "weightBrutto": math.ceil(weight * 1000) / 1000},
            "characteristics": chars, "size": product.get("wb_size", {})}


def card_lookup(token, vendor):
    result = api(token, "POST", CONTENT, "/content/v2/get/cards/list", {
        "settings": {"cursor": {"limit": 100}, "filter": {"withPhoto": -1, "textSearch": vendor}}})
    return next((c for c in result.get("cards", []) if c.get("vendorCode") == vendor), None)


def prices(token, nm):
    goods = api(token, "GET", PRICES, f"/api/v2/list/goods/filter?limit=100&filterNmID={nm}").get("data", {}).get("listGoods", [])
    return next((g for g in goods if g.get("nmID") == nm), None)


def relay_image(token, nm, index, url):
    # A bounded, in-memory relay only. No files, bucket uploads, or redirect following.
    try:
        with build_opener(NoRedirect()).open(Request(image_url(url), headers={"User-Agent": "WB-Image-Relay/1.0"}), timeout=15) as response:
            mime = response.headers.get_content_type()
            data = response.read(10 * 1024 * 1024 + 1)
    except (HTTPError, URLError, TimeoutError, OSError):
        raise BusinessError("image_unavailable") from None
    if len(data) > 10 * 1024 * 1024 or mime not in ("image/jpeg", "image/png", "image/webp"):
        raise BusinessError("invalid_image")
    boundary = uuid.uuid4().hex
    payload = (f'--{boundary}\r\nContent-Disposition: form-data; name="uploadfile"; filename="photo.jpg"\r\nContent-Type: {mime}\r\n\r\n'.encode()
               + data + f"\r\n--{boundary}--\r\n".encode())
    api(token, "POST", CONTENT, "/content/v3/media/file", extra={"Content-Type": f"multipart/form-data; boundary={boundary}", "X-Nm-Id": str(nm), "X-Photo-Number": str(index + 1)}, raw=payload)



def price_matches(goods, plan, discount):
    if not goods or goods.get('currencyIsoCode4217') != 'CNY' or goods.get('discount') != discount or goods.get('clubDiscount') != 0:
        return False
    expected = number(plan['strike_price']) * (100 - discount) / 100
    try:
        floor = minimum_sale(plan['costs'], plan['strike_price'])
        return bool(goods.get('sizes')) and all(number(s.get('price')) == number(plan['strike_price']) and
            abs(number(s.get('discountedPrice')) - expected) < Decimal('.01') and
            number(s.get('discountedPrice')) >= floor and
            (s.get('clubDiscountedPrice') is None or number(s['clubDiscountedPrice']) == number(s['discountedPrice'])) for s in goods['sizes'])
    except ValueError:
        return False



def check_quarantine(token, nm):
    offset = 0
    while True:
        rows = api(token, 'GET', PRICES, f'/api/v2/quarantine/goods?limit=1000&offset={offset}').get('data', {}).get('quarantineGoods', [])
        if any(g.get('nmID') == nm for g in rows):
            raise BusinessError('price_quarantined')
        if len(rows) < 1000:
            return
        offset += len(rows)

def price_task(token, upload_id, nm):
    try:
        status = api(token, 'GET', PRICES, f'/api/v2/history/tasks?uploadID={upload_id}').get('data') or {}
    except BusinessError as exc:
        if str(exc) != 'wb_request_rejected':
            raise
        try:
            pending = api(token, 'GET', PRICES, f'/api/v2/buffer/tasks?uploadID={upload_id}').get('data') or {}
        except BusinessError as exc:
            if str(exc) == 'wb_request_rejected':
                return 'pending'
            raise
        if pending.get('status') in (1, 2):
            return 'pending'
        raise BusinessError('price_task_unverified') from None
    if status.get('status') in (4, 6):
        check_quarantine(token, nm)
        raise BusinessError('price_task_failed')
    if status.get('status') not in (3, 5):
        return 'pending'
    offset, found = 0, False
    while True:
        rows = api(token, 'GET', PRICES, f'/api/v2/history/goods/task?uploadID={upload_id}&limit=1000&offset={offset}').get('data', {}).get('historyGoods', [])
        items = [g for g in rows if g.get('nmID') == nm]
        found = found or bool(items)
        if any(item.get('errors') or item.get('errorText') for item in items):
            check_quarantine(token, nm)
            raise BusinessError('price_task_failed')
        if len(rows) < 1000:
            if found:
                return 'success'
            raise BusinessError('price_task_unverified')
        offset += len(rows)


def protect_stock(token, job, reason):
    api(token, 'PUT', MARKET, f'/api/v3/stocks/{job["warehouse_id"]}', {'stocks': [{'sku': job['barcode'], 'amount': 0}]})
    stocks = api(token, 'POST', MARKET, f'/api/v3/stocks/{job["warehouse_id"]}', {'skus': [job['barcode']]}).get('stocks', [])
    if not any(s.get('sku') == job['barcode'] and s.get('amount') == 0 for s in stocks):
        raise BusinessError('stock_protection_failed')
    return {'phase': 'needs_review', 'error': reason, 'stock_protected': True, 'buyer_status': 'stock_disabled'}


def monitor_result(token, job, goods):
    plan = job['plan']
    if price_matches(goods, plan, job.get('active_discount', 30)):
        return {'phase': 'written', 'actual_price_cny': float(goods['sizes'][0]['discountedPrice']),
                'actual_discount': goods['discount'], 'price_verified_at': datetime.now(timezone.utc).isoformat()}
    try:
        safe = goods and goods.get('currencyIsoCode4217') == 'CNY' and goods.get('clubDiscount') == 0 and bool(goods.get('sizes')) and all(
            number(s.get('discountedPrice')) >= minimum_sale(plan['costs'], s.get('price')) and (s.get('clubDiscountedPrice') is None or number(s['clubDiscountedPrice']) >= minimum_sale(plan['costs'], s.get('price'))) for s in goods['sizes'])
    except ValueError:
        safe = False
    if not safe:
        return protect_stock(token, job, 'price_below_floor')
    return {'phase': 'needs_review', 'error': 'external_price_change', 'actual_price_cny': float(goods['sizes'][0]['discountedPrice']),
            'actual_discount': goods.get('discount'), 'price_verified_at': datetime.now(timezone.utc).isoformat()}

def step(token, job, phase):
    plan, vendor = job["plan"], job["vendor_code"]
    if plan.get("pricing_policy_version") != POLICY_VERSION:
        raise BusinessError("update_required")
    validate_category_plan(plan)
    if phase == 'protect':
        return protect_stock(token, job, job.get('error') or 'price_readback_mismatch')
    if phase in ("allocate", "create", "stock"):
        validate_characteristics(category_schema(token, plan["subjectID"], plan["category_source"]), plan["characteristics"])
    nm = job.get("nm_id")
    if nm:
        try:
            card = card_lookup(token, vendor)
            if phase in ('stock', 'verify', 'monitor', 'reprice', 'reprice_pending'):
                verify_card_content(card, plan)
            else:
                verify_card_category(card, plan)
        except BusinessError as exc:
            if str(exc) in ('category_readback_mismatch', 'brand_readback_mismatch', 'media_readback_mismatch', 'package_readback_mismatch', 'characteristics_mismatch', 'text_readback_mismatch') and phase in ("verify", "monitor", "reprice", "reprice_pending"):
                return protect_stock(token, job, str(exc))
            raise
    if phase == "allocate":
        if card_lookup(token, vendor):
            raise BusinessError("existing_card_requires_review")
        barcodes = api(token, "POST", CONTENT, "/content/v2/barcodes", {"count": 1}).get("data", [])
        if len(barcodes) != 1:
            raise BusinessError("barcode_unavailable")
        return {"phase": "create", "barcode": str(barcodes[0])}
    if phase == "create":
        if card_lookup(token, vendor):
            raise BusinessError("existing_card_requires_review")
        variant = {k: plan[k] for k in ("title", "description", "brand", "dimensions", "characteristics")}
        variant.update({"vendorCode": vendor, "sizes": [{**plan.get("size", {}), "skus": [job["barcode"]]}]})
        api(token, "POST", CONTENT, "/content/v2/cards/upload", [{"subjectID": plan["subjectID"], "variants": [variant]}])
        return {"phase": "card_pending"}
    if phase == "card_pending":
        card = card_lookup(token, vendor)
        if not card:
            return {"phase": "card_pending"}
        if job["barcode"] not in [s for size in card.get("sizes", []) for s in size.get("skus", [])]:
            raise BusinessError("existing_card_requires_review")
        verify_card_category(card, plan)
        if category_name(card.get('brand')) != category_name(plan['brand']):
            raise BusinessError('brand_readback_mismatch')
        if card.get("needKiz"):
            raise BusinessError("product_labeling_required")
        return {"phase": "media", "nm_id": card["nmID"]}
    if phase == "media":
        api(token, "POST", CONTENT, "/content/v3/media/save", {"nmId": nm, "data": plan["photos"]})
        return {"phase": "media_pending", "media_checks": 0}
    if phase == "media_pending":
        card = card_lookup(token, vendor)
        if photos_ready(card, plan):
            return {"phase": "price"}
        count = job.get("media_checks", 0) + 1
        if count >= 20 and job.get('relay_completed'):
            raise BusinessError('media_readback_mismatch')
        return {"phase": "relay" if count >= 5 and not job.get('relay_completed') else "media_pending", "media_checks": count, "relay_index": 0}
    if phase == "relay":
        index = job.get("relay_index", 0)
        relay_image(token, nm, index, plan["photos"][index])
        done = index + 1 == len(plan['photos'])
        return {"phase": "media_pending" if done else "relay", "relay_index": index + 1,
                "relay_completed": done, "media_checks": 0}
    target_discount = 50 if phase in ('reprice', 'reprice_pending') else job.get('active_discount', 30)
    if phase == 'club':
        result = api(token, 'POST', PRICES, '/api/v2/upload/task/club-discount', {'data': [{'nmID': nm, 'clubDiscount': 0}]})
        task = result.get('data', {}).get('id')
        if type(task) is not int or task <= 0:
            raise BusinessError('price_task_unverified')
        return {'phase': 'club_pending', 'price_task_id': task}
    if phase == 'club_pending':
        if job.get('price_task_id') and price_task(token, job['price_task_id'], nm) == 'pending':
            return {'phase': 'club_pending'}
        goods = prices(token, nm)
        return {'phase': 'price' if goods and goods.get('clubDiscount') == 0 else 'club_pending'}
    if phase in ('price', 'reprice'):
        goods = prices(token, nm)
        if not goods:
            return {'phase': phase}
        if goods.get('currencyIsoCode4217') != 'CNY':
            raise BusinessError('store_currency_mismatch')
        if phase == 'reprice' and not price_matches(goods, plan, job.get('active_discount', 30)):
            return monitor_result(token, job, goods)
        if goods.get('clubDiscount') != 0:
            if phase == 'price' and type(goods.get('clubDiscount')) is int and 0 < goods['clubDiscount'] <= 100:
                return {'phase': 'club'}
            raise BusinessError('seller_discount_stacking')
        result = api(token, 'POST', PRICES, '/api/v2/upload/task', {'data': [{'nmID': nm, 'price': plan['strike_price'], 'discount': target_discount}]})
        upload_id = result.get('data', {}).get('id')
        if type(upload_id) is not int or upload_id <= 0:
            raise BusinessError('price_task_unverified')
        return {'phase': 'reprice_pending' if phase == 'reprice' else 'price_pending', 'price_task_id': upload_id}
    if phase in ('price_pending', 'reprice_pending'):
        if not job.get('price_task_id'):
            # Crash/timeout: reconcile the exact target without resubmitting.
            result = "success" if price_matches(prices(token, nm), plan, target_discount) else "pending"
        else:
            result = price_task(token, job['price_task_id'], nm)
        if result == 'pending':
            return {'phase': phase}
        goods = prices(token, nm)
        if not price_matches(goods, plan, target_discount):
            return {'phase': phase}
        verified = {'actual_price_cny': float(goods['sizes'][0]['discountedPrice']),
                    'actual_discount': target_discount, 'price_verified_at': datetime.now(timezone.utc).isoformat()}
        if phase == 'reprice_pending':
            return {**verified, 'phase': 'written', 'active_discount': 50, 'reprice_completed': True}
        return {**verified, 'phase': 'stock', 'active_discount': 30}
    if phase == 'stock':
        # Recheck immediately before exposing stock, not just in the preceding step.
        if not price_matches(prices(token, nm), plan, 30):
            raise BusinessError('price_readback_mismatch')
        api(token, 'PUT', MARKET, f'/api/v3/stocks/{job["warehouse_id"]}', {'stocks': [{'sku': job['barcode'], 'amount': plan['stock']}]})
        return {'phase': 'verify'}
    if phase == 'monitor':
        return monitor_result(token, job, prices(token, nm))
    if phase == 'verify':
        card = card_lookup(token, vendor)
        goods = prices(token, nm)
        stocks = api(token, 'POST', MARKET, f'/api/v3/stocks/{job["warehouse_id"]}', {'skus': [job['barcode']]}).get('stocks', [])
        if not price_matches(goods, plan, job.get('active_discount', 30)):
            return protect_stock(token, job, 'price_readback_mismatch')
        verify_card_content(card, plan)
        if not any(s.get('sku') == job['barcode'] and s.get('amount') == plan['stock'] for s in stocks):
            return {'phase': 'verify'}
        return {'phase': 'written', 'buyer_status': 'pending_confirmation',
                'actual_price_cny': float(goods['sizes'][0]['discountedPrice']), 'actual_discount': job.get('active_discount', 30),
                'price_verified_at': datetime.now(timezone.utc).isoformat()}
    raise BusinessError('invalid_job_phase')
