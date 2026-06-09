# config.py
import os
from urllib.parse import quote_plus
from dotenv import load_dotenv

# Lade Umgebungsvariablen aus der .env Datei (primär für lokale Entwicklung)
# Diese Zeile wird auf Railway keine .env-Datei finden, was OK ist, da dort Umgebungsvariablen anders gesetzt werden.
basedir = os.path.abspath(os.path.dirname(__file__))
print(f"DEBUG [config.py]: Lade .env aus basedir: {basedir}") # DEBUG
if os.path.exists(os.path.join(basedir, '.env')):
    print("DEBUG [config.py]: .env Datei GEFUNDEN, wird geladen.") # DEBUG
    load_dotenv(os.path.join(basedir, '.env'))
else:
    print("DEBUG [config.py]: .env Datei NICHT gefunden (erwartet auf Railway).") # DEBUG


def _first_env_value(keys):
    for key in keys:
        value = os.environ.get(key)
        if value:
            return value, key
    return None, None


def _database_uri_from_pg_parts():
    pg_host = os.environ.get('PGHOST')
    pg_port = os.environ.get('PGPORT', '5432')
    pg_user = os.environ.get('PGUSER')
    pg_password = os.environ.get('PGPASSWORD')
    pg_database = os.environ.get('PGDATABASE')

    if all([pg_host, pg_user, pg_password, pg_database]):
        safe_user = quote_plus(pg_user)
        safe_password = quote_plus(pg_password)
        safe_database = quote_plus(pg_database)
        return (
            f"postgresql://{safe_user}:{safe_password}@{pg_host}:{pg_port}/{safe_database}",
            "PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE"
        )
    return None, None


def _resolve_database_uri():
    candidate_keys = (
        'DATABASE_URL',
        'DATABASE_PRIVATE_URL',
        'DATABASE_PUBLIC_URL',
        'POSTGRES_URL',
        'POSTGRESQL_URL',
        'RAILWAY_DATABASE_URL',
    )
    db_url, source = _first_env_value(candidate_keys)
    if db_url:
        return db_url, source

    return _database_uri_from_pg_parts()


def _env_bool_with_default(name, default):
    """If env unset or empty, return default; otherwise true for 1/true/yes/on (case-insensitive)."""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == '':
        return default
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


class Config:
    print("DEBUG [config.py]: Innerhalb der Config-Klasse, VOR dem Lesen von Umgebungsvariablen.") # DEBUG

    SECRET_KEY = os.environ.get('SECRET_KEY') or 'ein-sehr-geheimer-fallback-schluessel'
    print(f"DEBUG [config.py]: SECRET_KEY gelesen als: {'SET (Länge: ' + str(len(SECRET_KEY)) + ')' if SECRET_KEY != '1234' else 'Fallback verwendet'}") # DEBUG

    DATABASE_URL_FROM_ENV, DATABASE_URL_SOURCE = _resolve_database_uri()
    print(
        "DEBUG [config.py]: Aufgelöste DB-URL Quelle: "
        f"'{DATABASE_URL_SOURCE}', Wert: '{DATABASE_URL_FROM_ENV}' (Typ: {type(DATABASE_URL_FROM_ENV)})"
    ) # DEBUG

    SQLALCHEMY_DATABASE_URI = DATABASE_URL_FROM_ENV # Zuweisung

    if SQLALCHEMY_DATABASE_URI and isinstance(SQLALCHEMY_DATABASE_URI, str) and SQLALCHEMY_DATABASE_URI.startswith("postgres://"):
        print(f"DEBUG [config.py]: Ersetze 'postgres://' in '{SQLALCHEMY_DATABASE_URI}'") # DEBUG
        SQLALCHEMY_DATABASE_URI = SQLALCHEMY_DATABASE_URI.replace("postgres://", "postgresql://", 1)
        print(f"DEBUG [config.py]: SQLALCHEMY_DATABASE_URI nach replace: '{SQLALCHEMY_DATABASE_URI}'") # DEBUG
    elif not SQLALCHEMY_DATABASE_URI:
        print("DEBUG [config.py]: SQLALCHEMY_DATABASE_URI ist leer oder None VOR der postgres:// Prüfung.") # DEBUG
    elif not isinstance(SQLALCHEMY_DATABASE_URI, str):
        print(f"DEBUG [config.py]: SQLALCHEMY_DATABASE_URI ist kein String, sondern Typ {type(SQLALCHEMY_DATABASE_URI)}.") # DEBUG


    print(f"DEBUG [config.py]: Finale SQLALCHEMY_DATABASE_URI, die gesetzt wird: '{SQLALCHEMY_DATABASE_URI}'") # DEBUG
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,
        'pool_recycle': 280,
        'pool_size': 10,
        'max_overflow': 10,
        'pool_timeout': 8,
    }
    PERFORMANCE_BENCHMARK = 80.0
    # Team pre-filter on /add-coaching. Set ENABLE_ADD_COACHING_TEAM_FILTER=false to disable without git revert.
    ENABLE_ADD_COACHING_TEAM_FILTER = _env_bool_with_default('ENABLE_ADD_COACHING_TEAM_FILTER', True)
print("DEBUG [config.py]: config.py wurde vollständig geladen.") # DEBUG
