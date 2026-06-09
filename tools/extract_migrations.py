"""One-off script to build app/startup_migrations.py from app/__init__.py."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
init_path = ROOT / 'app' / '__init__.py'
lines = init_path.read_text(encoding='utf-8').splitlines()

start = next(i for i, l in enumerate(lines) if 'Running automatic migrations' in l) - 2
end = next(i for i, l in enumerate(lines) if l.strip() == 'conn.close()' and i > start) + 1

body_lines = lines[start + 3:end - 1]  # skip with app_context, print, drop conn.close

header = '''"""One-time startup schema bootstrap (skipped after version marker is set)."""
import os
from sqlalchemy import inspect, text

from app import db

STARTUP_MIGRATION_VERSION = 1


def _read_startup_migration_version(conn):
    try:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS app_meta ("
            "key VARCHAR(64) PRIMARY KEY, value VARCHAR(128))"
        ))
        conn.commit()
        row = conn.execute(text(
            "SELECT value FROM app_meta WHERE key = 'startup_migration_v'"
        )).fetchone()
        if row and row[0]:
            return int(row[0])
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    return 0


def _write_startup_migration_version(conn, version):
    conn.execute(text(
        "INSERT INTO app_meta (key, value) VALUES ('startup_migration_v', :v) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    ), {"v": str(version)})
    conn.commit()


def run_startup_migrations(app):
    with app.app_context():
        if os.environ.get("COACHING_SKIP_STARTUP_MIGRATIONS") == "1":
            print("--- Skipping startup migrations (background worker) ---")
            return
        conn = db.engine.connect()
        applied = _read_startup_migration_version(conn)
        if applied >= STARTUP_MIGRATION_VERSION:
            print(f"--- Startup migrations v{applied} already applied, skipping ---")
            conn.close()
            return

        print("--- Running automatic migrations ---")
        from app import models as _models  # noqa: F401
'''

footer = '''
        _write_startup_migration_version(conn, STARTUP_MIGRATION_VERSION)
        print("--- Migration abgeschlossen ---")
        conn.close()
'''

out = ROOT / 'app' / 'startup_migrations.py'
out.write_text(header + '\n'.join(body_lines) + footer + '\n', encoding='utf-8')
print(f'Wrote {out} ({len(body_lines)} body lines)')
