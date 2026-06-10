"""Coaching VS KPI range wizard: timeline payload for coaching Zeitraum selection."""
from datetime import datetime, timedelta

from sqlalchemy import cast, Date, func

from app import db
from app.models import Coaching, KpiSurvey, ProductivityInterval


def _dates_by_member(rows):
    out = {}
    for member_id, d in rows:
        if member_id is None or d is None:
            continue
        day = d.date() if isinstance(d, datetime) else d
        out.setdefault(int(member_id), set()).add(day)
    return out


def load_survey_dates_by_member(kpi_filters):
    rows = (
        db.session.query(KpiSurvey.team_member_id, KpiSurvey.antwort_date)
        .filter(*kpi_filters)
        .all()
    )
    return _dates_by_member(rows)


def load_prod_dates_by_member(prod_filters):
    rows = (
        db.session.query(
            ProductivityInterval.team_member_id,
            cast(ProductivityInterval.slot_at, Date),
        )
        .filter(*prod_filters)
        .all()
    )
    return _dates_by_member(rows)


def event_has_vn_context(member_id, coaching_date, survey_dates, prod_dates, show_surveys, show_productivity):
    """True if member has KPI data before and on/after coaching day (for timeline hint)."""
    qual_ok = False
    prod_ok = False
    if show_surveys:
        dates = survey_dates.get(int(member_id), set())
        qual_ok = bool(dates) and any(d < coaching_date for d in dates) and any(d >= coaching_date for d in dates)
    if show_productivity:
        dates = prod_dates.get(int(member_id), set())
        prod_ok = bool(dates) and any(d < coaching_date for d in dates) and any(d >= coaching_date for d in dates)
    if show_surveys and show_productivity:
        return qual_ok or prod_ok
    if show_surveys:
        return qual_ok
    if show_productivity:
        return prod_ok
    return False


def _serialize_date_sets(date_sets):
    return {
        str(mid): sorted(d.isoformat() for d in days)
        for mid, days in date_sets.items()
    }


def build_activity_map_payload(
    coaching_filters,
    kpi_filters,
    prod_filters,
    show_surveys,
    show_productivity,
):
    """Timeline data for the range wizard; one Zeitraum for all coachings."""
    coach_rows = (
        db.session.query(
            Coaching.team_member_id,
            cast(Coaching.coaching_date, Date),
        )
        .filter(*coaching_filters)
        .filter(Coaching.team_member_id.isnot(None))
        .all()
    )
    coaching_events = []
    for member_id, d in coach_rows:
        if d is None:
            continue
        coaching_events.append({'member_id': int(member_id), 'date': d.isoformat()})

    survey_rows = []
    if show_surveys:
        survey_rows = (
            db.session.query(KpiSurvey.antwort_date, func.count(KpiSurvey.id))
            .filter(*kpi_filters)
            .group_by(KpiSurvey.antwort_date)
            .all()
        )
    prod_rows = []
    if show_productivity:
        prod_rows = (
            db.session.query(
                cast(ProductivityInterval.slot_at, Date),
                func.count(ProductivityInterval.id),
            )
            .filter(*prod_filters)
            .group_by(cast(ProductivityInterval.slot_at, Date))
            .all()
        )

    survey_dates = load_survey_dates_by_member(kpi_filters) if show_surveys else {}
    prod_dates = load_prod_dates_by_member(prod_filters) if show_productivity else {}

    by_date = {}
    actionable_by_date = {}

    for ev in coaching_events:
        key = ev['date']
        mid = ev['member_id']
        d = datetime.strptime(key, '%Y-%m-%d').date()
        by_date.setdefault(key, {'coachings': 0, 'surveys': 0, 'productivity': 0})
        by_date[key]['coachings'] += 1
        if event_has_vn_context(
            mid, d, survey_dates, prod_dates, show_surveys, show_productivity,
        ):
            actionable_by_date[key] = actionable_by_date.get(key, 0) + 1

    for d, cnt in survey_rows:
        if d is None:
            continue
        key = d.isoformat()
        by_date.setdefault(key, {'coachings': 0, 'surveys': 0, 'productivity': 0})
        by_date[key]['surveys'] = int(cnt)
    for d, cnt in prod_rows:
        if d is None:
            continue
        key = d.isoformat()
        by_date.setdefault(key, {'coachings': 0, 'surveys': 0, 'productivity': 0})
        by_date[key]['productivity'] = int(cnt)

    if not by_date and not coaching_events:
        return {
            'days': [],
            'min_date': None,
            'max_date': None,
            'show_surveys': show_surveys,
            'show_productivity': show_productivity,
            'actionable_coaching_count': 0,
            'coaching_events': [],
            'survey_dates_by_member': _serialize_date_sets(survey_dates),
            'prod_dates_by_member': _serialize_date_sets(prod_dates),
        }

    dates = sorted(by_date.keys())
    min_d = datetime.strptime(dates[0], '%Y-%m-%d').date()
    max_d = datetime.strptime(dates[-1], '%Y-%m-%d').date()
    pad_start = min_d - timedelta(days=7)
    pad_end = max_d + timedelta(days=7)

    days_out = []
    cur = pad_start
    while cur <= pad_end:
        key = cur.isoformat()
        bucket = by_date.get(key, {'coachings': 0, 'surveys': 0, 'productivity': 0})
        act = actionable_by_date.get(key, 0)
        days_out.append({
            'date': key,
            'label': cur.strftime('%d.%m.'),
            'coachings': bucket['coachings'],
            'actionable_coachings': act,
            'surveys': bucket['surveys'] if show_surveys else 0,
            'productivity': bucket['productivity'] if show_productivity else 0,
            'has_productivity': bool(show_productivity and bucket['productivity'] > 0),
        })
        cur += timedelta(days=1)

    actionable_total = sum(actionable_by_date.values())

    return {
        'days': days_out,
        'min_date': pad_start.isoformat(),
        'max_date': pad_end.isoformat(),
        'show_surveys': show_surveys,
        'show_productivity': show_productivity,
        'actionable_coaching_count': actionable_total,
        'coaching_events': coaching_events,
        'survey_dates_by_member': _serialize_date_sets(survey_dates),
        'prod_dates_by_member': _serialize_date_sets(prod_dates),
    }
