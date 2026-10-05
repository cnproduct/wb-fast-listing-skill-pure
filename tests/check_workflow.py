"""Run: python -m unittest discover -s tests -p 'check_workflow.py'. No live writes."""
import copy
import hashlib
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from wb_pure import wb
from wb_pure.pricing import build_price_plan, next_discount_at, vendor_code
from wb_pure.state import State, home_dir
from wb_pure.runner import tick
from wb_pure.browser import sku_id, from_capture

PRODUCT = dict(sku='123456789', title='Acme Стальная кухонная подставка', description='Acme надежная подставка',
               currency='CNY', green_price=100, source_brand='Acme', photos=['https://cdn1.ozone.ru/s3/test.jpg'],
               source_evidence={'dimensions':'Упаковка: 20 x 10 x 5 см', 'weight':'Вес брутто: 450 г'},
               length_cm=20, width_cm=10, height_cm=5, weight_g=450,
               properties={'Тип':'Подставки', 'Материал':'сталь'})


class FakeWB:
    def __init__(self):
        self.card = None
        self.goods = {'nmID':999, 'currencyIsoCode4217':'CNY', 'clubDiscount':0, 'sizes':[], 'discount':0}
        self.stocks = []
        self.calls = []
        self.fail_create = False
        self.fail_price = False
        self.history_fail = False
        self.indexed = True

    def __call__(self, token, method, base, path, body=None, **kw):
        self.calls.append((method, path, copy.deepcopy(body)))
        hook = wb.BEFORE_WRITE.get()
        if hook:
            hook(method, path)
        if '/object/all?' in path:
            return {'data':[{'subjectID':123, 'subjectName':'Подставки'}]}
        if '/object/charcs/' in path:
            return {'data':[{'subjectID':123, 'subjectName':'Подставки', 'charcID':10, 'name':'Материал', 'required':True, 'charcType':1}]}
        if path.endswith('/cards/list'):
            return {'cards':[self.card] if self.card else []}
        if path.endswith('/barcodes'):
            return {'data':['1234567890123']}
        if path.endswith('/cards/upload'):
            self.card = {**copy.deepcopy(body[0]['variants'][0]), 'nmID':999, 'subjectID':123, 'photos':[]}
            if self.fail_create:
                raise wb.BusinessError('wb_unavailable')
            return {}
        if path.endswith('/media/save'):
            self.card['photos'] = [{'big':u} for u in body['data']]
            return {}
        if path.endswith('/goods/filter?limit=1'):
            return {'data':{'listGoods':[]}} # A new empty store is not falsely rejected.
        if '/goods/filter?' in path:
            return {'data':{'listGoods':[self.goods] if self.indexed else []}}
        if path.endswith('/upload/task'):
            row = body['data'][0]
            self.goods.update(discount=row['discount'], sizes=[{'price':row['price'], 'discountedPrice':round(row['price'] * (100-row['discount'])/100, 2)}])
            if self.fail_price:
                raise wb.BusinessError('wb_unavailable')
            return {'data':{'id':42}}
        if path.endswith('/club-discount'):
            self.goods['clubDiscount']=0
            return {'data':{'id':43}}
        if '/history/tasks?' in path:
            return {'data':{'status':4 if self.history_fail else 3}}
        if '/history/goods/task?' in path:
            return {'data':{'historyGoods':[{'nmID':999, 'errorText':''}]}}
        if '/quarantine/goods?' in path:
            return {'data':{'quarantineGoods':[]}}
        if '/stocks/' in path:
            if method == 'PUT':
                self.stocks = copy.deepcopy(body['stocks'])
                return {}
            return {'stocks':self.stocks}
        raise AssertionError(path)


class Workflow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = home_dir(self.tmp.name)
        self.state = State(self.home)
        self.state.bind({'seller_id':'synthetic', 'warehouse_id':23, 'token_fingerprint':hashlib.sha256(b'fake').hexdigest()})
        self.fake = FakeWB()
        self.mock = patch.object(wb, 'api', side_effect=self.fake)
        self.mock.start()
        self.now = time.time()

    def tearDown(self):
        self.mock.stop()
        self.state.db.close()
        self.tmp.cleanup()

    def new_job(self, product=None):
        p = wb.prepare('fake', product or copy.deepcopy(PRODUCT), 5)
        j = {'sku':PRODUCT['sku'], 'phase':'allocate', 'approved':True, 'created_at':self.now,
             'plan':p, 'warehouse_id':23, 'vendor_code':vendor_code(PRODUCT['sku'], self.now)}
        self.state.save(j)
        return j

    def complete(self):
        for i in range(40):
            tick(self.state, 'fake', self.now + i*60)
            j = self.state.get(PRODUCT['sku'])
            if j['phase'] in ('written','needs_review'):
                return j
        self.fail(str(j))

    def test_full_pipeline_restart_and_next_day(self):
        job = self.new_job()
        self.assertEqual(job['plan']['title'], 'Стальная кухонная подставка')
        self.assertEqual(job['plan']['brand'], 'Acme')
        first = self.complete()
        self.assertEqual((first['phase'],first['actual_price_cny'],first['actual_discount']), ('written',350,30))
        self.state.db.close()
        self.state = State(self.home)
        tick(self.state,'fake',first['reprice_at']+10)
        tick(self.state,'fake',first['reprice_at']+80)
        final = self.state.get(PRODUCT['sku'])
        self.assertEqual((final['actual_price_cny'],final['actual_discount']), (250,50))
        self.assertTrue(final['reprice_completed'])
        self.assertEqual(final['vendor_code'], job['vendor_code'])
        writes = [c for c in self.fake.calls if c[1].endswith('/upload/task')]
        self.assertEqual([c[2]['data'][0]['discount'] for c in writes],[30,50])
        self.assertEqual([c[2]['data'][0]['price'] for c in writes],[500,500])
        self.fake.goods['sizes'][0]['discountedPrice'] = 100
        tick(self.state,'fake',first['reprice_at']+2000)
        self.assertEqual(self.state.get(PRODUCT['sku'])['phase'],'needs_review')
        self.assertEqual(self.fake.stocks[0]['amount'],0)

    def test_lost_create_and_price_responses_never_duplicate(self):
        self.new_job()
        self.fake.fail_create = self.fake.fail_price = True
        j = self.complete()
        self.assertEqual(j['phase'],'written')
        for path in ('/cards/upload','/upload/task'):
            self.assertEqual(len([c for c in self.fake.calls if c[1].endswith(path)]),1)

    def test_not_indexed_and_failed_task_never_release_stock(self):
        self.new_job()
        self.fake.indexed = False
        for i in range(8):
            tick(self.state,'fake',self.now+i*60)
        self.assertEqual(self.state.get(PRODUCT['sku'])['phase'],'price')
        self.assertFalse(self.fake.stocks)
        self.fake.indexed = True
        self.fake.history_fail = True
        tick(self.state,'fake',self.now+600)
        tick(self.state,'fake',self.now+700)
        self.assertEqual(self.state.get(PRODUCT['sku'])['phase'],'needs_review')
        self.assertFalse(self.fake.stocks)

    def test_native_currency_package_and_category_guards(self):
        for update in ({'currency':'RUB'}, {'green_price':float('nan')}, {'weight_g':None},
                       {'source_evidence':{}}, {'photos':['https://localhost/x']},
                       {'properties':{'Тип':'Guess'}}, {'source_brand_conflict':True}):
            with self.assertRaises((wb.BusinessError,ValueError)):
                wb.prepare('fake',{**PRODUCT,**update},5)
        mapped = {**PRODUCT, 'properties':{'Тип':'Подставка кухонная','Материал':'сталь'},
                  'category_mapping':{'source_type':'Подставка кухонная','subject_id':123,'subject_name':'Подставки',
                                      'confirmed_by':'test','evidence':'synthetic catalog review'}}
        self.assertEqual(wb.prepare('fake',mapped,5)['subjectID'],123)
        with self.assertRaises(wb.BusinessError):
            wb.prepare('fake',mapped,5,999)

    def test_multipliers_floor_dates_and_token_guard(self):
        for m, base, first, final in ((5,500,350,250),(5.5,550,385,275),(6,600,420,300),(10,1000,700,500)):
            p = build_price_plan(100,m)
            self.assertEqual((p['strike_price'],p['sale_price'],p['final_sale_price']),(base,first,final))
        with self.assertRaises(ValueError):
            build_price_plan(100,5,{'minimum_sale_price':251})
        for bad in (float('nan'),-1,True,0,5.555):
            with self.assertRaises(ValueError):
                build_price_plan(100,bad)
        t = datetime(2026,9,29,20,59,59,tzinfo=timezone.utc).timestamp()
        self.assertEqual(next_discount_at(t),t+1)
        self.assertEqual(vendor_code('123456789',t),'oz20260930-123456789')
        self.new_job()
        with self.assertRaisesRegex(ValueError,'token_changed'):
            tick(self.state,'other-token')

    def test_input_boundary_and_dedup(self):
        from wb_pure.__main__ import sku_list
        self.assertEqual(sku_list(['123456789','https://www.ozon.ru/product/test-123456789/']),['123456789'])
        for url in ('https://evil.example/product/123456789/','https://ozon.ru.evil.example/product/123456789/'):
            with self.assertRaises(ValueError):
                sku_id(url)
        with self.assertRaises(ValueError):
            from_capture({'sku':'123456789','pages':[]})


if __name__ == '__main__':
    unittest.main()
