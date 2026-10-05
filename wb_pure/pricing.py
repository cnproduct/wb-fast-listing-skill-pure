"""CNY reference multiples; discounts always apply to the same reference."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP

POLICY_VERSION = 1
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


def next_discount_at(verified_at):
    day = datetime.fromtimestamp(verified_at, MOSCOW).date() + timedelta(days=1)
    return datetime.combine(day, datetime.min.time(), MOSCOW).timestamp()


def vendor_code(sku, created_at):
    return f"oz{datetime.fromtimestamp(created_at, SHANGHAI):%Y%m%d}-{sku}"
