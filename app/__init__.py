# app/__init__.py
import os
from datetime import datetime, timezone
import pytz
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, current_user
from flask_migrate import Migrate
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

    from app.startup_migrations import run_startup_migrations
    run_startup_migrations(app)

    @app.route('/health')
    def health():
        return 'ok', 200

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
        from app.utils import can_view_kpi_qualitaet, can_view_kpi_produktivitaet

        def has_perm(permission_name):
            if current_user.is_authenticated:
                return current_user.has_permission(permission_name)
            return False

        if current_user.is_authenticated:
            kpi_qual = can_view_kpi_qualitaet(current_user)
            kpi_prod = can_view_kpi_produktivitaet(current_user)
        else:
            kpi_qual = False
            kpi_prod = False
        return {
            'has_perm': has_perm,
            'can_view_kpi_qualitaet': kpi_qual,
            'can_view_kpi_produktivitaet': kpi_prod,
        }

    @app.context_processor
    def inject_kpi_features_enabled():
        from app.kpi import kpi_features_enabled
        return {'kpi_features_enabled': kpi_features_enabled()}

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

    @app.template_filter('de_decimal')
    def de_decimal(value, decimals=2):
        from app.kpi import format_de
        return format_de(value, decimals)

    @app.template_filter('kpi_status')
    def kpi_status(value, target_green, target_yellow):
        from app.kpi import metric_status
        return metric_status(value, target_green, target_yellow)

    @app.template_filter('kpi_status_max')
    def kpi_status_max(value, target_green, target_yellow):
        from app.kpi import metric_status_max
        return metric_status_max(value, target_green, target_yellow)

    @app.template_filter('kpi_bar_class')
    def kpi_bar_class(value, target_green, target_yellow):
        from app.kpi import metric_bar_class
        return metric_bar_class(value, target_green, target_yellow)

    @app.template_filter('kpi_bar_width')
    def kpi_bar_width(value, bar_min=0, bar_max=100):
        from app.kpi import metric_bar_width
        return metric_bar_width(value, bar_min, bar_max)

    @app.template_filter('nps_bar_width')
    def nps_bar_width(value):
        from app.kpi import nps_bar_width as _nps_w
        return _nps_w(value)

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
