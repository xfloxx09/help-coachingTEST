"""KPI logic for SMS-survey raw data.

KPIs derived from survey answers:
- Informationsquote / Lösungsquote: „Konnten wir Ihr Anliegen lösen?“
- NPS: 0–10 scale
- Fachkompetenz: 0–5 stars
- Vertriebliche Ansprache: positive/negative offer question → %
"""

# Question identifiers (the CSV "frage" cell is "<code> : <text>").
INFO_LOESUNG_CODE = '21000002002300'  # "Konnten wir Ihr Anliegen lösen?"
NPS_CODE = '21000002004601'           # "NPS >|<"
FACHKOMPETENZ_CODE = '21000002001200'  # "Wie bewerten Sie die Fachkompetenz …?"
VERTRIEB_CODE = '21000002001800'       # "Wurde Ihnen … ein Produkt oder Tarif … angeboten?"

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


def is_fachkompetenz_question(text):
    return 'fachkompetenz' in _norm_cmp(text)


def is_vertrieb_question(text):
    t = _norm_cmp(text)
    return 'produkt oder tarif' in t or 'festnetz oder mobilfunk' in t


def parse_stars(answer):
    """Parse a 0–5 star rating; return int or None."""
    a = normalize_text(answer)
    if not a:
        return None
    try:
        val = int(round(float(a.replace(',', '.').split()[0])))
    except (ValueError, TypeError, IndexError):
        return None
    if val < 0 or val > 5:
        return None
    return val


def classify_vertrieb(answer):
    """True = positive offer, False = negative, None = unknown."""
    a = _norm_cmp(answer)
    if not a:
        return None
    if a == 'nein' or a.startswith('nein ') or a.startswith('nein,'):
        return False
    return True


def compute_survey_flags(
    answers,
    nps_code=None,
    loesung_code=None,
    fachkompetenz_code=None,
    vertrieb_code=None,
):
    """Derive precomputed KPI fields from a survey's answers.

    answers: iterable of dicts with keys 'code', 'text', 'antwort'.
    Explicit *_code overrides auto-detection from question text.
    """
    nps_value = None
    loesung_answer = None
    info_positive = None
    loesung_positive = None
    fachkompetenz_stars = None
    vertrieb_positive = None
    for a in answers:
        code = (a.get('code') or '').strip()
        text = a.get('text') or ''
        antwort = a.get('antwort') or ''
        is_nps = (code == nps_code) if nps_code else (nps_code is None and is_nps_question(text))
        is_loes = (code == loesung_code) if loesung_code else (loesung_code is None and is_loesung_question(text))
        is_fach = (
            (code == fachkompetenz_code)
            if fachkompetenz_code
            else (fachkompetenz_code is None and (code == FACHKOMPETENZ_CODE or is_fachkompetenz_question(text)))
        )
        is_vert = (
            (code == vertrieb_code)
            if vertrieb_code
            else (vertrieb_code is None and (code == VERTRIEB_CODE or is_vertrieb_question(text)))
        )
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
        if is_fach and fachkompetenz_stars is None:
            stars = parse_stars(antwort)
            if stars is not None:
                fachkompetenz_stars = stars
        if is_vert and vertrieb_positive is None:
            vp = classify_vertrieb(antwort)
            if vp is not None:
                vertrieb_positive = vp
    return (
        nps_value, loesung_answer, info_positive, loesung_positive,
        fachkompetenz_stars, vertrieb_positive,
    )


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


def metric_status(value, target_green, target_yellow):
    """Return 'ok' | 'warn' | 'bad' | 'na' for KPI chip styling."""
    if value is None:
        return 'na'
    try:
        v = float(value)
        tg = float(target_green)
        ty = float(target_yellow)
    except (TypeError, ValueError):
        return 'na'
    if v >= tg:
        return 'ok'
    if v >= ty:
        return 'warn'
    return 'bad'


def metric_status_max(value, target_green, target_yellow):
    """Like metric_status but lower values are better (e.g. Nacharbeit, Idle)."""
    if value is None:
        return 'na'
    try:
        v = float(value)
        tg = float(target_green)
        ty = float(target_yellow)
    except (TypeError, ValueError):
        return 'na'
    if v <= tg:
        return 'ok'
    if v <= ty:
        return 'warn'
    return 'bad'


DEFAULT_TEAM_VIEW_CARD = {
    'show_nps': True,
    'show_loesung': True,
    'show_info': True,
    'show_performance': True,
    'show_fachkompetenz': True,
    'show_vertrieb': True,
    'target_nps': 50.0,
    'target_loesung': 80.0,
    'target_info': 80.0,
    'target_performance': 80.0,
    'target_fachkompetenz': 4.0,
    'target_vertrieb': 80.0,
    'warn_nps': 0.0,
    'warn_loesung': 60.0,
    'warn_info': 60.0,
    'warn_performance': 50.0,
    'warn_fachkompetenz': 3.0,
    'warn_vertrieb': 60.0,
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
    from sqlalchemy import or_
    from app import db
    from app.models import KpiSurvey
    if not project_id or not member_ids:
        return {}
    filters = [
        KpiSurvey.team_member_id.in_(member_ids),
        or_(KpiSurvey.project_id == project_id, KpiSurvey.project_id.is_(None)),
    ]
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
        KpiSurvey.fachkompetenz_stars,
        KpiSurvey.vertrieb_positive,
    ).filter(*filters).all()
    buckets = {}
    for mid, info_p, loes_p, nps_v, fach_s, vert_p in rows:
        buckets.setdefault(mid, []).append((info_p, loes_p, nps_v, fach_s, vert_p))

    def _agg(items):
        info_pos = info_total = loes_pos = loes_total = 0
        nps_values = []
        fach_values = []
        vert_pos = vert_total = 0
        for info_positive, loesung_positive, nps_value, fachkompetenz_stars, vertrieb_positive in items:
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
            if fachkompetenz_stars is not None:
                fach_values.append(fachkompetenz_stars)
            if vertrieb_positive is not None:
                vert_total += 1
                if vertrieb_positive:
                    vert_pos += 1
        nps = compute_nps(nps_values)
        fach_avg = round(sum(fach_values) / len(fach_values), 2) if fach_values else None
        return {
            'info_quote': quote_percent(info_pos, info_total),
            'info_count': info_total,
            'loes_quote': quote_percent(loes_pos, loes_total),
            'loes_count': loes_total,
            'nps': nps['nps'],
            'nps_count': nps['total'],
            'fachkompetenz': fach_avg,
            'fachkompetenz_count': len(fach_values),
            'vertrieb_quote': quote_percent(vert_pos, vert_total),
            'vertrieb_count': vert_total,
            'surveys_total': len(items),
        }

    empty = {
        'info_quote': None, 'info_count': 0,
        'loes_quote': None, 'loes_count': 0,
        'nps': None, 'nps_count': 0,
        'fachkompetenz': None, 'fachkompetenz_count': 0,
        'vertrieb_quote': None, 'vertrieb_count': 0,
        'surveys_total': 0,
    }
    return {mid: _agg(buckets[mid]) if mid in buckets else dict(empty) for mid in member_ids}


def kpi_features_enabled():
    """Platform-wide switch: survey KPI features in user-facing UI."""
    try:
        from app.models import PlatformSettings
        row = PlatformSettings.query.get(1)
        if row is None:
            return True
        return bool(row.kpi_features_enabled)
    except Exception:
        return True


def coaching_impact_window_days():
    """Default Wirkungsfenster (days) for Coaching VS KPI when user has not chosen one yet."""
    return 14
