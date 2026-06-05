"""Shared date-range and chart granularity helpers for KPI dashboards."""
from datetime import date, datetime, timedelta, timezone


def dashboard_date_range(period_arg, date_from_str, date_to_str):
    """Return (start_date, end_date, period) as Python date objects."""
    today = datetime.now(timezone.utc).date()
    period_arg = (period_arg or '').strip()
    if period_arg == 'vonbis':
        start = end = None
        try:
            if date_from_str:
                start = datetime.strptime(date_from_str, '%Y-%m-%d').date()
            if date_to_str:
                end = datetime.strptime(date_to_str, '%Y-%m-%d').date()
        except ValueError:
            start = end = None
        if start and end and start <= end:
            return start, end, 'vonbis'
        period_arg = '30days'
    if period_arg == '7days':
        return today - timedelta(days=6), today, '7days'
    if period_arg == '6months':
        return today - timedelta(days=179), today, '6months'
    if period_arg == '12months':
        return today - timedelta(days=364), today, '12months'
    if period_arg == '90days':
        return today - timedelta(days=89), today, '90days'
    if period_arg == 'this_month':
        return today.replace(day=1), today, 'this_month'
    if period_arg == 'this_year':
        return today.replace(month=1, day=1), today, 'this_year'
    if period_arg == 'last_year':
        ly = today.year - 1
        return date(ly, 1, 1), date(ly, 12, 31), 'last_year'
    if period_arg == 'all':
        return None, None, 'all'
    return today - timedelta(days=29), today, '30days'


def _week_start_monday(d):
    return d - timedelta(days=d.weekday())


def _month_start(d):
    return d.replace(day=1)


def _month_end(d):
    if d.month == 12:
        return date(d.year, 12, 31)
    return date(d.year, d.month + 1, 1) - timedelta(days=1)


def default_granularity_for_period(period_arg, start_date, end_date, data_dates=None):
    if period_arg in ('7days', '30days', 'this_month'):
        return 'day'
    if period_arg in ('90days', '6months'):
        return 'week'
    if period_arg in ('12months', 'this_year', 'last_year', 'all'):
        return 'month'
    if start_date and end_date:
        span = (end_date - start_date).days + 1
        if span <= 31:
            return 'day'
        if span <= 120:
            return 'week'
        return 'month'
    if data_dates:
        sd, ed = min(data_dates), max(data_dates)
        span = (ed - sd).days + 1
        if span <= 31:
            return 'day'
        if span <= 120:
            return 'week'
        return 'month'
    return 'month'


def resolve_granularity(granularity_arg, period_arg, start_date, end_date, data_dates=None):
    g = (granularity_arg or '').strip()
    if g in ('day', 'week', 'month'):
        return g
    return default_granularity_for_period(period_arg, start_date, end_date, data_dates)


def bucket_ranges(bucket, start_date, end_date, data_dates=None):
    """Return list of {start, end, key, label} period buckets."""
    if not start_date or not end_date:
        if not data_dates:
            return []
        start_date = min(data_dates)
        end_date = max(data_dates)

    long_span = (end_date - start_date).days > 365
    ranges = []
    if bucket == 'day':
        d = start_date
        while d <= end_date:
            ranges.append({
                'start': d,
                'end': d,
                'key': d.strftime('%Y-%m-%d'),
                'label': d.strftime('%d.%m.%y') if long_span else d.strftime('%d.%m.'),
            })
            d += timedelta(days=1)
        return ranges

    if bucket == 'week':
        d = _week_start_monday(start_date)
        while d <= end_date:
            we = min(d + timedelta(days=6), end_date)
            ws = max(d, start_date)
            iso = d.isocalendar()
            ranges.append({
                'start': ws,
                'end': we,
                'key': d.strftime('%Y-%m-%d'),
                'label': f'KW {iso[1]:02d}/{str(iso[0])[-2:]}',
            })
            d += timedelta(days=7)
        return ranges

    d = _month_start(start_date)
    while d <= end_date:
        me = min(_month_end(d), end_date)
        ms = max(d, start_date)
        ranges.append({
            'start': ms,
            'end': me,
            'key': d.strftime('%Y-%m-01'),
            'label': d.strftime('%m.%Y'),
        })
        if d.month == 12:
            d = date(d.year + 1, 1, 1)
        else:
            d = date(d.year, d.month + 1, 1)
    return ranges


def granularity_label(bucket):
    return {'day': 'Tag', 'week': 'Woche', 'month': 'Monat'}.get(bucket, bucket)


_GERMAN_MONTHS = (
    '',
    'Januar', 'Februar', 'März', 'April', 'Mai', 'Juni',
    'Juli', 'August', 'September', 'Oktober', 'November', 'Dezember',
)


def month_label_de(year, month):
    if 1 <= month <= 12:
        return f'{_GERMAN_MONTHS[month]} {year}'
    return f'{month:02d}.{year}'


def group_daily_by_month(daily_rows):
    """Group day-granularity table rows into calendar months (ordered)."""
    buckets = {}
    order = []
    for row in daily_rows or []:
        date_str = (row.get('date') or '').strip()
        if len(date_str) < 7:
            continue
        ym = date_str[:7]
        if ym not in buckets:
            buckets[ym] = []
            order.append(ym)
        buckets[ym].append(row)
    pages = []
    for ym in order:
        year, month = int(ym[:4]), int(ym[5:7])
        pages.append({
            'key': ym,
            'label': month_label_de(year, month),
            'rows': buckets[ym],
        })
    return pages
