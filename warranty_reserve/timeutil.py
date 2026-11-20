"""时间、金额与保修期计算的基础工具。"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from dateutil.relativedelta import relativedelta

CUTOFF_DEFAULT = "2026-09-02T23:59:59+08:00"


def parse_ts(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def warranty_end(sold_at, months):
    return parse_ts(sold_at) + relativedelta(months=int(months))


def days_between(start, end):
    return (parse_ts(end) - parse_ts(start)).days


def month_key(value):
    return parse_ts(value).strftime("%Y-%m")


def cents(value):
    return int(value or 0)


def yuan_format(minor):
    """整数分 -> 供终端展示的元字符串。"""
    return str((Decimal(int(minor)) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def percentile(sorted_values, q):
    """线性插值分位数；q 在 [0,1]。"""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = (len(sorted_values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return float(sorted_values[lo]) * (1 - frac) + float(sorted_values[hi]) * frac


def add_months(ts, months):
    return parse_ts(ts) + relativedelta(months=int(months))


def add_days(ts, days):
    base = ts if isinstance(ts, datetime) else parse_ts(ts)
    return base + timedelta(days=int(days))
