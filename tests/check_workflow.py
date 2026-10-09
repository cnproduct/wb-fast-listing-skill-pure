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
PRODUCT['currency_evidence'] = {'version': 1, 'sku': PRODUCT['sku'], 'currency': 'CNY',
    'green_price': {'sku': PRODUCT['sku'], 'currency': 'CNY', 'amount': 100, 'raw': '100 CNY'},
    'selected_currency_text': 'Китайский юань, CNY', 'visible_price_text': '100 CNY С банками',
    'saved_at': time.time()-3, 'refreshed_at': time.time()-2, 'captured_at': time.time()-1}


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
        self.assertEqual(job['plan']['brand'], 'generic')
        first = self.complete()
        self.assertEqual(self.fake.card['brand'], 'generic')
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
                       {'properties':{'Тип':'Guess'}}):
            with self.assertRaises((wb.BusinessError,ValueError)):
                wb.prepare('fake',{**PRODUCT,**update},5)
        mapped = {**PRODUCT, 'properties':{'Тип':'Подставка кухонная','Материал':'сталь'},
                  'category_mapping':{'source_type':'Подставка кухонная','subject_id':123,'subject_name':'Подставки',
                                      'confirmed_by':'test','evidence':'synthetic catalog review'}}
        self.assertEqual(wb.prepare('fake',mapped,5)['subjectID'],123)
        with self.assertRaises(wb.BusinessError):
            wb.prepare('fake',mapped,5,999)

    def test_currency_proof_and_cached_price_plan_guards(self):
        for changes in ({'currency_evidence': None}, {'green_price': 9999}):
            with self.assertRaisesRegex(ValueError, 'ozon_native_cny_evidence_required'):
                wb.prepare('fake', {**PRODUCT, **changes}, 5)
        for changes in ({'visible_price_text': '9999 ₽ С банками'},
                        {'selected_currency_text': 'Российский рубль, RUB'},
                        {'refreshed_at': PRODUCT['currency_evidence']['saved_at']-1}):
            p = copy.deepcopy(PRODUCT)
            p['currency_evidence'].update(changes)
            with self.assertRaises(ValueError):
                wb.prepare('fake', p, 5)
        for phase in ('allocate', 'create', 'price', 'stock', 'written', 'reprice_pending'):
            job = self.new_job()
            job.update(phase=phase, reprice_at=self.now-1)
            job['plan'].pop('source_price_evidence')
            self.state.save(job)
            calls = len(self.fake.calls)
            tick(self.state, 'fake', self.now)
            self.assertEqual(self.state.get(job['sku'])['phase'], 'needs_review')
            self.assertEqual(len(self.fake.calls), calls)
        for changes in ({'strike_price': 50000}, {'source_price_cny': 9999},
                        {'source_currency': 'RUB'}, {'final_discount': 0}):
            job = self.new_job()
            job['plan'].update(changes)
            self.state.save(job)
            calls = len(self.fake.calls)
            tick(self.state, 'fake', self.now)
            self.assertEqual(self.state.get(job['sku'])['phase'], 'needs_review')
            self.assertEqual(len(self.fake.calls), calls)

    def test_legacy_prepared_plan_cannot_be_approved(self):
        from wb_pure.__main__ import approve
        job = self.new_job()
        job.update(phase='prepared', approved=False, product={'captured_at': time.time()})
        job['plan'].pop('source_price_evidence')
        self.state.save(job)
        with self.assertRaisesRegex(ValueError, 'ozon_native_cny_evidence_required'):
            approve(self.state, [job['sku']])
        self.assertFalse(self.state.get(job['sku'])['approved'])

    def test_generic_brand_for_missing_and_conflicting_source_brands(self):
        for update in ({'source_brand': ''}, {'source_brand': None},
                       {'source_brand_conflict': True},
                       {'properties': {**PRODUCT['properties'], 'Бренд': 'Other'}}):
            with self.subTest(update=update):
                plan = wb.prepare('fake', {**PRODUCT, **update}, 5)
                self.assertEqual(plan['brand'], 'generic')
        product = {**PRODUCT, 'title': 'Acme Other Стальная кухонная подставка',
                   'description': 'Acme Other надежная подставка',
                   'properties': {**PRODUCT['properties'], 'Бренд': 'Other'}}
        plan = wb.prepare('fake', product, 5)
        self.assertEqual(plan['title'], 'Стальная кухонная подставка')
        self.assertEqual(plan['description'], 'надежная подставка')
        self.assertEqual(product['source_brand'], 'Acme')

    def test_cached_plan_generic_submission_and_brand_readback(self):
        job = self.new_job()
        job['plan']['brand'] = 'Acme'
        self.state.save(job)
        self.fake.fail_create = True
        completed = self.complete()
        self.assertEqual(completed['phase'], 'written')
        self.assertEqual(completed['plan']['brand'], 'generic')
        creates = [c for c in self.fake.calls if c[1].endswith('/cards/upload')]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0][2][0]['variants'][0]['brand'], 'generic')
        self.fake.card['brand'] = 'Acme'
        tick(self.state, 'fake', completed['next_run'] + 1)
        self.assertEqual(self.state.get(PRODUCT['sku'])['phase'], 'needs_review')
        self.assertEqual(self.fake.stocks[0]['amount'], 0)

    def test_prepared_plan_approval_uses_generic(self):
        from wb_pure.__main__ import approve
        job = self.new_job()
        job.update(phase='prepared', approved=False, product={'captured_at': time.time()})
        job['plan']['brand'] = 'Acme'
        self.state.save(job)
        approve(self.state, [job['sku']])
        self.assertEqual(self.state.get(job['sku'])['plan']['brand'], 'generic')

    def test_submitted_legacy_card_keeps_original_brand(self):
        job = self.new_job()
        job['plan']['brand'] = 'Acme'
        job.update(phase='card_pending', barcode='1234567890123')
        self.state.save(job)
        self.fake.card = {**copy.deepcopy(job['plan']), 'nmID': 999,
                          'vendorCode': job['vendor_code'], 'photos': [],
                          'sizes': [{'skus': [job['barcode']]}]}
        completed = self.complete()
        self.assertEqual(completed['phase'], 'written')
        self.assertEqual(completed['plan']['brand'], 'Acme')
        self.assertFalse(any(c[1].endswith('/cards/upload') for c in self.fake.calls))

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

    def test_windows_schedule_xml_and_rate_limit_checkpoint(self):
        import types
        import xml.etree.ElementTree as ET
        from wb_pure import scheduler
        with patch.object(scheduler, 'os', types.SimpleNamespace(name='nt')), patch.object(scheduler.subprocess, 'check_output', return_value='PC\\operator'), patch.object(scheduler.subprocess, 'run') as run:
            scheduler.configure(self.home)
            xml = ET.parse(self.home / 'task.xml')
            ns = {'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
            self.assertEqual(xml.find('.//t:Repetition/t:Interval', ns).text, 'PT1M')
            self.assertEqual(xml.find('.//t:LogonType', ns).text, 'InteractiveToken')
            self.assertIn('worker --once', xml.find('.//t:Arguments', ns).text)
            self.assertEqual(run.call_args.args[0][0], 'schtasks')
        self.new_job()
        tick(self.state, 'fake', self.now)
        self.assertEqual(self.state.get(PRODUCT['sku'])['phase'], 'create')
        original = self.fake.__call__
        def reject(token, method, base, path, body=None, **kw):
            if path.endswith('/cards/upload'):
                wb.BEFORE_WRITE.get()(method, path)
                raise wb.BusinessError('wb_rate_limited', retry_after=3600)
            return original(token, method, base, path, body, **kw)
        with patch.object(wb, 'api', side_effect=reject):
            tick(self.state, 'fake', self.now+60)
        job = self.state.get(PRODUCT['sku'])
        self.assertEqual(job['phase'], 'create')
        self.assertEqual(job['next_run'], self.now+3660)
        self.assertIsNone(self.fake.card)

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
