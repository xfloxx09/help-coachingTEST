"""KPI Qualität CSV import commit + prepared sidecar (web UI + import_worker subprocess)."""
import json
import os
from datetime import date, datetime

from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from app import db
from app.models import KpiAnswer, KpiImportBatch, KpiSurvey, TeamMember

PREPARED_SUFFIX = '.prepared.json'
_DELETE_CHUNK = 400
_DSID_BATCH = 400
_INSERT_FLUSH = 200

_FIELD_KEYS = (
    'antwort_date', 'be4', 'ma_kenner', 'studie', 'nps_value', 'loesung_answer',
    'info_positive', 'loesung_positive', 'fachkompetenz_stars', 'vertrieb_positive', 'answers',
)


def prepared_sidecar_path(csv_path):
    return csv_path + PREPARED_SUFFIX


def write_prepared_sidecar(csv_path, surveys, stats):
    path = prepared_sidecar_path(csv_path)
    payload = {
        'surveys': [_serialize_survey(s) for s in surveys],
        'stats': {
            'matched_team': stats.get('matched_team', 0),
            'matched_member': stats.get('matched_member', 0),
            'unassigned': stats.get('unassigned', 0),
        },
    }
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(',', ':'))
    return path


def read_prepared_sidecar(csv_path):
    path = prepared_sidecar_path(csv_path)
    if not os.path.isfile(path):
        return None, None
    with open(path, 'r', encoding='utf-8') as fh:
        payload = json.load(fh)
    surveys = [_deserialize_survey(s) for s in payload.get('surveys') or []]
    stats = payload.get('stats') or {}
    return surveys, stats


def remove_prepared_sidecar(csv_path):
    path = prepared_sidecar_path(csv_path)
    if os.path.isfile(path):
        try:
            os.unlink(path)
        except OSError:
            pass


def _serialize_survey(s):
    row = dict(s)
    for key in ('antwort_date', 'kontakt_date'):
        val = row.get(key)
        if isinstance(val, (date, datetime)):
            row[key] = val.isoformat()[:10]
    return row


def _deserialize_survey(s):
    row = dict(s)
    for key in ('antwort_date', 'kontakt_date'):
        val = row.get(key)
        if isinstance(val, str) and val:
            row[key] = date.fromisoformat(val[:10])
    return row


def _is_excel_error(value):
    v = (value or '').strip().upper()
    return v in ('#NAME?', '#DIV/0!', '#N/A', '#NULL!', '#NUM!', '#REF!', '#VALUE!')


def _dedupe_answers(answers):
    by_code = {}
    for a in answers or []:
        code = (a.get('code') or '').strip()
        if not code:
            continue
        entry = {
            'code': code,
            'text': (a.get('text') or '').strip(),
            'antwort': (a.get('antwort') or '').strip(),
        }
        by_code[code] = entry
    return list(by_code.values())


def _answers_key(answers):
    if not answers:
        return ()
    return tuple(sorted((a['code'], a['antwort']) for a in _dedupe_answers(answers)))


def _snapshot_from_dict(s):
    return {
        'antwort_date': s.get('antwort_date'),
        'be4': (s.get('be4') or '').strip(),
        'ma_kenner': (s.get('ma_kenner') or '').strip(),
        'studie': (s.get('studie') or '').strip(),
        'nps_value': s.get('nps_value'),
        'loesung_answer': (s.get('loesung_answer') or '').strip(),
        'info_positive': s.get('info_positive'),
        'loesung_positive': s.get('loesung_positive'),
        'fachkompetenz_stars': s.get('fachkompetenz_stars'),
        'vertrieb_positive': s.get('vertrieb_positive'),
        'answers': _answers_key(s.get('answers') or []),
    }


def _snapshot_from_db(sv, answers_key):
    return {
        'antwort_date': sv.antwort_date,
        'be4': (sv.be4 or '').strip(),
        'ma_kenner': (sv.ma_kenner or '').strip(),
        'studie': (sv.studie or '').strip(),
        'nps_value': sv.nps_value,
        'loesung_answer': (sv.loesung_answer or '').strip(),
        'info_positive': sv.info_positive,
        'loesung_positive': sv.loesung_positive,
        'fachkompetenz_stars': sv.fachkompetenz_stars,
        'vertrieb_positive': sv.vertrieb_positive,
        'answers': answers_key or (),
    }


def _field_diffs(a, b):
    for key in _FIELD_KEYS:
        if a.get(key) != b.get(key):
            return True
    return False


def _answers_only_excel_noise(incoming_answers, db_answers):
    in_map = {a['code']: a['antwort'] for a in _dedupe_answers(incoming_answers)}
    ex_map = {a['code']: a['antwort'] for a in _dedupe_answers(db_answers)}
    has_diff = False
    for code in set(in_map) | set(ex_map):
        inc = in_map.get(code, '')
        exc = ex_map.get(code, '')
        if inc == exc:
            continue
        has_diff = True
        if not (_is_excel_error(inc) and exc and not _is_excel_error(exc)):
            return False
    return has_diff


def _surveys_equal(incoming, existing_snap, ex_rich_answers):
    in_snap = _snapshot_from_dict(incoming)
    if _field_diffs(in_snap, existing_snap):
        return False
    if in_snap.get('answers') == existing_snap.get('answers'):
        return True
    return _answers_only_excel_noise(incoming.get('answers'), ex_rich_answers)


def _load_answers_by_survey_id(survey_ids):
    if not survey_ids:
        return {}
    buckets = {}
    for i in range(0, len(survey_ids), _DSID_BATCH):
        chunk = survey_ids[i:i + _DSID_BATCH]
        rows = db.session.query(
            KpiAnswer.survey_id, KpiAnswer.frage_code, KpiAnswer.antwort,
        ).filter(KpiAnswer.survey_id.in_(chunk)).all()
        for sid, code, antwort in rows:
            buckets.setdefault(sid, []).append(((code or '').strip(), (antwort or '').strip()))
    return {sid: tuple(sorted(items)) for sid, items in buckets.items()}


def _load_rich_answers_by_survey_id(survey_ids):
    if not survey_ids:
        return {}
    buckets = {}
    for i in range(0, len(survey_ids), _DSID_BATCH):
        chunk = survey_ids[i:i + _DSID_BATCH]
        rows = db.session.query(
            KpiAnswer.survey_id, KpiAnswer.frage_code, KpiAnswer.frage_text, KpiAnswer.antwort,
        ).filter(KpiAnswer.survey_id.in_(chunk)).all()
        for sid, code, ftext, antwort in rows:
            buckets.setdefault(sid, []).append({
                'code': (code or '').strip(),
                'text': (ftext or '').strip(),
                'antwort': (antwort or '').strip(),
            })
    return buckets


def _insert_survey(batch_id, s):
    survey = KpiSurvey(
        datensatz_id=s['datensatz_id'],
        interviewnummer=s['interviewnummer'],
        antwort_date=s['antwort_date'],
        kontakt_date=s['kontakt_date'],
        be4=(s['be4'] or '')[:100],
        ma_kenner=(s['ma_kenner'] or '')[:50],
        ospname=s['ospname'],
        kampagne=s['kampagne'],
        studie=s['studie'],
        queue=s['queue'],
        vorname=s['vorname'],
        nachname=s['nachname'],
        team_id=s['team_id'],
        project_id=s['project_id'],
        team_member_id=s['team_member_id'],
        nps_value=s['nps_value'],
        loesung_answer=s['loesung_answer'],
        info_positive=s['info_positive'],
        loesung_positive=s['loesung_positive'],
        fachkompetenz_stars=s.get('fachkompetenz_stars'),
        vertrieb_positive=s.get('vertrieb_positive'),
        batch_id=batch_id,
    )
    for a in s['answers']:
        survey.answers.append(KpiAnswer(
            frage_code=(a['code'] or '')[:40],
            frage_text=a['text'],
            antwort=a['antwort'],
        ))
    db.session.add(survey)
    return survey


def _delete_survey_ids(ids):
    if not ids:
        return
    db.session.query(KpiAnswer).filter(KpiAnswer.survey_id.in_(ids)).delete(synchronize_session=False)
    db.session.query(KpiSurvey).filter(KpiSurvey.id.in_(ids)).delete(synchronize_session=False)


def _delete_surveys_in_range(date_from, date_to):
    deleted = 0
    while True:
        ids = [
            r[0] for r in db.session.query(KpiSurvey.id).filter(
                KpiSurvey.antwort_date >= date_from,
                KpiSurvey.antwort_date <= date_to,
            ).limit(_DELETE_CHUNK).all()
        ]
        if not ids:
            break
        _delete_survey_ids(ids)
        db.session.commit()
        deleted += len(ids)
    return deleted


def _delete_surveys_by_dsid(dsids):
    deleted = 0
    dsid_list = list(dsids)
    for i in range(0, len(dsid_list), _DSID_BATCH):
        batch = dsid_list[i:i + _DSID_BATCH]
        while True:
            ids = [
                r[0] for r in db.session.query(KpiSurvey.id).filter(
                    KpiSurvey.datensatz_id.in_(batch),
                ).limit(_DELETE_CHUNK).all()
            ]
            if not ids:
                break
            _delete_survey_ids(ids)
            db.session.commit()
            deleted += len(ids)
    return deleted


def _load_existing_by_dsid(dsids):
    existing = {}
    dsid_list = list(dsids)
    for i in range(0, len(dsid_list), _DSID_BATCH):
        batch = dsid_list[i:i + _DSID_BATCH]
        for sv in KpiSurvey.query.filter(KpiSurvey.datensatz_id.in_(batch)).all():
            existing[sv.datensatz_id] = sv
    return existing


def _backfill_survey_links():
    rows = (
        KpiSurvey.query.filter(
            KpiSurvey.team_member_id.isnot(None),
            or_(KpiSurvey.project_id.is_(None), KpiSurvey.team_id.is_(None)),
        )
        .options(joinedload(KpiSurvey.team_member).joinedload(TeamMember.team))
        .limit(5000)
        .all()
    )
    updated = 0
    for sv in rows:
        member = sv.team_member
        if not member or not member.team:
            continue
        changed = False
        if sv.project_id != member.team.project_id:
            sv.project_id = member.team.project_id
            changed = True
        if sv.team_id != member.team_id:
            sv.team_id = member.team_id
            changed = True
        if changed:
            updated += 1
    if updated:
        db.session.flush()
    return updated


def _empty_conflicts():
    return {
        'changed_count': 0,
        'unchanged_count': 0,
        'new_count': 0,
        'orphan_in_range_count': 0,
    }


def format_commit_flash(batch, commit_result):
    c = commit_result.get('conflicts') or _empty_conflicts()
    parts = [f'{commit_result["inserted"]} Befragungen importiert']
    if commit_result.get('skipped_unchanged'):
        parts.append(f'{commit_result["skipped_unchanged"]} unverändert übersprungen')
    if commit_result.get('skipped_changed'):
        parts.append(
            f'{commit_result["skipped_changed"]} geändert übersprungen '
            f'(zum Überschreiben beim Import „Vorhandene ersetzen“ aktivieren)'
        )
    if commit_result.get('deleted'):
        parts.append(f'{commit_result["deleted"]} vorhandene ersetzt/gelöscht')
    if commit_result['inserted'] == 0 and (c.get('changed_count') or c.get('unchanged_count')):
        return (
            'warning',
            'Keine neuen Befragungen importiert. Alle Datensätze existieren bereits. '
            'Aktivieren Sie „Vorhandene KPI-Daten überschreiben“, um geänderte oder '
            'den Zeitraum zu ersetzen.',
        )
    return (
        'success',
        f'KPI-Import abgeschlossen: {", ".join(parts)}. '
        f'{batch.surveys_matched_team} mit Team, {batch.surveys_matched_member} mit Agent, '
        f'{batch.surveys_unassigned} ohne Team (unassigned).',
    )


def commit_surveys(
    filename, surveys, stats, overwrite=False, progress_cb=None, imported_by_id=None,
    analyze_conflicts=False,
):
    """Commit prepared KPI surveys. Conflict analysis is preview-only unless explicitly requested."""
    total = len(surveys)

    def _progress(done, message):
        if progress_cb:
            progress_cb(done, total, message)

    if analyze_conflicts:
        from app.admin import _kpi_analyze_import_conflicts
        _progress(0, 'Konflikte prüfen…')
        conflicts = _kpi_analyze_import_conflicts(surveys)
    else:
        _progress(0, 'Import vorbereiten…')
        conflicts = _empty_conflicts()

    dates = [s['antwort_date'] for s in surveys if s['antwort_date']]
    date_from = min(dates) if dates else None
    date_to = max(dates) if dates else None

    batch = KpiImportBatch(
        filename=(filename or '')[:255],
        imported_by_id=imported_by_id,
        date_from=date_from,
        date_to=date_to,
        surveys_total=len(surveys),
        surveys_matched_team=stats.get('matched_team', 0),
        surveys_matched_member=stats.get('matched_member', 0),
        surveys_unassigned=stats.get('unassigned', 0),
    )
    db.session.add(batch)
    db.session.flush()

    result = {
        'inserted': 0,
        'skipped_unchanged': 0,
        'skipped_changed': 0,
        'deleted': 0,
        'overwrite': overwrite,
        'conflicts': conflicts,
    }

    incoming_dsids = {s['datensatz_id'] for s in surveys}

    if overwrite:
        _progress(0, 'Vorhandene Befragungen ersetzen…')
        deleted = 0
        if date_from and date_to:
            deleted += _delete_surveys_in_range(date_from, date_to)
        if incoming_dsids:
            deleted += _delete_surveys_by_dsid(incoming_dsids)
        result['deleted'] = deleted
        for i, s in enumerate(surveys):
            _insert_survey(batch.id, s)
            result['inserted'] += 1
            if i and i % _INSERT_FLUSH == 0:
                db.session.flush()
                _progress(i, f'{i}/{total} Befragungen speichern…')
    else:
        existing_by_dsid = _load_existing_by_dsid(incoming_dsids)
        survey_ids = [sv.id for sv in existing_by_dsid.values()]
        answers_by_id = _load_answers_by_survey_id(survey_ids)
        rich_by_id = _load_rich_answers_by_survey_id(survey_ids)
        for i, s in enumerate(surveys):
            existing = existing_by_dsid.get(s['datensatz_id'])
            if existing:
                ex_snap = _snapshot_from_db(existing, answers_by_id.get(existing.id))
                if _surveys_equal(s, ex_snap, rich_by_id.get(existing.id, [])):
                    result['skipped_unchanged'] += 1
                else:
                    result['skipped_changed'] += 1
            else:
                _insert_survey(batch.id, s)
                result['inserted'] += 1
            if i and i % _INSERT_FLUSH == 0:
                db.session.flush()
                _progress(i, f'{i}/{total} Befragungen prüfen…')

    _progress(total, 'Abschluss…')
    batch.surveys_total = result['inserted']
    _backfill_survey_links()
    db.session.commit()
    return batch, date_from, date_to, result
