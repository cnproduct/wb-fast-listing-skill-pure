"""Small offline check for truth-only Ozon parsing."""

import html
import json

from wb_pure.ozon import parse_features, parse_pdp, parse_product


def main():
    # Native Ozon description labels observed on the replacement pilot SKUs.
    for weight, dimensions, expected in (
        ("1.64", "27.1x22.5x9.5", (27.1, 22.5, 9.5, 1640)),
        ("1.0", "33.7 x 12.0 x 9.5", (33.7, 12.0, 9.5, 1000)),
    ):
        parsed = parse_features(f"<p>Вес брутто, (кг.): {weight}</p><p>Габариты упаковки, (см.): {dimensions}</p>")
        assert tuple(parsed[key] for key in ("length_cm", "width_cm", "height_cm", "weight_g")) == expected
    assert parse_features("Вес брутто (г): 317")["weight_g"] == 317
    assert parse_features("Вес брутто (кг): 1,64 кг")["weight_g"] == 1640
    for unknown in ("Вес брутто: 1.64", "Вес нетто, (кг.): 1.64", "Вес товара, (кг.): 1.64", "Вес брутто, (кг.): 1.64.0", "Вес брутто (кг): 1.64 г"):
        assert parse_features(unknown)["weight_g"] is None

    pdp = '''<html><script type="application/ld+json">{
      "@type":"Product","name":"Test product","description":"Useful item Ozon 123456789",
      "image":["https://cdn1.ozone.ru/s3/multimedia-a/test.jpg"],
      "offers":{"price":"1200"}}
      </script><script>{"cardPrice":"999 ₽","price":"1200 ₽"}</script></html>'''
    state = {"characteristics": [{"short": [
        {"name": "Размер упаковки", "values": [{"text": "20 x 10 x 5 см"}]},
        {"name": "Вес с упаковкой", "values": [{"text": "0,45 кг"}]},
    ], "long": []}]}
    features = f'<div data-state="{html.escape(json.dumps(state, ensure_ascii=False), quote=True)}"></div>'
    result = parse_product("123456789", pdp, features)
    assert result["ready_for_listing"] is False
    assert "unsupported_price_currency" in result["blockers"]
    assert parse_product("123456789", pdp.replace("₽", "CNY"), features)["ready_for_listing"] is True
    assert result["product"]["green_price"] == 999
    assert result["product"]["weight_g"] == 450
    assert result["product"]["description"] == "Useful item"

    blocked = parse_product("123456789", pdp, "<html></html>")
    assert blocked["ready_for_listing"] is False
    assert "missing_package_dimensions" in blocked["blockers"]
    assert blocked["product"]["length_cm"] is None

    localized_pdp = '''<html><script type="application/ld+json">{
      "@type":"Product","name":"Armed aspirator","description":"*毛重(单位):4.6公斤 *毛重:9.8公斤",
      "image":["https://ir-20.ozonstatic.cn/s3/multimedia-1-4/test.jpg"],
      "offers":{"price":"102.63","priceCurrency":"CHF"}}
      </script><div data-state="{&quot;cardPrice&quot;:&quot;92,36 Fr&quot;,&quot;price&quot;:&quot;102,63 Fr&quot;}"></div></html>'''
    localized_features = "<div>包装尺寸（长X宽x高），厘米: 35x27x29</div>"
    localized = parse_product("248438836", localized_pdp, localized_features)
    assert localized["product"]["green_price"] == 92.36
    assert localized["product"]["currency"] == "CHF"
    assert localized["product"]["photos"] == ["https://ir-20.ozonstatic.cn/s3/multimedia-1-4/test.jpg"]
    assert localized["product"]["weight_g"] == 4600
    assert [localized["product"][key] for key in ("length_cm", "width_cm", "height_cm")] == [35, 27, 29]
    assert localized["blockers"] == ["unsupported_price_currency"]

    # These are structured evidence cases, not a claim about an unobserved widget ID.
    target = '<script type="application/ld+json">' + json.dumps({
        "@type": "Product", "sku": "123456789", "name": "Target product",
        "offers": {"price": "120", "priceCurrency": "CNY"},
    }) + '</script>'
    def state_html(value):
        return '<div data-state="' + html.escape(json.dumps(value), quote=True) + '"></div>'
    recommendation = state_html({"sku": "987654321", "cardPrice": "999 RUB"})
    target_price = state_html({"sku": "123456789", "cardPrice": "100 CNY"})
    selected = parse_pdp("123456789", target + recommendation + target_price)
    assert (selected["green_price"], selected["currency"]) == (100, "CNY")
    ambiguous = parse_pdp("123456789", target + target_price + state_html({"sku": "123456789", "cardPrice": "110 CNY"}))
    assert ambiguous["green_price"] is None
    assert parse_pdp("123456789", target + recommendation)["green_price"] is None
    mixed = state_html({"items": [{"sku": "123456789"}, {"sku": "987654321"}], "price": {"cardPrice": "100 CNY"}})
    assert parse_pdp("123456789", target + mixed)["green_price"] is None
    unscoped = state_html({"cardPrice": "100 CNY"}) + state_html({"cardPrice": "110 CNY"})
    assert parse_pdp("123456789", target + unscoped)["green_price"] is None
    missing = parse_product("123456789", target, features)
    assert "missing_green_price" in missing["blockers"]
    for amount in ("100", "100 unknown", "100 ¥", "100 JPY"):
        unknown = parse_pdp("123456789", target + state_html({"cardPrice": amount}))
        assert unknown["green_price"] == 100
        assert unknown["currency"] != "CNY", "unrelated offer currency must not label the green price"
    explicit = parse_pdp("123456789", target + state_html({"sku": "123456789", "cardPrice": "100 ¥", "currency": "CNY"}))
    assert explicit["currency"] == "CNY", "same price object explicitly establishes the ambiguous symbol"
    for prices in ({'price': '1200 RUB'}, {'originalPrice': '1200 ₽'}, {'price': '1200 CNY RUB'}):
        stale = parse_pdp('123456789', target + state_html({'sku': '123456789', 'cardPrice': '999', 'currency': 'CNY', **prices}))
        assert stale['currency'] != 'CNY', 'CNY metadata must not relabel a conflicting RUB price object'
    assert parse_pdp('123456789', target + state_html({'cardPrice': '100 CNY'}))['green_price_evidence'] is None
    for amount in ("100 RUB", "100 RUB CNY", "100 unknown"):
        conflict = parse_pdp("123456789", target + state_html({"sku": "123456789", "cardPrice": amount, "currency": "CNY"}))
        assert conflict["currency"] == ""

    # Minimal structure captured after human verification on the real Ozon PDP.
    live_sku = "1681663190"
    live_product = '<script type="application/ld+json">' + json.dumps({
        "@type": "Product", "sku": live_sku, "name": "Plushair serum",
        "offers": {"price": "93.21", "priceCurrency": "CNY", "url": "https://www.ozon.ru/product/plushair-syvorotka-dlya-rosta-volos-5-loson-ot-vypadeniya-volos-buster-aktivator-dlya-rosta-borody-1681663190/"},
    }) + '</script>'
    live_state = {"cardPrice": "83,90\u2009¥", "price": "93,21\u2009¥", "originalPrice": "358,20\u2009¥",
                  "disclaimerPriceHeader": "总计 CNY", "link": "/modal/pdpListOfBanks?product_id=1681663190", "isAvailable": True}
    def live_widget(value, widget_id="state-webPrice-3121879-default-1"):
        return state_html(value).replace('<div ', '<div id="' + widget_id + '" ', 1)
    live = parse_pdp(live_sku, live_product + recommendation + live_widget(live_state))
    assert (live["green_price"], live["regular_price"], live["currency"]) == (83.9, 93.21, "CNY")
    for header in ("Итого CNY", "Всего: CNY", "Total CNY"):
        translated = parse_pdp(live_sku, live_product + live_widget({**live_state, "disclaimerPriceHeader": header}))
        assert (translated["green_price"], translated["currency"]) == (83.9, "CNY")
    other = live_widget({**live_state, "link": "/modal/pdpListOfBanks?product_id=987654321", "cardPrice": "5,00 ¥"})
    assert parse_pdp(live_sku, live_product + other + live_widget(live_state))["green_price"] == 83.9
    for changed in ({"cardPrice": "83,90 RUB"}, {"currency": "RUB"}, {"priceCurrency": "JPY"},
                    {"disclaimerPriceHeader": "总计 RUB"}, {"disclaimerPriceHeader": "总计 RUB", "cardPrice": "83,90 CNY"},
                    {"disclaimerPriceHeader": "Total CNY RUB"}, {"disclaimerPriceHeader": "Итого CNY JPY"},
                    {"disclaimerPriceHeader": "some unrelated CNY text"}):
        assert parse_pdp(live_sku, live_product + live_widget({**live_state, **changed}))["currency"] != "CNY"
    assert parse_pdp(live_sku, live_product + live_widget(live_state, "state-recommendation-1"))["currency"] == ""
    for link in ("/modal/pdpListOfBanks?product_id=987654321", "/modal/pdpListOfBanks?product_id=1681663190&product_id=987654321", "https://example.test/modal/pdpListOfBanks?product_id=1681663190", "https://["):
        assert parse_pdp(live_sku, live_product + live_widget({**live_state, "link": link}))["green_price"] is None


if __name__ == "__main__":
    main()
# Recommendation and review URLs cannot enter a SKU album; package units never default to grams/cm.
pdp = '<script type="application/ld+json">'+json.dumps({'@type':'Product','sku':'123456789','image':['https://cdn1.ozone.ru/s3/item.jpg']})+'</script>'
other = '<script type="application/ld+json">'+json.dumps({'@type':'Product','sku':'987654321','image':['https://cdn1.ozone.ru/s3/other.jpg']})+'</script><img src="https://cdn1.ozone.ru/s3/review.jpg">'
assert parse_pdp('123456789', pdp+other)['photos'] == ['https://cdn1.ozone.ru/s3/item.jpg']
assert not parse_pdp('123456789', other)['photos']
for name, value, expected in [('Вес брутто, кг','1.64',1640),('Вес брутто','1.64',None),('Вес брутто, кг','1.64 г',None)]:
    state={'characteristics':[{'short':[{'name':name,'values':[{'text':value}]}],'long':[]}]}
    record='<div data-state="'+html.escape(json.dumps(state),quote=True)+'"></div>'
    assert parse_features(record)['weight_g']==expected
assert parse_features('Габариты упаковки (мм): 200x100x50')['length_cm']==20
assert parse_features('Габариты упаковки: 200x100x50')['length_cm'] is None
print('Ozon: same-SKU images and explicit package unit conversion passed')
