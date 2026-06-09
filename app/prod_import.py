"""Productivity CSV import commit + interval sidecar (used by web UI and import_worker subprocess)."""
import json
import os
from datetime import datetime, time

from app import db
from app.models import ProductivityImportBatch, ProductivityInterval
from app import productivity as productivity_logic

INTERVALS_SIDE_SUFFIX = '.intervals.json'
_DELETE_CHUNK = 2000
_INSERT_CHUNK = 500


def intervals_sidecar_path(temp_path):
    return temp_path + INTERVALS_SIDE_SUFFIX


def write_intervals_sidecar(temp_path, intervals):
    path = intervals_sidecar_path(temp_path)
    payload = []
    for iv in intervals:
        row = {}
        for key, val in iv.items():
            if isinstance(val, datetime):
                row[key] = val.isoformat()
            elif isinstance(val, (str, int, float, bool)) or val is None:
                row[key] = val
        payload.append(row)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(',', ':'))
    return path


def read_intervals_sidecar(path):
    with open(path, 'r', encoding='utf-8') as fh:
        raw = json.load(fh)
    intervals = []
    for iv in raw:
        row = dict(iv)
        sa = row.get('slot_at')
        if isinstance(sa, str):
            row['slot_at'] = datetime.fromisoformat(sa)
        intervals.append(row)
    return intervals


def remove_intervals_sidecar(temp_path):
    path = intervals_sidecar_path(temp_path)
    if os.path.isfile(path):
        try:
            os.unlink(path)
        except OSError:
            pass


def commit_intervals(filename, intervals, overwrite=False, progress_cb=None, imported_by_id=None):
    dates = [iv['slot_at'].date() for iv in intervals if iv.get('slot_at')]
    date_from = min(dates) if dates else None
    date_to = max(dates) if dates else None
    total = len(intervals)

    def _progress(stored, message):
        if progress_cb:
            progress_cb(stored, total, message)

    _progress(0, 'Vorhandene Intervalle prüfen…')
    deleted = 0
    if overwrite and date_from and date_to:
        slot_lo = datetime.combine(date_from, time.min)
        slot_hi = datetime.combine(date_to, time.max)
        while True:
            ids = [
                r[0] for r in db.session.query(ProductivityInterval.id).filter(
                    ProductivityInterval.slot_at >= slot_lo,
                    ProductivityInterval.slot_at <= slot_hi,
                ).limit(_DELETE_CHUNK).all()
            ]
            if not ids:
                break
            ProductivityInterval.query.filter(
                ProductivityInterval.id.in_(ids),
            ).delete(synchronize_session=False)
            db.session.commit()
            deleted += len(ids)
            _progress(0, f'{deleted:,} alte Intervalle gelöscht…')

    batch = ProductivityImportBatch(
        filename=filename,
        imported_by_id=imported_by_id,
        date_from=date_from,
        date_to=date_to,
        rows_total=len(intervals),
        intervals_stored=0,
        matched_member=sum(1 for iv in intervals if iv.get('team_member_id')),
        unmatched_member=sum(1 for iv in intervals if not iv.get('team_member_id')),
    )
    db.session.add(batch)
    db.session.flush()

    stored = 0
    for i in range(0, len(intervals), _INSERT_CHUNK):
        part = intervals[i:i + _INSERT_CHUNK]
        objs = []
        for iv in part:
            objs.append(ProductivityInterval(
                batch_id=batch.id,
                team_member_id=iv.get('team_member_id'),
                team_id=iv.get('team_id'),
                project_id=iv.get('project_id'),
                slot_at=iv['slot_at'],
                interval_sec=int(iv.get('interval_sec') or productivity_logic.INTERVAL_DEFAULT),
                sign_on_sec=iv.get('sign_on_sec') or 0,
                prod_sec=iv.get('prod_sec') or 0,
                nach_sec=iv.get('nach_sec') or 0,
                idle_sec=iv.get('idle_sec') or 0,
                pause_sec=iv.get('pause_sec') or 0,
                calls=iv.get('calls') or 0,
                works_beendet=iv.get('works_beendet') or 0,
                sign_on_pct=iv.get('sign_on_pct'),
                prod_pct=iv.get('prod_pct'),
                nach_pct=iv.get('nach_pct'),
                idle_pct=iv.get('idle_pct'),
                nach_per_call=iv.get('nach_per_call'),
                kpi_denom=iv.get('kpi_denom'),
            ))
        db.session.bulk_save_objects(objs)
        db.session.commit()
        stored += len(objs)
        _progress(stored, f'{stored:,}/{total:,} Intervalle speichern…')
    batch.intervals_stored = stored
    _progress(total, 'Abschluss…')
    db.session.commit()
    return batch, deleted
