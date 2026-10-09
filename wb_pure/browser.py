"""Visible persistent local browser. No proxy rotation or cloud capture."""
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from .ozon import parse_product, parse_pdp, _text
from .pricing import SOURCE_PRICE_POLICY, validate_source_price

MAX_HTML = 12 * 1024 * 1024


def sku_id(value):
    value = str(value).strip()
    if re.fullmatch(r'\d{6,14}', value):
        return value
    p = urlsplit(value)
    if p.scheme == 'https' and p.hostname in ('ozon.ru', 'www.ozon.ru'):
        m = re.fullmatch(r'/product/(?:[^/]*-)?(\d{6,14})/(?:features/)?', p.path)
        if m:
            return m[1]
    raise ValueError('invalid_ozon_sku_or_url')


def page_problem(html):
    text = _text(re.sub(r'<(script|style)\b[^>]*>.*?</\1>', '', html, flags=re.S | re.I)).lower()
    for pattern, code in (
        (r'похоже, нет соединения|err_connection|err_proxy|this site can.t be reached', 'ozon_connection_unavailable'),
        (r'выключите\s+vpn|отключите\s+vpn|access denied|доступ ограничен', 'ozon_access_restricted'),
        (r'подтвердите,? что вы (?:не робот|человек)|проверка безопасности|verify.*human|slide the slider|滑动滑块|请确认您不是机器人', 'ozon_verification_required'),
    ):
        if re.search(pattern, text):
            return code
    return None


def from_capture(record):
    sku = sku_id(record['sku'])
    pages = record.get('pages', [])
    if len(pages) != 2:
        raise ValueError('two_fresh_pages_required')
    pdp, features, pdp_page = None, None, None
    for page in pages:
        if sku_id(page['url']) != sku:
            raise ValueError('capture_sku_mismatch')
        age = time.time() - float(page['captured_at'])
        if not 0 <= age <= 86400 or len(page['html'].encode()) > MAX_HTML:
            raise ValueError('capture_expired_or_too_large')
        if problem := page_problem(page['html']):
            raise ValueError(problem)
        if urlsplit(page['url']).path.endswith('/features/'):
            features = page['html']
        else:
            pdp = page['html']
            pdp_page = page
    if pdp is None or features is None:
        raise ValueError('product_and_features_required')
    result = parse_product(sku, pdp, features)
    check = record.get('currency_check')
    if not isinstance(check, dict):
        raise ValueError('ozon_native_cny_evidence_required')
    feature_price = parse_pdp(sku, features)
    has_feature_price = feature_price.get('green_price') is not None or re.search(r'id=["\']state-webPrice-\d+-default-\d+["\']', features)
    if has_feature_price and feature_price.get('currency') != 'CNY':
        raise ValueError('ozon_currency_reverted')
    if has_feature_price and feature_price.get('green_price') != result['product']['green_price']:
        raise ValueError('ozon_green_price_changed')
    evidence = {**check, 'version': SOURCE_PRICE_POLICY, 'sku': sku,
                'captured_at': float(pdp_page['captured_at']),
                'green_price': result['product'].get('green_price_evidence')}
    validate_source_price(sku, result['product']['currency'], result['product']['green_price'], evidence)
    if not all(float(p['captured_at']) >= float(check['refreshed_at']) for p in pages):
        raise ValueError('capture_before_currency_reload')
    result['product']['currency_evidence'] = evidence
    result['product']['captured_at'] = min(float(p['captured_at']) for p in pages)
    return result


def currency_dialog(page):
    title = page.get_by_text(re.compile(r'^(Язык и валюта|语言和货币|Language and currency)$')).filter(visible=True)
    if not title.count():
        entry = page.get_by_text(re.compile(r'^(RU|EN|ZH|CN)$')).filter(visible=True)
        if entry.count() != 1:
            raise ValueError('ozon_currency_layout_changed')
        entry.click(timeout=5000)
    title.wait_for(state='visible', timeout=5000)
    dialog = title.locator('xpath=ancestor::*[.//input and .//button][1]')
    inputs = [i for i in dialog.locator('input:visible').all()
              if re.search(r'(?:,|\s)(RUB|CNY|USD|EUR|CHF|BYN|KZT)\s*$', i.locator('..').inner_text())]
    if len(inputs) != 1:
        raise ValueError('ozon_currency_layout_changed')
    return title, dialog, inputs[0]


def select_cny(page, after_reload=None):
    title, dialog, field = currency_dialog(page)
    if not re.search(r'\bCNY\s*$', field.locator('..').inner_text()):
        field.fill('CNY')
        option = page.get_by_text('CNY', exact=True).filter(visible=True)
        option.click(timeout=5000)
    if not re.search(r'\bCNY\s*$', field.locator('..').inner_text()):
        raise ValueError('ozon_currency_unverified')
    saved = time.time()
    dialog.get_by_role('button', name=re.compile(r'^(Сохранить|保存|Save)$')).click(timeout=5000)
    title.wait_for(state='hidden', timeout=8000)
    page.reload(wait_until='domcontentloaded')
    page.wait_for_timeout(1500)
    if after_reload:
        after_reload()
    # Read the saved preference again after reload; a click alone is insufficient.
    title, dialog, field = currency_dialog(page)
    selected = field.locator('..').inner_text()
    if not re.search(r'\bCNY\s*$', selected):
        raise ValueError('ozon_currency_reverted')
    dialog.get_by_role('button', name=re.compile(r'^(Сохранить|保存|Save)$')).click(timeout=5000)
    title.wait_for(state='hidden', timeout=8000)
    page.reload(wait_until='domcontentloaded')
    page.wait_for_timeout(1500)
    if after_reload:
        after_reload()
    return {'currency': 'CNY', 'selected_currency_text': selected,
            'saved_at': saved, 'refreshed_at': time.time()}


def visible_price_text(page, product):
    evidence = product.get('green_price_evidence')
    if product.get('currency') != 'CNY' or not evidence:
        raise ValueError('ozon_native_cny_evidence_required')
    widgets = page.locator('[data-widget="webPrice"]').filter(visible=True)
    texts = [w.inner_text() for w in widgets.all()]
    compact = lambda text: re.sub(r'\s+', '', text)
    raw = compact(evidence['raw'])
    matches = [text for text in texts if re.search(r'(?<![\d.,])' + re.escape(raw) + r'(?![\d.,])', compact(text))]
    if len(matches) != 1:
        raise ValueError('ozon_visible_green_price_unverified')
    return matches[0]


class Browser:
    def __init__(self, home, manual_wait=180):
        self.home, self.manual_wait = Path(home), manual_wait

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self.driver = sync_playwright().start()
        try:
            self.context = self.driver.chromium.launch_persistent_context(
                str(self.home / 'browser'), headless=False, viewport={'width': 1440, 'height': 1000})
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.set_default_timeout(10000)
            return self
        except Exception:
            self.driver.stop()
            raise

    def __exit__(self, *_):
        self.context.close()
        self.driver.stop()

    def html(self):
        deadline = time.monotonic() + self.manual_wait
        prompted = False
        while True:
            html = self.page.content()
            problem = page_problem(html)
            if problem != 'ozon_verification_required':
                if problem:
                    raise ValueError(problem)
                return html
            if not prompted:
                print('请在已打开的 Ozon 浏览器完成人工验证；程序正在等待。', flush=True)
                prompted = True
            if time.monotonic() >= deadline:
                raise ValueError(problem)
            self.page.wait_for_timeout(1500)

    def capture(self, sku):
        sku = sku_id(sku)
        page = self.page
        page.goto(f'https://www.ozon.ru/product/{sku}/', wait_until='domcontentloaded', timeout=45000)
        page.wait_for_timeout(1800)
        self.html()
        for attempt in range(2):
            try:
                # Always set/save/reload in this browser, including apparently CNY pages.
                check = select_cny(page, self.html)
                pdp = {'url': page.url, 'captured_at': time.time(), 'html': self.html()}
                check['visible_price_text'] = visible_price_text(page, parse_pdp(sku, pdp['html']))
                break
            except Exception as exc:
                if attempt == 1:
                    if isinstance(exc, ValueError):
                        raise
                    raise ValueError('ozon_currency_unverified') from None
        page.goto(f'https://www.ozon.ru/product/{sku}/features/', wait_until='domcontentloaded', timeout=45000)
        page.wait_for_timeout(1500)
        features = {'url': page.url, 'captured_at': time.time(), 'html': self.html()}
        record = {'sku': sku, 'currency_check': check, 'pages': [pdp, features]}
        result = from_capture(record)
        folder = self.home / 'captures'
        folder.mkdir(exist_ok=True)
        path = folder / f'{sku}.json'
        path.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
        return result
