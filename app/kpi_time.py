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
    if not period_arg:
        return None, None, ''
    return today - timedelta(days=29), today, '30days'


def _parse_ymd(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip()[:10], '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def iso_week_range(year, week):
    """Return (monday, sunday) for ISO calendar week."""
    try:
        year = int(year)
        week = int(week)
    except (TypeError, ValueError):
        return None, None
    if week < 1 or week > 53:
        return None, None
    try:
        start = date.fromisocalendar(year, week, 1)
        end = date.fromisocalendar(year, week, 7)
        return start, end
    except ValueError:
        return None, None


def quarter_range(year, quarter):
    try:
        year = int(year)
        quarter = int(quarter)
    except (TypeError, ValueError):
        return None, None
    if quarter < 1 or quarter > 4:
        return None, None
    start_month = (quarter - 1) * 3 + 1
    start = date(year, start_month, 1)
    end = _month_end(date(year, start_month + 2, 1))
    return start, end


def parse_dashboard_period(
    period_arg,
    date_from_str='',
    date_to_str='',
    period_year=None,
    period_month=None,
    period_week=None,
    period_quarter=None,
):
    """
    Parse KPI dashboard period from request params.
    Returns (confirmed, start_date, end_date, normalized_period, meta).
    """
    p = (period_arg or '').strip().lower()
    if p == 'vonbis':
        p = 'free'
    meta = {
        'period_year': period_year,
        'period_month': period_month,
        'period_week': period_week,
        'period_quarter': period_quarter,
        'date_from_str': (date_from_str or '').strip(),
        'date_to_str': (date_to_str or '').strip(),
    }
    if not p:
        return False, None, None, '', meta

    if p == 'all':
        return True, None, None, 'all', meta

    if p == 'free':
        start = _parse_ymd(date_from_str)
        end = _parse_ymd(date_to_str)
        if start and end and start <= end:
            meta['date_from_str'] = start.isoformat()
            meta['date_to_str'] = end.isoformat()
            return True, start, end, 'free', meta
        return False, None, None, 'free', meta

    if p == 'month' and period_year and period_month:
        try:
            y, m = int(period_year), int(period_month)
            if 1 <= m <= 12:
                start = date(y, m, 1)
                end = _month_end(start)
                meta['period_year'] = y
                meta['period_month'] = m
                return True, start, end, 'month', meta
        except (TypeError, ValueError):
            pass
        return False, None, None, 'month', meta

    if p == 'week' and period_year and period_week:
        start, end = iso_week_range(period_year, period_week)
        if start and end:
            meta['period_year'] = int(period_year)
            meta['period_week'] = int(period_week)
            return True, start, end, 'week', meta
        return False, None, None, 'week', meta

    if p == 'quarter' and period_year and period_quarter:
        start, end = quarter_range(period_year, period_quarter)
        if start and end:
            meta['period_year'] = int(period_year)
            meta['period_quarter'] = int(period_quarter)
            return True, start, end, 'quarter', meta
        return False, None, None, 'quarter', meta

    if p == 'year' and period_year:
        try:
            y = int(period_year)
            return True, date(y, 1, 1), date(y, 12, 31), 'year', meta
        except (TypeError, ValueError):
            pass
        return False, None, None, 'year', meta

    # Legacy rolling presets (old bookmarks only)
    if p in ('7days', '30days', '90days', '6months', '12months', 'this_month', 'this_year', 'last_year'):
        start, end, legacy = dashboard_date_range(p, '', '')
        if legacy == p or (p == 'vonbis' and start):
            return True, start, end, legacy, meta

    return False, None, None, p, meta


def dashboard_period_label(period_arg, start_date=None, end_date=None,
                           period_year=None, period_month=None,
                           period_week=None, period_quarter=None):
    p = (period_arg or '').strip().lower()
    if p == 'vonbis':
        p = 'free'
    if not p:
        return 'Noch nicht festgelegt'
    if p == 'all':
        return 'Gesamt'
    if p == 'free' and start_date and end_date:
        return f'{start_date.strftime("%d.%m.%Y")} – {end_date.strftime("%d.%m.%Y")}'
    if p == 'month' and period_year and period_month:
        return month_label_de(int(period_year), int(period_month))
    if p == 'week' and period_year and period_week:
        return f'KW {int(period_week):02d}/{str(int(period_year))[-2:]}'
    if p == 'quarter' and period_year and period_quarter:
        return f'Q{int(period_quarter)} {int(period_year)}'
    if p == 'year' and period_year:
        return str(int(period_year))
    return 'Zeitraum wählen'


def _week_start_monday(d):
    return d - timedelta(days=d.weekday())


def _month_start(d):
    return d.replace(day=1)


def _month_end(d):
    if d.month == 12:
        return date(d.year, 12, 31)
    return date(d.year, d.month + 1, 1) - timedelta(days=1)


def default_granularity_for_period(period_arg, start_date, end_date, data_dates=None):
    p = (period_arg or '').strip().lower()
    if p in ('week',):
        return 'day'
    if p in ('month', 'free', 'vonbis', '7days', '30days', 'this_month'):
        return 'day'
    if p in ('quarter', '90days', '6months'):
        return 'week'
    if p in ('year', '12months', 'this_year', 'last_year', 'all'):
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


def span_days(start_date, end_date, data_dates=None):
    if start_date and end_date:
        return (end_date - start_date).days + 1
    if data_dates:
        sd, ed = min(data_dates), max(data_dates)
        return (ed - sd).days + 1
    return 0


MAX_TAG_VIEW_DAYS = 90


def tag_view_exceeds_limit(start_date, end_date, data_dates=None):
    span = span_days(start_date, end_date, data_dates)
    return span > MAX_TAG_VIEW_DAYS


def suggested_granularity_for_span(span):
    if span > 400:
        return 'month'
    if span > MAX_TAG_VIEW_DAYS:
        return 'week'
    return 'day'


def chart_granularity_for_span(table_granularity, start_date, end_date, data_dates=None):
    """
    Keep table granularity as chosen by the user, but cap chart buckets for long spans
    so multi-year KPI views stay responsive.
    """
    g = (table_granularity or 'day').strip()
    span = span_days(start_date, end_date, data_dates)
    if span <= 0:
        return g
    if g == 'day' and span > MAX_TAG_VIEW_DAYS:
        return 'week'
    if g in ('day', 'week') and span > 400:
        return 'month'
    return g


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
