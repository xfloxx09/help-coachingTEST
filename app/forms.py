# app/forms.py
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField, SubmitField, SelectField, SelectMultipleField, IntegerField, TextAreaField, DateField, HiddenField
from wtforms.widgets import HiddenInput
from wtforms.validators import DataRequired, EqualTo, ValidationError, Length, NumberRange, Optional
from flask_login import current_user
from sqlalchemy import false, or_
from sqlalchemy.orm import joinedload
from app import db
from app.models import User, Team, TeamMember, Project, Role, Permission, LeitfadenItem, CoachingThemaItem, Abteilung, AssignedCoaching
from app.utils import (
    ARCHIV_TEAM_NAME,
    ROLE_TEAMLEITER,
    ROLE_ADMIN,
    ROLE_BETRIEBSLEITER,
    ROLE_ABTEILUNGSLEITER,
    users_for_assignment_coach_dropdown,
)


class LoginForm(FlaskForm):
    username = StringField('Benutzername', validators=[DataRequired("Benutzername ist erforderlich.")])
    password = PasswordField('Passwort', validators=[DataRequired("Passwort ist erforderlich.")])
    remember_me = BooleanField('Angemeldet bleiben')
    submit = SubmitField('Anmelden')


class RegistrationForm(FlaskForm):
    username = StringField('Benutzername', validators=[DataRequired("Benutzername ist erforderlich."), Length(min=3, max=64)])
    email = StringField('E-Mail (Optional)')
    password = PasswordField('Passwort', validators=[DataRequired("Passwort ist erforderlich."), Length(min=6)])
    password2 = PasswordField(
        'Passwort wiederholen',
        validators=[DataRequired("Passwortwiederholung ist erforderlich."), EqualTo('password', message='Passwörter müssen übereinstimmen.')]
    )
    role_id = SelectField('Rolle', coerce=int, validators=[DataRequired("Rolle ist erforderlich.")], choices=[])
    team_ids = SelectMultipleField('Geführte Teams (nur Rollen mit „assign_teams“ / team_leaders)', coerce=int, choices=[])
    project_id = SelectField('Projekt', coerce=int, choices=[])
    project_ids = SelectMultipleField('Zugeordnete Projekte (nur für Abteilungsleiter)', coerce=int, choices=[])
    extra_project_ids = SelectMultipleField(
        'Weitere Projekte (optional)',
        coerce=int,
        choices=[],
        validators=[Optional()],
    )
    abteilung_id = SelectField(
        'Abteilung (nur bei Berechtigung „Abteilung einsehen“)',
        coerce=int,
        validators=[Optional()],
        choices=[],
    )

    # Team member fields
    first_name = StringField('Vorname', validators=[DataRequired(), Length(min=1, max=50)])
    last_name = StringField('Nachname', validators=[DataRequired(), Length(min=1, max=50)])
    pylon = StringField('Pylon-Nr', validators=[DataRequired("Pylon-Nr ist erforderlich."), Length(max=50)])
    plt_id = StringField('PLT-ID', validators=[Length(max=50)])
    ma_kennung = StringField('MA-Kennung', validators=[Length(max=50)])
    dag_id = StringField('DAG-ID', validators=[Length(max=50)])
    team_id_for_member = SelectField('Team des Mitglieds (eine Zuordnung)', coerce=int, validators=[Optional()], choices=[])
    team_ids_for_member = SelectMultipleField(
        'Teams des Mitglieds (mehrere nur mit Berechtigung „Mehrere Teams“)', coerce=int, validators=[Optional()], choices=[]
    )
    active = BooleanField('Aktiv (nicht im Archiv)', default=True)

    submit = SubmitField('Benutzer registrieren/aktualisieren')

    def __init__(self, original_username=None, password_optional=False, *args, **kwargs):
        super(RegistrationForm, self).__init__(*args, **kwargs)
        self.original_username = original_username
        if password_optional:
            self.password.validators = [Optional(), Length(min=6, message='Mindestens 6 Zeichen.')]
            self.password2.validators = [
                Optional(),
                EqualTo('password', message='Passwörter müssen übereinstimmen.'),
            ]
            self.password.flags.required = False
            self.password2.flags.required = False
        active_teams = (
            Team.query.options(joinedload(Team.project))
            .filter(Team.name != ARCHIV_TEAM_NAME)
            .order_by(Team.name)
            .all()
        )

        def _team_label(t):
            if t.project:
                return f'{t.name} ({t.project.name})'
            return t.name

        lbls = [(t.id, _team_label(t)) for t in active_teams]
        self.team_ids.choices = lbls
        self.team_id_for_member.choices = lbls
        self.team_ids_for_member.choices = lbls
        all_projects = Project.query.order_by(Project.name).all()
        self.project_id.choices = [(p.id, p.name) for p in all_projects]
        self.project_ids.choices = [(p.id, p.name) for p in all_projects]
        self.extra_project_ids.choices = [(p.id, p.name) for p in all_projects]
        self.abteilung_id.choices = [(0, '— keine —')] + [(a.id, a.name) for a in Abteilung.query.order_by(Abteilung.name).all()]
        self.role_id.choices = [(r.id, r.name) for r in Role.query.order_by(Role.name).all()]

    def validate_username(self, username_field):
        query = User.query.filter(User.username == username_field.data)
        if self.original_username and self.original_username == username_field.data:
            return
        user = query.first()
        if user:
            raise ValidationError('Dieser Benutzername ist bereits vergeben.')

    def validate_project_id(self, field):
        role = Role.query.get(self.role_id.data)
        role_name = role.name if role else None
        if role_name == ROLE_ABTEILUNGSLEITER:
            return
        if role and role.has_permission('view_abteilung') and self.abteilung_id.data:
            return
        if role_name != ROLE_ABTEILUNGSLEITER and not field.data:
            raise ValidationError('Projekt ist erforderlich.')

    def validate_project_ids(self, field):
        role = Role.query.get(self.role_id.data)
        if not role or role.name != ROLE_ABTEILUNGSLEITER:
            return
        if role.has_permission('view_abteilung') and self.abteilung_id.data:
            return
        if not field.data:
            raise ValidationError('Mindestens ein Projekt oder eine Abteilung muss ausgewählt werden.')

    def validate(self, extra_validators=None):
        if not super(RegistrationForm, self).validate(extra_validators):
            return False
        role = Role.query.get(self.role_id.data)
        if not role:
            return True
        if role.has_permission('multiple_teams'):
            if not self.team_ids_for_member.data:
                self.team_ids_for_member.errors.append('Mindestens ein Team ist erforderlich.')
                return False
        else:
            if not self.team_id_for_member.data:
                self.team_id_for_member.errors.append('Team ist erforderlich.')
                return False
        return True


class TeamForm(FlaskForm):
    name = StringField('Team Name', validators=[DataRequired(), Length(min=3, max=100)])
    team_leaders = SelectMultipleField('Teamleiter', coerce=int, choices=[])
    project_id = SelectField('Projekt', coerce=int, choices=[])
    active_for_coaching = BooleanField(
        'Für neue Coachings & Workshops verfügbar',
        default=True,
    )
    submit = SubmitField('Team erstellen/aktualisieren')

    def __init__(self, original_name=None, *args, **kwargs):
        super(TeamForm, self).__init__(*args, **kwargs)
        self.original_name = original_name
        possible_leaders = User.query.filter(User.role.has(name=ROLE_TEAMLEITER)).order_by(User.username).all()
        self.team_leaders.choices = [(u.id, u.username) for u in possible_leaders]
        self.project_id.choices = [(p.id, p.name) for p in Project.query.order_by(Project.name).all()]

    def validate_name(self, name_field):
        if self.original_name and self.original_name.strip().upper() == name_field.data.strip().upper():
            return
        if Team.query.filter(Team.name.ilike(name_field.data)).first():
            raise ValidationError('Ein Team mit diesem Namen existiert bereits.')
        if name_field.data.strip().upper() == ARCHIV_TEAM_NAME:
            raise ValidationError(f'Der Teamname \\\"{ARCHIV_TEAM_NAME}\\\" ist für das System reserviert.')


class TeamMemberForm(FlaskForm):
    first_name = StringField('Vorname', validators=[DataRequired(), Length(min=1, max=50)])
    last_name = StringField('Nachname', validators=[DataRequired(), Length(min=1, max=50)])
    team_id = SelectField('Team', coerce=int, validators=[DataRequired("Team ist erforderlich.")], choices=[])
    pylon = StringField('Pylon-Nr', validators=[DataRequired("Pylon-Nr ist erforderlich."), Length(max=50)])
    plt_id = StringField('PLT-ID', validators=[Length(max=50)])
    ma_kennung = StringField('MA-Kennung', validators=[Length(max=50)])
    dag_id = StringField('DAG-ID', validators=[Length(max=50)])
    active = BooleanField('Aktiv (nicht im Archiv)', default=True)
    submit = SubmitField('Teammitglied erstellen/aktualisieren')

    def __init__(self, *args, **kwargs):
        super(TeamMemberForm, self).__init__(*args, **kwargs)
        active_teams = Team.query.filter(Team.name != ARCHIV_TEAM_NAME).order_by(Team.name).all()
        if active_teams:
            self.team_id.choices = [(t.id, t.name) for t in active_teams]
        else:
            self.team_id.choices = [("", "Bitte zuerst Teams erstellen")]


LEITFADEN_CHOICES = [('Ja', 'Ja'), ('Nein', 'Nein'), ('k.A.', 'k.A.')]
COACHING_SUBJECT_CHOICES = [
    ('', '--- Bitte wählen ---'),
    ('Sales', 'Sales'),
    ('Qualität', 'Qualität'),
    ('Allgemein', 'Allgemein')
]


class CoachingForm(FlaskForm):
    team_member_id = SelectField(
        'Teammitglied',
        coerce=int,
        validators=[DataRequired("Teammitglied ist erforderlich.")],
        choices=[]
    )
    coaching_style = SelectField('Coaching Stil', choices=[('Side-by-Side', 'Side-by-Side'), ('TCAP', 'TCAP')], validators=[DataRequired("Coaching-Stil ist erforderlich.")])
    tcap_id = StringField(
        'T-CAP ID (Pflichtfeld bei Stil „TCAP“)',
        validators=[Length(max=100, message='T-CAP ID darf höchstens 100 Zeichen haben.')],
    )
    coaching_subject = SelectField('Coaching Thema', choices=COACHING_SUBJECT_CHOICES, validators=[DataRequired("Coaching-Thema ist erforderlich.")])
    leitfaden_begruessung = SelectField('Begrüßung', choices=LEITFADEN_CHOICES, default='k.A.', validate_choice=False)
    leitfaden_legitimation = SelectField('Legitimation', choices=LEITFADEN_CHOICES, default='k.A.', validate_choice=False)
    leitfaden_pka = SelectField('PKA', choices=LEITFADEN_CHOICES, default='k.A.', validate_choice=False)
    leitfaden_kek = SelectField('KEK', choices=LEITFADEN_CHOICES, default='k.A.', validate_choice=False)
    leitfaden_angebot = SelectField('Angebot', choices=LEITFADEN_CHOICES, default='k.A.', validate_choice=False)
    leitfaden_zusammenfassung = SelectField('Zusammenfassung', choices=LEITFADEN_CHOICES, default='k.A.', validate_choice=False)
    leitfaden_kzb = SelectField('KZB', choices=LEITFADEN_CHOICES, default='k.A.', validate_choice=False)
    performance_mark = IntegerField('Performance Note (0-10)', validators=[DataRequired("Performance Note ist erforderlich."), NumberRange(min=0, max=10)])
    time_spent = IntegerField('Zeitaufwand (Minuten)', validators=[DataRequired("Zeitaufwand ist erforderlich."), NumberRange(min=1)])
    coach_notes = TextAreaField('Notizen des Coaches', validators=[Length(max=2000)])
    assigned_coaching_id = SelectField('Zugewiesene Aufgabe (optional)', coerce=int, choices=[], validators=[Optional()], validate_choice=False)
    submit = SubmitField('Coaching speichern')

    def __init__(self, current_user_role=None, current_user_team_ids=None, *args, **kwargs):
        super(CoachingForm, self).__init__(*args, **kwargs)
        self.current_user_role = current_user_role
        self.current_user_team_ids = current_user_team_ids if current_user_team_ids is not None else []

    def _team_member_pick_query(
        self,
        exclude_archiv=False,
        project_id=None,
        include_member_ids=None,
        team_filter_id=None,
    ):
        """Base query for add/edit coaching team-member dropdown (same scoping as before)."""
        query = TeamMember.query.join(Team, TeamMember.team_id == Team.id)

        if project_id:
            query = query.filter(Team.project_id == project_id)

        if current_user.is_authenticated and current_user.has_permission('coach_own_team_only'):
            coach_team_member = current_user.team_members[0] if current_user.team_members else None
            if coach_team_member:
                query = query.filter(TeamMember.team_id == coach_team_member.team_id)
            else:
                query = query.filter(false())
        else:
            if self.current_user_role == ROLE_TEAMLEITER and self.current_user_team_ids:
                query = query.filter(TeamMember.team_id.in_(self.current_user_team_ids))
            elif self.current_user_role not in [ROLE_ADMIN, ROLE_BETRIEBSLEITER]:
                pass

        if exclude_archiv:
            query = query.filter(Team.name != ARCHIV_TEAM_NAME)

        include_ids = include_member_ids or []
        coaching_ok = Team.active_for_coaching.is_(True)
        if include_ids:
            coaching_ok = or_(coaching_ok, TeamMember.id.in_(include_ids))
        query = query.filter(coaching_ok)

        if team_filter_id:
            query = query.filter(TeamMember.team_id == team_filter_id)
        return query

    def teams_for_member_pick_filter(self, exclude_archiv=False, project_id=None, include_member_ids=None):
        """Distinct teams that appear in the member picker (for optional team pre-filter on add-coaching)."""
        q = self._team_member_pick_query(
            exclude_archiv=exclude_archiv,
            project_id=project_id,
            include_member_ids=include_member_ids,
            team_filter_id=None,
        )
        team_ids = [row[0] for row in q.with_entities(TeamMember.team_id).distinct().all() if row[0]]
        if not team_ids:
            return []
        return Team.query.filter(Team.id.in_(team_ids)).order_by(Team.name).all()

    def update_team_member_choices(
        self,
        exclude_archiv=False,
        project_id=None,
        include_member_ids=None,
        team_filter_id=None,
    ):
        generated_choices = []
        query = self._team_member_pick_query(
            exclude_archiv=exclude_archiv,
            project_id=project_id,
            include_member_ids=include_member_ids,
            team_filter_id=team_filter_id,
        )

        members = query.order_by(TeamMember.name).all()
        for m in members:
            generated_choices.append((m.id, f"{m.name} ({m.team.name})"))
        self.team_member_id.choices = generated_choices

    def update_assignment_choices(self, team_member_id, coach_id):
        from datetime import datetime
        assignments = (
            AssignedCoaching.query.filter(
                AssignedCoaching.team_member_id == team_member_id,
                AssignedCoaching.coach_id == coach_id,
                AssignedCoaching.status.in_(['pending', 'accepted', 'in_progress']),
                or_(
                    AssignedCoaching.deadline.is_(None),
                    AssignedCoaching.deadline >= datetime.utcnow(),
                ),
            )
            .order_by(AssignedCoaching.deadline)
            .all()
        )
        self.assigned_coaching_id.choices = [(0, '--- Keine zugewiesene Aufgabe ---')] + [(a.id, f"Aufgabe #{a.id} (bis {a.deadline.strftime('%d.%m.%y')}) – Fortschritt: {a.progress}%") for a in assignments]

    def apply_bogen(self, project_id, coaching=None):
        """Set coaching style & subject choices from project bogen config and Thema-Liste."""
        from app.utils import bogen_layout_for_project, thema_items_for_project

        layout = bogen_layout_for_project(project_id)
        style_choices = []
        if getattr(layout, 'allow_side_by_side', True):
            style_choices.append(('Side-by-Side', 'Side-by-Side'))
        if getattr(layout, 'allow_tcap', True):
            style_choices.append(('TCAP', 'TCAP'))
        if not style_choices:
            style_choices = [('Side-by-Side', 'Side-by-Side'), ('TCAP', 'TCAP')]
        cur_style = coaching.coaching_style if coaching and coaching.coaching_style else None
        if cur_style and not any(cur_style == x[0] for x in style_choices):
            style_choices.append((cur_style, cur_style))
        self.coaching_style.choices = style_choices

        themes = thema_items_for_project(project_id)
        subj = [('', '--- Bitte wählen ---')]
        if themes:
            for t in themes:
                subj.append((t.name, t.name))
        else:
            subj.extend([x for x in COACHING_SUBJECT_CHOICES if x[0]])
        cur_sub = (coaching.coaching_subject or '').strip() if coaching and coaching.coaching_subject else None
        if cur_sub and not any(cur_sub == x[0] for x in subj):
            subj.append((cur_sub, cur_sub))
        self.coaching_subject.choices = subj

    def validate_tcap_id(self, field):
        style = (self.coaching_style.data or '').strip()
        if style == 'TCAP':
            if not (field.data or '').strip():
                raise ValidationError('T-CAP ID ist bei Coaching-Stil „TCAP“ erforderlich.')


class PasswordChangeForm(FlaskForm):
    old_password = PasswordField('Aktuelles Passwort', validators=[DataRequired("Bitte aktuelles Passwort eingeben.")])
    new_password = PasswordField('Neues Passwort', validators=[DataRequired("Neues Passwort ist erforderlich."), Length(min=6)])
    confirm_password = PasswordField('Neues Passwort wiederholen', validators=[DataRequired("Bitte wiederholen."), EqualTo('new_password', message='Passwörter müssen übereinstimmen.')])
    submit = SubmitField('Passwort ändern')


class WorkshopForm(FlaskForm):
    title = StringField('Workshop-Thema', validators=[DataRequired("Bitte ein Thema angeben."), Length(max=200)])
    team_member_ids = SelectMultipleField('Teilnehmer', coerce=int, validators=[DataRequired("Mindestens ein Teilnehmer erforderlich.")], choices=[])
    overall_rating = IntegerField('Gesamtbewertung (0-10)', validators=[DataRequired(), NumberRange(min=0, max=10)])
    time_spent = IntegerField('Zeitaufwand (Minuten)', validators=[DataRequired(), NumberRange(min=1)])
    notes = TextAreaField('Notizen', validators=[Length(max=2000)])
    submit = SubmitField('Workshop speichern')

    def __init__(self, current_user_role=None, current_user_team_ids=None, *args, **kwargs):
        super(WorkshopForm, self).__init__(*args, **kwargs)
        self.current_user_role = current_user_role
        self.current_user_team_ids = current_user_team_ids if current_user_team_ids is not None else []

    def update_participant_choices(self, project_id=None, include_member_ids=None):
        """Same scoping rules as CoachingForm.update_team_member_choices (coach_own_team_only, Teamleiter teams, Admin/BL)."""
        generated_choices = []
        query = TeamMember.query.join(Team, TeamMember.team_id == Team.id)

        if project_id:
            query = query.filter(Team.project_id == project_id)

        if current_user.is_authenticated and current_user.has_permission('coach_own_team_only'):
            coach_team_member = current_user.team_members[0] if current_user.team_members else None
            if coach_team_member:
                query = query.filter(TeamMember.team_id == coach_team_member.team_id)
            else:
                query = query.filter(false())
        else:
            if self.current_user_role == ROLE_TEAMLEITER and self.current_user_team_ids:
                query = query.filter(TeamMember.team_id.in_(self.current_user_team_ids))
            elif self.current_user_role not in [ROLE_ADMIN, ROLE_BETRIEBSLEITER]:
                pass

        query = query.filter(Team.name != ARCHIV_TEAM_NAME)

        include_ids = include_member_ids or []
        coaching_ok = Team.active_for_coaching.is_(True)
        if include_ids:
            coaching_ok = or_(coaching_ok, TeamMember.id.in_(include_ids))
        query = query.filter(coaching_ok)

        members = query.order_by(TeamMember.name).all()
        for m in members:
            generated_choices.append((m.id, f"{m.name} ({m.team.name})"))
        self.team_member_ids.choices = generated_choices

    def validate_team_member_ids(self, field):
        if len(field.data) < 2:
            raise ValidationError('Es müssen mindestens zwei Teilnehmer ausgewählt werden.')


class TeamsCoachingBulkForm(FlaskForm):
    """CSRF + submit for bulk toggle of team active_for_coaching."""
    submit = SubmitField('Änderungen speichern')


class ProjectForm(FlaskForm):
    name = StringField('Projektname', validators=[DataRequired(), Length(min=3, max=100)])
    description = TextAreaField('Beschreibung', validators=[Length(max=500)])
    abteilung_id = SelectField('Abteilung (optional)', coerce=int, validators=[Optional()], choices=[])
    submit = SubmitField('Projekt speichern')

    def __init__(self, *args, **kwargs):
        super(ProjectForm, self).__init__(*args, **kwargs)
        self.abteilung_id.choices = [(0, '— keine —')] + [(a.id, a.name) for a in Abteilung.query.order_by(Abteilung.name).all()]


class AbteilungForm(FlaskForm):
    name = StringField('Name der Abteilung', validators=[DataRequired(), Length(min=2, max=150)])
    description = TextAreaField('Beschreibung', validators=[Length(max=500)])
    project_ids = SelectMultipleField('Projekte (alle zugehörigen Teams gelten mit)', coerce=int, choices=[])
    submit = SubmitField('Speichern')

    def __init__(self, *args, **kwargs):
        super(AbteilungForm, self).__init__(*args, **kwargs)
        self.project_ids.choices = [(p.id, p.name) for p in Project.query.order_by(Project.name).all()]


class AssignedCoachingForm(FlaskForm):
    team_member_id = SelectField('Teammitglied', coerce=int, validators=[Optional()], choices=[])
    coach_id = SelectField('Coach', coerce=int, validators=[DataRequired("Coach ist erforderlich.")], choices=[])
    deadline = DateField('Deadline', format='%Y-%m-%d', validators=[DataRequired("Deadline ist erforderlich.")])
    expected_coaching_count = IntegerField('Anzahl erwarteter Coachings', validators=[DataRequired("Anzahl ist erforderlich."), NumberRange(min=1, max=50)], default=1)
    desired_performance_note = IntegerField('Gewünschte Performance Note (0-10)', validators=[Optional(), NumberRange(min=0, max=10)], default=None)
    submit = SubmitField('Coaching zuweisen')

    def __init__(self, allowed_project_ids=None, team_member_id=None, *args, **kwargs):
        super(AssignedCoachingForm, self).__init__(*args, **kwargs)
        self.team_member_active_assignment_counts = {}
        if allowed_project_ids:
            project_id = allowed_project_ids[0]
            members = TeamMember.query.options(joinedload(TeamMember.team)).join(Team, TeamMember.team_id == Team.id).filter(
                Team.project_id.in_(allowed_project_ids),
                Team.name != ARCHIV_TEAM_NAME,
                or_(Team.active_for_coaching.is_(True), Team.visible_for_coaching_assignment.is_(True)),
            ).order_by(Team.name, TeamMember.name).all()
            member_ids = [m.id for m in members]
            if member_ids:
                rows = db.session.query(
                    AssignedCoaching.team_member_id,
                    db.func.count(AssignedCoaching.id),
                ).filter(
                    AssignedCoaching.team_member_id.in_(member_ids),
                    AssignedCoaching.status.in_(['pending', 'accepted', 'in_progress']),
                ).group_by(AssignedCoaching.team_member_id).all()
                self.team_member_active_assignment_counts = {mid: int(cnt or 0) for mid, cnt in rows}

            self.team_member_id.choices = [
                (
                    m.id,
                    f"{m.name}{' ⚠' if self.team_member_active_assignment_counts.get(m.id, 0) > 0 else ''} ({m.team.name})"
                )
                for m in members
            ]

            coaches = users_for_assignment_coach_dropdown(project_id, team_member_id)
            self.coach_id.choices = [(u.id, f"{u.coach_display_name} ({u.role_name})") for u in coaches]


class RoleForm(FlaskForm):
    name = StringField('Rollenname', validators=[DataRequired(), Length(min=3, max=100)])
    description = TextAreaField('Beschreibung', validators=[Length(max=500)])
    permissions = SelectMultipleField('Berechtigungen', coerce=int, choices=[])
    projects = SelectMultipleField('Projekte (optional)', coerce=int, choices=[])
    submit = SubmitField('Speichern')

    def __init__(self, *args, **kwargs):
        super(RoleForm, self).__init__(*args, **kwargs)
        self.permissions.choices = [(p.id, f"{p.name} - {p.description}") for p in Permission.query.order_by(Permission.name).all()]
        self.projects.choices = [(p.id, p.name) for p in Project.query.order_by(Project.name).all()]


class AdminAssignedCoachingForm(FlaskForm):
    coach_id = SelectField('Coach', coerce=int, validators=[DataRequired()], choices=[])
    team_member_id = SelectField('Teammitglied', coerce=int, validators=[DataRequired()], choices=[])
    deadline = DateField('Deadline', format='%Y-%m-%d', validators=[DataRequired()])
    expected_coaching_count = IntegerField('Erwartete Coachings', validators=[DataRequired(), NumberRange(min=1, max=50)], default=1)
    desired_performance_note = IntegerField('Gewünschte Performance Note (0-10)', validators=[Optional(), NumberRange(min=0, max=10)], default=None)
    status = SelectField('Status', choices=[
        ('pending', 'Ausstehend'),
        ('accepted', 'Angenommen'),
        ('in_progress', 'In Bearbeitung'),
        ('completed', 'Abgeschlossen'),
        ('expired', 'Abgelaufen'),
        ('rejected', 'Abgelehnt'),
        ('cancelled', 'Storniert')
    ], validators=[DataRequired()])
    submit = SubmitField('Speichern')

    def __init__(self, *args, **kwargs):
        super(AdminAssignedCoachingForm, self).__init__(*args, **kwargs)
        coaches = list(User.query.order_by(User.username).all())
        coaches.sort(key=lambda u: (u.coach_display_name or '').lower())
        self.coach_id.choices = [(u.id, f"{u.coach_display_name} ({u.role_name})") for u in coaches]
        self.team_member_id.choices = [
            (m.id, f"{m.name} ({m.team.name})")
            for m in TeamMember.query.join(Team, TeamMember.team_id == Team.id).filter(
                Team.name != ARCHIV_TEAM_NAME,
                or_(Team.active_for_coaching.is_(True), Team.visible_for_coaching_assignment.is_(True)),
            ).order_by(Team.name, TeamMember.name).all()
        ]


class TeamMemberWithUserForm(FlaskForm):
    first_name = StringField('Vorname', validators=[DataRequired(), Length(min=1, max=50)])
    last_name = StringField('Nachname', validators=[DataRequired(), Length(min=1, max=50)])
    team_id = SelectField('Team', coerce=int, validators=[DataRequired("Team ist erforderlich.")], choices=[])
    pylon = StringField('Pylon-Nr', validators=[DataRequired("Pylon-Nr ist erforderlich."), Length(max=50)])
    plt_id = StringField('PLT-ID', validators=[Length(max=50)])
    ma_kennung = StringField('MA-Kennung', validators=[Length(max=50)])
    dag_id = StringField('DAG-ID', validators=[Length(max=50)])
    active = BooleanField('Aktiv (nicht im Archiv)', default=True)
    
    create_user = BooleanField('Benutzerkonto erstellen')
    username = StringField('Benutzername', validators=[Length(min=3, max=64)])
    email = StringField('E-Mail (Optional)')
    password = PasswordField('Passwort', validators=[Length(min=6)])
    password2 = PasswordField('Passwort wiederholen', validators=[EqualTo('password', message='Passwörter müssen übereinstimmen.')])
    submit = SubmitField('Teammitglied erstellen')

    def __init__(self, *args, **kwargs):
        super(TeamMemberWithUserForm, self).__init__(*args, **kwargs)
        active_teams = Team.query.filter(Team.name != ARCHIV_TEAM_NAME).order_by(Team.name).all()
        self.team_id.choices = [(t.id, t.name) for t in active_teams]

    def validate_username(self, field):
        if self.create_user.data and field.data:
            user = User.query.filter_by(username=field.data).first()
            if user:
                raise ValidationError('Dieser Benutzername ist bereits vergeben.')


class LeitfadenItemForm(FlaskForm):
    name = StringField('Bezeichnung', validators=[DataRequired(), Length(min=2, max=120)])
    position = IntegerField('Position', validators=[DataRequired(), NumberRange(min=0, max=9999)])
    is_active = BooleanField('Aktiv', default=True)
    submit = SubmitField('Speichern')

    def __init__(self, original_name=None, scope_project_id=None, *args, **kwargs):
        super(LeitfadenItemForm, self).__init__(*args, **kwargs)
        self.original_name = original_name
        self.scope_project_id = scope_project_id

    def validate_name(self, field):
        name = (field.data or '').strip()
        query = LeitfadenItem.query.filter(db.func.lower(LeitfadenItem.name) == name.lower())
        if self.scope_project_id is None:
            query = query.filter(LeitfadenItem.project_id.is_(None))
        else:
            query = query.filter(LeitfadenItem.project_id == self.scope_project_id)
        if self.original_name and self.original_name.strip().lower() == name.lower():
            return
        if query.first():
            raise ValidationError('Diese Leitfaden-Bezeichnung existiert bereits (in diesem Kontext).')


class CoachingThemaItemForm(FlaskForm):
    name = StringField('Bezeichnung', validators=[DataRequired(), Length(min=2, max=120)])
    position = IntegerField('Position', validators=[DataRequired(), NumberRange(min=0, max=9999)])
    is_active = BooleanField('Aktiv', default=True)
    submit = SubmitField('Speichern')

    def __init__(self, original_name=None, scope_project_id=None, *args, **kwargs):
        super(CoachingThemaItemForm, self).__init__(*args, **kwargs)
        self.original_name = original_name
        self.scope_project_id = scope_project_id

    def validate_name(self, field):
        name = (field.data or '').strip()
        query = CoachingThemaItem.query.filter(db.func.lower(CoachingThemaItem.name) == name.lower())
        if self.scope_project_id is None:
            query = query.filter(CoachingThemaItem.project_id.is_(None))
        else:
            query = query.filter(CoachingThemaItem.project_id == self.scope_project_id)
        if self.original_name and self.original_name.strip().lower() == name.lower():
            return
        if query.first():
            raise ValidationError('Diese Thema-Bezeichnung existiert bereits (in diesem Kontext).')


class CoachingBogenLayoutForm(FlaskForm):
    allow_side_by_side = BooleanField('Coaching-Stil „Side-by-Side“ anbieten', default=True)
    allow_tcap = BooleanField('Coaching-Stil „TCAP“ anbieten', default=True)
    show_performance_bar = BooleanField('Performance-Balken (Note 0–10) im Bogen anzeigen', default=True)
    show_coach_notes = BooleanField('Notizen-Feld im Bogen anzeigen', default=True)
    show_time_spent = BooleanField('Zeitaufwand (Minuten) im Bogen anzeigen', default=True)
    submit = SubmitField('Speichern')

    def validate(self, extra_validators=None):
        rv = super(CoachingBogenLayoutForm, self).validate(extra_validators)
        if not rv:
            return False
        if not self.allow_side_by_side.data and not self.allow_tcap.data:
            self.allow_tcap.errors.append('Mindestens ein Coaching-Stil muss aktiv sein.')
            return False
        return True


class CoachingReviewForm(FlaskForm):
    """coaching-PK kommt als plain HTML name=review_coaching_pk (robuster als WTForms Hidden)."""
    rating = IntegerField(
        widget=HiddenInput(),
        default=5,
        validators=[DataRequired(message='Bitte eine Sternebewertung wählen.'), NumberRange(min=1, max=5)],
    )
    # Visibility options controlled by the coached employee.
    # The labels are purely for UX; the actual filtering is permission-driven in the routes.
    visible_to_coach = BooleanField('Coach/Teamleiter', default=True)
    visible_to_manager = BooleanField('Projektleiter/Manager/Abteilungsleiter', default=True)
    comment = TextAreaField('Kommentar (optional)', validators=[Optional(), Length(max=2000)])
    next = HiddenField(validators=[Optional(), Length(max=2048)])
    submit = SubmitField('Bewertung absenden')

    def validate(self, *args, **kwargs):
        rv = super().validate(*args, **kwargs)
        if not rv:
            return False
        if not (self.visible_to_coach.data or self.visible_to_manager.data):
            self.visible_to_coach.errors.append('Bitte wählen, für wen die Bewertung sichtbar sein soll.')
            self.visible_to_manager.errors.append('Bitte wählen, für wen die Bewertung sichtbar sein soll.')
            return False
        return True
