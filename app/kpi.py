# app/kpi.py
"""KPI logic for the "KPIs (Demo)" feature.

Three KPIs derived from SMS-survey raw data:
- Informationsquote: share of surveys where "Konnten wir Ihr Anliegen lösen?"
  is answered "Ja" OR "Noch nicht. Ich wurde über das weitere Vorgehen informiert."
- Lösungsquote: same question, but only "Ja" counts as positive.
- NPS: from the NPS question (0-10); NPS = %promoters (9-10) - %detractors (0-6).
"""

# Question identifiers (the CSV "frage" cell is "<code> : <text>").
INFO_LOESUNG_CODE = '21000002002300'  # "Konnten wir Ihr Anliegen lösen?"
NPS_CODE = '21000002004601'           # "NPS >|<"

# Answer sets for the Info/Lösung question.
INFO_POSITIVE_ANSWERS = {
    'ja',
    'noch nicht. ich wurde über das weitere vorgehen informiert.',
}
LOESUNG_POSITIVE_ANSWERS = {
    'ja',
}


def normalize_text(value):
    """Trim + collapse inner whitespace; keep original casing for storage."""
    if value is None:
        return ''
    return ' '.join(str(value).split())


def _norm_cmp(value):
    return normalize_text(value).lower()


def question_code(frage):
    """Extract the leading question code from a 'frage' cell."""
    s = normalize_text(frage)
    if not s:
        return ''
    # Format: "21000002004601 : NPS >|<"
    if ' : ' in s:
        return s.split(' : ', 1)[0].strip()
    return s.split(' ', 1)[0].strip()


def question_text(frage):
    s = normalize_text(frage)
    if ' : ' in s:
        return s.split(' : ', 1)[1].strip()
    return s


def classify_info(answer):
    """True/False if the Lösung-question answer is known, else None."""
    a = _norm_cmp(answer)
    if not a:
        return None
    return a in INFO_POSITIVE_ANSWERS


def classify_loesung(answer):
    a = _norm_cmp(answer)
    if not a:
        return None
    return a in LOESUNG_POSITIVE_ANSWERS


def parse_nps(answer):
    """Parse a 0-10 NPS rating; return int or None."""
    a = normalize_text(answer)
    if not a:
        return None
    try:
        val = int(round(float(a.replace(',', '.'))))
    except (ValueError, TypeError):
        return None
    if val < 0 or val > 10:
        return None
    return val


def nps_category(value):
    """'promoter' | 'neutral' | 'detractor' | None."""
    if value is None:
        return None
    if value >= 9:
        return 'promoter'
    if value >= 7:
        return 'neutral'
    return 'detractor'


def compute_nps(values):
    """values: iterable of ints (0-10). Returns dict with nps + breakdown."""
    promoters = neutrals = detractors = 0
    total = 0
    for v in values:
        if v is None:
            continue
        total += 1
        cat = nps_category(v)
        if cat == 'promoter':
            promoters += 1
        elif cat == 'neutral':
            neutrals += 1
        else:
            detractors += 1
    if total == 0:
        return {'nps': None, 'promoters': 0, 'neutrals': 0, 'detractors': 0, 'total': 0}
    nps = round((promoters - detractors) / total * 100, 2)
    return {
        'nps': nps,
        'promoters': promoters,
        'neutrals': neutrals,
        'detractors': detractors,
        'total': total,
    }


def is_nps_question(text):
    """Heuristic: a question is an NPS question if its text mentions 'NPS'."""
    return 'NPS' in (text or '').upper()


def is_loesung_question(text):
    """Heuristic: the 'Konnten wir Ihr Anliegen lösen?' question (Info/Lösung)."""
    return 'konnten wir ihr anliegen' in _norm_cmp(text)


def compute_survey_flags(answers, nps_code=None, loesung_code=None):
    """Derive (nps_value, loesung_answer, info_positive, loesung_positive) from a
    survey's answers.

    answers: iterable of dicts with keys 'code', 'text', 'antwort'.
    nps_code / loesung_code: explicit question codes (per-project mapping). When a
    code is None, the question is auto-detected from its text.
    """
    nps_value = None
    loesung_answer = None
    info_positive = None
    loesung_positive = None
    for a in answers:
        code = (a.get('code') or '').strip()
        text = a.get('text') or ''
        antwort = a.get('antwort') or ''
        is_nps = (code == nps_code) if nps_code else (nps_code is None and is_nps_question(text))
        is_loes = (code == loesung_code) if loesung_code else (loesung_code is None and is_loesung_question(text))
        if is_nps and nps_value is None:
            v = parse_nps(antwort)
            if v is not None:
                nps_value = v
        if is_loes and loesung_answer is None:
            norm = normalize_text(antwort)
            if norm:
                loesung_answer = norm[:255]
                info_positive = classify_info(antwort)
                loesung_positive = classify_loesung(antwort)
    return nps_value, loesung_answer, info_positive, loesung_positive


def quote_percent(positive, total):
    """Share in percent with 2 decimals, or None if no answers."""
    if not total:
        return None
    return round(positive / total * 100, 2)


def format_de(value, decimals=2):
    """Format a number in German style (comma decimal separator). None -> en dash."""
    if value is None:
        return '–'
    try:
        return ('{:,.' + str(decimals) + 'f}').format(float(value)).replace(',', '_').replace('.', ',').replace('_', '.')
    except (ValueError, TypeError):
        return '–'


def metric_bar_class(value, target_green, target_yellow):
    """Bootstrap progress-bar class for a metric vs. configurable Ziele."""
    if value is None:
        return 'bg-secondary'
    try:
        v = float(value)
        tg = float(target_green)
        ty = float(target_yellow)
    except (TypeError, ValueError):
        return 'bg-secondary'
    if v >= tg:
        return 'bg-success'
    if v >= ty:
        return 'bg-warning'
    return 'bg-danger'


def metric_bar_width(value, bar_min=0.0, bar_max=100.0):
    """Clamp a metric to 0–100 for progress-bar width."""
    if value is None:
        return 0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0
    if bar_max <= bar_min:
        return 0
    pct = (v - bar_min) / (bar_max - bar_min) * 100.0
    return max(0.0, min(100.0, pct))


def nps_bar_width(nps_value):
    """Map NPS (-100..100) to bar width 0..100."""
    return metric_bar_width(nps_value, bar_min=-100.0, bar_max=100.0)


DEFAULT_TEAM_VIEW_CARD = {
    'show_nps': True,
    'show_loesung': True,
    'show_info': True,
    'show_performance': True,
    'target_nps': 50.0,
    'target_loesung': 80.0,
    'target_info': 80.0,
    'target_performance': 80.0,
    'warn_nps': 0.0,
    'warn_loesung': 60.0,
    'warn_info': 60.0,
    'warn_performance': 50.0,
}


def team_view_card_settings_dict(setting_row):
    """Merge DB row with defaults into a plain dict for templates."""
    out = dict(DEFAULT_TEAM_VIEW_CARD)
    if setting_row is None:
        return out
    for key in out:
        if hasattr(setting_row, key):
            out[key] = getattr(setting_row, key)
    return out


def _counting_studie_filter(project_id):
    """SQLAlchemy filter clause list for counting-only survey types."""
    from sqlalchemy import false
    from app.models import ProjectKpiSource, KpiSurvey
    if not project_id:
        return []
    rows = ProjectKpiSource.query.filter_by(project_id=project_id).all()
    if not rows:
        return []
    types = [r.survey_type for r in rows if r.counts]
    if not types:
        return [false()]
    return [KpiSurvey.studie.in_(types)]


def members_kpi_quotes(project_id, member_ids, date_from=None, date_to=None):
    """Bulk KPI quotes per team member (counting survey types only).

    Optional date_from/date_to filter on antwort_date. Returns counts per KPI.
    """
    from app import db
    from app.models import KpiSurvey
    if not project_id or not member_ids:
        return {}
    filters = [KpiSurvey.project_id == project_id, KpiSurvey.team_member_id.in_(member_ids)]
    filters.extend(_counting_studie_filter(project_id))
    if date_from is not None:
        filters.append(KpiSurvey.antwort_date >= date_from)
    if date_to is not None:
        filters.append(KpiSurvey.antwort_date <= date_to)
    rows = db.session.query(
        KpiSurvey.team_member_id,
        KpiSurvey.info_positive,
        KpiSurvey.loesung_positive,
        KpiSurvey.nps_value,
    ).filter(*filters).all()
    buckets = {}
    for mid, info_p, loes_p, nps_v in rows:
        buckets.setdefault(mid, []).append((info_p, loes_p, nps_v))

    def _agg(items):
        info_pos = info_total = loes_pos = loes_total = 0
        nps_values = []
        for info_positive, loesung_positive, nps_value in items:
            if info_positive is not None:
                info_total += 1
                if info_positive:
                    info_pos += 1
            if loesung_positive is not None:
                loes_total += 1
                if loesung_positive:
                    loes_pos += 1
            if nps_value is not None:
                nps_values.append(nps_value)
        nps = compute_nps(nps_values)
        return {
            'info_quote': quote_percent(info_pos, info_total),
            'info_count': info_total,
            'loes_quote': quote_percent(loes_pos, loes_total),
            'loes_count': loes_total,
            'nps': nps['nps'],
            'nps_count': nps['total'],
            'surveys_total': len(items),
        }

    empty = {
        'info_quote': None, 'info_count': 0,
        'loes_quote': None, 'loes_count': 0,
        'nps': None, 'nps_count': 0, 'surveys_total': 0,
    }
    return {mid: _agg(buckets[mid]) if mid in buckets else dict(empty) for mid in member_ids}
