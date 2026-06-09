"""Productivity KPI computation (ODVS interval CSV, no Ampel UI)."""
import json
import re
from datetime import datetime, date, timedelta
from typing import Any

INTERVAL_DEFAULT = 1800

DEFAULT_SIGN_ON_COLS = ['SignOn']
DEFAULT_PROD_COLS = ['CF_IB_Gespraech', 'Produktiv_WF', 'Bereit']
DEFAULT_NACH_COLS = ['CF_IB_Nacharbeit']
DEFAULT_IDLE_COLS = ['IDLE']
DEFAULT_PAUSE_COL = 'IDLE_RC12_Bearbeitung'
DEFAULT_CALLS_COL = 'Mex1'
DEFAULT_WORKS_COL = 'Works_Beendet'

DEFAULT_LABELS = {
    'sign_on': 'Sign-On',
    'prod': 'Produktivität',
    'nach': 'Nacharbeit',
    'idle': 'Idle',
    'calls': 'Calls',
    'works': 'Works',
}

META_COLS = frozenset({
    'Dienstleister', 'BE4', 'BE3', 'BE2', 'BE1',
    'DAG_ID', 'DAG_VN_NN', 'Datum', 'Zeit', 'ZM_FCG',
})


def _json_list(val, default=None):
    if default is None:
        default = []
    if not val:
        return list(default)
    try:
        parsed = json.loads(val)
        return list(parsed) if isinstance(parsed, list) else list(default)
    except (TypeError, json.JSONDecodeError):
        return list(default)


def labels_dict(row):
    """Editable display names for productivity metrics (per project)."""
    if row is None:
        return dict(DEFAULT_LABELS)
    return {
        'sign_on': (row.label_sign_on or DEFAULT_LABELS['sign_on']).strip(),
        'prod': (row.label_prod or DEFAULT_LABELS['prod']).strip(),
        'nach': (row.label_nach or DEFAULT_LABELS['nach']).strip(),
        'idle': (row.label_idle or DEFAULT_LABELS['idle']).strip(),
        'calls': (row.label_calls or DEFAULT_LABELS['calls']).strip(),
        'works': (row.label_works or DEFAULT_LABELS['works']).strip(),
    }


def settings_dict(row):
    """Merge DB ProjectProductivitySetting row with defaults."""
    return {
        'interval_sec': (row.interval_sec if row else INTERVAL_DEFAULT) or INTERVAL_DEFAULT,
        'pause_col': (row.pause_col if row else DEFAULT_PAUSE_COL) or DEFAULT_PAUSE_COL,
        'calls_col': (row.calls_col if row else DEFAULT_CALLS_COL) or DEFAULT_CALLS_COL,
        'works_col': (row.works_col if row else DEFAULT_WORKS_COL) or DEFAULT_WORKS_COL,
        'sign_on_cols': _json_list(row.sign_on_cols if row else None, DEFAULT_SIGN_ON_COLS),
        'prod_cols': _json_list(row.prod_cols if row else None, DEFAULT_PROD_COLS),
        'nach_cols': _json_list(row.nach_cols if row else None, DEFAULT_NACH_COLS),
        'idle_cols': _json_list(row.idle_cols if row else None, DEFAULT_IDLE_COLS),
        'excluded_cols': _json_list(row.excluded_cols if row else None, []),
        'target_sign_on': row.target_sign_on if row else 95.0,
        'target_prod': row.target_prod if row else 85.0,
        'target_nach_per_call': row.target_nach_per_call if row else 30.0,
        'target_idle_max': row.target_idle_max if row else 10.0,
        'labels': labels_dict(row),
    }


def dashboard_visibility_dict(row):
    if row is None:
        return {
            'sign_on': True, 'prod': True, 'nach': True, 'idle': True, 'calls': True, 'works': True,
        }
    return {
        'sign_on': row.dashboard_show_sign_on,
        'prod': row.dashboard_show_prod,
        'nach': row.dashboard_show_nach,
        'idle': row.dashboard_show_idle,
        'calls': row.dashboard_show_calls,
        'works': row.dashboard_show_works,
    }


def impact_visibility_dict(row):
    if row is None:
        return {
            'sign_on': True, 'prod': True, 'nach': True, 'idle': True, 'calls': False, 'works': False,
        }
    return {
        'sign_on': row.impact_show_sign_on,
        'prod': row.impact_show_prod,
        'nach': row.impact_show_nach,
        'idle': row.impact_show_idle,
        'calls': row.impact_show_calls,
        'works': row.impact_show_works,
    }


def parse_german_num(value):
    if value is None:
        return 0.0
    s = str(value).strip()
    if not s or s in ('-', '–'):
        return 0.0
    s = s.replace('.', '').replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return 0.0


def row_column_val(row, col_name):
    """Read a CSV row cell; match column name case-insensitively."""
    if not col_name:
        return None
    if col_name in row:
        return row.get(col_name)
    col_lower = col_name.lower()
    for key, val in row.items():
        if key and key.lower() == col_lower:
            return val
    return None


def combine_datum_zeit(row):
    zeit = (row.get('Zeit') or '').strip()
    if zeit:
        return re.sub(r'\s+', ' ', zeit)
    datum = (row.get('Datum') or '').strip()
    zeit_only = (row.get('Zeit') or '').strip()
    if datum and zeit_only:
        return re.sub(r'\s+', ' ', f'{datum} {zeit_only}')
    return datum or ''


def parse_slot_datetime(dt_str):
    s = re.sub(r'\s+', ' ', (dt_str or '').strip())
    for fmt in ('%d.%m.%Y %H:%M:%S', '%d.%m.%Y %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_csv_date(value):
    s = (value or '').strip()
    if not s:
        return None
    for fmt in ('%d.%m.%Y', '%Y-%m-%d', '%d.%m.%y'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def sum_row_columns(row, cols, excluded=None):
    excluded = excluded or set()
    total = 0.0
    for c in cols or []:
        if c in excluded:
            continue
        total += parse_german_num(row.get(c))
    return total


def cap_sign_on_sec(sec, interval_sec):
    cap = interval_sec or INTERVAL_DEFAULT
    return min(max(0.0, sec or 0.0), cap)


def cap_kpi_sec(sec, kpi_denom):
    cap = kpi_denom or INTERVAL_DEFAULT
    return min(max(0.0, sec or 0.0), cap)


def kpi_interval_sec(interval_sec, pause_sec, has_pause_col=True):
    if not has_pause_col or not pause_sec or pause_sec <= 0:
        return interval_sec or INTERVAL_DEFAULT
    return max(1.0, (interval_sec or INTERVAL_DEFAULT) - pause_sec)


def idle_cols_for_sum(settings):
    pause = settings['pause_col']
    return [c for c in settings['idle_cols'] if c not in settings['excluded_cols'] and c != pause]


def active_metric_columns(settings, metric_key):
    """CSV columns used for a metric (as configured in KPI-Verwaltung)."""
    excluded = set(settings.get('excluded_cols') or [])
    if metric_key == 'sign_on':
        cols = settings.get('sign_on_cols') or []
    elif metric_key == 'prod':
        cols = settings.get('prod_cols') or []
    elif metric_key == 'nach':
        cols = settings.get('nach_cols') or []
    elif metric_key == 'idle':
        cols = idle_cols_for_sum(settings)
    elif metric_key == 'calls':
        col = settings.get('calls_col')
        return [col] if col and col not in excluded else []
    elif metric_key == 'works':
        col = settings.get('works_col')
        return [col] if col and col not in excluded else []
    else:
        return []
    return [c for c in cols if c not in excluded]


def prod_formula_hint(settings):
    """Column list for the Produktivität KPI hint (from KPI-Verwaltung)."""
    settings = settings or settings_dict(None)
    labels = settings.get('labels') or labels_dict(None)
    label = labels.get('prod') or DEFAULT_LABELS['prod']
    cols = active_metric_columns(settings, 'prod')
    cols_expr = ' + '.join(cols) if cols else '—'
    return {
        'label': label,
        'columns': cols,
        'columns_expr': cols_expr,
    }


def merge_rows_to_slot(rows, settings):
    """Aggregate multiple CSV rows for same agent+slot."""
    interval_sec = settings['interval_sec']
    excluded = set(settings['excluded_cols'])
    pause_col = settings['pause_col']
    calls_col = settings['calls_col']
    works_col = settings.get('works_col') or DEFAULT_WORKS_COL
    has_pause = pause_col not in excluded

    acc = {
        'sign_on_raw': 0.0,
        'prod_raw': 0.0,
        'nach_raw': 0.0,
        'idle_gross': 0.0,
        'pause': 0.0,
        'calls': 0.0,
        'works_beendet': 0.0,
        'be4': '',
        'dag_id': '',
        'agent_name': '',
        'slot_str': '',
        'zm_fcg': '',
    }
    for row in rows:
        if not acc['slot_str']:
            acc['slot_str'] = combine_datum_zeit(row)
        if not acc['be4']:
            acc['be4'] = (row.get('BE4') or '').strip()
        if not acc['dag_id']:
            acc['dag_id'] = (row.get('DAG_ID') or '').strip()
        if not acc['agent_name']:
            acc['agent_name'] = (row.get('DAG_VN_NN') or '').strip()
        if not acc['zm_fcg']:
            acc['zm_fcg'] = (row.get('ZM_FCG') or '').strip()

        acc['sign_on_raw'] += sum_row_columns(row, settings['sign_on_cols'], excluded)
        acc['prod_raw'] += sum_row_columns(row, settings['prod_cols'], excluded)
        acc['nach_raw'] += sum_row_columns(row, settings['nach_cols'], excluded)
        acc['idle_gross'] += sum_row_columns(row, idle_cols_for_sum(settings))
        if has_pause and pause_col in row:
            acc['pause'] += parse_german_num(row.get(pause_col))
        if calls_col not in excluded:
            acc['calls'] += parse_german_num(row_column_val(row, calls_col))
        if works_col not in excluded:
            acc['works_beendet'] += parse_german_num(row_column_val(row, works_col))

    pause_sec = acc['pause']
    kpi_denom = kpi_interval_sec(interval_sec, pause_sec, has_pause)
    sign_on = cap_sign_on_sec(acc['sign_on_raw'], interval_sec)
    prod_sec = cap_kpi_sec(acc['prod_raw'], kpi_denom)
    nach_sec = cap_kpi_sec(acc['nach_raw'], kpi_denom)
    idle_net = max(0.0, acc['idle_gross'] - pause_sec)

    sign_on_pct = (sign_on / interval_sec * 100) if interval_sec else None
    prod_pct = (prod_sec / kpi_denom * 100) if kpi_denom else None
    nach_pct = (nach_sec / kpi_denom * 100) if kpi_denom else None
    idle_pct = (idle_net / interval_sec * 100) if interval_sec else None
    nach_per_call = (nach_sec / acc['calls']) if acc['calls'] > 0 else None

    return {
        **acc,
        'interval_sec': interval_sec,
        'kpi_denom': kpi_denom,
        'sign_on_sec': sign_on,
        'prod_sec': prod_sec,
        'nach_sec': nach_sec,
        'idle_sec': idle_net,
        'pause_sec': pause_sec,
        'sign_on_pct': round(sign_on_pct, 2) if sign_on_pct is not None else None,
        'prod_pct': round(prod_pct, 2) if prod_pct is not None else None,
        'nach_pct': round(nach_pct, 2) if nach_pct is not None else None,
        'idle_pct': round(idle_pct, 2) if idle_pct is not None else None,
        'nach_per_call': round(nach_per_call, 2) if nach_per_call is not None else None,
        'slot_at': parse_slot_datetime(acc['slot_str']),
    }


def discover_numeric_headers(headers):
    return [h for h in headers if h not in META_COLS and h not in ('Dienst', 'Segment', 'ID_Dienst', 'ID_Segment')]


def normalize_member_id_key(value):
    return (value or '').strip()


def looks_like_numeric_id(value):
    """True for non-empty all-digit IDs (typical DAG-ID / MA-Kennung in ODVS exports)."""
    s = normalize_member_id_key(value)
    return bool(s) and s.isdigit() and s not in ('-1',)


def split_agent_display_name(agent_name):
    """Split DAG_VN_NN style name into (first_name, last_name) for member forms."""
    s = (agent_name or '').strip()
    if not s or s == '–':
        return '', ''
    if ',' in s:
        parts = [p.strip() for p in s.split(',', 1)]
        last = parts[0]
        first = parts[1] if len(parts) > 1 else ''
        return first, last
    parts = s.split(None, 1)
    if len(parts) == 1:
        return parts[0], ''
    return parts[0], parts[1]


def _pick_from_candidates(cands, preferred_team_id):
    if not cands:
        return None, None
    if preferred_team_id is not None:
        for cmid, cmt in cands:
            if cmt == preferred_team_id:
                return cmid, cmt
    return cands[0][0], cands[0][1]


def build_link_maps(team_query, member_query):
    team_map = {}
    for t in team_query:
        if t.name:
            team_map.setdefault(t.name.strip(), (t.id, t.project_id))

    dag_map = {}
    name_map = {}
    ma_map = {}
    for m in member_query:
        dag_key = normalize_member_id_key(m.dag_id)
        if dag_key and dag_key != '-1':
            dag_map.setdefault(dag_key, []).append((m.id, m.team_id))
        ma_key = normalize_member_id_key(m.ma_kennung)
        if ma_key:
            ma_map.setdefault(ma_key, []).append((m.id, m.team_id))
        if m.name:
            name_map.setdefault(m.name.strip().lower(), []).append((m.id, m.team_id))

    return team_map, dag_map, name_map, ma_map


def resolve_member(be4, dag_id, agent_name, team_map, dag_map, name_map, ma_map=None):
    tid = pid = mid = None
    be4 = (be4 or '').strip()
    if be4 and be4 in team_map:
        tid, pid = team_map[be4]

    dag_key = normalize_member_id_key(dag_id)
    if dag_key and dag_key != '-1':
        mid, team_from_id = _pick_from_candidates(dag_map.get(dag_key, []), tid)
        if mid is not None:
            if tid is None:
                tid = team_from_id
        elif ma_map:
            mid, team_from_id = _pick_from_candidates(ma_map.get(dag_key, []), tid)
            if mid is not None and tid is None:
                tid = team_from_id

    if mid is None and agent_name:
        mid, team_from_name = _pick_from_candidates(
            name_map.get(agent_name.strip().lower(), []), tid,
        )
        if mid is not None and tid is None:
            tid = team_from_name

    return tid, pid, mid


def _iv_val(obj, key, default=None):
    """Read a field from a ProductivityInterval ORM row or dict."""
    if isinstance(obj, dict):
        val = obj.get(key, default)
    else:
        val = getattr(obj, key, default)
    return default if val is None else val


def aggregate_summary(intervals):
    """Weighted summary from interval rows (list of dicts or ORM objects)."""
    total_interval = total_kpi_denom = 0.0
    sign_on_sum = prod_sum = nach_sum = idle_sum = calls_sum = works_sum = 0.0
    count = 0
    nach_per_call_vals = []

    for iv in intervals:
        count += 1
        isec = _iv_val(iv, 'interval_sec', INTERVAL_DEFAULT) or INTERVAL_DEFAULT
        kden = _iv_val(iv, 'kpi_denom', isec) or isec
        total_interval += isec
        total_kpi_denom += kden
        sign_on_sum += _iv_val(iv, 'sign_on_sec', 0)
        prod_sum += _iv_val(iv, 'prod_sec', 0)
        nach_sum += _iv_val(iv, 'nach_sec', 0)
        idle_sum += _iv_val(iv, 'idle_sec', 0)
        calls_sum += _iv_val(iv, 'calls', 0)
        works_sum += _iv_val(iv, 'works_beendet', 0)
        npc = _iv_val(iv, 'nach_per_call')
        if npc is not None:
            nach_per_call_vals.append(npc)

    if count == 0:
        return None

    return {
        'intervals': count,
        'sign_on_pct': round(sign_on_sum / total_interval * 100, 2) if total_interval else None,
        'prod_pct': round(prod_sum / total_kpi_denom * 100, 2) if total_kpi_denom else None,
        'nach_pct': round(nach_sum / total_kpi_denom * 100, 2) if total_kpi_denom else None,
        'idle_pct': round(idle_sum / total_interval * 100, 2) if total_interval else None,
        'calls': round(calls_sum, 1),
        'works': round(works_sum, 1),
        'nach_per_call': (
            round(sum(nach_per_call_vals) / len(nach_per_call_vals), 2) if nach_per_call_vals else None
        ),
    }


def aggregate_daily(intervals, start_date=None, end_date=None):
    """Build chart_daily + daily table rows from intervals."""
    by_day = {}
    for iv in intervals:
        slot = iv['slot_at'] if isinstance(iv, dict) else iv.slot_at
        if slot is None:
            continue
        d = slot.date() if isinstance(slot, datetime) else slot
        if start_date and d < start_date:
            continue
        if end_date and d > end_date:
            continue
        by_day.setdefault(d, []).append(iv)

    daily = []
    chart_daily = []
    cur = start_date
    if cur and end_date:
        while cur <= end_date:
            rows = by_day.get(cur, [])
            sm = aggregate_summary(rows) if rows else None
            entry = {
                'date': cur.strftime('%Y-%m-%d'),
                'label': cur.strftime('%d.%m.'),
                'count': len(rows),
                'sign_on_pct': sm['sign_on_pct'] if sm else None,
                'prod_pct': sm['prod_pct'] if sm else None,
                'nach_pct': sm['nach_pct'] if sm else None,
                'idle_pct': sm['idle_pct'] if sm else None,
                'nach_per_call': sm['nach_per_call'] if sm else None,
                'calls': sm['calls'] if sm else None,
                'works': sm['works'] if sm else None,
            }
            daily.append(entry)
            if rows:
                chart_daily.append(entry)
            cur += timedelta(days=1)
    else:
        for d in sorted(by_day.keys()):
            rows = by_day[d]
            sm = aggregate_summary(rows)
            entry = {
                'date': d.strftime('%Y-%m-%d'),
                'label': d.strftime('%d.%m.'),
                'count': len(rows),
                'sign_on_pct': sm['sign_on_pct'],
                'prod_pct': sm['prod_pct'],
                'nach_pct': sm['nach_pct'],
                'idle_pct': sm['idle_pct'],
                'nach_per_call': sm['nach_per_call'],
                'calls': sm['calls'],
                'works': sm['works'],
            }
            daily.append(entry)
            chart_daily.append(entry)

    return chart_daily, daily


def cumulative_chart_daily(chart_daily):
    """Cumulative trend for charts (like quality KPI dashboard)."""
    if not chart_daily:
        return []
    acc = {
        'interval_sec': 0.0, 'kpi_denom': 0.0,
        'sign_on_sec': 0.0, 'prod_sec': 0.0, 'nach_sec': 0.0, 'idle_sec': 0.0,
        'calls': 0.0, 'nach_weighted': 0.0, 'nach_calls': 0.0, 'works': 0.0,
    }
    out = []
    for d in chart_daily:
        # Re-derive from daily isn't perfect without raw intervals; use running weighted avg approximation
        acc['sign_on_sec'] += (d.get('sign_on_pct') or 0) * (d.get('count') or 1)
        acc['prod_sec'] += (d.get('prod_pct') or 0) * (d.get('count') or 1)
        acc['nach_sec'] += (d.get('nach_pct') or 0) * (d.get('count') or 1)
        acc['idle_sec'] += (d.get('idle_pct') or 0) * (d.get('count') or 1)
        acc['interval_sec'] += d.get('count') or 1
        if d.get('nach_per_call') is not None and d.get('calls'):
            acc['nach_weighted'] += d['nach_per_call'] * d['calls']
            acc['nach_calls'] += d['calls']
        out.append({
            'date': d['date'],
            'label': d['label'],
            'count': d['count'],
            'sign_on_pct': round(acc['sign_on_sec'] / acc['interval_sec'], 2) if acc['interval_sec'] else None,
            'prod_pct': round(acc['prod_sec'] / acc['interval_sec'], 2) if acc['interval_sec'] else None,
            'nach_pct': round(acc['nach_sec'] / acc['interval_sec'], 2) if acc['interval_sec'] else None,
            'idle_pct': round(acc['idle_sec'] / acc['interval_sec'], 2) if acc['interval_sec'] else None,
            'nach_per_call': (
                round(acc['nach_weighted'] / acc['nach_calls'], 2) if acc['nach_calls'] else d.get('nach_per_call')
            ),
            'calls': d.get('calls'),
            'works': d.get('works'),
        })
    return out


def cumulative_from_intervals(intervals, start_date, end_date):
    """True cumulative series from raw interval rows sorted by slot_at."""
    sorted_ivs = sorted(
        intervals,
        key=lambda x: (x.slot_at if hasattr(x, 'slot_at') else x['slot_at']) or datetime.min,
    )
    acc_isec = acc_kden = 0.0
    acc_sign = acc_prod = acc_nach = acc_idle = 0.0
    acc_nach_sec = acc_calls = acc_works = 0.0
    by_day = {}
    for iv in sorted_ivs:
        slot = iv.slot_at if hasattr(iv, 'slot_at') else iv['slot_at']
        if not slot:
            continue
        d = slot.date()
        if start_date and d < start_date:
            continue
        if end_date and d > end_date:
            continue
        isec = _iv_val(iv, 'interval_sec', INTERVAL_DEFAULT) or INTERVAL_DEFAULT
        kden = _iv_val(iv, 'kpi_denom', isec) or isec
        acc_isec += isec
        acc_kden += kden
        acc_sign += _iv_val(iv, 'sign_on_sec', 0)
        acc_prod += _iv_val(iv, 'prod_sec', 0)
        acc_nach += _iv_val(iv, 'nach_sec', 0)
        acc_idle += _iv_val(iv, 'idle_sec', 0)
        acc_nach_sec += _iv_val(iv, 'nach_sec', 0)
        acc_calls += _iv_val(iv, 'calls', 0)
        acc_works += _iv_val(iv, 'works_beendet', 0)
        by_day[d] = {
            'date': d.strftime('%Y-%m-%d'),
            'label': d.strftime('%d.%m.'),
            'count': by_day.get(d, {}).get('count', 0) + 1,
            'sign_on_pct': round(acc_sign / acc_isec * 100, 2) if acc_isec else None,
            'prod_pct': round(acc_prod / acc_kden * 100, 2) if acc_kden else None,
            'nach_pct': round(acc_nach / acc_kden * 100, 2) if acc_kden else None,
            'idle_pct': round(acc_idle / acc_isec * 100, 2) if acc_isec else None,
            'nach_per_call': round(acc_nach_sec / acc_calls, 2) if acc_calls else None,
            'calls': round(acc_calls, 1),
            'works': round(acc_works, 1),
        }
    if start_date and end_date:
        result = []
        cur = start_date
        while cur <= end_date:
            if cur in by_day:
                result.append(by_day[cur])
            else:
                result.append({
                    'date': cur.strftime('%Y-%m-%d'),
                    'label': cur.strftime('%d.%m.'),
                    'count': 0,
                    'sign_on_pct': 0,
                    'prod_pct': 0,
                    'nach_pct': 0,
                    'idle_pct': 0,
                    'nach_per_call': 0,
                    'calls': 0,
                    'works': 0,
                })
            cur += timedelta(days=1)
        return result
    return [by_day[d] for d in sorted(by_day.keys())]


def _interval_dates(intervals):
    out = []
    for iv in intervals:
        slot = iv.slot_at if hasattr(iv, 'slot_at') else iv.get('slot_at')
        if slot:
            out.append(slot.date() if isinstance(slot, datetime) else slot)
    return out


def build_dashboard_series(intervals, start_date, end_date, chart_granularity, bucket_ranges_fn):
    """Chart + table series with optional week/month bucketing."""
    if chart_granularity == 'day':
        chart_daily = cumulative_from_intervals(intervals, start_date, end_date)
        _, daily = aggregate_daily(intervals, start_date, end_date)
        return chart_daily, daily

    periods = bucket_ranges_fn(
        chart_granularity, start_date, end_date, _interval_dates(intervals),
    )
    if not periods:
        return [], []

    sorted_ivs = sorted(
        intervals,
        key=lambda x: (x.slot_at if hasattr(x, 'slot_at') else x.get('slot_at')) or datetime.min,
    )
    acc_isec = acc_kden = 0.0
    acc_sign = acc_prod = acc_nach = acc_idle = 0.0
    acc_nach_sec = acc_calls = acc_works = 0.0
    idx = 0
    chart_daily = []
    daily = []

    for period in periods:
        end_d = period['end']
        period_count = 0
        while idx < len(sorted_ivs):
            iv = sorted_ivs[idx]
            slot = iv.slot_at if hasattr(iv, 'slot_at') else iv.get('slot_at')
            if not slot or slot.date() > end_d:
                break
            d = slot.date()
            if d >= period['start']:
                period_count += 1
            isec = _iv_val(iv, 'interval_sec', INTERVAL_DEFAULT) or INTERVAL_DEFAULT
            kden = _iv_val(iv, 'kpi_denom', isec) or isec
            acc_isec += isec
            acc_kden += kden
            acc_sign += _iv_val(iv, 'sign_on_sec', 0)
            acc_prod += _iv_val(iv, 'prod_sec', 0)
            acc_nach += _iv_val(iv, 'nach_sec', 0)
            acc_idle += _iv_val(iv, 'idle_sec', 0)
            acc_nach_sec += _iv_val(iv, 'nach_sec', 0)
            acc_calls += _iv_val(iv, 'calls', 0)
            acc_works += _iv_val(iv, 'works_beendet', 0)
            idx += 1

        if period_count > 0:
            chart_daily.append({
                'date': period['key'],
                'label': period['label'],
                'count': period_count,
                'sign_on_pct': round(acc_sign / acc_isec * 100, 2) if acc_isec else 0,
                'prod_pct': round(acc_prod / acc_kden * 100, 2) if acc_kden else 0,
                'nach_pct': round(acc_nach / acc_kden * 100, 2) if acc_kden else 0,
                'idle_pct': round(acc_idle / acc_isec * 100, 2) if acc_isec else 0,
                'nach_per_call': round(acc_nach_sec / acc_calls, 2) if acc_calls else 0,
                'calls': round(acc_calls, 1),
                'works': round(acc_works, 1),
            })
        else:
            chart_daily.append({
                'date': period['key'],
                'label': period['label'],
                'count': 0,
                'sign_on_pct': 0,
                'prod_pct': 0,
                'nach_pct': 0,
                'idle_pct': 0,
                'nach_per_call': 0,
                'calls': 0,
                'works': 0,
            })

        period_ivs = []
        for iv in intervals:
            slot = iv.slot_at if hasattr(iv, 'slot_at') else iv.get('slot_at')
            if not slot:
                continue
            d = slot.date()
            if period['start'] <= d <= period['end']:
                period_ivs.append(iv)
        if period_ivs:
            sm = aggregate_summary(period_ivs)
            daily.append({
                'date': period['key'],
                'label': period['label'],
                'count': len(period_ivs),
                'sign_on_pct': sm['sign_on_pct'],
                'prod_pct': sm['prod_pct'],
                'nach_pct': sm['nach_pct'],
                'idle_pct': sm['idle_pct'],
                'nach_per_call': sm['nach_per_call'],
                'calls': sm['calls'],
                'works': sm['works'],
            })
        else:
            daily.append({
                'date': period['key'],
                'label': period['label'],
                'count': 0,
                'sign_on_pct': None,
                'prod_pct': None,
                'nach_pct': None,
                'idle_pct': None,
                'nach_per_call': None,
                'calls': None,
                'works': None,
            })

    return chart_daily, daily


def _totals_entry(day, count, isec, kden, sign, prod, nach, idle, calls, works=0):
    isec = float(isec or 0)
    kden = float(kden or isec or 0)
    return {
        'day': day,
        'count': int(count or 0),
        'isec': isec,
        'kden': kden,
        'sign': float(sign or 0),
        'prod': float(prod or 0),
        'nach': float(nach or 0),
        'idle': float(idle or 0),
        'calls': float(calls or 0),
        'works': float(works or 0),
    }


def _row_metrics_from_totals(totals, cumulative=False):
    isec = totals['isec']
    kden = totals['kden']
    empty = 0 if cumulative else None
    return {
        'sign_on_pct': round(totals['sign'] / isec * 100, 2) if isec else empty,
        'prod_pct': round(totals['prod'] / kden * 100, 2) if kden else empty,
        'nach_pct': round(totals['nach'] / kden * 100, 2) if kden else empty,
        'idle_pct': round(totals['idle'] / isec * 100, 2) if isec else empty,
        'nach_per_call': round(totals['nach'] / totals['calls'], 2) if totals['calls'] else empty,
        'calls': round(totals['calls'], 1),
        'works': round(totals['works'], 1),
    }


def query_interval_summary_sql(filters):
    from sqlalchemy import func
    from app import db
    from app.models import ProductivityInterval

    row = (
        db.session.query(
            func.count(ProductivityInterval.id),
            func.sum(ProductivityInterval.interval_sec),
            func.sum(ProductivityInterval.kpi_denom),
            func.sum(ProductivityInterval.sign_on_sec),
            func.sum(ProductivityInterval.prod_sec),
            func.sum(ProductivityInterval.nach_sec),
            func.sum(ProductivityInterval.idle_sec),
            func.sum(ProductivityInterval.calls),
            func.sum(ProductivityInterval.works_beendet),
        )
        .filter(*filters)
        .one()
    )
    if not row[0]:
        return None
    totals = _totals_entry(None, row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8])
    return {'intervals': totals['count'], **_row_metrics_from_totals(totals)}


def query_daily_buckets_sql(filters):
    from sqlalchemy import func, cast, Date
    from app import db
    from app.models import ProductivityInterval

    day_col = cast(ProductivityInterval.slot_at, Date)
    rows = (
        db.session.query(
            day_col,
            func.count(ProductivityInterval.id),
            func.sum(ProductivityInterval.interval_sec),
            func.sum(ProductivityInterval.kpi_denom),
            func.sum(ProductivityInterval.sign_on_sec),
            func.sum(ProductivityInterval.prod_sec),
            func.sum(ProductivityInterval.nach_sec),
            func.sum(ProductivityInterval.idle_sec),
            func.sum(ProductivityInterval.calls),
            func.sum(ProductivityInterval.works_beendet),
        )
        .filter(*filters)
        .group_by(day_col)
        .order_by(day_col)
        .all()
    )
    return [
        _totals_entry(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9])
        for r in rows if r[0] is not None
    ]


def _sum_buckets(buckets):
    out = {
        'count': 0, 'isec': 0.0, 'kden': 0.0, 'sign': 0.0, 'prod': 0.0,
        'nach': 0.0, 'idle': 0.0, 'calls': 0.0, 'works': 0.0,
    }
    for b in buckets:
        if not b:
            continue
        out['count'] += b['count']
        out['isec'] += b['isec']
        out['kden'] += b['kden']
        out['sign'] += b['sign']
        out['prod'] += b['prod']
        out['nach'] += b['nach']
        out['idle'] += b['idle']
        out['calls'] += b['calls']
        out['works'] += b['works']
    return out


def build_dashboard_series_from_buckets(
    daily_buckets, start_date, end_date, chart_granularity, table_granularity, bucket_ranges_fn,
):
    table_granularity = table_granularity or chart_granularity
    by_day = {b['day']: b for b in daily_buckets}
    data_dates = list(by_day.keys())
    if not data_dates:
        return [], []

    def _period_buckets(period):
        items = []
        d = period['start']
        while d <= period['end']:
            if d in by_day:
                items.append(by_day[d])
            d += timedelta(days=1)
        return items

    chart_periods = bucket_ranges_fn(chart_granularity, start_date, end_date, data_dates)
    table_periods = (
        chart_periods if table_granularity == chart_granularity
        else bucket_ranges_fn(table_granularity, start_date, end_date, data_dates)
    )

    acc = _sum_buckets([])
    chart_daily = []
    if chart_granularity == 'day' and start_date and end_date:
        cur = start_date
        while cur <= end_date:
            day_b = by_day.get(cur)
            if day_b:
                acc = _sum_buckets([acc, day_b])
                metrics = _row_metrics_from_totals(acc, cumulative=True)
                chart_daily.append({
                    'date': cur.strftime('%Y-%m-%d'),
                    'label': cur.strftime('%d.%m.'),
                    'count': day_b['count'],
                    **metrics,
                })
            else:
                chart_daily.append({
                    'date': cur.strftime('%Y-%m-%d'),
                    'label': cur.strftime('%d.%m.'),
                    'count': 0,
                    'sign_on_pct': 0, 'prod_pct': 0, 'nach_pct': 0, 'idle_pct': 0,
                    'nach_per_call': 0, 'calls': 0, 'works': 0,
                })
            cur += timedelta(days=1)
    else:
        for period in chart_periods or []:
            period_totals = _sum_buckets(_period_buckets(period))
            acc = _sum_buckets([acc, period_totals])
            metrics = _row_metrics_from_totals(acc, cumulative=True)
            chart_daily.append({
                'date': period['key'],
                'label': period['label'],
                'count': period_totals['count'],
                **metrics,
            })

    daily = []
    for period in table_periods or []:
        totals = _sum_buckets(_period_buckets(period))
        if totals['count']:
            daily.append({
                'date': period['key'],
                'label': period['label'],
                'count': totals['count'],
                **_row_metrics_from_totals(totals),
            })
        elif table_granularity == 'day':
            daily.append({
                'date': period['key'],
                'label': period['label'],
                'count': 0,
                'sign_on_pct': 0, 'prod_pct': 0, 'nach_pct': 0, 'idle_pct': 0,
                'nach_per_call': 0, 'calls': 0, 'works': 0,
            })
        else:
            daily.append({
                'date': period['key'],
                'label': period['label'],
                'count': 0,
                'sign_on_pct': None, 'prod_pct': None, 'nach_pct': None, 'idle_pct': None,
                'nach_per_call': None, 'calls': None, 'works': None,
            })
    return chart_daily, daily
