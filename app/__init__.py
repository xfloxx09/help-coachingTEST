# app/__init__.py
import os
from datetime import datetime, timezone
import pytz
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, current_user
from flask_migrate import Migrate
from sqlalchemy import inspect, text
from config import Config

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Bitte melden Sie sich an, um auf diese Seite zuzugreifen.'
login_manager.login_message_category = 'info'

migrate = Migrate()

def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)

    # Flask-Login user loader (eager role + permissions so checks match DB after role edits)
    @login_manager.user_loader
    def load_user(user_id):
        from sqlalchemy.orm import joinedload, selectinload
        from app.models import User, Role
        return User.query.options(
            joinedload(User.role).joinedload(Role.permissions),
            selectinload(User.teams_led),
            selectinload(User.team_members),
        ).get(int(user_id))

    # --- Migration: ensure necessary columns and tables exist ---
    with app.app_context():
        print("--- Running automatic migrations ---")
        # „import app.models“ würde den Namen app überschreiben (Paket statt Flask-Instanz).
        from app import models as _models  # noqa: F401 — Modelle registrieren

        inspector = inspect(db.engine)
        conn = db.engine.connect()

        db.create_all()

        # 1. coachings.team_id
        if 'coachings' in inspector.get_table_names():
            columns_coachings = [col['name'] for col in inspector.get_columns('coachings')]
            if 'team_id' not in columns_coachings:
                conn.execute(text('ALTER TABLE coachings ADD COLUMN team_id INTEGER REFERENCES teams(id)'))
                conn.commit()
                print("✅ Spalte 'team_id' in coachings hinzugefügt.")
            conn.execute(text('''
                UPDATE coachings
                SET team_id = team_members.team_id
                FROM team_members
                WHERE coachings.team_member_id = team_members.id
                AND coachings.team_id IS NULL
            '''))
            conn.commit()
            print("ℹ️ Bestehende Coachings mit team_id aktualisiert.")

        # 2. workshop_participants.original_team_id
        if 'workshop_participants' in inspector.get_table_names():
            columns_wp = [col['name'] for col in inspector.get_columns('workshop_participants')]
            if 'original_team_id' not in columns_wp:
                conn.execute(text('ALTER TABLE workshop_participants ADD COLUMN original_team_id INTEGER REFERENCES teams(id)'))
                conn.commit()
                print("✅ Spalte 'original_team_id' in workshop_participants hinzugefügt.")
            conn.execute(text('''
                UPDATE workshop_participants
                SET original_team_id = team_members.team_id
                FROM team_members
                WHERE workshop_participants.team_member_id = team_members.id
                AND workshop_participants.original_team_id IS NULL
            '''))
            conn.commit()
            print("ℹ️ Bestehende Workshop-Teilnehmer mit original_team_id aktualisiert.")

        # 3. assigned_coachings auto-increment
        if 'assigned_coachings' in inspector.get_table_names():
            conn.execute(text('''
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                                   WHERE table_name='assigned_coachings' AND column_name='id' 
                                   AND column_default IS NOT NULL AND column_default LIKE 'nextval%') THEN
                        CREATE SEQUENCE IF NOT EXISTS assigned_coachings_id_seq;
                        ALTER TABLE assigned_coachings ALTER COLUMN id SET DEFAULT nextval('assigned_coachings_id_seq');
                        PERFORM setval('assigned_coachings_id_seq', COALESCE((SELECT MAX(id) FROM assigned_coachings), 1));
                    END IF;
                END
                $$;
            '''))
            conn.commit()
            print("✅ Auto-increment für assigned_coachings.id sichergestellt.")
            cols_ac = [col['name'] for col in inspector.get_columns('assigned_coachings')]
            if 'rejection_reason' not in cols_ac:
                conn.execute(text('ALTER TABLE assigned_coachings ADD COLUMN rejection_reason TEXT'))
                conn.commit()
                print("✅ Spalte 'rejection_reason' in assigned_coachings hinzugefügt.")

        # 4. assigned_coaching_id in coachings
        if 'coachings' in inspector.get_table_names():
            columns_coachings = [col['name'] for col in inspector.get_columns('coachings')]
            if 'assigned_coaching_id' not in columns_coachings:
                conn.execute(text('ALTER TABLE coachings ADD COLUMN assigned_coaching_id INTEGER REFERENCES assigned_coachings(id)'))
                conn.commit()
                print("✅ Spalte 'assigned_coaching_id' in coachings hinzugefügt.")

        # 5. role_id in users
        if 'users' in inspector.get_table_names():
            columns_users = [col['name'] for col in inspector.get_columns('users')]
            if 'role_id' not in columns_users:
                conn.execute(text('ALTER TABLE users ADD COLUMN role_id INTEGER REFERENCES roles(id)'))
                conn.commit()
                print("✅ Spalte 'role_id' in users hinzugefügt.")

        # 6. Default permissions
        default_permissions = [
            ('view_own_coachings', 'View own coachings'),
            ('leave_coaching_review', 'Leave a review for the coach after being coached'),
            ('view_review', 'View reviews received as a coach'),
            ('view_all_reviews', 'View all coaching reviews in allowed projects'),
            ('view_own_team', 'View own team dashboard (teams where user is a member)'),
            ('multiple_teams', 'User may belong to multiple teams (TeamMember rows)'),
            ('coach', 'Can perform coaching'),
            ('assign_teams', 'Can be assigned as team leader (has teams_led)'),
            ('coach_own_team_only', 'Coach can only coach members of their own team'),
            ('view_coaching_dashboard', 'View coaching dashboard'),
            ('view_coaching_dashboard_all_teams', 'Coaching dashboard: all teams in project(s); without this, only own team(s)'),
            ('view_workshop_dashboard', 'View workshop dashboard'),
            ('view_pl_qm_dashboard', 'View PL/QM project dashboard'),
            ('assign_coachings', 'Assign coaching tasks to coaches'),
            ('view_assigned_coachings', 'View assigned coaching tasks'),
            ('view_assigned_coaching_report', 'Übersicht & Berichte zu zugewiesenen Coachings im eigenen Projektbereich (inkl. Abteilung)'),
            ('accept_assigned_coaching', 'Accept assigned coaching task'),
            ('reject_assigned_coaching', 'Reject assigned coaching task'),
            ('view_abteilung', 'Scope: access all projects of assigned Abteilung (department)'),
            ('planned_coachings', 'Geplante Coachings: Folgetermine planen, Liste und Start am geplanten Tag'),
            (
                'view_others_planned_coachings',
                'Geplante Coachings/Workshops anderer Coaches im Projektbereich einsehen (nur Ansicht)',
            ),
            ('terminkalender', 'Terminkalender anzeigen (Kalender mit Terminen und Coachings im Sichtbereich)'),
        ]
        for name, desc in default_permissions:
            res = conn.execute(text("SELECT id FROM permissions WHERE name = :name"), {"name": name}).fetchone()
            if not res:
                conn.execute(
                    text("INSERT INTO permissions (name, description) VALUES (:name, :desc)"),
                    {"name": name, "desc": desc}
                )
                print(f"✅ Permission '{name}' hinzugefügt.")
        conn.commit()

        # 7. Default roles
        default_roles = [
            ('Admin', 'Administrator'),
            ('Betriebsleiter', 'Operations manager'),
            ('Teamleiter', 'Team leader'),
            ('Mitarbeiter', 'Regular employee'),
            ('Projektleiter', 'Project leader'),
            ('Qualitätsmanager', 'Quality coach'),
            ('SalesCoach', 'Sales coach'),
            ('Trainer', 'Trainer'),
            ('Abteilungsleiter', 'Department head'),
        ]
        for role_name, role_desc in default_roles:
            res = conn.execute(text("SELECT id FROM roles WHERE name = :name"), {"name": role_name}).fetchone()
            if not res:
                conn.execute(
                    text("INSERT INTO roles (name, description) VALUES (:name, :desc)"),
                    {"name": role_name, "desc": role_desc}
                )
                print(f"✅ Rolle '{role_name}' hinzugefügt.")

        # 8. Assign permissions to roles
        all_perms = conn.execute(text("SELECT id, name FROM permissions")).fetchall()
        perm_map = {p[1]: p[0] for p in all_perms}

        # Admin gets all permissions
        admin_role = conn.execute(text("SELECT id FROM roles WHERE name = 'Admin'")).fetchone()
        if admin_role:
            for perm_id in perm_map.values():
                conn.execute(
                    text("INSERT INTO role_permissions (role_id, permission_id) VALUES (:role_id, :perm_id) ON CONFLICT DO NOTHING"),
                    {"role_id": admin_role[0], "perm_id": perm_id}
                )
            print("✅ Admin hat alle Berechtigungen.")

        # Betriebsleiter gets all permissions
        betriebsleiter_role = conn.execute(text("SELECT id FROM roles WHERE name = 'Betriebsleiter'")).fetchone()
        if betriebsleiter_role:
            for perm_id in perm_map.values():
                conn.execute(
                    text("INSERT INTO role_permissions (role_id, permission_id) VALUES (:role_id, :perm_id) ON CONFLICT DO NOTHING"),
                    {"role_id": betriebsleiter_role[0], "perm_id": perm_id}
                )
            print("✅ Betriebsleiter hat alle Berechtigungen.")

        # Teamleiter: u. a. view_own_team, multiple_teams (mehrere TeamMember-Zeilen)
        teamleiter_role = conn.execute(text("SELECT id FROM roles WHERE name = 'Teamleiter'")).fetchone()
        if teamleiter_role:
            for perm_name in [
                'assign_teams', 'coach', 'coach_own_team_only', 'view_own_team', 'multiple_teams',
                'view_assigned_coachings', 'accept_assigned_coaching', 'reject_assigned_coaching',
                'planned_coachings', 'terminkalender',
            ]:
                if perm_name in perm_map:
                    conn.execute(
                        text("INSERT INTO role_permissions (role_id, permission_id) VALUES (:role_id, :perm_id) ON CONFLICT DO NOTHING"),
                        {"role_id": teamleiter_role[0], "perm_id": perm_map[perm_name]}
                    )
            print("✅ Teamleiter hat u. a. Coach- und Zuweisungs-Berechtigungen (inkl. zugewiesene Coachings).")

        for pl_role_name in ('Projektleiter', 'Qualitätsmanager', 'Abteilungsleiter'):
            plrid = conn.execute(text("SELECT id FROM roles WHERE name = :n"), {"n": pl_role_name}).fetchone()
            if plrid:
                for perm_name in (
                    'view_pl_qm_dashboard',
                    'assign_coachings',
                    'view_coaching_dashboard',
                    'view_coaching_dashboard_all_teams',
                    'view_workshop_dashboard',
                    'view_assigned_coachings',
                    'view_assigned_coaching_report',
                    'accept_assigned_coaching',
                    'reject_assigned_coaching',
                    'view_abteilung',
                    'planned_coachings',
                    'terminkalender',
                ):
                    if perm_name in perm_map:
                        conn.execute(
                            text("INSERT INTO role_permissions (role_id, permission_id) VALUES (:role_id, :perm_id) ON CONFLICT DO NOTHING"),
                            {"role_id": plrid[0], "perm_id": perm_map[perm_name]}
                        )
                print(f"✅ Rolle '{pl_role_name}': PL/QM-Dashboard, Coaching zuweisen, zugewiesene Coachings.")

        for coach_role_name in ('Trainer', 'SalesCoach'):
            crid = conn.execute(text("SELECT id FROM roles WHERE name = :n"), {"n": coach_role_name}).fetchone()
            if crid:
                for perm_name in (
                    'view_assigned_coachings',
                    'accept_assigned_coaching',
                    'reject_assigned_coaching',
                    'planned_coachings',
                    'terminkalender',
                ):
                    if perm_name in perm_map:
                        conn.execute(
                            text("INSERT INTO role_permissions (role_id, permission_id) VALUES (:role_id, :perm_id) ON CONFLICT DO NOTHING"),
                            {"role_id": crid[0], "perm_id": perm_map[perm_name]}
                        )
                print(f"✅ Rolle '{coach_role_name}': zugewiesene Coachings (Annehmen/Ablehnen).")

        # Agent / Mitarbeiter: eigene Coachings + Coach bewerten (rein über Berechtigungen; Rollenname ist egal)
        for employee_role_name in ('Mitarbeiter', 'Agent'):
            er = conn.execute(text("SELECT id FROM roles WHERE name = :n"), {"n": employee_role_name}).fetchone()
            if er:
                for perm_name in ('view_own_coachings', 'leave_coaching_review'):
                    if perm_name in perm_map:
                        conn.execute(
                            text("INSERT INTO role_permissions (role_id, permission_id) VALUES (:role_id, :perm_id) ON CONFLICT DO NOTHING"),
                            {"role_id": er[0], "perm_id": perm_map[perm_name]}
                        )
                print(f"✅ Rolle '{employee_role_name}': view_own_coachings + leave_coaching_review (falls nicht schon gesetzt).")

        # Terminkalender: Rollen, die den Kalender früher über Dashboard / geplant / zugewiesen nutzen konnten
        try:
            tkal_id = perm_map.get('terminkalender')
            if tkal_id:
                legacy_roles = conn.execute(
                    text(
                        """SELECT DISTINCT rp.role_id FROM role_permissions rp
                           JOIN permissions p ON p.id = rp.permission_id
                           WHERE p.name IN (
                               'view_coaching_dashboard', 'planned_coachings', 'view_assigned_coachings'
                           )"""
                    )
                ).fetchall()
                for (lrid,) in legacy_roles:
                    conn.execute(
                        text(
                            "INSERT INTO role_permissions (role_id, permission_id) "
                            "VALUES (:role_id, :perm_id) ON CONFLICT DO NOTHING"
                        ),
                        {"role_id": lrid, "perm_id": tkal_id},
                    )
                conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"ℹ️ terminkalender Abwärtskompatibilität (Rollen): {e}")

        # 9. user_id and custom fields in team_members
        if 'team_members' in inspector.get_table_names():
            columns_team_members = [col['name'] for col in inspector.get_columns('team_members')]
            if 'user_id' not in columns_team_members:
                conn.execute(text('ALTER TABLE team_members ADD COLUMN user_id INTEGER REFERENCES users(id)'))
                conn.commit()
                print("✅ Spalte 'user_id' in team_members hinzugefügt.")
            try:
                conn.execute(text('ALTER TABLE team_members DROP CONSTRAINT IF EXISTS team_members_user_id_key'))
                conn.commit()
                print("✅ team_members: UNIQUE auf user_id entfernt (Postgres, falls vorhanden).")
            except Exception as e:
                conn.rollback()
                print(f"ℹ️ team_members user_id UNIQUE drop: {e}")
            for field in ['pylon', 'plt_id', 'ma_kennung', 'dag_id']:
                if field not in columns_team_members:
                    conn.execute(text(f'ALTER TABLE team_members ADD COLUMN {field} VARCHAR(50)'))
                    conn.commit()
                    print(f"✅ Spalte '{field}' in team_members hinzugefügt.")

        # 10. Team uniqueness per project
        if 'teams' in inspector.get_table_names():
            try:
                conn.execute(text('ALTER TABLE teams DROP CONSTRAINT IF EXISTS teams_name_key'))
                conn.execute(text('ALTER TABLE teams ADD CONSTRAINT teams_name_project_id_key UNIQUE (name, project_id)'))
                conn.commit()
                print("✅ Unique constraint on teams updated to (name, project_id).")
            except Exception as e:
                conn.rollback()
                print(f"ℹ️ Note on team constraint: {e}")

        # 11. teams.active_for_coaching (hide teams from new coaching/workshops)
        if 'teams' in inspector.get_table_names():
            team_cols = [c['name'] for c in inspector.get_columns('teams')]
            if 'active_for_coaching' not in team_cols:
                try:
                    conn.execute(text(
                        'ALTER TABLE teams ADD COLUMN active_for_coaching BOOLEAN DEFAULT true'
                    ))
                    conn.execute(text(
                        'UPDATE teams SET active_for_coaching = true WHERE active_for_coaching IS NULL'
                    ))
                    conn.commit()
                    print("✅ Spalte 'active_for_coaching' in teams hinzugefügt.")
                except Exception as e:
                    conn.rollback()
                    try:
                        conn.execute(text(
                            'ALTER TABLE teams ADD COLUMN active_for_coaching INTEGER DEFAULT 1'
                        ))
                        conn.execute(text(
                            'UPDATE teams SET active_for_coaching = 1 WHERE active_for_coaching IS NULL'
                        ))
                        conn.commit()
                        print("✅ Spalte 'active_for_coaching' in teams hinzugefügt (Fallback).")
                    except Exception as e2:
                        conn.rollback()
                        print(f"ℹ️ teams.active_for_coaching: {e} / {e2}")

        # 12. teams.visible_for_coaching_assignment (inactive teams whitelisted for Coaching zuweisen only)
        if 'teams' in inspector.get_table_names():
            team_cols = [c['name'] for c in inspector.get_columns('teams')]
            if 'visible_for_coaching_assignment' not in team_cols:
                try:
                    conn.execute(text(
                        'ALTER TABLE teams ADD COLUMN visible_for_coaching_assignment BOOLEAN DEFAULT false'
                    ))
                    conn.execute(text(
                        'UPDATE teams SET visible_for_coaching_assignment = false WHERE visible_for_coaching_assignment IS NULL'
                    ))
                    conn.commit()
                    print("✅ Spalte 'visible_for_coaching_assignment' in teams hinzugefügt.")
                except Exception as e:
                    conn.rollback()
                    try:
                        conn.execute(text(
                            'ALTER TABLE teams ADD COLUMN visible_for_coaching_assignment INTEGER DEFAULT 0'
                        ))
                        conn.execute(text(
                            'UPDATE teams SET visible_for_coaching_assignment = 0 WHERE visible_for_coaching_assignment IS NULL'
                        ))
                        conn.commit()
                        print("✅ Spalte 'visible_for_coaching_assignment' in teams hinzugefügt (Fallback).")
                    except Exception as e2:
                        conn.rollback()
                        print(f"ℹ️ teams.visible_for_coaching_assignment: {e} / {e2}")

        # 13. Abteilungen (departments above projects)
        inspector = inspect(db.engine)
        if 'abteilungen' not in inspector.get_table_names():
            try:
                conn.execute(text(
                    'CREATE TABLE abteilungen ('
                    'id SERIAL PRIMARY KEY, '
                    'name VARCHAR(150) NOT NULL UNIQUE, '
                    'description VARCHAR(500))'
                ))
                conn.commit()
                print("✅ Tabelle 'abteilungen' erstellt.")
            except Exception as e:
                conn.rollback()
                print(f"ℹ️ abteilungen table: {e}")
        inspector = inspect(db.engine)
        if 'projects' in inspector.get_table_names():
            pc = [c['name'] for c in inspector.get_columns('projects')]
            if 'abteilung_id' not in pc:
                try:
                    conn.execute(text(
                        'ALTER TABLE projects ADD COLUMN abteilung_id INTEGER REFERENCES abteilungen(id)'
                    ))
                    conn.commit()
                    print("✅ projects.abteilung_id hinzugefügt.")
                except Exception as e:
                    conn.rollback()
                    print(f"ℹ️ projects.abteilung_id: {e}")
        if 'users' in inspector.get_table_names():
            uc = [c['name'] for c in inspector.get_columns('users')]
            if 'abteilung_id' not in uc:
                try:
                    conn.execute(text(
                        'ALTER TABLE users ADD COLUMN abteilung_id INTEGER REFERENCES abteilungen(id)'
                    ))
                    conn.commit()
                    print("✅ users.abteilung_id hinzugefügt.")
                except Exception as e:
                    conn.rollback()
                    print(f"ℹ️ users.abteilung_id: {e}")

        # 14. leitfaden_items.project_id (NULL = global standard checklist)
        inspector = inspect(db.engine)
        if 'leitfaden_items' in inspector.get_table_names():
            lic = [c['name'] for c in inspector.get_columns('leitfaden_items')]
            if 'project_id' not in lic:
                try:
                    conn.execute(text(
                        'ALTER TABLE leitfaden_items ADD COLUMN project_id INTEGER REFERENCES projects(id)'
                    ))
                    conn.commit()
                    print("✅ leitfaden_items.project_id hinzugefügt.")
                except Exception as e:
                    conn.rollback()
                    print(f"ℹ️ leitfaden_items.project_id: {e}")

        # 15. Coaching-Bogen: Themen, Layout, coaching_subject Länge
        inspector = inspect(db.engine)
        if 'coaching_thema_items' not in inspector.get_table_names():
            try:
                conn.execute(text(
                    'CREATE TABLE coaching_thema_items ('
                    'id SERIAL PRIMARY KEY, '
                    'name VARCHAR(120) NOT NULL, '
                    '"position" INTEGER NOT NULL DEFAULT 0, '
                    'is_active BOOLEAN NOT NULL DEFAULT true, '
                    'created_at TIMESTAMP NOT NULL DEFAULT NOW(), '
                    'project_id INTEGER REFERENCES projects(id))'
                ))
                conn.commit()
                print("✅ Tabelle coaching_thema_items erstellt.")
            except Exception as e:
                conn.rollback()
                print(f"ℹ️ coaching_thema_items: {e}")
        if 'coaching_bogen_layouts' not in inspector.get_table_names():
            try:
                conn.execute(text(
                    'CREATE TABLE coaching_bogen_layouts ('
                    'id SERIAL PRIMARY KEY, '
                    'project_id INTEGER REFERENCES projects(id), '
                    'show_performance_bar BOOLEAN NOT NULL DEFAULT true, '
                    'show_coach_notes BOOLEAN NOT NULL DEFAULT true, '
                    'show_time_spent BOOLEAN NOT NULL DEFAULT true, '
                    'allow_side_by_side BOOLEAN NOT NULL DEFAULT true, '
                    'allow_tcap BOOLEAN NOT NULL DEFAULT true)'
                ))
                conn.commit()
                print("✅ Tabelle coaching_bogen_layouts erstellt.")
            except Exception as e:
                conn.rollback()
                print(f"ℹ️ coaching_bogen_layouts: {e}")
        inspector = inspect(db.engine)
        if 'coachings' in inspector.get_table_names():
            cc = [c['name'] for c in inspector.get_columns('coachings')]
            if 'coaching_subject' in cc:
                try:
                    conn.execute(text('ALTER TABLE coachings ALTER COLUMN coaching_subject TYPE VARCHAR(120)'))
                    conn.commit()
                    print("✅ coachings.coaching_subject auf VARCHAR(120) erweitert.")
                except Exception as e:
                    conn.rollback()
                    try:
                        conn.execute(text(
                            'ALTER TABLE coachings MODIFY coaching_subject VARCHAR(120)'
                        ))
                        conn.commit()
                        print("✅ coachings.coaching_subject erweitert (Fallback).")
                    except Exception as e2:
                        conn.rollback()
                        print(f"ℹ️ coachings.coaching_subject: {e} / {e2}")
        try:
            if 'coaching_bogen_layouts' in inspect(db.engine).get_table_names():
                r = conn.execute(text('SELECT COUNT(*) FROM coaching_bogen_layouts WHERE project_id IS NULL')).fetchone()
                cnt_layout = r[0] if r else 0
            else:
                cnt_layout = 1
            if cnt_layout == 0:
                conn.execute(text(
                    'INSERT INTO coaching_bogen_layouts '
                    '(project_id, show_performance_bar, show_coach_notes, show_time_spent, allow_side_by_side, allow_tcap) '
                    'VALUES (NULL, true, true, true, true, true)'
                ))
                conn.commit()
                print("✅ Standard coaching_bogen_layouts (global) eingefügt.")
        except Exception as e:
            conn.rollback()
            print(f"ℹ️ coaching_bogen_layouts seed: {e}")
        try:
            if 'coaching_thema_items' in inspect(db.engine).get_table_names():
                r2 = conn.execute(text('SELECT COUNT(*) FROM coaching_thema_items')).fetchone()
                cnt_t = r2[0] if r2 else 0
            else:
                cnt_t = 1
            if cnt_t == 0:
                conn.execute(text(
                    "INSERT INTO coaching_thema_items (name, \"position\", is_active, created_at, project_id) VALUES "
                    "('Sales', 1, true, NOW(), NULL), ('Qualität', 2, true, NOW(), NULL), ('Allgemein', 3, true, NOW(), NULL)"
                ))
                conn.commit()
                print("✅ Standard coaching_thema_items eingefügt.")
        except Exception as e:
            conn.rollback()
            print(f"ℹ️ coaching_thema_items seed: {e}")

        print("--- Migration abgeschlossen ---")

    # --- Blueprint registration ---
    from app.auth import bp as auth_bp
    app.register_blueprint(auth_bp, url_prefix='/auth')
    from app.main_routes import bp as main_bp
    app.register_blueprint(main_bp)
    from app.admin import bp as admin_bp
    app.register_blueprint(admin_bp, url_prefix='/admin')

    # --- Context processors ---
    @app.context_processor
    def inject_current_year():
        return {'current_year': datetime.utcnow().year}

    @app.context_processor
    def inject_user_allowed_projects():
        from app.models import Project
        from app.utils import ROLE_ADMIN, ROLE_BETRIEBSLEITER, get_accessible_project_ids
        projects = []
        active_project_id = None
        active_project_name = None
        show_project_switcher = False
        if current_user.is_authenticated:
            from app.main_routes import get_visible_project_id

            if current_user.role_name in [ROLE_ADMIN, ROLE_BETRIEBSLEITER]:
                projects = Project.query.order_by(Project.name).all()
            else:
                ids = get_accessible_project_ids()
                if ids is not None and len(ids) > 0:
                    projects = Project.query.filter(Project.id.in_(ids)).order_by(Project.name).all()
            show_project_switcher = len(projects) > 1
            active_project_id = get_visible_project_id()
            if active_project_id:
                ap = Project.query.get(active_project_id)
                active_project_name = ap.name if ap else None
        return {
            'user_allowed_projects': projects,
            'active_project_id': active_project_id,
            'active_project_name': active_project_name,
            'show_project_switcher': show_project_switcher,
        }

    @app.context_processor
    def inject_assigned_count():
        from app.utils import ROLE_ADMIN, ROLE_BETRIEBSLEITER, ROLE_PROJEKTLEITER
        if current_user.is_authenticated and current_user.role_name not in [ROLE_ADMIN, ROLE_BETRIEBSLEITER, ROLE_PROJEKTLEITER]:
            from app.models import AssignedCoaching
            count = AssignedCoaching.query.filter_by(coach_id=current_user.id, status='pending').count()
        else:
            count = 0
        return {'pending_assigned_count': count}

    @app.context_processor
    def inject_permissions():
        def has_perm(permission_name):
            if current_user.is_authenticated:
                return current_user.has_permission(permission_name)
            return False
        return {'has_perm': has_perm}

    @app.context_processor
    def inject_mein_team_nav():
        from app.utils import user_has_mein_team_nav
        if current_user.is_authenticated:
            return {'show_mein_team_nav': user_has_mein_team_nav(current_user)}
        return {'show_mein_team_nav': False}

    @app.context_processor
    def inject_quick_coaching_suggestions():
        from app.utils import quick_coaching_suggestions
        if current_user.is_authenticated:
            return {'quick_coaching_suggestions': quick_coaching_suggestions(limit=6, max_without_coaching=40)}
        return {'quick_coaching_suggestions': {'primary': [], 'without_coaching': []}}

    @app.context_processor
    def inject_planned_due_today_notifications():
        from app.utils import quick_planned_due_today_notifications
        if current_user.is_authenticated:
            return {'planned_due_today_notifications': quick_planned_due_today_notifications()}
        return {'planned_due_today_notifications': []}

    @app.template_filter('athens_time')
    def format_athens_time(utc_dt, fmt='%d.%m.%Y %H:%M'):
        if not utc_dt:
            return ""
        if not isinstance(utc_dt, datetime):
            if isinstance(utc_dt, str):
                try:
                    utc_dt = datetime.fromisoformat(utc_dt.replace('Z', '+00:00'))
                except ValueError:
                    try:
                        utc_dt = datetime.strptime(utc_dt, "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        return str(utc_dt)
            else:
                return str(utc_dt)

        if utc_dt.tzinfo is None or utc_dt.tzinfo.utcoffset(utc_dt) is None:
            utc_dt = utc_dt.replace(tzinfo=timezone.utc)

        athens_tz = pytz.timezone('Europe/Athens')
        try:
            local_dt = utc_dt.astimezone(athens_tz)
            return local_dt.strftime(fmt)
        except Exception:
            try:
                return utc_dt.strftime(fmt) + " (UTC?)"
            except:
                return str(utc_dt)

    @app.template_filter('status_de')
    def translate_status(status):
        translations = {
            'pending': 'Ausstehend',
            'accepted': 'Angenommen',
            'in_progress': 'In Bearbeitung',
            'completed': 'Abgeschlossen',
            'expired': 'Abgelaufen',
            'rejected': 'Abgelehnt',
            'cancelled': 'Storniert'
        }
        return translations.get(status, status)

    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass

    return app
