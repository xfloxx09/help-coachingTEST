# app/models.py
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from app import db
from datetime import datetime, timezone
from sqlalchemy.exc import SQLAlchemyError

# Association tables
team_leaders = db.Table('team_leaders',
    db.Column('team_id', db.Integer, db.ForeignKey('teams.id')),
    db.Column('user_id', db.Integer, db.ForeignKey('users.id'))
)

user_projects = db.Table('user_projects',
    db.Column('user_id', db.Integer, db.ForeignKey('users.id')),
    db.Column('project_id', db.Integer, db.ForeignKey('projects.id'))
)

workshop_participants = db.Table('workshop_participants',
    db.Column('workshop_id', db.Integer, db.ForeignKey('workshops.id')),
    db.Column('team_member_id', db.Integer, db.ForeignKey('team_members.id')),
    db.Column('individual_rating', db.Integer),
    db.Column('original_team_id', db.Integer, db.ForeignKey('teams.id'))
)

role_permissions = db.Table('role_permissions',
    db.Column('role_id', db.Integer, db.ForeignKey('roles.id')),
    db.Column('permission_id', db.Integer, db.ForeignKey('permissions.id'))
)

role_projects = db.Table('role_projects',
    db.Column('role_id', db.Integer, db.ForeignKey('roles.id')),
    db.Column('project_id', db.Integer, db.ForeignKey('projects.id'))
)


class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    email = db.Column(db.String(120))
    password_hash = db.Column(db.String(128))
    role_id = db.Column(db.Integer, db.ForeignKey('roles.id'))
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False)
    team_id_if_leader = db.Column(db.Integer, db.ForeignKey('teams.id'))
    abteilung_id = db.Column(db.Integer, db.ForeignKey('abteilungen.id'), nullable=True)

    role = db.relationship('Role', back_populates='users')
    project = db.relationship('Project', back_populates='users')
    abteilung = db.relationship('Abteilung', back_populates='users')
    teams_led = db.relationship('Team', secondary=team_leaders, back_populates='leaders')
    projects = db.relationship('Project', secondary=user_projects, back_populates='assigned_users')
    team_members = db.relationship('TeamMember', back_populates='user')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def role_name(self):
        return self.role.name if self.role else None

    def has_permission(self, permission_name):
        if self.role:
            return self.role.has_permission(permission_name)
        return False

    @property
    def is_active(self):
        """Flask-Login: inactive when the account exists only as archived team member(s)."""
        from app.utils import user_is_archived_only_for_login
        return not user_is_archived_only_for_login(self)

    @property
    def coach_display_name(self):
        """Vor-/Nachname aus verknüpftem Teammitglied (Import), sonst Benutzername."""
        try:
            for tm in self.team_members:
                if tm and tm.name and str(tm.name).strip():
                    return str(tm.name).strip()
        except (TypeError, AttributeError):
            pass
        return (self.username or '').strip() or '—'


class Role(db.Model):
    __tablename__ = 'roles'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.String(255))

    permissions = db.relationship('Permission', secondary=role_permissions, back_populates='roles')
    projects = db.relationship('Project', secondary=role_projects, back_populates='roles')
    users = db.relationship('User', back_populates='role')

    def has_permission(self, permission_name):
        return any(perm.name == permission_name for perm in self.permissions)


class Permission(db.Model):
    __tablename__ = 'permissions'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.String(255))

    roles = db.relationship('Role', secondary=role_permissions, back_populates='permissions')


class Abteilung(db.Model):
    """Department above projects: assign whole projects (and their teams) for scoped access."""
    __tablename__ = 'abteilungen'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, unique=True)
    description = db.Column(db.String(500))

    projects = db.relationship('Project', back_populates='abteilung')
    users = db.relationship('User', back_populates='abteilung')


class Project(db.Model):
    __tablename__ = 'projects'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    description = db.Column(db.String(500))
    abteilung_id = db.Column(db.Integer, db.ForeignKey('abteilungen.id'), nullable=True)

    abteilung = db.relationship('Abteilung', back_populates='projects')
    users = db.relationship('User', back_populates='project')
    teams = db.relationship('Team', back_populates='project')
    workshops = db.relationship('Workshop', back_populates='project')
    coachings = db.relationship('Coaching', back_populates='project')
    assigned_users = db.relationship('User', secondary=user_projects, back_populates='projects')
    roles = db.relationship('Role', secondary=role_projects, back_populates='projects')
    leitfaden_items = db.relationship('LeitfadenItem', back_populates='project')
    thema_items = db.relationship('CoachingThemaItem', back_populates='project')


class Team(db.Model):
    __tablename__ = 'teams'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False)
    # False: no new coachings/workshops targeting this team; history & dashboards unchanged
    active_for_coaching = db.Column(db.Boolean, nullable=False, default=True)
    # If True (and active_for_coaching is False): members still appear in "Coaching zuweisen" / assignment UI only; set in admin.
    visible_for_coaching_assignment = db.Column(db.Boolean, nullable=False, default=False)

    members = db.relationship('TeamMember', foreign_keys='TeamMember.team_id', back_populates='team')
    leaders = db.relationship('User', secondary=team_leaders, back_populates='teams_led')
    project = db.relationship('Project', back_populates='teams')
    __table_args__ = (db.UniqueConstraint('name', 'project_id', name='teams_name_project_id_key'),)


class TeamMember(db.Model):
    __tablename__ = 'team_members'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=False)
    # Mehrere Zeilen pro User möglich (Permission multiple_teams); „Mein Team“ leitet sich daraus ab
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    pylon = db.Column(db.String(50))
    plt_id = db.Column(db.String(50))
    ma_kennung = db.Column(db.String(50))
    dag_id = db.Column(db.String(50))
    original_team_id = db.Column(db.Integer, db.ForeignKey('teams.id'))
    original_project_id = db.Column(db.Integer, db.ForeignKey('projects.id'))

    team = db.relationship('Team', foreign_keys=[team_id], back_populates='members')
    original_team = db.relationship('Team', foreign_keys=[original_team_id])
    original_project = db.relationship('Project', foreign_keys=[original_project_id])
    user = db.relationship('User', back_populates='team_members')
    coachings = db.relationship('Coaching', back_populates='team_member')
    workshops = db.relationship('Workshop', secondary=workshop_participants, back_populates='participants')
    assigned_coachings = db.relationship('AssignedCoaching', back_populates='team_member')


class Coaching(db.Model):
    __tablename__ = 'coachings'
    id = db.Column(db.Integer, primary_key=True)
    team_member_id = db.Column(db.Integer, db.ForeignKey('team_members.id'), nullable=False)
    coach_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    coaching_date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    coaching_style = db.Column(db.String(50), nullable=False)
    tcap_id = db.Column(db.String(100))
    coaching_subject = db.Column(db.String(120))
    leitfaden_begruessung = db.Column(db.String(10), default='k.A.')
    leitfaden_legitimation = db.Column(db.String(10), default='k.A.')
    leitfaden_pka = db.Column(db.String(10), default='k.A.')
    leitfaden_kek = db.Column(db.String(10), default='k.A.')
    leitfaden_angebot = db.Column(db.String(10), default='k.A.')
    leitfaden_zusammenfassung = db.Column(db.String(10), default='k.A.')
    leitfaden_kzb = db.Column(db.String(10), default='k.A.')
    performance_mark = db.Column(db.Integer, nullable=False)
    time_spent = db.Column(db.Integer, nullable=False)
    coach_notes = db.Column(db.Text)
    project_leader_notes = db.Column(db.Text)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'))
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'))
    assigned_coaching_id = db.Column(db.Integer, db.ForeignKey('assigned_coachings.id'))

    team_member = db.relationship('TeamMember', back_populates='coachings')
    team_member_coached = db.relationship('TeamMember', foreign_keys=[team_member_id], overlaps='team_member,coachings', viewonly=True)
    coach = db.relationship('User', backref='coachings_done')
    team = db.relationship('Team')
    project = db.relationship('Project', back_populates='coachings')
    assigned_coaching = db.relationship('AssignedCoaching', back_populates='coachings')
    leitfaden_responses = db.relationship(
        'CoachingLeitfadenResponse',
        back_populates='coaching',
        cascade='all, delete-orphan'
    )
    employee_review = db.relationship(
        'CoachingReview',
        back_populates='coaching',
        uselist=False,
        cascade='all, delete-orphan'
    )

    @property
    def overall_score(self):
        """Calculate overall score as performance_mark * 10 (percentage)."""
        return (self.performance_mark or 0) * 10

    @property
    def leitfaden_fields_list(self):
        # Prefer dynamic responses; fallback to legacy fixed columns for old records.
        try:
            if self.leitfaden_responses:
                return [(r.item.name, r.value or 'k.A.') for r in sorted(self.leitfaden_responses, key=lambda x: x.item.position if x.item else 9999)]
        except SQLAlchemyError:
            db.session.rollback()

        legacy = [
            ('Begrüßung', self.leitfaden_begruessung),
            ('Legitimation', self.leitfaden_legitimation),
            ('PKA', self.leitfaden_pka),
            ('KEK', self.leitfaden_kek),
            ('Angebot', self.leitfaden_angebot),
            ('Zusammenfassung', self.leitfaden_zusammenfassung),
            ('KZB', self.leitfaden_kzb),
        ]
        return [(name, value or 'k.A.') for name, value in legacy]

    @property
    def leitfaden_erfuellung_stats(self):
        """Returns (percent:int, positive:int, total:int) or None if nothing to score."""
        checks = [value for _, value in self.leitfaden_fields_list if value and value != 'k.A.']
        if not checks:
            return None
        positive = sum(1 for value in checks if str(value).strip().lower() in ['ja', 'yes', '1', 'true'])
        total = len(checks)
        percent = round((positive / total) * 100)
        return (percent, positive, total)

    @property
    def leitfaden_erfuellung_display(self):
        st = self.leitfaden_erfuellung_stats
        if st is None:
            return 'k.A.'
        percent, positive, total = st
        return f"{percent}% ({positive}/{total})"


class LeitfadenItem(db.Model):
    __tablename__ = 'leitfaden_items'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # NULL = global standard (default for all projects without their own set)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)

    project = db.relationship('Project', back_populates='leitfaden_items')
    responses = db.relationship('CoachingLeitfadenResponse', back_populates='item')


class CoachingThemaItem(db.Model):
    """Selectable coaching topics (coaching_subject). NULL project_id = global defaults."""
    __tablename__ = 'coaching_thema_items'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)

    project = db.relationship('Project', back_populates='thema_items')


class CoachingBogenLayout(db.Model):
    """Per-project or global (project_id NULL) layout flags for the Einzelcoaching form."""
    __tablename__ = 'coaching_bogen_layouts'
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)
    show_performance_bar = db.Column(db.Boolean, nullable=False, default=True)
    show_coach_notes = db.Column(db.Boolean, nullable=False, default=True)
    show_time_spent = db.Column(db.Boolean, nullable=False, default=True)
    allow_side_by_side = db.Column(db.Boolean, nullable=False, default=True)
    allow_tcap = db.Column(db.Boolean, nullable=False, default=True)


class CoachingLeitfadenResponse(db.Model):
    __tablename__ = 'coaching_leitfaden_responses'
    id = db.Column(db.Integer, primary_key=True)
    coaching_id = db.Column(db.Integer, db.ForeignKey('coachings.id'), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey('leitfaden_items.id'), nullable=False)
    value = db.Column(db.String(10), nullable=False, default='k.A.')

    coaching = db.relationship('Coaching', back_populates='leitfaden_responses')
    item = db.relationship('LeitfadenItem', back_populates='responses')

    __table_args__ = (
        db.UniqueConstraint('coaching_id', 'item_id', name='uq_coaching_leitfaden_item'),
    )


class PlannedCoaching(db.Model):
    """Nachfolge-Termin aus dem Coaching-Bogen; Coach sieht Liste bis zur Durchführung."""
    __tablename__ = 'planned_coachings'

    id = db.Column(db.Integer, primary_key=True)
    team_member_id = db.Column(db.Integer, db.ForeignKey('team_members.id'), nullable=False)
    coach_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=True)
    planned_for_date = db.Column(db.Date, nullable=False)
    notes = db.Column(db.Text)
    has_verabredung = db.Column(db.Boolean, nullable=False, default=False)
    verabredung_text = db.Column(db.Text)
    # Wenn erfüllt und has_verabredung: True/False; sonst NULL
    verabredung_erfuellt = db.Column(db.Boolean, nullable=True)
    source_coaching_id = db.Column(db.Integer, db.ForeignKey('coachings.id'), nullable=True)
    status = db.Column(db.String(20), nullable=False, default='open')
    fulfilled_coaching_id = db.Column(db.Integer, db.ForeignKey('coachings.id'), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    team_member = db.relationship('TeamMember', foreign_keys=[team_member_id])
    coach = db.relationship('User', foreign_keys=[coach_id])
    project = db.relationship('Project')
    team = db.relationship('Team', foreign_keys=[team_id])
    fulfilled_coaching = db.relationship('Coaching', foreign_keys=[fulfilled_coaching_id])


class PlannedWorkshop(db.Model):
    """Geplanter Workshop-Termin bis zur Erfassung im Workshop-Formular."""

    __tablename__ = 'planned_workshops'

    id = db.Column(db.Integer, primary_key=True)
    coach_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True)
    title = db.Column(db.String(200), nullable=False)
    planned_for_date = db.Column(db.Date, nullable=False)
    notes = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, default='open')
    fulfilled_workshop_id = db.Column(db.Integer, db.ForeignKey('workshops.id'), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    coach = db.relationship('User', foreign_keys=[coach_id])
    project = db.relationship('Project')
    fulfilled_workshop = db.relationship('Workshop', foreign_keys=[fulfilled_workshop_id])


class CoachingReview(db.Model):
    """Employee review of the coach for a specific Einzel-Coaching (one per coaching)."""
    __tablename__ = 'coaching_reviews'
    id = db.Column(db.Integer, primary_key=True)
    coaching_id = db.Column(db.Integer, db.ForeignKey('coachings.id'), nullable=False, unique=True)
    reviewer_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    rating = db.Column(db.Integer, nullable=False)
    comment = db.Column(db.Text)
    # Visibility flags controlled by the coached employee.
    # - view_review: visible to the coach (and roles that can view this list)
    # - view_all_reviews: visible to manager roles (project-scoped list)
    visible_to_coach = db.Column(db.Boolean, nullable=False, default=True)
    visible_to_manager = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    coaching = db.relationship('Coaching', back_populates='employee_review')
    reviewer = db.relationship('User', foreign_keys=[reviewer_user_id])


class Workshop(db.Model):
    __tablename__ = 'workshops'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    coach_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    workshop_date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    overall_rating = db.Column(db.Integer, nullable=False)
    time_spent = db.Column(db.Integer, nullable=False)
    notes = db.Column(db.Text)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'))

    coach = db.relationship('User', backref='workshops')
    participants = db.relationship('TeamMember', secondary=workshop_participants, back_populates='workshops')
    project = db.relationship('Project', back_populates='workshops')


class AssignedCoaching(db.Model):
    __tablename__ = 'assigned_coachings'
    id = db.Column(db.Integer, primary_key=True)
    project_leader_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    coach_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    team_member_id = db.Column(db.Integer, db.ForeignKey('team_members.id'), nullable=False)
    deadline = db.Column(db.DateTime, nullable=False)
    expected_coaching_count = db.Column(db.Integer, nullable=False)
    desired_performance_note = db.Column(db.Integer)
    current_performance_note_at_assign = db.Column(db.Float)
    # KPI snapshots at assignment / completion (counting survey types only)
    start_nps_at_assign = db.Column(db.Float, nullable=True)
    start_loesung_quote_at_assign = db.Column(db.Float, nullable=True)
    start_info_quote_at_assign = db.Column(db.Float, nullable=True)
    end_nps = db.Column(db.Float, nullable=True)
    end_loesung_quote = db.Column(db.Float, nullable=True)
    end_info_quote = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(20), nullable=False, default='pending')
    # Von Coach bei Ablehnung (pending → rejected); sichtbar für Zuweiser / Übersichten
    rejection_reason = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    project_leader = db.relationship('User', foreign_keys=[project_leader_id])
    coach = db.relationship('User', foreign_keys=[coach_id])
    team_member = db.relationship('TeamMember', back_populates='assigned_coachings')
    coachings = db.relationship('Coaching', back_populates='assigned_coaching')

    @property
    def progress(self):
        exp = self.expected_coaching_count or 0
        if exp <= 0:
            return 0
        done = Coaching.query.filter_by(assigned_coaching_id=self.id).count()
        return min(100, int(round(100.0 * done / exp)))

    @property
    def is_overdue(self):
        if self.status in ('completed', 'expired', 'cancelled', 'rejected'):
            return False
        if not self.deadline:
            return False
        now = datetime.utcnow()
        dl = self.deadline
        if dl.tzinfo is not None:
            now = datetime.now(timezone.utc)
        return dl < now


class KpiImportBatch(db.Model):
    """One CSV import run of SMS-survey raw data (KPIs Demo)."""
    __tablename__ = 'kpi_import_batches'
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255))
    imported_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    imported_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    date_from = db.Column(db.Date, nullable=True)
    date_to = db.Column(db.Date, nullable=True)
    surveys_total = db.Column(db.Integer, nullable=False, default=0)
    surveys_matched_team = db.Column(db.Integer, nullable=False, default=0)
    surveys_matched_member = db.Column(db.Integer, nullable=False, default=0)
    surveys_unassigned = db.Column(db.Integer, nullable=False, default=0)

    imported_by = db.relationship('User', foreign_keys=[imported_by_id])


class KpiSurvey(db.Model):
    """One SMS survey (all rows sharing the same datensatz_id), linked to team/agent."""
    __tablename__ = 'kpi_surveys'
    id = db.Column(db.Integer, primary_key=True)
    datensatz_id = db.Column(db.String(64), nullable=False, index=True)
    interviewnummer = db.Column(db.String(64))
    antwort_date = db.Column(db.Date, index=True)
    kontakt_date = db.Column(db.Date)
    be4 = db.Column(db.String(100))
    ma_kenner = db.Column(db.String(50))
    ospname = db.Column(db.String(100))
    kampagne = db.Column(db.String(150))
    studie = db.Column(db.String(150))
    queue = db.Column(db.String(150))
    vorname = db.Column(db.String(100))
    nachname = db.Column(db.String(100))
    team_id = db.Column(db.Integer, db.ForeignKey('teams.id'), nullable=True, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=True, index=True)
    team_member_id = db.Column(db.Integer, db.ForeignKey('team_members.id'), nullable=True, index=True)
    # Precomputed KPI fields (fast aggregation):
    nps_value = db.Column(db.Integer, nullable=True)
    loesung_answer = db.Column(db.String(255), nullable=True)
    info_positive = db.Column(db.Boolean, nullable=True)
    loesung_positive = db.Column(db.Boolean, nullable=True)
    fachkompetenz_stars = db.Column(db.Integer, nullable=True)
    vertrieb_positive = db.Column(db.Boolean, nullable=True)
    batch_id = db.Column(db.Integer, db.ForeignKey('kpi_import_batches.id'), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    team = db.relationship('Team', foreign_keys=[team_id])
    project = db.relationship('Project', foreign_keys=[project_id])
    team_member = db.relationship('TeamMember', foreign_keys=[team_member_id])
    batch = db.relationship('KpiImportBatch', backref=db.backref('surveys', lazy='dynamic'))
    answers = db.relationship(
        'KpiAnswer', back_populates='survey', cascade='all, delete-orphan'
    )


class KpiAnswer(db.Model):
    """A single question/answer row inside a survey (for the raw-data popup)."""
    __tablename__ = 'kpi_answers'
    id = db.Column(db.Integer, primary_key=True)
    survey_id = db.Column(db.Integer, db.ForeignKey('kpi_surveys.id'), nullable=False)
    frage_code = db.Column(db.String(40))
    frage_text = db.Column(db.Text)
    antwort = db.Column(db.Text)

    survey = db.relationship('KpiSurvey', back_populates='answers')


class ProjectKpiSource(db.Model):
    """A survey type (studie) that is relevant for a project. No rows = all types.

    counts=True: feeds the project's KPIs (NPS/Info/Lösung) and is shown in raw data.
    counts=False: shown in the raw-data popup (marked as not counted) but excluded
    from KPI calculation.
    """
    __tablename__ = 'project_kpi_sources'
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False, index=True)
    survey_type = db.Column(db.String(150), nullable=False)
    counts = db.Column(db.Boolean, nullable=False, default=True)
    __table_args__ = (
        db.UniqueConstraint('project_id', 'survey_type', name='uq_project_kpi_source'),
    )


class ProjectKpiSetting(db.Model):
    """Per-project toggle of which KPIs are visible."""
    __tablename__ = 'project_kpi_settings'
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), primary_key=True)
    show_info = db.Column(db.Boolean, nullable=False, default=True)
    show_loesung = db.Column(db.Boolean, nullable=False, default=True)
    show_nps = db.Column(db.Boolean, nullable=False, default=True)
    show_fachkompetenz = db.Column(db.Boolean, nullable=False, default=True)
    show_vertrieb = db.Column(db.Boolean, nullable=False, default=True)


class KpiQuestionMapping(db.Model):
    """Per-project, per-survey-type question code that defines a KPI.

    kpi_kind is 'nps', 'loesung', 'fachkompetenz', or 'vertrieb'.
    'loesung' drives both Informations- and Lösungsquote.
    """
    __tablename__ = 'kpi_question_mappings'
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False, index=True)
    survey_type = db.Column(db.String(150), nullable=False)
    kpi_kind = db.Column(db.String(20), nullable=False)
    frage_code = db.Column(db.String(40), nullable=False)
    __table_args__ = (
        db.UniqueConstraint('project_id', 'survey_type', 'kpi_kind', name='uq_kpi_question_mapping'),
    )


class TeamViewCardSettings(db.Model):
    """Per-project: which metrics appear on /team-view member cards and color thresholds (Ziele)."""
    __tablename__ = 'team_view_card_settings'
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'), primary_key=True)
    show_nps = db.Column(db.Boolean, nullable=False, default=True)
    show_loesung = db.Column(db.Boolean, nullable=False, default=True)
    show_info = db.Column(db.Boolean, nullable=False, default=True)
    show_performance = db.Column(db.Boolean, nullable=False, default=True)
    show_fachkompetenz = db.Column(db.Boolean, nullable=False, default=True)
    show_vertrieb = db.Column(db.Boolean, nullable=False, default=True)
    # Green threshold (>= target = success)
    target_nps = db.Column(db.Float, nullable=False, default=50.0)
    target_loesung = db.Column(db.Float, nullable=False, default=80.0)
    target_info = db.Column(db.Float, nullable=False, default=80.0)
    target_performance = db.Column(db.Float, nullable=False, default=80.0)
    target_fachkompetenz = db.Column(db.Float, nullable=False, default=4.0)
    target_vertrieb = db.Column(db.Float, nullable=False, default=80.0)
    # Yellow threshold (>= warn = warning, below = danger)
    warn_nps = db.Column(db.Float, nullable=False, default=0.0)
    warn_loesung = db.Column(db.Float, nullable=False, default=60.0)
    warn_info = db.Column(db.Float, nullable=False, default=60.0)
    warn_performance = db.Column(db.Float, nullable=False, default=50.0)
    warn_fachkompetenz = db.Column(db.Float, nullable=False, default=3.0)
    warn_vertrieb = db.Column(db.Float, nullable=False, default=60.0)
