"""CNY reference multiples; discounts always apply to the same reference."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
import re
import math

POLICY_VERSION = 1
SOURCE_PRICE_POLICY = 1
CENT = Decimal('.01')
MOSCOW = timezone(timedelta(hours=3))
SHANGHAI = timezone(timedelta(hours=8))


def number(value, positive=False):
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError()
        n = Decimal(str(value))
        if not n.is_finite() or n < 0 or positive and n == 0 or n > 2**53-1 or n.as_tuple().exponent < -6:
            raise ValueError()
        return n
    except (ValueError, TypeError, InvalidOperation):
        raise ValueError('invalid_pricing_input') from None


def minimum_sale(costs, reference, now=None):
    # Optional operator floor is a sales-price limit, not a claim of profitability.
    return number((costs or {}).get('minimum_sale_price', 0))


def build_price_plan(price, multiplier=5, costs=None, now=None):
    p, m = number(price, True), number(multiplier, True)
    if m != m.quantize(CENT):
        raise ValueError('multiplier_max_two_decimals')
    base = (p * m).to_integral_value(rounding=ROUND_CEILING)
    number(base, True)
    first = (base * Decimal('.7')).quantize(CENT, rounding=ROUND_HALF_UP)
    final = (base * Decimal('.5')).quantize(CENT, rounding=ROUND_HALF_UP)
    floor = max(final, minimum_sale(costs, base))
    if final < floor:
        raise ValueError('final_price_below_operator_floor')
    return dict(pricing_policy_version=POLICY_VERSION, source_price_cny=float(p),
                requested_multiplier=float(m), effective_multiplier=float(m), strike_price=int(base),
                sale_price=float(first), final_sale_price=float(final), discount=30, final_discount=50,
                minimum_sale_price=float(floor), costs={'minimum_sale_price': float(floor)})


def validate_source_price(sku, currency, price, evidence):
    """Require same-SKU native price and post-save/reload browser evidence."""
    try:
        from .ozon import _currencies, _number
        if currency != 'CNY' or not isinstance(evidence, dict):
            raise ValueError()
        if evidence.get('version') != SOURCE_PRICE_POLICY or evidence.get('sku') != sku or evidence.get('currency') != 'CNY':
            raise ValueError()
        green = evidence['green_price']
        if green['sku'] != sku or green['currency'] != 'CNY' or number(green['amount'], True) != number(price, True):
            raise ValueError()
        if not green.get('raw') or not evidence.get('visible_price_text') or not evidence.get('selected_currency_text'):
            raise ValueError()
        raw, visible, selected = green['raw'], evidence['visible_price_text'], evidence['selected_currency_text']
        if not all(isinstance(v, str) for v in (raw, visible, selected)) or not re.search(r'\bCNY\s*$', selected):
            raise ValueError()
        if any(code != 'CNY' for text in (raw, visible) for code in _currencies(text)):
            raise ValueError()
        if number(_number(raw), True) != number(price, True):
            raise ValueError()
        compact = lambda text: re.sub(r'\s+', '', text)
        if not re.search(r'(?<![\d.,])' + re.escape(compact(raw)) + r'(?![\d.,])', compact(visible)):
            raise ValueError()
        saved, refreshed, captured = [float(evidence[k]) for k in ('saved_at', 'refreshed_at', 'captured_at')]
        if not all(math.isfinite(t) and t > 0 for t in (saved, refreshed, captured)) or not saved <= refreshed <= captured or captured - saved > 600:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ValueError('ozon_native_cny_evidence_required') from None


def validate_price_plan(plan):
    if not isinstance(plan, dict):
        raise ValueError('ozon_native_cny_evidence_required')
    validate_source_price(plan.get('sku'), plan.get('source_currency'), plan.get('source_price_cny'), plan.get('source_price_evidence'))
    try:
        expected = build_price_plan(plan['source_price_cny'], plan['requested_multiplier'], plan.get('costs'))
    except (KeyError, TypeError, ValueError):
        raise ValueError('source_price_plan_mismatch') from None
    if any(plan.get(k) != expected[k] for k in ('pricing_policy_version', 'effective_multiplier', 'strike_price',
                                              'sale_price', 'final_sale_price', 'discount', 'final_discount', 'minimum_sale_price')):
        raise ValueError('source_price_plan_mismatch')


def next_discount_at(verified_at):
    day = datetime.fromtimestamp(verified_at, MOSCOW).date() + timedelta(days=1)
    return datetime.combine(day, datetime.min.time(), MOSCOW).timestamp()


def vendor_code(sku, created_at):
    return f"oz{datetime.fromtimestamp(created_at, SHANGHAI):%Y%m%d}-{sku}"
