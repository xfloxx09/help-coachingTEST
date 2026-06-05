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
