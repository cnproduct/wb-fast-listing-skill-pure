"""Real Chromium test on a synthetic currency dialog; no external marketplace calls."""
from playwright.sync_api import sync_playwright
import json
import tempfile
from pathlib import Path
from wb_pure.browser import select_cny, Browser, from_capture
from wb_pure.pricing import build_price_plan

HTML = '''<html><meta charset="utf-8"><body><button id="entry" onclick="document.querySelector('section').hidden=false">RU</button>
<section hidden><h2>Язык и валюта</h2><div><input oninput="document.querySelector('#option').hidden=false"><span id="value">Российский рубль, RUB</span></div>
<button onclick="localStorage.setItem('currency',document.querySelector('#value').textContent);document.querySelector('section').hidden=true">Сохранить</button></section>
<div id="option" hidden onclick="document.querySelector('#value').textContent='Китайский юань, CNY';this.hidden=true">CNY</div>
<script>document.querySelector('#value').textContent=localStorage.getItem('currency')||'Российский рубль, RUB';localStorage.setItem('loads',Number(localStorage.getItem('loads')||0)+1);</script></body></html>'''

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.route('https://currency.test/**', lambda route: route.fulfill(body=HTML, content_type='text/html'))
    page.goto('https://currency.test/product/')
    select_cny(page)
    assert page.locator('#value').text_content().endswith('CNY')
    assert page.evaluate("localStorage.getItem('loads')") == '3'
    select_cny(page)
    assert page.locator('#value').text_content().endswith('CNY')
    assert page.evaluate("localStorage.getItem('loads')") == '5'

    # Exercise Browser.capture end to end. The simulated RUB price is 9999,
    # while the native CNY widget is 100; no FX conversion is involved.
    product = {'@type': 'Product', 'sku': '123456789', 'name': 'Test product',
               'image': ['https://cdn1.ozone.ru/s3/test.jpg']}
    fixture = HTML.replace('</body>', '''
<script type="application/ld+json">PRODUCT_JSON</script>
<div id="state-webPrice-123456-default-1" data-state=""></div>
<div data-widget="webPrice" id="visible-price"></div>
<p>Размер упаковки: 20 x 10 x 5 см</p><p>Вес брутто (г): 450</p>
<script>
const cny=(localStorage.getItem('currency')||'').endsWith('CNY');
const raw=cny?'100 ¥':'9999 ₽';
document.querySelector('[data-state]').setAttribute('data-state',JSON.stringify({
 cardPrice:raw,price:cny?'120 ¥':'11000 ₽',
 disclaimerPriceHeader:cny?'Итого CNY':'Итого RUB',
 link:'/modal/pdpListOfBanks?product_id=123456789'}));
document.querySelector('#visible-price').textContent=raw+' С банками';
if(localStorage.getItem('stale-visible'))document.querySelector('#visible-price').textContent='9999 ₽ С банками';
if(localStorage.getItem('revert-features')&&location.pathname.endsWith('/features/')){
 document.querySelector('[data-state]').setAttribute('data-state',JSON.stringify({sku:'123456789',cardPrice:'9999 RUB'}));}
</script></body>''').replace('PRODUCT_JSON', json.dumps(product))
    page.route('https://www.ozon.ru/**', lambda route: route.fulfill(body=fixture, content_type='text/html'))
    page.goto('https://www.ozon.ru/product/123456789/')
    with tempfile.TemporaryDirectory() as directory:
        controlled = Browser(Path(directory))
        controlled.page = page
        result = controlled.capture('123456789')
        assert result['ready_for_listing']
        assert (result['product']['currency'], result['product']['green_price']) == ('CNY', 100)
        plan = build_price_plan(result['product']['green_price'], 5)
        assert (plan['strike_price'], plan['sale_price'], plan['final_sale_price']) == (500, 350, 250)
        record = json.loads((Path(directory)/'captures/123456789.json').read_text())
        assert from_capture(record)['product']['green_price'] == 100
        missing = {**record, 'currency_check': None}
        try:
            from_capture(missing)
        except ValueError as exc:
            assert str(exc) == 'ozon_native_cny_evidence_required'
        else:
            raise AssertionError('unverified imported capture accepted')
        for flag, code in (('stale-visible', 'ozon_visible_green_price_unverified'),
                           ('revert-features', 'ozon_currency_reverted')):
            page.evaluate('(flag)=>localStorage.setItem(flag,"1")', flag)
            try:
                controlled.capture('123456789')
            except ValueError as exc:
                assert str(exc) == code, (flag, str(exc))
            else:
                raise AssertionError('unsafe currency capture accepted: '+flag)
            page.evaluate('(flag)=>localStorage.removeItem(flag)', flag)
        page.evaluate('localStorage.removeItem("currency")')
        reverted = fixture.replace("localStorage.setItem('currency',document.querySelector('#value').textContent)", 'void(0)')
        page.unroute('https://www.ozon.ru/**')
        page.route('https://www.ozon.ru/**', lambda route: route.fulfill(body=reverted, content_type='text/html'))
        try:
            controlled.capture('123456789')
        except ValueError as exc:
            assert str(exc) == 'ozon_currency_reverted'
        else:
            raise AssertionError('save/reload reverted to RUB but was accepted')
    browser.close()
print('Chromium: RUB → native CNY capture and price plan passed; missing proof, stale visible RUB, features and save/reload reversion blocked (synthetic pages).')
