"""Heavy import/revert jobs in a separate OS process (never blocks gunicorn workers)."""
import json
import os
import sys
import time
import tempfile

from sqlalchemy import create_engine, text

JOB_DIR = os.path.join(tempfile.gettempdir(), 'coaching_import_jobs')


def _job_path(job_id):
    return os.path.join(JOB_DIR, f'{job_id}.json')


def _job_write(job_id, user_id, payload):
    os.makedirs(JOB_DIR, exist_ok=True)
    data = {**payload, 'user_id': user_id, 'updated_at': time.time()}
    with open(_job_path(job_id), 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False)


def _normalize_db_url():
    for key in (
        'DATABASE_URL', 'DATABASE_PRIVATE_URL', 'DATABASE_PUBLIC_URL',
        'RAILWAY_DATABASE_URL', 'SQLALCHEMY_DATABASE_URI',
    ):
        url = os.environ.get(key)
        if url:
            if url.startswith('postgres://'):
                url = url.replace('postgres://', 'postgresql://', 1)
            return url
    raise RuntimeError('No database URL in environment')


def _make_engine():
    return create_engine(
        _normalize_db_url(),
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=True,
        isolation_level='READ COMMITTED',
    )


def _progress(job_id, user_id, deleted, total, message):
    pct = 5 + int((deleted / max(total, 1)) * 90) if total else 50
    _job_write(job_id, user_id, {
        'status': 'running', 'pct': pct, 'message': message,
    })


def run_kpi_revert(batch_id, job_id, user_id):
    engine = _make_engine()
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text('SELECT surveys_total FROM kpi_import_batches WHERE id = :id'),
                {'id': batch_id},
            ).fetchone()
            if not row:
                raise ValueError(f'Import-Batch #{batch_id} nicht gefunden.')
            total = int(row[0] or 0)

        _job_write(job_id, user_id, {
            'status': 'running', 'pct': 2,
            'message': f'Rückgängig machen: {total} Befragungen…',
        })

        deleted = 0
        chunk = 150
        while True:
            with engine.begin() as conn:
                ids = [
                    r[0] for r in conn.execute(
                        text('SELECT id FROM kpi_surveys WHERE batch_id = :bid LIMIT :lim'),
                        {'bid': batch_id, 'lim': chunk},
                    ).fetchall()
                ]
                if not ids:
                    break
                conn.execute(
                    text('DELETE FROM kpi_answers WHERE survey_id = ANY(:ids)'),
                    {'ids': ids},
                )
                conn.execute(
                    text('DELETE FROM kpi_surveys WHERE id = ANY(:ids)'),
                    {'ids': ids},
                )
            deleted += len(ids)
            _progress(job_id, user_id, deleted, total, f'{deleted}/{total} Befragungen gelöscht…')
            time.sleep(0.12)

        with engine.begin() as conn:
            conn.execute(
                text('DELETE FROM kpi_import_batches WHERE id = :id'),
                {'id': batch_id},
            )

        _job_write(job_id, user_id, {
            'status': 'done',
            'pct': 100,
            'message': 'Import rückgängig gemacht.',
            'done_url': f'/admin/import_kpi_csv/done/{job_id}',
            'flash_category': 'success',
            'flash_message': f'Import rückgängig gemacht: {deleted} Befragungen gelöscht.',
        })
    except Exception as e:
        _job_write(job_id, user_id, {'status': 'error', 'pct': 0, 'message': str(e)})
    finally:
        engine.dispose()


def run_prod_revert(batch_id, job_id, user_id):
    engine = _make_engine()
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text('SELECT intervals_stored FROM productivity_import_batches WHERE id = :id'),
                {'id': batch_id},
            ).fetchone()
            if not row:
                raise ValueError(f'Produktivitäts-Import #{batch_id} nicht gefunden.')
            total = int(row[0] or 0)

        _job_write(job_id, user_id, {
            'status': 'running', 'pct': 2,
            'message': f'Zurücksetzen: {total} Intervalle…',
        })

        deleted = 0
        chunk = 500
        while True:
            with engine.begin() as conn:
                n = conn.execute(
                    text(
                        'DELETE FROM productivity_intervals WHERE id IN ('
                        'SELECT id FROM productivity_intervals WHERE batch_id = :bid LIMIT :lim'
                        ')'
                    ),
                    {'bid': batch_id, 'lim': chunk},
                ).rowcount
            if not n:
                break
            deleted += n
            _progress(job_id, user_id, deleted, total, f'{deleted}/{total} Intervalle gelöscht…')
            time.sleep(0.08)

        with engine.begin() as conn:
            conn.execute(
                text('DELETE FROM productivity_import_batches WHERE id = :id'),
                {'id': batch_id},
            )

        _job_write(job_id, user_id, {
            'status': 'done',
            'pct': 100,
            'message': 'Import zurückgesetzt.',
            'done_url': f'/admin/import_productivity_csv/done/{job_id}',
            'flash_category': 'success',
            'flash_message': (
                f'Produktivitäts-Import #{batch_id} zurückgesetzt ({deleted} Intervalle gelöscht).'
            ),
        })
    except Exception as e:
        _job_write(job_id, user_id, {'status': 'error', 'pct': 0, 'message': str(e)})
    finally:
        engine.dispose()


def run_prod_import(job_id, user_id, intervals_path, filename, overwrite_flag):
    _job_write(job_id, user_id, {
        'status': 'running', 'pct': 2, 'message': 'Import-Prozess gestartet…',
    })
    from app import create_app, db
    from app import prod_import

    app = create_app()
    with app.app_context():
        try:
            _job_write(job_id, user_id, {
                'status': 'running', 'pct': 8, 'message': 'Intervalle laden…',
            })
            if not os.path.isfile(intervals_path):
                raise FileNotFoundError(
                    'Vorschau-Daten fehlen. Bitte CSV erneut hochladen.'
                )
            intervals = prod_import.read_intervals_sidecar(intervals_path)
            total = len(intervals)
            _job_write(job_id, user_id, {
                'status': 'running', 'pct': 12,
                'message': f'{total:,} Intervalle, speichern…',
            })

            def progress(done, _total, message):
                pct = 12 + int((done / max(_total, 1)) * 83)
                _job_write(job_id, user_id, {
                    'status': 'running', 'pct': pct, 'message': message,
                })

            batch, deleted = prod_import.commit_intervals(
                filename,
                intervals,
                overwrite=(overwrite_flag == '1'),
                progress_cb=progress,
                imported_by_id=int(user_id),
            )
            msg = f'{batch.intervals_stored:,} Intervalle importiert'
            if deleted:
                msg += f', {deleted:,} im Zeitraum ersetzt'
            msg += (
                f'. {batch.matched_member:,} mit Agent, '
                f'{batch.unmatched_member:,} ohne Zuordnung.'
            )
            _job_write(job_id, user_id, {
                'status': 'done',
                'pct': 100,
                'message': 'Import abgeschlossen.',
                'done_url': f'/admin/import_productivity_csv/done/{job_id}',
                'flash_category': 'success',
                'flash_message': msg,
            })
        except Exception as e:
            db.session.rollback()
            _job_write(job_id, user_id, {'status': 'error', 'pct': 0, 'message': str(e)})
        finally:
            db.session.remove()


def main(argv=None):
    argv = argv or sys.argv[1:]
    if not argv:
        return 1
    cmd = argv[0]
    if cmd == 'kpi_revert':
        run_kpi_revert(int(argv[1]), argv[2], int(argv[3]))
    elif cmd == 'prod_revert':
        run_prod_revert(int(argv[1]), argv[2], int(argv[3]))
    elif cmd == 'prod_import':
        run_prod_import(argv[1], int(argv[2]), argv[3], argv[4], argv[5])
    else:
        print(f'Unknown command: {cmd}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
