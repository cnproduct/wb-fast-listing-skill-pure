"""Real Chromium test on a synthetic currency dialog; no external marketplace calls."""
from playwright.sync_api import sync_playwright
from wb_pure.browser import select_cny

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
    assert page.evaluate("localStorage.getItem('loads')") == '2'
    select_cny(page)
    assert page.locator('#value').text_content().endswith('CNY')
    assert page.evaluate("localStorage.getItem('loads')") == '3'
    browser.close()
print('Chromium: select CNY, save, reload, persist, repeat passed (synthetic page).')
