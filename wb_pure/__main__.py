import argparse
import getpass
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from . import __version__, wb
from .browser import Browser, from_capture, sku_id
from .pricing import vendor_code
from .state import State, home_dir, lock, save_token, load_token
from .runner import tick


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def read_json(path):
    p = Path(path)
    if p.stat().st_size > 26 * 1024 * 1024:
        raise ValueError('input_file_too_large')
    return json.loads(p.read_text(encoding='utf-8-sig'))


def summaries(state):
    return [{k: j[k] for k in ('sku', 'phase', 'error', 'vendor_code', 'nm_id', 'actual_price_cny', 'actual_discount',
                              'reprice_at', 'reprice_completed', 'stock_protected', 'next_run') if k in j}
            for j in state.jobs()]


def prepare_one(state, token, result, args, overrides):
    product = result['product']
    sku = sku_id(product['sku'])
    old = state.get(sku)
    if old and old['phase'] not in ('blocked', 'prepared'):
        return old
    extras = overrides.get(sku, {})
    allowed = {'category_mapping', 'wb_characteristics', 'mapping_evidence', 'wb_size',
               'source_brand', 'properties', 'length_cm', 'width_cm', 'height_cm', 'weight_g', 'source_evidence',
               'supplement_evidence'}
    if not isinstance(extras, dict) or set(extras) - allowed:
        raise ValueError('invalid_product_supplement')
    facts = set(extras) & {'source_brand', 'properties', 'length_cm', 'width_cm', 'height_cm', 'weight_g', 'source_evidence', 'wb_size'}
    if facts and not extras.get('supplement_evidence'):
        raise ValueError('supplement_evidence_required')
    for key, value in extras.items():
        product[key] = {**product.get(key, {}), **value} if key in ('properties', 'source_evidence') else value
    if not 0 <= time.time() - product.get('captured_at', 0) <= 86400:
        raise ValueError('capture_expired')
    created = old['created_at'] if old else time.time()
    job = {'sku': sku, 'created_at': created, 'vendor_code': vendor_code(sku, created),
           'warehouse_id': state.config()['warehouse_id'], 'product': product, 'phase': 'blocked'}
    if old:
        job['vendor_code'] = old['vendor_code']
    try:
        job['plan'] = wb.prepare(token, product, args.stock, args.subject, args.multiplier,
                                costs={'minimum_sale_price': args.minimum_sale})
        job.update(phase='prepared', approved=False)
    except (wb.BusinessError, ValueError) as exc:
        job['error'] = str(exc)
    state.save(job)
    return job


def approve(state, skus):
    now = time.time()
    for sku in skus:
        job = state.get(sku)
        if not job:
            raise ValueError('sku_not_prepared')
        if job['phase'] == 'prepared':
            if now - job['product']['captured_at'] > 86400:
                raise ValueError('capture_expired')
            job.update(approved=True, phase='allocate', next_run=now, phase_started=now)
            state.save(job)


def parser():
    p = argparse.ArgumentParser(description='Local Ozon → WB. No cloud executor, activation or payment.')
    p.add_argument('--version', action='version', version=__version__)
    p.add_argument('--home', help='Independent local directory for this store (default ~/.wb-pure)')
    sub = p.add_subparsers(dest='command', required=True)
    b = sub.add_parser('bind', help='Verify token/warehouse once; save token in OS keyring')
    b.add_argument('--warehouse', type=int, required=True)
    b.add_argument('--token-stdin', action='store_true')
    b.add_argument('--token-env', action='store_true', help='Use WB_API_TOKEN without persisting it')
    for name in ('capture', 'batch'):
        a = sub.add_parser(name)
        a.add_argument('skus', nargs='+', help='Ozon SKUs, product URLs or a UTF-8 text file')
        a.add_argument('--manual-wait', type=int, default=180)
        if name == 'batch':
            plan_args(a)
            a.add_argument('--start', action='store_true', help='Authorize writes for this batch')
    a = sub.add_parser('prepare', help='Parse local browser capture; read-only WB preflight')
    a.add_argument('capture_file')
    plan_args(a)
    a = sub.add_parser('start', help='Authorize/resume previously prepared SKUs')
    a.add_argument('skus', nargs='+')
    a = sub.add_parser('worker', help='Process authorized local jobs and due discounts')
    a.add_argument('--once', action='store_true')
    sub.add_parser('status')
    a = sub.add_parser('plan')
    a.add_argument('sku')
    a = sub.add_parser('categories', help='Live official categories and required characteristics')
    a.add_argument('--query')
    a.add_argument('--subject', type=int)
    sub.add_parser('audit', help='Read-only prices/discounts for this store')
    sub.add_parser('doctor')
    sub.add_parser('schedule', help='Install current-user local timer, catching up after downtime')
    sub.add_parser('unschedule')
    return p


def plan_args(p):
    p.add_argument('--multiplier', type=float, default=5)
    p.add_argument('--stock', type=int, default=5)
    p.add_argument('--minimum-sale', type=float, default=0, help='Optional minimum CNY sale price')
    p.add_argument('--subject', type=int)
    p.add_argument('--overrides', help='JSON keyed by SKU: evidenced category/attribute/package facts')


def sku_list(values):
    result = []
    for value in values:
        if Path(value).is_file():
            result.extend(sku_list(Path(value).read_text(encoding='utf-8-sig').split()))
        else:
            result.append(sku_id(value.strip(',;')))
    return list(dict.fromkeys(result))


def main(argv=None):
    args = parser().parse_args(argv)
    home = home_dir(args.home)
    state = State(home)
    command = args.command
    if command == 'status':
        return output(summaries(state))
    if command == 'plan':
        job = state.get(sku_id(args.sku))
        return output(job.get('plan', {'error': job.get('error')}) if job else {'error': 'sku_not_found'})
    if command == 'doctor':
        import importlib.metadata
        return output({'version': __version__, 'python': sys.version.split()[0], 'home': str(home),
                       'playwright': importlib.metadata.version('playwright'), 'jobs': len(state.jobs()),
                       'token_source': 'environment' if os.environ.get('WB_API_TOKEN') else 'os_keyring',
                       'mode': 'local_only', 'schedule': (home / 'schedule.json').exists()})
    if command in ('schedule', 'unschedule'):
        from .scheduler import configure
        return output(configure(home, remove=command == 'unschedule'))
    if command == 'worker':
        while True:
            with lock(home):
                tick(state, load_token(home))
            if args.once:
                return output(summaries(state))
            time.sleep(8)
    with lock(home):
        if command == 'bind':
            token = os.environ.get('WB_API_TOKEN', '') if args.token_env else sys.stdin.readline().strip() if args.token_stdin else getpass.getpass('WB 店铺 API 令牌：')
            if not token:
                raise ValueError('wb_token_required')
            config = wb.bind(token, args.warehouse)
            config["token_fingerprint"] = hashlib.sha256(token.encode()).hexdigest()
            # Check store identity before replacing a previously saved token.
            state.bind(config)
            if not args.token_env:
                save_token(home, token)
            output({k: v for k, v in config.items() if k != "token_fingerprint"})
            return
        if command == 'capture':
            with Browser(home, args.manual_wait) as browser:
                for sku in sku_list(args.skus):
                    output(browser.capture(sku))
            return
        token = load_token(home)
        config = state.config()
        if hashlib.sha256(token.encode()).hexdigest() != config['token_fingerprint']:
            raise ValueError('token_changed_bind_again')
        wb.SELLER.set(config['seller_id'])
        if command == 'categories':
            if args.subject:
                return output(wb.api(token, 'GET', wb.CONTENT, f'/content/v2/object/charcs/{args.subject}'))
            return output(wb.api(token, 'GET', wb.CONTENT, '/content/v2/object/all?' + wb.urlencode({'name': args.query or '', 'limit': 1000, 'offset': 0, 'locale': 'ru'})))
        if command == 'audit':
            rows, offset = [], 0
            while True:
                goods = wb.api(token, 'GET', wb.PRICES, f'/api/v2/list/goods/filter?limit=1000&offset={offset}').get('data', {}).get('listGoods', [])
                rows.extend(goods)
                if len(goods) < 1000:
                    break
                offset += len(goods)
            return output({'read_only': True, 'goods': rows})
        if command in ('prepare', 'batch'):
            overrides = read_json(args.overrides) if args.overrides else {}
            if command == 'prepare':
                job = prepare_one(state, token, from_capture(read_json(args.capture_file)), args, overrides)
                return output({'sku': job['sku'], 'phase': job['phase'], 'plan': job.get('plan'), 'error': job.get('error')})
            skus = sku_list(args.skus)
            with Browser(home, args.manual_wait) as browser:
                for sku in skus:
                    old = state.get(sku)
                    if old and old['phase'] not in ('blocked', 'prepared'):
                        continue
                    try:
                        job = prepare_one(state, token, browser.capture(sku), args, overrides)
                    except Exception as exc:
                        code = str(exc) if isinstance(exc, (ValueError, wb.BusinessError)) else type(exc).__name__
                        now = time.time()
                        state.save({'sku': sku, 'phase': 'blocked', 'error': code, 'created_at': old['created_at'] if old else now,
                                    'vendor_code': old['vendor_code'] if old else vendor_code(sku, now)})
                        if code.startswith('ozon_') or code in ('wb_unauthorized', 'wb_permission_denied', 'wb_rate_limited'):
                            break
                    if state.get(sku).get('error') in ('wb_unauthorized', 'wb_permission_denied', 'wb_rate_limited'):
                        break
                    if args.start:
                        approve(state, [sku])
                        tick(state, token)
            return output(summaries(state))
        if command == 'start':
            approve(state, sku_list(args.skus))
            tick(state, token)
            return output(summaries(state))


def entry():
    try:
        main()
    except KeyboardInterrupt:
        print('已暂停；已保存的任务可继续。', file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        # Never print a traceback containing submitted credentials or raw responses.
        code = str(exc) if isinstance(exc, (ValueError, wb.BusinessError)) else type(exc).__name__
        output({'ok': False, 'error': code})
        sys.exit(1)


if __name__ == '__main__':
    entry()
