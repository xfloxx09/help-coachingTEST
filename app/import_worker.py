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


def main(argv=None):
    argv = argv or sys.argv[1:]
    if not argv:
        return 1
    cmd = argv[0]
    if cmd == 'kpi_revert':
        run_kpi_revert(int(argv[1]), argv[2], int(argv[3]))
    elif cmd == 'prod_revert':
        run_prod_revert(int(argv[1]), argv[2], int(argv[3]))
    else:
        print(f'Unknown command: {cmd}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
