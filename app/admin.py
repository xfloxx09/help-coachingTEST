# app/admin.py
from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app, session, jsonify, abort
from flask_login import login_required, current_user
from sqlalchemy import desc, or_, false, update, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload
from app import db
from app.models import (
    User, Team, TeamMember, Coaching, Workshop, workshop_participants, Project, Role, Permission,
    AssignedCoaching, LeitfadenItem, CoachingLeitfadenResponse, Abteilung, CoachingThemaItem,
    CoachingBogenLayout, PlannedCoaching, PlannedWorkshop, KpiImportBatch, KpiSurvey, KpiAnswer,
    ProjectKpiSource, ProjectKpiSetting, KpiQuestionMapping, TeamViewCardSettings, KpiCategory,
    ProductivityImportBatch, ProductivityInterval, ProjectProductivitySetting,
)
from app import productivity as productivity_logic
from app import kpi as kpi_logic
from app.forms import RegistrationForm, TeamForm, TeamMemberForm, CoachingForm, WorkshopForm, ProjectForm, RoleForm, AdminAssignedCoachingForm, TeamMemberWithUserForm, LeitfadenItemForm, TeamsCoachingBulkForm, AbteilungForm, CoachingThemaItemForm, CoachingBogenLayoutForm
from app.utils import role_required, permission_required, ROLE_ADMIN, ROLE_BETRIEBSLEITER, ROLE_TEAMLEITER, ROLE_ABTEILUNGSLEITER, get_or_create_archiv_team, ARCHIV_TEAM_NAME, get_or_create_role, workshop_individual_rating_from_request, projects_in_abteilung, leitfaden_items_for_coaching_edit, bogen_layout_for_project
from app.main_routes import calculate_date_range, get_month_name_german, _sync_assigned_coaching_status_from_progress
from datetime import datetime, timezone, time
import csv
import json
import tempfile
import os
import re
import subprocess
import sys
import threading
import time
import uuid

bp = Blueprint('admin', __name__)
LEITFADEN_CHOICES = {'Ja', 'Nein', 'k.A.'}


def _normalize_int_ids(raw_list):
    out = []
    for x in raw_list or []:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(out))


def _precheck_coaching_planned_links(coaching_ids):
    """Counts planned_coachings rows referencing these coachings (fulfilled or as Folgetermin-Quelle)."""
    ids = _normalize_int_ids(coaching_ids)
    if not ids:
        return {'fulfilled': 0, 'source': 0, 'has_links': False}
    f_cnt = PlannedCoaching.query.filter(PlannedCoaching.fulfilled_coaching_id.in_(ids)).count()
    s_cnt = PlannedCoaching.query.filter(PlannedCoaching.source_coaching_id.in_(ids)).count()
    return {'fulfilled': f_cnt, 'source': s_cnt, 'has_links': f_cnt + s_cnt > 0}


def _unlink_planned_coachings_before_delete(coaching_ids):
    """
    Erfüllte Plan-Zeilen mit dieser Coaching-ID entfernen (sonst FK auf coachings).
    Offene Pläne: nur source_coaching_id aufheben, Termin bleibt erhalten.
    """
    ids = _normalize_int_ids(coaching_ids)
    if not ids:
        return
    PlannedCoaching.query.filter(PlannedCoaching.fulfilled_coaching_id.in_(ids)).delete(
        synchronize_session=False
    )
    PlannedCoaching.query.filter(PlannedCoaching.source_coaching_id.in_(ids)).update(
        {PlannedCoaching.source_coaching_id: None},
        synchronize_session=False,
    )


def _admin_delete_coachings_by_ids(coaching_ids):
    """
    Löscht Coachings inkl. Leitfaden/Antworten (ORM-Cascade) und bereinigt planned_coachings.
    Gibt die Anzahl gelöschter Coachings zurück.
    """
    ids = _normalize_int_ids(coaching_ids)
    if not ids:
        return 0
    _unlink_planned_coachings_before_delete(ids)
    assigned_refs = set()
    deleted = 0
    for cid in ids:
        c = Coaching.query.get(cid)
        if c:
            if c.assigned_coaching_id:
                assigned_refs.add(c.assigned_coaching_id)
            db.session.delete(c)
            deleted += 1
    db.session.flush()
    for aid in assigned_refs:
        ac = AssignedCoaching.query.get(aid)
        if ac:
            _sync_assigned_coaching_status_from_progress(ac)
    db.session.commit()
    return deleted


def _unlink_planned_workshops_before_delete(workshop_ids):
    """
    Remove FK links from planned_workshops before deleting workshops.
    A previously fulfilled planned workshop becomes open again.
    """
    ids = _normalize_int_ids(workshop_ids)
    if not ids:
        return
    PlannedWorkshop.query.filter(PlannedWorkshop.fulfilled_workshop_id.in_(ids)).update(
        {
            PlannedWorkshop.fulfilled_workshop_id: None,
            PlannedWorkshop.status: 'open',
        },
        synchronize_session=False,
    )


def _role_ids_with_multiple_teams():
    return [r.id for r in Role.query.order_by(Role.name).all() if r.has_permission('multiple_teams')]


def _role_ids_with_view_abteilung():
    return [r.id for r in Role.query.order_by(Role.name).all() if r.has_permission('view_abteilung')]


def _sync_abteilung_projects(abteilung_id, project_id_list):
    Project.query.filter_by(abteilung_id=abteilung_id).update({Project.abteilung_id: None}, synchronize_session='fetch')
    for pid in project_id_list or []:
        p = Project.query.get(pid)
        if p:
            p.abteilung_id = abteilung_id


def _abteilung_pk_from_form(form):
    v = form.abteilung_id.data if getattr(form, 'abteilung_id', None) else None
    return v if v else None


def _sync_user_team_members_from_form(user, role, form):
    """TeamMember-Zeilen aus Benutzerformular; „Mein Team“ basiert nur darauf (nicht teams_led)."""
    archiv = get_or_create_archiv_team()
    full_name = f"{form.first_name.data} {form.last_name.data}".strip()
    if role.has_permission('multiple_teams'):
        want = list(dict.fromkeys(int(x) for x in (form.team_ids_for_member.data or []) if x))
    else:
        want = [int(form.team_id_for_member.data)] if form.team_id_for_member.data else []
    members = TeamMember.query.filter_by(user_id=user.id).all()
    for m in members:
        m.name = full_name
        m.pylon = form.pylon.data
        m.plt_id = form.plt_id.data
        m.ma_kennung = form.ma_kennung.data
        m.dag_id = form.dag_id.data
    if not form.active.data:
        for m in members:
            if m.team_id != archiv.id:
                m.original_team_id = m.team_id
                m.original_project_id = m.team.project_id if m.team else None
                m.team_id = archiv.id
        if not members and want:
            for tid in want:
                tm_team = Team.query.get(tid)
                if not tm_team:
                    continue
                nm = TeamMember(
                    user_id=user.id,
                    team_id=archiv.id,
                    name=full_name,
                    pylon=form.pylon.data,
                    plt_id=form.plt_id.data,
                    ma_kennung=form.ma_kennung.data,
                    dag_id=form.dag_id.data,
                    original_team_id=tid,
                    original_project_id=tm_team.project_id,
                )
                db.session.add(nm)
        return True
    if not want:
        return False
    want_set = set(want)
    all_rows = lambda: TeamMember.query.filter_by(user_id=user.id).all()
    def _archive_member_row(member_row):
        if member_row.team_id == archiv.id:
            return
        member_row.original_team_id = member_row.team_id
        member_row.original_project_id = member_row.team.project_id if member_row.team else None
        member_row.team_id = archiv.id

    for tid in want:
        found = False
        for m in all_rows():
            if m.team_id == tid:
                found = True
                break
            if m.team_id == archiv.id and m.original_team_id == tid:
                m.team_id = tid
                m.original_team_id = None
                m.original_project_id = None
                found = True
                break
        if not found:
            db.session.add(TeamMember(
                user_id=user.id,
                team_id=tid,
                name=full_name,
                pylon=form.pylon.data,
                plt_id=form.plt_id.data,
                ma_kennung=form.ma_kennung.data,
                dag_id=form.dag_id.data,
            ))
    db.session.flush()
    for m in list(all_rows()):
        if m.team_id == archiv.id:
            continue
        if m.team_id not in want_set:
            n = Coaching.query.filter_by(team_member_id=m.id).count()
            if n == 0:
                db.session.delete(m)
            else:
                old_team_name = m.team.name if m.team else "?"
                _archive_member_row(m)
                flash(
                    f'Team „{old_team_name}“: Mitglied ins ARCHIV verschoben (Coachings vorhanden).',
                    'info',
                )
    if not role.has_permission('multiple_teams'):
        keep = want[0] if want else None
        if keep:
            for m in list(all_rows()):
                if m.team_id == archiv.id:
                    continue
                if m.team_id != keep:
                    if Coaching.query.filter_by(team_member_id=m.id).count() == 0:
                        db.session.delete(m)
                    else:
                        _archive_member_row(m)
    return True


@bp.route('/')
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def panel():
    page_users = request.args.get('page_users', 1, type=int)
    page_teams = request.args.get('page_teams', 1, type=int)
    page_members = request.args.get('page_members', 1, type=int)
    page_archiv = request.args.get('page_archiv', 1, type=int)
    
    user_project_filter = request.args.get('user_project', type=int)
    user_role_filter = request.args.get('user_role', default='', type=str)
    user_search = request.args.get('user_search', default='', type=str).strip()
    
    team_project_filter = request.args.get('team_project', type=int)
    team_search = request.args.get('team_search', default='', type=str).strip()
    
    member_project_filter = request.args.get('member_project', type=int)
    member_team_filter = request.args.get('member_team', type=int)
    member_search = request.args.get('member_search', default='', type=str).strip()
    
    archiv_project_filter = request.args.get('archiv_project', type=int)
    archiv_team_filter = request.args.get('archiv_team', type=int)
    archiv_search = request.args.get('archiv_search', default='', type=str).strip()

    user_filter_active = any([user_project_filter, user_role_filter, user_search])
    team_filter_active = any([team_project_filter, team_search])
    member_filter_active = any([member_project_filter, member_team_filter, member_search])
    archiv_filter_active = any([archiv_project_filter, archiv_team_filter, archiv_search])

    # Users query - search in username, email, team member name, pylon
    users_query = User.query
    if not user_filter_active:
        users_query = users_query.filter(false())
    else:
        if user_project_filter:
            users_query = users_query.filter(User.project_id == user_project_filter)
        if user_role_filter:
            users_query = users_query.join(User.role).filter(Role.name == user_role_filter)
        if user_search:
            users_query = users_query.outerjoin(User.team_members).filter(
                or_(
                    User.username.ilike(f'%{user_search}%'),
                    User.email.ilike(f'%{user_search}%'),
                    TeamMember.name.ilike(f'%{user_search}%'),
                    TeamMember.pylon.ilike(f'%{user_search}%')
                )
            )
    users_paginated = users_query.order_by(User.username).paginate(page=page_users, per_page=20, error_out=False)

    # Teams query
    teams_query = Team.query.filter(Team.name != ARCHIV_TEAM_NAME)
    if not team_filter_active:
        teams_query = teams_query.filter(false())
    else:
        if team_project_filter:
            teams_query = teams_query.filter(Team.project_id == team_project_filter)
        if team_search:
            teams_query = teams_query.filter(Team.name.ilike(f'%{team_search}%'))
    teams_paginated = teams_query.order_by(Team.name).paginate(page=page_teams, per_page=20, error_out=False)

    # Members query (not used in UI but kept for compatibility)
    members_query = TeamMember.query.join(Team, TeamMember.team_id == Team.id).filter(Team.name != ARCHIV_TEAM_NAME)
    if not member_filter_active:
        members_query = members_query.filter(false())
    else:
        if member_project_filter:
            members_query = members_query.filter(Team.project_id == member_project_filter)
        if member_team_filter:
            members_query = members_query.filter(TeamMember.team_id == member_team_filter)
        if member_search:
            members_query = members_query.filter(TeamMember.name.ilike(f'%{member_search}%'))
    members_paginated = members_query.order_by(TeamMember.name).paginate(page=page_members, per_page=20, error_out=False)

    archiv_team = get_or_create_archiv_team()
    archiv_query = TeamMember.query.filter_by(team_id=archiv_team.id)
    if not archiv_filter_active:
        archiv_query = archiv_query.filter(false())
    else:
        if archiv_project_filter:
            archiv_query = archiv_query.filter(TeamMember.original_project_id == archiv_project_filter)
        if archiv_team_filter:
            archiv_query = archiv_query.filter(TeamMember.original_team_id == archiv_team_filter)
        if archiv_search:
            archiv_query = archiv_query.filter(TeamMember.name.ilike(f'%{archiv_search}%'))
    archiv_paginated = archiv_query.order_by(TeamMember.name).paginate(page=page_archiv, per_page=20, error_out=False)

    all_projects = Project.query.order_by(Project.name).all()
    all_teams = Team.query.filter(Team.name != ARCHIV_TEAM_NAME).order_by(Team.name).all()
    all_roles = [role.name for role in Role.query.order_by(Role.name).all()]

    return render_template('admin/admin_panel.html', title='Admin Panel',
                           users_paginated=users_paginated,
                           teams_paginated=teams_paginated,
                           members_paginated=members_paginated,
                           archiv_paginated=archiv_paginated,
                           all_projects=all_projects,
                           all_teams=all_teams,
                           all_roles=all_roles,
                           filter_params={
                               'user_project': user_project_filter,
                               'user_role': user_role_filter,
                               'user_search': user_search,
                               'team_project': team_project_filter,
                               'team_search': team_search,
                               'member_project': member_project_filter,
                               'member_team': member_team_filter,
                               'member_search': member_search,
                               'archiv_project': archiv_project_filter,
                               'archiv_team': archiv_team_filter,
                               'archiv_search': archiv_search
                           },
                           user_filter_active=user_filter_active,
                           team_filter_active=team_filter_active,
                           member_filter_active=member_filter_active,
                           archiv_filter_active=archiv_filter_active,
                           config=current_app.config)


# --- Project Management ---
@bp.route('/projects')
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def manage_projects():
    projects = Project.query.options(joinedload(Project.abteilung)).order_by(Project.name).all()
    return render_template('admin/manage_projects.html', projects=projects)


@bp.route('/projects/teams-coaching', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def manage_teams_coaching():
    """Bulk toggle: teams excluded here cannot be used for new coachings or workshops."""
    form = TeamsCoachingBulkForm()
    if form.validate_on_submit():
        active_ids = {int(x) for x in request.form.getlist('active_team') if str(x).isdigit()}
        assignment_visible_ids = {
            int(x) for x in request.form.getlist('assignment_visible_team') if str(x).isdigit()
        }
        teams = Team.query.filter(Team.name != ARCHIV_TEAM_NAME).all()
        for team in teams:
            is_active = team.id in active_ids
            team.active_for_coaching = is_active
            if is_active:
                team.visible_for_coaching_assignment = False
            else:
                team.visible_for_coaching_assignment = team.id in assignment_visible_ids
        db.session.commit()
        flash('Team-Sichtbarkeit für Coaching, Workshops und Zuweisungen gespeichert.', 'success')
        return redirect(url_for('admin.manage_teams_coaching'))

    projects = Project.query.order_by(Project.name).all()
    teams_by_project = {}
    for p in projects:
        teams_by_project[p.id] = (
            Team.query.filter_by(project_id=p.id)
            .filter(Team.name != ARCHIV_TEAM_NAME)
            .order_by(Team.name)
            .all()
        )
    return render_template(
        'admin/manage_teams_coaching.html',
        title='Teams & aktives Coaching',
        form=form,
        projects=projects,
        teams_by_project=teams_by_project,
        archiv_name=ARCHIV_TEAM_NAME,
        config=current_app.config,
    )


@bp.route('/abteilungen')
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def manage_abteilungen():
    abteilungen = Abteilung.query.options(joinedload(Abteilung.projects)).order_by(Abteilung.name).all()
    return render_template(
        'admin/manage_abteilungen.html',
        title='Abteilungen',
        abteilungen=abteilungen,
        config=current_app.config,
    )


@bp.route('/abteilungen/create', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def create_abteilung():
    form = AbteilungForm()
    if form.validate_on_submit():
        a = Abteilung(name=form.name.data.strip(), description=(form.description.data or '').strip() or None)
        db.session.add(a)
        db.session.flush()
        _sync_abteilung_projects(a.id, form.project_ids.data)
        db.session.commit()
        flash('Abteilung gespeichert.', 'success')
        return redirect(url_for('admin.manage_abteilungen'))
    return render_template('admin/create_abteilung.html', title='Abteilung anlegen', form=form, config=current_app.config)


@bp.route('/abteilungen/<int:abteilung_id>/edit', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def edit_abteilung(abteilung_id):
    abt = Abteilung.query.get_or_404(abteilung_id)
    form = AbteilungForm()
    if form.validate_on_submit():
        abt.name = form.name.data.strip()
        abt.description = (form.description.data or '').strip() or None
        _sync_abteilung_projects(abt.id, form.project_ids.data)
        db.session.commit()
        flash('Abteilung aktualisiert.', 'success')
        return redirect(url_for('admin.manage_abteilungen'))
    if request.method == 'GET':
        form.name.data = abt.name
        form.description.data = abt.description
        form.project_ids.data = [p.id for p in abt.projects]
    return render_template('admin/edit_abteilung.html', title='Abteilung bearbeiten', form=form, abteilung=abt, config=current_app.config)


@bp.route('/abteilungen/<int:abteilung_id>/delete', methods=['POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def delete_abteilung(abteilung_id):
    abt = Abteilung.query.get_or_404(abteilung_id)
    User.query.filter_by(abteilung_id=abteilung_id).update({User.abteilung_id: None}, synchronize_session='fetch')
    Project.query.filter_by(abteilung_id=abteilung_id).update({Project.abteilung_id: None}, synchronize_session='fetch')
    db.session.delete(abt)
    db.session.commit()
    flash('Abteilung gelöscht. Benutzer- und Projekt-Verknüpfungen wurden entfernt.', 'success')
    return redirect(url_for('admin.manage_abteilungen'))


@bp.route('/projects/create', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def create_project():
    form = ProjectForm()
    if form.validate_on_submit():
        pk = _abteilung_pk_from_form(form)
        project = Project(name=form.name.data, description=form.description.data, abteilung_id=pk)
        db.session.add(project)
        db.session.commit()
        flash('Projekt erfolgreich erstellt.', 'success')
        return redirect(url_for('admin.manage_projects'))
    return render_template('admin/create_project.html', form=form)


@bp.route('/projects/edit/<int:project_id>', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def edit_project(project_id):
    project = Project.query.get_or_404(project_id)
    form = ProjectForm(obj=project)
    if request.method == 'GET':
        form.abteilung_id.data = project.abteilung_id or 0
    if form.validate_on_submit():
        project.name = form.name.data
        project.description = form.description.data
        project.abteilung_id = _abteilung_pk_from_form(form)

        db.session.commit()
        flash('Projekt aktualisiert.', 'success')
        return redirect(url_for('admin.manage_projects'))

    return render_template(
        'admin/edit_project.html',
        form=form,
        project=project,
    )


@bp.route('/projects/delete/<int:project_id>', methods=['POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def delete_project(project_id):
    project = Project.query.get_or_404(project_id)
    if project.users.count() > 0 or project.teams.count() > 0 or project.workshops.count() > 0 or project.coachings.count() > 0:
        flash('Projekt kann nicht gelöscht werden, da noch Benutzer, Teams, Workshops oder Coachings zugeordnet sind.', 'danger')
        return redirect(url_for('admin.manage_projects'))
    db.session.delete(project)
    db.session.commit()
    flash('Projekt gelöscht.', 'success')
    return redirect(url_for('admin.manage_projects'))


# --- User Management ---
@bp.route('/users/create', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def create_user():
    form = RegistrationForm()
    if form.validate_on_submit():
        try:
            role = Role.query.get(form.role_id.data)
            if not role:
                flash('Ungültige Rolle.', 'danger')
                return render_template(
                    'admin/create_user.html',
                    title='Benutzer erstellen',
                    form=form,
                    role_ids_multiple_teams=_role_ids_with_multiple_teams(),
                    role_ids_view_abteilung=_role_ids_with_view_abteilung(),
                    config=current_app.config,
                )

            dept_id = _abteilung_pk_from_form(form) if role.has_permission('view_abteilung') else None

            if role.name == ROLE_ABTEILUNGSLEITER:
                if dept_id:
                    plist = projects_in_abteilung(dept_id)
                    if not plist:
                        flash('Die gewählte Abteilung enthält keine Projekte. Ordnen Sie der Abteilung zuerst Projekte zu.', 'danger')
                        return render_template(
                            'admin/create_user.html',
                            title='Benutzer erstellen',
                            form=form,
                            role_ids_multiple_teams=_role_ids_with_multiple_teams(),
                            role_ids_view_abteilung=_role_ids_with_view_abteilung(),
                            config=current_app.config,
                        )
                    primary_project_id = plist[0].id
                    user = User(
                        username=form.username.data,
                        email=form.email.data if form.email.data else None,
                        role_id=form.role_id.data,
                        project_id=primary_project_id,
                        abteilung_id=dept_id,
                    )
                else:
                    primary_project_id = form.project_ids.data[0] if form.project_ids.data else None
                    if primary_project_id is None:
                        flash('Mindestens ein Projekt muss ausgewählt werden.', 'danger')
                        return render_template(
                            'admin/create_user.html',
                            title='Benutzer erstellen',
                            form=form,
                            role_ids_multiple_teams=_role_ids_with_multiple_teams(),
                            role_ids_view_abteilung=_role_ids_with_view_abteilung(),
                            config=current_app.config,
                        )
                    user = User(
                        username=form.username.data,
                        email=form.email.data if form.email.data else None,
                        role_id=form.role_id.data,
                        project_id=primary_project_id,
                        abteilung_id=None,
                    )
            else:
                if dept_id:
                    plist = projects_in_abteilung(dept_id)
                    if not plist:
                        flash('Die gewählte Abteilung enthält keine Projekte. Ordnen Sie der Abteilung zuerst Projekte zu.', 'danger')
                        return render_template(
                            'admin/create_user.html',
                            title='Benutzer erstellen',
                            form=form,
                            role_ids_multiple_teams=_role_ids_with_multiple_teams(),
                            role_ids_view_abteilung=_role_ids_with_view_abteilung(),
                            config=current_app.config,
                        )
                    primary_project_id = plist[0].id
                else:
                    primary_project_id = form.project_id.data
                user = User(
                    username=form.username.data,
                    email=form.email.data if form.email.data else None,
                    role_id=form.role_id.data,
                    project_id=primary_project_id,
                    abteilung_id=dept_id,
                )
            user.set_password(form.password.data)
            db.session.add(user)
            db.session.flush()

            if role.has_permission('assign_teams') and form.team_ids.data:
                user.teams_led = Team.query.filter(Team.id.in_(form.team_ids.data)).all()
            else:
                user.teams_led = []

            if role.name == ROLE_ABTEILUNGSLEITER:
                if dept_id:
                    user.projects = list(projects_in_abteilung(dept_id))
                elif form.project_ids.data:
                    user.projects = Project.query.filter(Project.id.in_(form.project_ids.data)).all()
                else:
                    user.projects = []
            else:
                extra_ids = [i for i in (form.extra_project_ids.data or []) if i and i != user.project_id]
                user.projects = (
                    Project.query.filter(Project.id.in_(extra_ids)).all() if extra_ids else []
                )

            archiv_team = get_or_create_archiv_team()
            full_name = f"{form.first_name.data} {form.last_name.data}".strip()
            if role.has_permission('multiple_teams'):
                team_id_list = form.team_ids_for_member.data or []
            else:
                team_id_list = [form.team_id_for_member.data]
            for tid in team_id_list:
                tm_team = Team.query.get(tid)
                if not tm_team:
                    db.session.rollback()
                    flash('Team für das Mitglied nicht gefunden.', 'danger')
                    return redirect(url_for('admin.create_user'))
                member = TeamMember(
                    name=full_name,
                    team_id=tm_team.id,
                    pylon=form.pylon.data,
                    plt_id=form.plt_id.data,
                    ma_kennung=form.ma_kennung.data,
                    dag_id=form.dag_id.data,
                    user_id=user.id,
                )
                if not form.active.data:
                    member.original_team_id = tm_team.id
                    member.original_project_id = tm_team.project_id
                    member.team_id = archiv_team.id
                db.session.add(member)

            db.session.commit()
            flash('Benutzer und Teammitglied erfolgreich erstellt!', 'success')
            return redirect(url_for('admin.panel'))
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"FEHLER beim Erstellen des Benutzers: {str(e)}")
            flash(f'Fehler beim Erstellen: {str(e)}', 'danger')
    elif request.method == 'POST':
        for field, errors in form.errors.items():
            for error in errors:
                flash(f"Fehler im Feld '{form[field].label.text if hasattr(form[field], 'label') else field}': {error}", 'danger')
    return render_template(
        'admin/create_user.html',
        title='Benutzer erstellen',
        form=form,
        role_ids_multiple_teams=_role_ids_with_multiple_teams(),
        role_ids_view_abteilung=_role_ids_with_view_abteilung(),
        config=current_app.config,
    )


@bp.route('/users/edit/<int:user_id>', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def edit_user(user_id):
    user_to_edit = User.query.get_or_404(user_id)
    archiv_team = get_or_create_archiv_team()
    form = RegistrationForm(
        obj=user_to_edit,
        original_username=user_to_edit.username,
        password_optional=True,
    )

    if request.method == 'GET':
        form.username.data = user_to_edit.username
        form.email.data = user_to_edit.email
        form.role_id.data = user_to_edit.role_id
        form.abteilung_id.data = user_to_edit.abteilung_id or 0
        form.team_ids.data = [team.id for team in user_to_edit.teams_led]
        role_g = user_to_edit.role
        if not role_g or role_g.name != ROLE_ABTEILUNGSLEITER:
            form.project_id.data = user_to_edit.project_id
            form.extra_project_ids.data = [p.id for p in user_to_edit.projects if p.id != user_to_edit.project_id]
        else:
            form.project_ids.data = [p.id for p in user_to_edit.projects]
        members = TeamMember.query.filter_by(user_id=user_to_edit.id).order_by(TeamMember.id).all()
        if members:
            m0 = members[0]
            parts = m0.name.split(' ', 1)
            form.first_name.data = parts[0] if parts else ''
            form.last_name.data = parts[1] if len(parts) > 1 else ''
            form.pylon.data = m0.pylon
            form.plt_id.data = m0.plt_id
            form.ma_kennung.data = m0.ma_kennung
            form.dag_id.data = m0.dag_id
            if role_g and role_g.has_permission('multiple_teams'):
                tids = []
                for m in members:
                    if m.team_id == archiv_team.id and m.original_team_id:
                        tids.append(m.original_team_id)
                    elif m.team_id != archiv_team.id:
                        tids.append(m.team_id)
                form.team_ids_for_member.data = list(dict.fromkeys(tids))
            else:
                m_use = next((m for m in members if m.team_id != archiv_team.id), m0)
                if m_use.team_id == archiv_team.id and m_use.original_team_id:
                    form.team_id_for_member.data = m_use.original_team_id
                else:
                    form.team_id_for_member.data = m_use.team_id
            form.active.data = any(m.team_id != archiv_team.id for m in members)
        else:
            form.first_name.data = ''
            form.last_name.data = ''
            form.active.data = True
            if form.team_id_for_member.choices:
                form.team_id_for_member.data = form.team_id_for_member.choices[0][0]

    if form.validate_on_submit():
        try:
            role = Role.query.get(form.role_id.data)
            if not role:
                flash('Ungültige Rolle.', 'danger')
                return render_template(
                    'admin/edit_user.html',
                    title='Benutzer bearbeiten',
                    form=form,
                    user=user_to_edit,
                    role_ids_multiple_teams=_role_ids_with_multiple_teams(),
                    role_ids_view_abteilung=_role_ids_with_view_abteilung(),
                    config=current_app.config,
                )

            user_to_edit.username = form.username.data
            user_to_edit.email = form.email.data if form.email.data else None
            user_to_edit.role_id = form.role_id.data

            dept_id = _abteilung_pk_from_form(form) if role.has_permission('view_abteilung') else None
            if not role.has_permission('view_abteilung'):
                user_to_edit.abteilung_id = None
            else:
                user_to_edit.abteilung_id = dept_id

            if role.name == ROLE_ABTEILUNGSLEITER:
                if dept_id:
                    plist = projects_in_abteilung(dept_id)
                    if not plist:
                        flash('Die gewählte Abteilung enthält keine Projekte.', 'danger')
                        return render_template(
                            'admin/edit_user.html',
                            title='Benutzer bearbeiten',
                            form=form,
                            user=user_to_edit,
                            role_ids_multiple_teams=_role_ids_with_multiple_teams(),
                            role_ids_view_abteilung=_role_ids_with_view_abteilung(),
                            config=current_app.config,
                        )
                    user_to_edit.project_id = plist[0].id
                    user_to_edit.projects = list(plist)
                else:
                    primary_project_id = form.project_ids.data[0] if form.project_ids.data else None
                    if primary_project_id is None:
                        flash('Mindestens ein Projekt muss ausgewählt werden.', 'danger')
                        return render_template(
                            'admin/edit_user.html',
                            title='Benutzer bearbeiten',
                            form=form,
                            user=user_to_edit,
                            role_ids_multiple_teams=_role_ids_with_multiple_teams(),
                            role_ids_view_abteilung=_role_ids_with_view_abteilung(),
                            config=current_app.config,
                        )
                    user_to_edit.project_id = primary_project_id
                    user_to_edit.projects = Project.query.filter(Project.id.in_(form.project_ids.data)).all()
            else:
                if dept_id:
                    plist = projects_in_abteilung(dept_id)
                    if not plist:
                        flash('Die gewählte Abteilung enthält keine Projekte.', 'danger')
                        return render_template(
                            'admin/edit_user.html',
                            title='Benutzer bearbeiten',
                            form=form,
                            user=user_to_edit,
                            role_ids_multiple_teams=_role_ids_with_multiple_teams(),
                            role_ids_view_abteilung=_role_ids_with_view_abteilung(),
                            config=current_app.config,
                        )
                    user_to_edit.project_id = plist[0].id
                else:
                    user_to_edit.project_id = form.project_id.data
                extra_ids = [i for i in (form.extra_project_ids.data or []) if i and i != user_to_edit.project_id]
                user_to_edit.projects = (
                    Project.query.filter(Project.id.in_(extra_ids)).all() if extra_ids else []
                )

            if form.password.data:
                user_to_edit.set_password(form.password.data)

            if role.has_permission('assign_teams') and form.team_ids.data:
                user_to_edit.teams_led = Team.query.filter(Team.id.in_(form.team_ids.data)).all()
            else:
                user_to_edit.teams_led = []

            if not _sync_user_team_members_from_form(user_to_edit, role, form):
                flash('Teamzuordnung prüfen (mindestens ein Team, wenn aktiv).', 'danger')
                return render_template(
                    'admin/edit_user.html',
                    title='Benutzer bearbeiten',
                    form=form,
                    user=user_to_edit,
                    role_ids_multiple_teams=_role_ids_with_multiple_teams(),
                    role_ids_view_abteilung=_role_ids_with_view_abteilung(),
                    config=current_app.config,
                )

            db.session.commit()
            flash('Benutzer und Teammitglied erfolgreich aktualisiert!', 'success')
            return redirect(url_for('admin.panel'))
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"FEHLER beim Aktualisieren des Benutzers: {str(e)}")
            flash(f'Fehler beim Aktualisieren: {str(e)}', 'danger')

    return render_template(
        'admin/edit_user.html',
        title='Benutzer bearbeiten',
        form=form,
        user=user_to_edit,
        role_ids_multiple_teams=_role_ids_with_multiple_teams(),
        role_ids_view_abteilung=_role_ids_with_view_abteilung(),
        config=current_app.config,
    )


@bp.route('/users/delete/<int:user_id>', methods=['POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.username == 'admin' or user.id == current_user.id:
        flash('Dieser Benutzer kann nicht gelöscht werden.', 'danger')
        return redirect(url_for('admin.panel'))

    try:
        TeamMember.query.filter_by(user_id=user_id).delete()
        user.teams_led = []
        Coaching.query.filter_by(coach_id=user_id).update({"coach_id": None})
        db.session.delete(user)
        db.session.commit()
        flash('Benutzer und zugehöriges Teammitglied gelöscht.', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Fehler beim Löschen von User ID {user_id}: {e}")
        flash(f'Fehler beim Löschen des Benutzers. Es könnten noch verbundene Daten existieren (z.B. Coachings). Details im Log.', 'danger')
    return redirect(url_for('admin.panel'))


# --- Team Management ---
@bp.route('/teams/create', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def create_team():
    form = TeamForm()
    all_team_leaders = User.query.filter(User.role.has(name=ROLE_TEAMLEITER)).order_by(User.username).all()
    if form.validate_on_submit():
        if form.name.data.strip().upper() == ARCHIV_TEAM_NAME:
            flash(f'Der Teamname \\\"{ARCHIV_TEAM_NAME}\\\" ist für das System reserviert.', 'danger')
            return render_template('admin/create_team.html', title='Team erstellen', form=form, config=current_app.config)
        try:
            team = Team(
                name=form.name.data,
                project_id=form.project_id.data,
                active_for_coaching=form.active_for_coaching.data,
            )
            db.session.add(team)
            db.session.flush()

            if form.team_leaders.data:
                leaders = User.query.filter(User.id.in_(form.team_leaders.data), User.role.has(name=ROLE_TEAMLEITER)).all()
                team.leaders = leaders
            else:
                team.leaders = []

            db.session.commit()
            flash('Team erfolgreich erstellt!', 'success')
            return redirect(url_for('admin.panel'))
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Fehler beim Erstellen des Teams: {e}")
            flash(f'Fehler beim Erstellen des Teams: {str(e)}', 'danger')
    return render_template('admin/create_team.html', title='Team erstellen', form=form, all_team_leaders=all_team_leaders, config=current_app.config)


@bp.route('/teams/edit/<int:team_id>', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def edit_team(team_id):
    team_to_edit = Team.query.get_or_404(team_id)
    form = TeamForm(obj=team_to_edit, original_name=team_to_edit.name)

    if team_to_edit.name == ARCHIV_TEAM_NAME and request.method == 'GET':
        flash('Das ARCHIV-Team kann nicht bearbeitet werden.', 'info')
        form.name.render_kw = {'readonly': True}
        form.team_leaders.render_kw = {'disabled': True}
        form.project_id.render_kw = {'disabled': True}

    if form.validate_on_submit():
        if team_to_edit.name == ARCHIV_TEAM_NAME:
            flash('Das ARCHIV-Team kann nicht geändert werden.', 'danger')
            return redirect(url_for('admin.edit_team', team_id=team_id))

        try:
            team_to_edit.name = form.name.data
            team_to_edit.project_id = form.project_id.data
            if team_to_edit.name != ARCHIV_TEAM_NAME:
                team_to_edit.active_for_coaching = form.active_for_coaching.data

            if form.team_leaders.data:
                leaders = User.query.filter(User.id.in_(form.team_leaders.data), User.role.has(name=ROLE_TEAMLEITER)).all()
                team_to_edit.leaders = leaders
            else:
                team_to_edit.leaders = []

            db.session.commit()
            flash('Team erfolgreich aktualisiert!', 'success')
            return redirect(url_for('admin.panel'))
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Fehler beim Bearbeiten des Teams {team_id}: {e}")
            flash(f'Fehler beim Bearbeiten des Teams: {str(e)}', 'danger')

    elif request.method == 'GET':
        form.name.data = team_to_edit.name
        form.team_leaders.data = [leader.id for leader in team_to_edit.leaders]
        form.project_id.data = team_to_edit.project_id
        form.active_for_coaching.data = team_to_edit.active_for_coaching

    return render_template('admin/edit_team.html', title='Team bearbeiten', form=form, team=team_to_edit, config=current_app.config)


@bp.route('/teams/delete/<int:team_id>', methods=['POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def delete_team(team_id):
    team = Team.query.get_or_404(team_id)
    if team.name == ARCHIV_TEAM_NAME:
        flash('Das ARCHIV-Team kann nicht gelöscht werden.', 'danger')
        return redirect(url_for('admin.panel'))
    if len(team.members) > 0:
        flash('Team kann nicht gelöscht werden, da ihm noch Mitglieder zugeordnet sind. Verschieben Sie die Mitglieder zuerst ins Archiv.', 'danger')
        return redirect(url_for('admin.panel'))

    try:
        tid = team.id
        # FK-Verweise lösen, die nicht über team.members laufen (Archiv-Ursprung, Coachings, …)
        TeamMember.query.filter_by(original_team_id=tid).update(
            {TeamMember.original_team_id: None, TeamMember.original_project_id: None},
            synchronize_session=False,
        )
        db.session.execute(
            update(workshop_participants)
            .where(workshop_participants.c.original_team_id == tid)
            .values(original_team_id=None)
        )
        User.query.filter_by(team_id_if_leader=tid).update(
            {User.team_id_if_leader: None},
            synchronize_session=False,
        )
        Coaching.query.filter_by(team_id=tid).update(
            {Coaching.team_id: None},
            synchronize_session=False,
        )
        PlannedCoaching.query.filter_by(team_id=tid).update(
            {PlannedCoaching.team_id: None},
            synchronize_session=False,
        )

        team.leaders = []
        db.session.delete(team)
        db.session.commit()
        flash('Team gelöscht.', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Fehler beim Löschen von Team ID {team_id}: {e}")
        flash('Fehler beim Löschen des Teams.', 'danger')
    return redirect(url_for('admin.panel'))


# --- Team Member Management (kept for compatibility but not used in UI) ---
@bp.route('/teammembers/create', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def create_team_member():
    form = TeamMemberForm()
    projects = Project.query.order_by(Project.name).all()
    all_teams = Team.query.filter(Team.name != ARCHIV_TEAM_NAME).order_by(Team.name).all()
    if request.method == 'GET':
        args = request.args
        if args.get('first_name'):
            form.first_name.data = args.get('first_name', '').strip()
        if args.get('last_name'):
            form.last_name.data = args.get('last_name', '').strip()
        if args.get('ma_kennung'):
            form.ma_kennung.data = args.get('ma_kennung', '').strip()
        if args.get('dag_id'):
            form.dag_id.data = args.get('dag_id', '').strip()
        tid = args.get('team_id', type=int)
        if tid:
            form.team_id.data = tid
        elif args.get('be4'):
            team = Team.query.filter_by(name=args.get('be4', '').strip()).first()
            if team:
                form.team_id.data = team.id
        if args.get('from_prod') == '1':
            flash(
                'Daten aus Produktivitäts-Import übernommen. Bitte prüfen und Pylon-Nr ergänzen.',
                'info',
            )
    if form.validate_on_submit():
        try:
            team = Team.query.get(form.team_id.data)
            if not team:
                flash('Team nicht gefunden.', 'danger')
                return redirect(url_for('admin.create_team_member'))
            
            full_name = f"{form.first_name.data} {form.last_name.data}".strip()
            member = TeamMember(
                name=full_name,
                team_id=form.team_id.data,
                pylon=form.pylon.data,
                plt_id=form.plt_id.data,
                ma_kennung=form.ma_kennung.data,
                dag_id=form.dag_id.data
            )
            db.session.add(member)
            db.session.flush()
            
            if not form.active.data:
                archiv_team = get_or_create_archiv_team()
                member.original_team_id = member.team_id
                member.original_project_id = member.team.project_id
                member.team_id = archiv_team.id
            
            db.session.commit()
            flash('Teammitglied erfolgreich erstellt!', 'success')
            return redirect(url_for('admin.panel'))
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Fehler beim Erstellen des Teammitglieds: {e}")
            flash(f'Fehler beim Erstellen des Teammitglieds: {str(e)}', 'danger')
    return render_template('admin/create_team_member.html', title='Teammitglied erstellen',
                           form=form, projects=projects, all_teams=all_teams, config=current_app.config)


@bp.route('/teammembers/edit/<int:member_id>', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def edit_team_member(member_id):
    member = TeamMember.query.get_or_404(member_id)
    form = TeamMemberForm(obj=member)
    projects = Project.query.order_by(Project.name).all()
    all_teams = Team.query.filter(Team.name != ARCHIV_TEAM_NAME).order_by(Team.name).all()
    
    if request.method == 'GET':
        parts = member.name.split(' ', 1)
        form.first_name.data = parts[0] if parts else ''
        form.last_name.data = parts[1] if len(parts) > 1 else ''
    
    archiv_team = get_or_create_archiv_team()
    is_active = member.team_id != archiv_team.id
    if request.method == 'GET':
        form.active.data = is_active
    
    if form.validate_on_submit():
        try:
            full_name = f"{form.first_name.data} {form.last_name.data}".strip()
            member.name = full_name
            member.pylon = form.pylon.data
            member.plt_id = form.plt_id.data
            member.ma_kennung = form.ma_kennung.data
            member.dag_id = form.dag_id.data

            sel_team = Team.query.get(form.team_id.data)
            if not sel_team:
                flash('Team nicht gefunden.', 'danger')
                return redirect(url_for('admin.edit_team_member', member_id=member_id))
            if sel_team.id == archiv_team.id or sel_team.name == ARCHIV_TEAM_NAME:
                flash('Bitte ein Projektteam wählen (nicht ARCHIV).', 'danger')
                return redirect(url_for('admin.edit_team_member', member_id=member_id))

            target_is_live = sel_team.id != archiv_team.id and sel_team.name != ARCHIV_TEAM_NAME
            # Teamwechsel zwischen zwei Live-Teams: immer auf Zielteam setzen (verhindert Archiv bei
            # vergessenem „Aktiv“-Häkchen oder alter original_team_id-Logik).
            moving_between_live_teams = (
                is_active
                and target_is_live
                and form.team_id.data != member.team_id
            )

            if form.active.data or moving_between_live_teams:
                member.team_id = form.team_id.data
                member.original_team_id = None
                member.original_project_id = None
            else:
                # Inaktiv: Archiv; Wiederherstellungsziel immer aus Dropdown (nicht alter member.team_id
                # bei gleichzeitigem Teamwechsel SCOZTH_32 → SCOZTH_31).
                member.original_team_id = sel_team.id
                member.original_project_id = sel_team.project_id
                member.team_id = archiv_team.id

            db.session.commit()
            flash('Teammitglied erfolgreich aktualisiert!', 'success')
            if member.team_id == archiv_team.id:
                target_team_id = member.original_team_id or form.team_id.data
            else:
                target_team_id = member.team_id
            return redirect(url_for('admin.edit_team', team_id=target_team_id))
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Fehler beim Bearbeiten des Teammitglieds {member_id}: {e}")
            flash(f'Fehler beim Bearbeiten des Teammitglieds: {str(e)}', 'danger')
    elif request.method == 'GET':
        if is_active:
            form.team_id.data = member.team_id
        else:
            if member.original_team_id:
                form.team_id.data = member.original_team_id
            else:
                form.team_id.data = all_teams[0].id if all_teams else None
    
    return render_template('admin/edit_team_member.html', title='Teammitglied bearbeiten',
                           form=form, member=member, projects=projects, all_teams=all_teams, config=current_app.config)


@bp.route('/teammembers/<int:member_id>/move-to-archiv', methods=['POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def move_to_archiv(member_id):
    member_to_move = TeamMember.query.get_or_404(member_id)
    original_team_id = member_to_move.team_id
    original_team_name = member_to_move.team.name
    archiv_team = get_or_create_archiv_team()
    if member_to_move.team_id == archiv_team.id:
        flash(f'{member_to_move.name} ist bereits im Archiv.', 'info')
        return redirect(url_for('admin.panel'))
    try:
        member_to_move.original_team_id = member_to_move.team_id
        member_to_move.original_project_id = member_to_move.team.project_id
        member_to_move.team_id = archiv_team.id
        db.session.commit()
        flash(f'Mitglied \\\"{member_to_move.name}\\\" wurde von Team \\\"{original_team_name}\\\" ins ARCHIV verschoben.', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Fehler beim Verschieben von Mitglied {member_id} ins Archiv: {e}")
        flash('Fehler beim Verschieben des Mitglieds ins Archiv.', 'danger')
    return redirect(url_for('admin.edit_team', team_id=original_team_id))


@bp.route('/teammembers/delete-permanent/<int:member_id>', methods=['POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def delete_team_member_permanently(member_id):
    member = TeamMember.query.get_or_404(member_id)
    member_name = member.name
    
    try:
        Coaching.query.filter_by(team_member_id=member_id).delete()
        db.session.execute(workshop_participants.delete().where(workshop_participants.c.team_member_id == member_id))
        db.session.delete(member)
        db.session.commit()
        flash(f'Mitglied \\\"{member_name}\\\" wurde endgültig gelöscht.', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Fehler beim endgültigen Löschen von Mitglied {member_id}: {e}")
        flash(f'Fehler beim Löschen: {str(e)}', 'danger')
    
    return redirect(url_for('admin.panel'))


# --- Coaching Management (Admin) ---
@bp.route('/manage_coachings', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def manage_coachings():
    page = request.args.get('page', 1, type=int)
    period_filter_arg = request.args.get('period', 'all')
    team_filter_arg = request.args.get('team', 'all')
    team_member_filter_arg = request.args.get('teammember', 'all')
    coach_filter_arg = request.args.get('coach', 'all')
    search_term = request.args.get('search', default="", type=str).strip()
    project_filter = request.args.get('project', type=int) or session.get('active_project')

    coachings_query = Coaching.query \
        .join(TeamMember, Coaching.team_member_id == TeamMember.id) \
        .join(User, Coaching.coach_id == User.id, isouter=True) \
        .join(Team, TeamMember.team_id == Team.id)

    if team_filter_arg == 'all':
        coachings_query = coachings_query.filter(Team.name != ARCHIV_TEAM_NAME)

    if project_filter:
        coachings_query = coachings_query.filter(Coaching.project_id == project_filter)

    start_date, end_date = calculate_date_range(period_filter_arg)
    if start_date:
        coachings_query = coachings_query.filter(Coaching.coaching_date >= start_date)
    if end_date:
        coachings_query = coachings_query.filter(Coaching.coaching_date <= end_date)

    if team_filter_arg and team_filter_arg.isdigit():
        coachings_query = coachings_query.filter(TeamMember.team_id == int(team_filter_arg))
    if team_member_filter_arg and team_member_filter_arg.isdigit():
        coachings_query = coachings_query.filter(Coaching.team_member_id == int(team_member_filter_arg))
    if coach_filter_arg and coach_filter_arg.isdigit():
        coachings_query = coachings_query.filter(Coaching.coach_id == int(coach_filter_arg))

    if search_term:
        search_pattern = f"%{search_term}%"
        coachings_query = coachings_query.filter(
            or_(
                TeamMember.name.ilike(search_pattern),
                User.username.ilike(search_pattern),
                Team.name.ilike(search_pattern),
                Coaching.coaching_subject.ilike(search_pattern),
                Coaching.coaching_style.ilike(search_pattern),
                Coaching.tcap_id.ilike(search_pattern),
                Coaching.coach_notes.ilike(search_pattern),
            )
        )

    if request.method == 'POST':
        if 'delete_selected' in request.form:
            coaching_ids_to_delete = request.form.getlist('coaching_ids')
            if coaching_ids_to_delete:
                try:
                    deleted_count = _admin_delete_coachings_by_ids(coaching_ids_to_delete)
                    flash(f'{deleted_count} Coaching(s) erfolgreich gelöscht.', 'success')
                except ValueError:
                    flash('Ungültige Coaching-IDs zum Löschen ausgewählt.', 'danger')
                except Exception as e:
                    db.session.rollback()
                    current_app.logger.error(f"Fehler beim Löschen von Coachings: {e}")
                    flash(f'Fehler beim Löschen der Coachings: {str(e)}', 'danger')
                return redirect(url_for('admin.manage_coachings', page=page, period=period_filter_arg, team=team_filter_arg, teammember=team_member_filter_arg, coach=coach_filter_arg, search=search_term))
            else:
                flash('Keine Coachings zum Löschen ausgewählt.', 'info')

    coachings_paginated = coachings_query.order_by(desc(Coaching.coaching_date))\
        .paginate(page=page, per_page=15, error_out=False)

    all_teams = Team.query.order_by(Team.name).all()
    all_team_members = TeamMember.query.order_by(TeamMember.name).all()
    all_coaches = User.query.filter(User.coachings_done.any()).distinct().order_by(User.username).all()
    all_projects = Project.query.order_by(Project.name).all()

    now_dt = datetime.now(timezone.utc)
    current_year_val = now_dt.year
    previous_year_val = current_year_val - 1
    month_options_for_filter = []
    for m_num in range(12, 0, -1):
        month_options_for_filter.append({'value': f"{previous_year_val}-{m_num:02d}", 'text': f"{get_month_name_german(m_num)} {previous_year_val}"})
    for m_num in range(now_dt.month, 0, -1):
        month_options_for_filter.append({'value': f"{current_year_val}-{m_num:02d}", 'text': f"{get_month_name_german(m_num)} {current_year_val}"})

    return render_template('admin/manage_coachings.html',
                           title='Coachings Verwalten',
                           coachings_paginated=coachings_paginated,
                           all_teams=all_teams,
                           all_team_members=all_team_members,
                           all_coaches=all_coaches,
                           all_projects=all_projects,
                           month_options=month_options_for_filter,
                           current_period_filter=period_filter_arg,
                           current_team_id_filter=team_filter_arg,
                           current_teammember_id_filter=team_member_filter_arg,
                           current_coach_id_filter=coach_filter_arg,
                           current_search_term=search_term,
                           current_project_filter=project_filter,
                           config=current_app.config,
                           ARCHIV_TEAM_NAME=ARCHIV_TEAM_NAME)


@bp.route('/coaching/<int:coaching_id>/edit', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def edit_coaching_entry(coaching_id):
    coaching_to_edit = Coaching.query.get_or_404(coaching_id)
    form = CoachingForm(obj=coaching_to_edit, current_user_role=current_user.role_name, current_user_team_ids=[])
    form.update_team_member_choices(exclude_archiv=False, project_id=coaching_to_edit.project_id)
    form.apply_bogen(coaching_to_edit.project_id, coaching=coaching_to_edit)
    leitfaden_items = leitfaden_items_for_coaching_edit(coaching_to_edit)
    selected_leitfaden_values = {}
    if leitfaden_items:
        try:
            selected_leitfaden_values = {response.item_id: response.value for response in coaching_to_edit.leitfaden_responses}
        except SQLAlchemyError:
            db.session.rollback()
            selected_leitfaden_values = {}

    if form.validate_on_submit():
        try:
            form.populate_obj(coaching_to_edit)
            if leitfaden_items:
                CoachingLeitfadenResponse.query.filter_by(coaching_id=coaching_to_edit.id).delete()
                for item in leitfaden_items:
                    selected_value = request.form.get(f'leitfaden_item_{item.id}', 'k.A.')
                    value = selected_value if selected_value in LEITFADEN_CHOICES else 'k.A.'
                    db.session.add(CoachingLeitfadenResponse(
                        coaching_id=coaching_to_edit.id,
                        item_id=item.id,
                        value=value
                    ))
            db.session.commit()
            flash(f'Coaching ID {coaching_id} erfolgreich aktualisiert!', 'success')
            return redirect(url_for('admin.manage_coachings'))
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error updating coaching ID {coaching_id}: {e}")
            flash(f'Fehler beim Aktualisieren von Coaching ID {coaching_id}.', 'danger')
    elif request.method == 'GET':
        form.team_member_id.data = coaching_to_edit.team_member_id

    bogen_layout = bogen_layout_for_project(coaching_to_edit.project_id)

    tcap_js_for_edit = """document.addEventListener('DOMContentLoaded', function() {
    var styleSelect = document.getElementById('coaching_style');
    var tcapField = document.getElementById('tcap_id_field');
    var tcapInput = document.getElementById('tcap_id');
    function toggleTcapField() {
        if (styleSelect && tcapField && tcapInput) {
            if (styleSelect.value === 'TCAP') {
                tcapField.style.display = '';
                tcapInput.required = true;
            } else {
                tcapField.style.display = 'none';
                tcapInput.required = false;
            }
        }
    }
    if (styleSelect && tcapField && tcapInput) {
        styleSelect.addEventListener('change', toggleTcapField);
        toggleTcapField();
    }
});"""
    return render_template('main/add_coaching.html',
                            title=f'Coaching ID {coaching_id} bearbeiten',
                            form=form,
                            is_edit_mode=True,
                            coaching=coaching_to_edit,
                            leitfaden_items=leitfaden_items,
                            selected_leitfaden_values=selected_leitfaden_values,
                            bogen_layout=bogen_layout,
                            tcap_js=tcap_js_for_edit,
                            config=current_app.config,
                            initial_fulfill_planned_id=None)


@bp.route('/coaching/<int:coaching_id>/delete', methods=['POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def delete_coaching_entry(coaching_id):
    if not Coaching.query.get(coaching_id):
        abort(404)
    try:
        _admin_delete_coachings_by_ids([coaching_id])
        flash(f'Coaching ID {coaching_id} erfolgreich gelöscht.', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Fehler beim Löschen von Coaching ID {coaching_id}: {e}")
        flash(f'Fehler beim Löschen von Coaching ID {coaching_id}.', 'danger')
    return redirect(url_for('admin.manage_coachings'))


@bp.route('/api/coachings/delete-precheck')
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def coaching_delete_precheck():
    """JSON: wie viele geplante Coachings hängen an den IDs (für Lösch-Bestätigung)."""
    raw = (request.args.get('ids') or '').strip()
    id_strs = [x.strip() for x in raw.split(',') if x.strip()]
    pre = _precheck_coaching_planned_links(id_strs)
    return jsonify(
        {
            'fulfilled': pre['fulfilled'],
            'source': pre['source'],
            'has_links': pre['has_links'],
        }
    )


# --- Workshop Management (Admin) ---
@bp.route('/manage_workshops', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def manage_workshops():
    page = request.args.get('page', 1, type=int)
    period_filter_arg = request.args.get('period', 'all')
    search_term = request.args.get('search', default="", type=str).strip()
    project_filter = request.args.get('project', type=int) or session.get('active_project')

    workshops_query = Workshop.query
    if project_filter:
        workshops_query = workshops_query.filter(Workshop.project_id == project_filter)

    start_date, end_date = calculate_date_range(period_filter_arg)
    if start_date:
        workshops_query = workshops_query.filter(Workshop.workshop_date >= start_date)
    if end_date:
        workshops_query = workshops_query.filter(Workshop.workshop_date <= end_date)

    if search_term:
        search_pattern = f"%{search_term}%"
        workshops_query = workshops_query.filter(
            or_(
                Workshop.title.ilike(search_pattern),
                Workshop.notes.ilike(search_pattern),
                User.username.ilike(search_pattern)
            )
        ).join(User, Workshop.coach_id == User.id)

    if request.method == 'POST':
        if 'delete_selected' in request.form:
            workshop_ids_to_delete = request.form.getlist('workshop_ids')
            if workshop_ids_to_delete:
                try:
                    workshop_ids_to_delete_int = [int(id_str) for id_str in workshop_ids_to_delete]
                    _unlink_planned_workshops_before_delete(workshop_ids_to_delete_int)
                    db.session.execute(workshop_participants.delete().where(workshop_participants.c.workshop_id.in_(workshop_ids_to_delete_int)))
                    deleted_count = Workshop.query.filter(Workshop.id.in_(workshop_ids_to_delete_int)).delete(synchronize_session='fetch')
                    db.session.commit()
                    flash(f'{deleted_count} Workshop(s) erfolgreich gelöscht.', 'success')
                except ValueError:
                    flash('Ungültige Workshop-IDs zum Löschen ausgewählt.', 'danger')
                except Exception as e:
                    db.session.rollback()
                    current_app.logger.error(f"Fehler beim Löschen von Workshops: {e}")
                    flash(f'Fehler beim Löschen der Workshops: {str(e)}', 'danger')
                return redirect(url_for('admin.manage_workshops', page=page, period=period_filter_arg, search=search_term))
            else:
                flash('Keine Workshops zum Löschen ausgewählt.', 'info')

    workshops_paginated = workshops_query.order_by(desc(Workshop.workshop_date))\
        .paginate(page=page, per_page=15, error_out=False)

    now_dt = datetime.now(timezone.utc)
    current_year_val = now_dt.year
    previous_year_val = current_year_val - 1
    month_options_for_filter = []
    for m_num in range(12, 0, -1):
        month_options_for_filter.append({'value': f"{previous_year_val}-{m_num:02d}", 'text': f"{get_month_name_german(m_num)} {previous_year_val}"})
    for m_num in range(now_dt.month, 0, -1):
        month_options_for_filter.append({'value': f"{current_year_val}-{m_num:02d}", 'text': f"{get_month_name_german(m_num)} {current_year_val}"})

    all_projects = Project.query.order_by(Project.name).all()

    return render_template('admin/manage_workshops.html',
                           title='Workshops Verwalten',
                           workshops_paginated=workshops_paginated,
                           month_options=month_options_for_filter,
                           current_period_filter=period_filter_arg,
                           current_search_term=search_term,
                           current_project_filter=project_filter,
                           all_projects=all_projects,
                           config=current_app.config,
                           workshop_participants=workshop_participants,
                           db=db)


@bp.route('/workshop/<int:workshop_id>/edit', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def edit_workshop_entry(workshop_id):
    workshop_to_edit = Workshop.query.get_or_404(workshop_id)
    existing_participant_ids = [p.id for p in workshop_to_edit.participants]
    form = WorkshopForm(obj=workshop_to_edit, current_user_role=current_user.role_name, current_user_team_ids=[])
    form.update_participant_choices(
        project_id=workshop_to_edit.project_id,
        include_member_ids=existing_participant_ids,
    )
    form.team_member_ids.data = existing_participant_ids

    if form.validate_on_submit():
        try:
            workshop_to_edit.title = form.title.data
            workshop_to_edit.overall_rating = form.overall_rating.data
            workshop_to_edit.time_spent = form.time_spent.data
            workshop_to_edit.notes = form.notes.data

            workshop_to_edit.participants = []
            db.session.flush()

            for member_id in form.team_member_ids.data:
                individual_rating = workshop_individual_rating_from_request(member_id)
                member = TeamMember.query.get(member_id)
                original_team_id = member.team_id if member else None

                stmt = workshop_participants.insert().values(
                    workshop_id=workshop_to_edit.id,
                    team_member_id=member_id,
                    individual_rating=individual_rating,
                    original_team_id=original_team_id
                )
                db.session.execute(stmt)

            db.session.commit()
            flash(f'Workshop ID {workshop_id} erfolgreich aktualisiert!', 'success')
            return redirect(url_for('admin.manage_workshops'))
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error updating workshop ID {workshop_id}: {e}")
            flash(f'Fehler beim Aktualisieren von Workshop ID {workshop_id}.', 'danger')
    elif request.method == 'GET':
        pass

    existing_ratings = {}
    for participant in workshop_to_edit.participants:
        rating = db.session.query(workshop_participants.c.individual_rating).filter_by(
            workshop_id=workshop_id, team_member_id=participant.id).scalar()
        existing_ratings[participant.id] = rating

    return render_template('main/add_workshop.html',
                           title=f'Workshop ID {workshop_id} bearbeiten',
                           form=form,
                           is_edit_mode=True,
                           workshop=workshop_to_edit,
                           existing_ratings=existing_ratings,
                           config=current_app.config)


@bp.route('/workshop/<int:workshop_id>/delete', methods=['POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def delete_workshop_entry(workshop_id):
    workshop = Workshop.query.get_or_404(workshop_id)
    try:
        _unlink_planned_workshops_before_delete([workshop_id])
        db.session.delete(workshop)
        db.session.commit()
        flash(f'Workshop ID {workshop_id} erfolgreich gelöscht.', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Fehler beim Löschen von Workshop ID {workshop_id}: {e}")
        flash(f'Fehler beim Löschen von Workshop ID {workshop_id}.', 'danger')
    return redirect(url_for('admin.manage_workshops'))


# --- Role Management ---
@bp.route('/roles')
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def manage_roles():
    roles = Role.query.order_by(Role.name).all()
    return render_template('admin/manage_roles.html', roles=roles, config=current_app.config)


@bp.route('/roles/create', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def create_role():
    form = RoleForm()
    if form.validate_on_submit():
        role = Role(name=form.name.data, description=form.description.data)
        db.session.add(role)
        db.session.flush()
        if form.permissions.data:
            perms = Permission.query.filter(Permission.id.in_(form.permissions.data)).all()
            role.permissions = perms
        if form.projects.data:
            projs = Project.query.filter(Project.id.in_(form.projects.data)).all()
            role.projects = projs
        db.session.commit()
        flash('Rolle erfolgreich erstellt.', 'success')
        return redirect(url_for('admin.manage_roles'))
    return render_template('admin/create_role.html', form=form, config=current_app.config)


@bp.route('/roles/edit/<int:role_id>', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def edit_role(role_id):
    role = Role.query.get_or_404(role_id)
    form = RoleForm(obj=role)
    if form.validate_on_submit():
        role.name = form.name.data
        role.description = form.description.data
        role.permissions = []
        if form.permissions.data:
            perms = Permission.query.filter(Permission.id.in_(form.permissions.data)).all()
            role.permissions = perms
        role.projects = []
        if form.projects.data:
            projs = Project.query.filter(Project.id.in_(form.projects.data)).all()
            role.projects = projs
        db.session.commit()
        flash('Rolle aktualisiert.', 'success')
        return redirect(url_for('admin.manage_roles'))
    form.permissions.data = [p.id for p in role.permissions]
    form.projects.data = [p.id for p in role.projects]
    return render_template('admin/edit_role.html', form=form, role=role, config=current_app.config)


@bp.route('/roles/delete/<int:role_id>', methods=['POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def delete_role(role_id):
    role = Role.query.get_or_404(role_id)
    if role.name in ['Admin', 'Betriebsleiter']:
        flash('Diese Rolle kann nicht gelöscht werden.', 'danger')
        return redirect(url_for('admin.manage_roles'))
    if role.users.count() > 0:
        flash('Rolle kann nicht gelöscht werden, da sie noch Benutzern zugewiesen ist.', 'danger')
        return redirect(url_for('admin.manage_roles'))
    db.session.delete(role)
    db.session.commit()
    flash('Rolle gelöscht.', 'success')
    return redirect(url_for('admin.manage_roles'))


# --- Leitfaden Management ---
def _parse_raw_leitfaden_project(raw, *, abort_on_invalid=False):
    """None / leer / 0 = Standard (global). Bei ungültigem Wert: Flash; (None, True) nur wenn abort_on_invalid."""
    if raw is None or str(raw).strip() in ('', '0'):
        return None, False
    try:
        v = int(raw)
    except (TypeError, ValueError):
        flash('Ungültiges Projekt.', 'warning')
        return None, abort_on_invalid
    if v <= 0:
        flash('Ungültiges Projekt.', 'warning')
        return None, abort_on_invalid
    if not Project.query.get(v):
        flash('Projekt nicht gefunden.', 'warning')
        return None, abort_on_invalid
    return v, False


def _parse_leitfaden_project_param(*, abort_on_invalid=False):
    return _parse_raw_leitfaden_project(request.args.get('project'), abort_on_invalid=abort_on_invalid)


def _leitfaden_create_scope_from_request():
    """GET: ?project= ; POST: hidden field project (damit der Kontext beim Speichern erhalten bleibt)."""
    raw = request.form.get('project') if request.method == 'POST' else request.args.get('project')
    return _parse_raw_leitfaden_project(raw, abort_on_invalid=True)


def _redirect_manage_leitfaden(project_id=None):
    kw = {'tab': 'leitfaden'}
    if project_id:
        kw['project'] = project_id
    return redirect(url_for('admin.manage_coaching_bogen', **kw))


def _redirect_manage_themen(project_id=None):
    kw = {'tab': 'themen'}
    if project_id:
        kw['project'] = project_id
    return redirect(url_for('admin.manage_coaching_bogen', **kw))


def _coaching_bogen_url(tab, project_id=None):
    kw = {'tab': tab}
    if project_id:
        kw['project'] = project_id
    return url_for('admin.manage_coaching_bogen', **kw)


def get_or_create_bogen_layout(scope_project_id):
    if scope_project_id is not None:
        q = CoachingBogenLayout.query.filter_by(project_id=scope_project_id)
    else:
        q = CoachingBogenLayout.query.filter(CoachingBogenLayout.project_id.is_(None))
    row = q.first()
    if row:
        return row
    row = CoachingBogenLayout(
        project_id=scope_project_id,
        show_performance_bar=True,
        show_coach_notes=True,
        show_time_spent=True,
        allow_side_by_side=True,
        allow_tcap=True,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _thema_create_scope_from_request():
    raw = request.form.get('project') if request.method == 'POST' else request.args.get('project')
    return _parse_raw_leitfaden_project(raw, abort_on_invalid=True)


@bp.route('/coaching-bogen')
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def manage_coaching_bogen():
    scope_project_id, _ = _parse_leitfaden_project_param(abort_on_invalid=False)
    tab = request.args.get('tab', 'leitfaden')
    if tab not in ('leitfaden', 'themen', 'felder'):
        tab = 'leitfaden'

    all_projects = Project.query.order_by(Project.name).all()

    leitfaden_items = []
    show_copy_leitfaden = False
    themen_items = []
    show_copy_themen = False
    layout_form = None

    try:
        if tab == 'leitfaden':
            if scope_project_id is None:
                q = LeitfadenItem.query.filter(LeitfadenItem.project_id.is_(None))
            else:
                q = LeitfadenItem.query.filter_by(project_id=scope_project_id)
            leitfaden_items = q.order_by(LeitfadenItem.position, LeitfadenItem.id).all()
            show_copy_leitfaden = bool(
                scope_project_id
                and LeitfadenItem.query.filter_by(project_id=scope_project_id).count() == 0
            )
        elif tab == 'themen':
            if scope_project_id is None:
                q = CoachingThemaItem.query.filter(CoachingThemaItem.project_id.is_(None))
            else:
                q = CoachingThemaItem.query.filter_by(project_id=scope_project_id)
            themen_items = q.order_by(CoachingThemaItem.position, CoachingThemaItem.id).all()
            show_copy_themen = bool(
                scope_project_id
                and CoachingThemaItem.query.filter_by(project_id=scope_project_id).count() == 0
            )
        else:
            layout = get_or_create_bogen_layout(scope_project_id)
            layout_form = CoachingBogenLayoutForm(obj=layout)
    except SQLAlchemyError:
        db.session.rollback()
        flash('Datenbank-Tabellen für den Coaching-Bogen fehlen ggf. noch. Bitte Migration ausführen.', 'warning')

    return render_template(
        'admin/manage_coaching_bogen.html',
        active_tab=tab,
        all_projects=all_projects,
        scope_project_id=scope_project_id,
        leitfaden_items=leitfaden_items,
        show_copy_from_standard_leitfaden=show_copy_leitfaden,
        themen_items=themen_items,
        show_copy_from_standard_themen=show_copy_themen,
        layout_form=layout_form,
        config=current_app.config,
    )


@bp.route('/coaching-bogen/layout', methods=['POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def save_coaching_bogen_layout():
    scope_project_id, _ = _parse_raw_leitfaden_project(request.form.get('project'), abort_on_invalid=False)
    form = CoachingBogenLayoutForm()
    if not form.validate_on_submit():
        for err in form.errors.values():
            for e in err:
                flash(e, 'danger')
        kw = {'tab': 'felder'}
        if scope_project_id:
            kw['project'] = scope_project_id
        return redirect(url_for('admin.manage_coaching_bogen', **kw))
    try:
        layout = get_or_create_bogen_layout(scope_project_id)
        layout.allow_side_by_side = form.allow_side_by_side.data
        layout.allow_tcap = form.allow_tcap.data
        layout.show_performance_bar = form.show_performance_bar.data
        layout.show_coach_notes = form.show_coach_notes.data
        layout.show_time_spent = form.show_time_spent.data
        db.session.commit()
        flash('Layout des Coaching-Bogens gespeichert.', 'success')
    except SQLAlchemyError:
        db.session.rollback()
        flash('Layout konnte nicht gespeichert werden.', 'danger')
    kw = {'tab': 'felder'}
    if scope_project_id:
        kw['project'] = scope_project_id
    return redirect(url_for('admin.manage_coaching_bogen', **kw))


@bp.route('/leitfaden')
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def manage_leitfaden():
    kw = {'tab': 'leitfaden'}
    if request.args.get('project'):
        kw['project'] = request.args.get('project')
    return redirect(url_for('admin.manage_coaching_bogen', **kw))


@bp.route('/leitfaden/copy_standard', methods=['POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def copy_standard_leitfaden_to_project():
    project_id = request.form.get('project', type=int)
    if not project_id or not Project.query.get(project_id):
        flash('Ungültiges Projekt.', 'danger')
        return _redirect_manage_leitfaden()
    if LeitfadenItem.query.filter_by(project_id=project_id).count() > 0:
        flash('Dieses Projekt hat bereits einen eigenen Leitfaden.', 'info')
        return _redirect_manage_leitfaden(project_id)
    try:
        standard = (
            LeitfadenItem.query.filter(LeitfadenItem.project_id.is_(None))
            .order_by(LeitfadenItem.position, LeitfadenItem.id)
            .all()
        )
        if not standard:
            flash('Es gibt keinen Standard-Leitfaden zum Kopieren.', 'warning')
            return _redirect_manage_leitfaden(project_id)
        for src in standard:
            db.session.add(
                LeitfadenItem(
                    name=src.name,
                    position=src.position,
                    is_active=src.is_active,
                    project_id=project_id,
                )
            )
        db.session.commit()
        flash(f'{len(standard)} Punkt(e) aus dem Standard-Leitfaden übernommen.', 'success')
    except SQLAlchemyError:
        db.session.rollback()
        flash('Leitfaden konnte nicht kopiert werden.', 'danger')
    return _redirect_manage_leitfaden(project_id)


@bp.route('/leitfaden/create', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def create_leitfaden_item():
    scope_project_id, bad = _leitfaden_create_scope_from_request()
    if bad:
        return _redirect_manage_leitfaden()
    form = LeitfadenItemForm(scope_project_id=scope_project_id)
    if request.method == 'GET' and form.position.data is None:
        try:
            q = LeitfadenItem.query
            if scope_project_id is None:
                q = q.filter(LeitfadenItem.project_id.is_(None))
            else:
                q = q.filter_by(project_id=scope_project_id)
            last_item = q.order_by(LeitfadenItem.position.desc(), LeitfadenItem.id.desc()).first()
            form.position.data = (last_item.position + 1) if last_item else 1
        except SQLAlchemyError:
            db.session.rollback()
            flash('Leitfaden-Tabellen fehlen noch in der Datenbank. Bitte zuerst Migration ausführen: flask db upgrade', 'warning')
            return _redirect_manage_leitfaden(scope_project_id)
    if form.validate_on_submit():
        item = LeitfadenItem(
            name=form.name.data.strip(),
            position=form.position.data,
            is_active=form.is_active.data,
            project_id=scope_project_id,
        )
        db.session.add(item)
        db.session.commit()
        flash('Leitfaden-Punkt erstellt.', 'success')
        return _redirect_manage_leitfaden(scope_project_id)
    leitfaden_list_url = _coaching_bogen_url('leitfaden', scope_project_id)
    return render_template(
        'admin/edit_leitfaden_item.html',
        form=form,
        title='Leitfaden-Punkt erstellen',
        item=None,
        scope_project_id=scope_project_id,
        leitfaden_list_url=leitfaden_list_url,
        config=current_app.config,
    )


@bp.route('/leitfaden/edit/<int:item_id>', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def edit_leitfaden_item(item_id):
    try:
        item = LeitfadenItem.query.get_or_404(item_id)
    except SQLAlchemyError:
        db.session.rollback()
        flash('Leitfaden-Tabellen fehlen noch in der Datenbank. Bitte zuerst Migration ausführen: flask db upgrade', 'warning')
        return _redirect_manage_leitfaden()
    form = LeitfadenItemForm(obj=item, original_name=item.name, scope_project_id=item.project_id)
    if form.validate_on_submit():
        item.name = form.name.data.strip()
        item.position = form.position.data
        item.is_active = form.is_active.data
        db.session.commit()
        flash('Leitfaden-Punkt aktualisiert.', 'success')
        return _redirect_manage_leitfaden(item.project_id)
    leitfaden_list_url = _coaching_bogen_url('leitfaden', item.project_id)
    return render_template(
        'admin/edit_leitfaden_item.html',
        form=form,
        title='Leitfaden-Punkt bearbeiten',
        item=item,
        scope_project_id=item.project_id,
        leitfaden_list_url=leitfaden_list_url,
        config=current_app.config,
    )


@bp.route('/leitfaden/delete/<int:item_id>', methods=['POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def delete_leitfaden_item(item_id):
    try:
        item = LeitfadenItem.query.get_or_404(item_id)
        scope_pid = item.project_id
        db.session.delete(item)
        db.session.commit()
        flash('Leitfaden-Punkt gelöscht.', 'success')
        return _redirect_manage_leitfaden(scope_pid)
    except SQLAlchemyError:
        db.session.rollback()
        flash('Leitfaden-Tabellen fehlen noch in der Datenbank. Bitte zuerst Migration ausführen: flask db upgrade', 'warning')
    return _redirect_manage_leitfaden()


@bp.route('/coaching-bogen/themen/copy_standard', methods=['POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def copy_standard_themen_to_project():
    project_id = request.form.get('project', type=int)
    if not project_id or not Project.query.get(project_id):
        flash('Ungültiges Projekt.', 'danger')
        return _redirect_manage_themen()
    if CoachingThemaItem.query.filter_by(project_id=project_id).count() > 0:
        flash('Dieses Projekt hat bereits eigene Themen.', 'info')
        return _redirect_manage_themen(project_id)
    try:
        standard = (
            CoachingThemaItem.query.filter(CoachingThemaItem.project_id.is_(None))
            .order_by(CoachingThemaItem.position, CoachingThemaItem.id)
            .all()
        )
        if not standard:
            flash('Es gibt keine Standard-Themen zum Kopieren.', 'warning')
            return _redirect_manage_themen(project_id)
        for src in standard:
            db.session.add(
                CoachingThemaItem(
                    name=src.name,
                    position=src.position,
                    is_active=src.is_active,
                    project_id=project_id,
                )
            )
        db.session.commit()
        flash(f'{len(standard)} Thema/Themen aus dem Standard übernommen.', 'success')
    except SQLAlchemyError:
        db.session.rollback()
        flash('Themen konnten nicht kopiert werden.', 'danger')
    return _redirect_manage_themen(project_id)


@bp.route('/coaching-bogen/themen/create', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def create_thema_item():
    scope_project_id, bad = _thema_create_scope_from_request()
    if bad:
        return _redirect_manage_themen()
    form = CoachingThemaItemForm(scope_project_id=scope_project_id)
    if request.method == 'GET' and form.position.data is None:
        try:
            q = CoachingThemaItem.query
            if scope_project_id is None:
                q = q.filter(CoachingThemaItem.project_id.is_(None))
            else:
                q = q.filter_by(project_id=scope_project_id)
            last_item = q.order_by(CoachingThemaItem.position.desc(), CoachingThemaItem.id.desc()).first()
            form.position.data = (last_item.position + 1) if last_item else 1
        except SQLAlchemyError:
            db.session.rollback()
            flash('Themen-Tabelle fehlt ggf. noch. Bitte Migration ausführen.', 'warning')
            return _redirect_manage_themen(scope_project_id)
    if form.validate_on_submit():
        db.session.add(
            CoachingThemaItem(
                name=form.name.data.strip(),
                position=form.position.data,
                is_active=form.is_active.data,
                project_id=scope_project_id,
            )
        )
        db.session.commit()
        flash('Thema angelegt.', 'success')
        return _redirect_manage_themen(scope_project_id)
    thema_list_url = _coaching_bogen_url('themen', scope_project_id)
    return render_template(
        'admin/edit_thema_item.html',
        form=form,
        title='Coaching-Thema anlegen',
        item=None,
        scope_project_id=scope_project_id,
        thema_list_url=thema_list_url,
        config=current_app.config,
    )


@bp.route('/coaching-bogen/themen/edit/<int:item_id>', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def edit_thema_item(item_id):
    try:
        item = CoachingThemaItem.query.get_or_404(item_id)
    except SQLAlchemyError:
        db.session.rollback()
        flash('Themen-Tabelle fehlt ggf. noch.', 'warning')
        return _redirect_manage_themen()
    form = CoachingThemaItemForm(obj=item, original_name=item.name, scope_project_id=item.project_id)
    if form.validate_on_submit():
        item.name = form.name.data.strip()
        item.position = form.position.data
        item.is_active = form.is_active.data
        db.session.commit()
        flash('Thema aktualisiert.', 'success')
        return _redirect_manage_themen(item.project_id)
    thema_list_url = _coaching_bogen_url('themen', item.project_id)
    return render_template(
        'admin/edit_thema_item.html',
        form=form,
        title='Coaching-Thema bearbeiten',
        item=item,
        scope_project_id=item.project_id,
        thema_list_url=thema_list_url,
        config=current_app.config,
    )


@bp.route('/coaching-bogen/themen/delete/<int:item_id>', methods=['POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def delete_thema_item(item_id):
    try:
        item = CoachingThemaItem.query.get_or_404(item_id)
        scope_pid = item.project_id
        db.session.delete(item)
        db.session.commit()
        flash('Thema gelöscht.', 'success')
        return _redirect_manage_themen(scope_pid)
    except SQLAlchemyError:
        db.session.rollback()
        flash('Thema konnte nicht gelöscht werden.', 'danger')
    return _redirect_manage_themen()


# --- Assigned Coachings Management (Admin) ---
@bp.route('/manage_assigned_coachings', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def manage_assigned_coachings():
    page = request.args.get('page', 1, type=int)
    team_filter = request.args.get('team', type=int)
    coach_filter = request.args.get('coach', type=int)
    member_filter = request.args.get('member', type=int)
    status_filter = request.args.get('status', default='')
    search_term = request.args.get('search', default="", type=str).strip()
    project_filter = request.args.get('project', type=int) or session.get('active_project')

    query = AssignedCoaching.query

    if project_filter:
        query = query.join(AssignedCoaching.team_member).join(TeamMember.team).filter(Team.project_id == project_filter)

    if team_filter:
        query = query.join(AssignedCoaching.team_member).join(TeamMember.team).filter(Team.id == team_filter)
    if coach_filter:
        query = query.filter(AssignedCoaching.coach_id == coach_filter)
    if member_filter:
        query = query.filter(AssignedCoaching.team_member_id == member_filter)
    if status_filter:
        query = query.filter(AssignedCoaching.status == status_filter)
    if search_term:
        search_pattern = f"%{search_term}%"
        query = query.join(AssignedCoaching.team_member).join(AssignedCoaching.coach).filter(
            or_(
                TeamMember.name.ilike(search_pattern),
                User.username.ilike(search_pattern)
            )
        )

    if request.method == 'POST':
        if 'delete_selected' in request.form:
            assignment_ids_to_delete = request.form.getlist('assignment_ids')
            if assignment_ids_to_delete:
                try:
                    assignment_ids_to_delete_int = [int(id_str) for id_str in assignment_ids_to_delete]
                    Coaching.query.filter(Coaching.assigned_coaching_id.in_(assignment_ids_to_delete_int)).update({Coaching.assigned_coaching_id: None}, synchronize_session='fetch')
                    deleted_count = AssignedCoaching.query.filter(AssignedCoaching.id.in_(assignment_ids_to_delete_int)).delete(synchronize_session='fetch')
                    db.session.commit()
                    flash(f'{deleted_count} Coaching-Aufgabe(n) erfolgreich gelöscht.', 'success')
                except ValueError:
                    flash('Ungültige IDs zum Löschen ausgewählt.', 'danger')
                except Exception as e:
                    db.session.rollback()
                    current_app.logger.error(f"Fehler beim Löschen von zugewiesenen Coachings: {e}")
                    flash(f'Fehler beim Löschen: {str(e)}', 'danger')
                return redirect(url_for('admin.manage_assigned_coachings', page=page, team=team_filter, coach=coach_filter, member=member_filter, status=status_filter, search=search_term))
            else:
                flash('Keine Coachings zum Löschen ausgewählt.', 'info')

    assignments_paginated = query.order_by(AssignedCoaching.deadline.desc()).paginate(page=page, per_page=15, error_out=False)

    all_teams = Team.query.filter(Team.name != ARCHIV_TEAM_NAME).order_by(Team.name).all()
    all_coaches = User.query.join(User.role).filter(Role.name.in_(['Teamleiter', 'Qualitätsmanager', 'SalesCoach', 'Trainer', 'Betriebsleiter'])).order_by(User.username).all()
    all_members = TeamMember.query.join(Team, TeamMember.team_id == Team.id).filter(Team.name != ARCHIV_TEAM_NAME).order_by(Team.name, TeamMember.name).all()
    all_projects = Project.query.order_by(Project.name).all()
    status_choices = [
        ('', 'Alle Status'),
        ('pending', 'Ausstehend'),
        ('accepted', 'Angenommen'),
        ('in_progress', 'In Bearbeitung'),
        ('completed', 'Abgeschlossen'),
        ('expired', 'Abgelaufen'),
        ('rejected', 'Abgelehnt'),
        ('cancelled', 'Storniert')
    ]

    return render_template('admin/manage_assigned_coachings.html',
                           assignments=assignments_paginated,
                           all_teams=all_teams,
                           all_coaches=all_coaches,
                           all_members=all_members,
                           all_projects=all_projects,
                           status_choices=status_choices,
                           team_filter=team_filter,
                           coach_filter=coach_filter,
                           member_filter=member_filter,
                           status_filter=status_filter,
                           search_term=search_term,
                           current_project_filter=project_filter,
                           config=current_app.config)


@bp.route('/assigned_coaching/<int:assignment_id>/edit', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def edit_assigned_coaching(assignment_id):
    assignment = AssignedCoaching.query.get_or_404(assignment_id)
    form = AdminAssignedCoachingForm(obj=assignment)
    if form.validate_on_submit():
        assignment.coach_id = form.coach_id.data
        assignment.team_member_id = form.team_member_id.data
        deadline_datetime = datetime.combine(form.deadline.data, time(23, 59, 59))
        assignment.deadline = deadline_datetime
        assignment.expected_coaching_count = form.expected_coaching_count.data
        assignment.desired_performance_note = form.desired_performance_note.data
        assignment.status = form.status.data
        db.session.commit()
        flash('Coaching-Aufgabe erfolgreich aktualisiert.', 'success')
        return redirect(url_for('admin.manage_assigned_coachings'))
    elif request.method == 'GET':
        form.coach_id.data = assignment.coach_id
        form.team_member_id.data = assignment.team_member_id
        form.deadline.data = assignment.deadline.date()
        form.expected_coaching_count.data = assignment.expected_coaching_count
        form.desired_performance_note.data = assignment.desired_performance_note
        form.status.data = assignment.status
    return render_template('admin/edit_assigned_coaching.html', form=form, assignment=assignment, config=current_app.config)


@bp.route('/assigned_coaching/<int:assignment_id>/delete', methods=['POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def delete_assigned_coaching(assignment_id):
    assignment = AssignedCoaching.query.get_or_404(assignment_id)
    try:
        Coaching.query.filter_by(assigned_coaching_id=assignment_id).update({Coaching.assigned_coaching_id: None})
        db.session.delete(assignment)
        db.session.commit()
        flash('Coaching-Aufgabe gelöscht.', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Fehler beim Löschen von zugewiesener Coaching-Aufgabe {assignment_id}: {e}")
        flash('Fehler beim Löschen.', 'danger')
    return redirect(url_for('admin.manage_assigned_coachings'))


# --- Team Member with User Creation ---
@bp.route('/teammembers/create-with-user', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def create_team_member_with_user():
    form = TeamMemberWithUserForm()
    if form.validate_on_submit():
        try:
            team = Team.query.get(form.team_id.data)
            if not team:
                flash('Team nicht gefunden.', 'danger')
                return redirect(url_for('admin.create_team_member_with_user'))
            
            full_name = f"{form.first_name.data} {form.last_name.data}".strip()
            team_member = TeamMember(
                name=full_name,
                team_id=form.team_id.data,
                pylon=form.pylon.data,
                plt_id=form.plt_id.data,
                ma_kennung=form.ma_kennung.data,
                dag_id=form.dag_id.data
            )
            db.session.add(team_member)
            db.session.flush()
            
            if not form.active.data:
                archiv_team = get_or_create_archiv_team()
                team_member.original_team_id = team_member.team_id
                team_member.original_project_id = team_member.team.project_id
                team_member.team_id = archiv_team.id
            
            if form.create_user.data and form.username.data:
                role = Role.query.filter_by(name='Mitarbeiter').first()
                if not role:
                    flash('Die Rolle \"Mitarbeiter\" existiert nicht. Bitte zuerst erstellen.', 'danger')
                    db.session.rollback()
                    return redirect(url_for('admin.create_team_member_with_user'))
                user = User(
                    username=form.username.data,
                    email=form.email.data if form.email.data else None,
                    role_id=role.id,
                    project_id=team.project_id
                )
                user.set_password(form.password.data)
                db.session.add(user)
                db.session.flush()
                team_member.user_id = user.id
            
            db.session.commit()
            flash('Teammitglied erfolgreich erstellt!', 'success')
            return redirect(url_for('admin.panel'))
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Fehler beim Erstellen des Teammitglieds mit Benutzer: {e}")
            flash(f'Fehler: {str(e)}', 'danger')
    return render_template('admin/create_team_member_with_user.html', form=form, config=current_app.config)


# --- CSV Import: Mapping-Felder & Vorschau ---
CSV_IMPORT_MAP_FIELDS = [
    'pylon', 'plt_id', 'first_name', 'last_name', 'ma_kennung', 'dag_id', 'email',
    'team', 'project', 'active_status', 'agent_status', 'role',
]

# Reihenfolge der Vergleichstabelle Vorschau (Alt vs Neu)
CSV_REVIEW_DISPLAY_KEYS = [
    'pylon', 'plt_id', 'first_name', 'last_name', 'email', 'project', 'team',
    'role', 'agent_status', 'active_status', 'ma_kennung', 'dag_id',
]

# Vorschau begrenzen (HTML/ORM); nicht sichtbare Zeilen gelten beim Import als „angehakt“
CSV_PREVIEW_MAX_ROWS = 6000
# Ab diese Größe: UI standardmäßig zugeklappt + Suchleiste
CSV_PREVIEW_LARGE_UI_THRESHOLD = 100
_PREVIEW_PYLONS_SIDE_SUFFIX = '.preview_pylons.json'


def _csv_preview_sidecar_path(temp_path):
    return temp_path + _PREVIEW_PYLONS_SIDE_SUFFIX


def _remove_csv_preview_sidecar(temp_path):
    path = _csv_preview_sidecar_path(temp_path)
    if os.path.isfile(path):
        try:
            os.unlink(path)
        except OSError:
            pass


def _csv_clean_cell_text(val, collapse_spaces=True):
    """Excel/Export: NBSP, ZWSP, BOM in Zellen entfernen; Whitespace vereinheitlichen."""
    if val is None:
        return ''
    s = str(val).strip()
    if not s:
        return ''
    s = s.replace('\ufeff', '').replace('\u00a0', ' ').replace('\u202f', ' ')
    s = re.sub(r'[\u200b-\u200d]', '', s)
    if collapse_spaces:
        s = re.sub(r'\s+', ' ', s).strip()
    return s


def _csv_normalize_full_name(s):
    """Vollname für Vergleich CSV vs DB (nur Whitespace/Unicode, keine Groß/Klein-Umsetzung)."""
    t = _csv_clean_cell_text(s, collapse_spaces=True)
    return t


def _csv_mapped_cell_clean(row, mapping, field_key):
    col = mapping.get(field_key)
    if not col:
        return None
    t = _csv_clean_cell_text(row.get(col, ''), True)
    return t or None


def _csv_row_pylon_value(row, mapping):
    if not mapping.get('pylon'):
        return None
    raw = row.get(mapping['pylon'], '')
    if raw is None:
        return None
    s = _csv_clean_cell_text(raw, collapse_spaces=True)
    # Excel-Zahl als Pylon: 60860.0
    if re.fullmatch(r'\d+\.0', s):
        s = s[:-2]
    return s or None


class _CsvPreviewCaches:
    """Einmalige Ladevorgänge für die Änderungsvorschau (vermeidet N+1-Queries)."""

    __slots__ = (
        'members_by_pylon', 'projects_by_name', 'default_project',
        'teams_by_proj_name', 'first_team_by_project_id', 'teams_by_id', 'users_by_id',
    )

    def __init__(self, pylons):
        pylons = {p for p in pylons if p}
        self.members_by_pylon = {}
        if pylons:
            q = TeamMember.query.options(
                joinedload(TeamMember.team).joinedload(Team.project),
                joinedload(TeamMember.original_team),
            ).filter(TeamMember.pylon.in_(pylons))
            for m in q:
                if m.pylon:
                    self.members_by_pylon[m.pylon] = m
        projects = Project.query.order_by(Project.id).all()
        self.projects_by_name = {p.name: p for p in projects}
        self.default_project = projects[0] if projects else None
        teams = Team.query.options(joinedload(Team.project)).order_by(Team.id).all()
        self.teams_by_proj_name = {(t.project_id, t.name): t for t in teams}
        self.first_team_by_project_id = {}
        for t in teams:
            if t.project_id not in self.first_team_by_project_id:
                self.first_team_by_project_id[t.project_id] = t
        self.teams_by_id = {t.id: t for t in teams}
        user_ids = {m.user_id for m in self.members_by_pylon.values() if m.user_id}
        self.users_by_id = {}
        if user_ids:
            for u in User.query.options(joinedload(User.role)).filter(User.id.in_(user_ids)).all():
                self.users_by_id[u.id] = u


class _CsvImportRunCaches:
    """Lookups für den Schreib-Import; Dicts werden bei neuen Projekt/Team/Rolle erweitert."""

    __slots__ = (
        'members_by_pylon', 'projects_by_name', 'default_project',
        'teams_by_proj_name', 'first_team_by_project', 'roles_by_name',
    )

    def __init__(self, included_pylons):
        pylons = {p for p in included_pylons if p}
        self.members_by_pylon = {}
        if pylons:
            for m in TeamMember.query.filter(TeamMember.pylon.in_(pylons)).all():
                if m.pylon:
                    self.members_by_pylon[m.pylon] = m
        plist = Project.query.order_by(Project.id).all()
        self.projects_by_name = {p.name: p for p in plist}
        self.default_project = min(plist, key=lambda p: p.id) if plist else None
        self.teams_by_proj_name = {}
        self.first_team_by_project = {}
        for t in Team.query.order_by(Team.id).all():
            self.teams_by_proj_name[(t.project_id, t.name)] = t
            if t.project_id not in self.first_team_by_project:
                self.first_team_by_project[t.project_id] = t
        self.roles_by_name = {r.name: r for r in Role.query.all()}


def _csv_mapping_from_request(form):
    mapping = {}
    for field in CSV_IMPORT_MAP_FIELDS:
        col_name = form.get(f'map_{field}')
        if col_name:
            mapping[field] = col_name
    return mapping


def _csv_row_active_flag(row, mapping):
    active_col = mapping.get('active_status')
    if not active_col:
        # Ohne zugeordnete Spalte nicht als „inaktiv“ werten — sonst landen alle Neuimporte im ARCHIV.
        return True
    raw_active = row.get(active_col, '')
    active_str = _csv_clean_cell_text(raw_active, collapse_spaces=True) if raw_active is not None else ''
    if not active_str:
        return False
    s = active_str.lower().replace(' ', '')
    return s in ('1', '1.0', '1,0', 'true', 'ja', 'j', 'yes', 'y', 'wahr', 'x', 'aktiv')


def _norm_csv_cmp(val):
    if val is None:
        return ''
    return str(val).strip()


def _csv_row_role_name(row, mapping):
    col = mapping.get('role') or mapping.get('agent_status')
    if not col:
        return 'Mitarbeiter'
    v = row.get(col, '')
    if v is None:
        return 'Mitarbeiter'
    s = _csv_clean_cell_text(v, collapse_spaces=True)
    return s if s else 'Mitarbeiter'


def _csv_import_row_strings(row, mapping):
    """Felder wie beim Import (leere Strings möglich)."""
    def pull(field):
        col = mapping.get(field)
        if not col:
            return None
        v = row.get(col, '')
        if v is None:
            return None
        return _csv_clean_cell_text(v, collapse_spaces=True) or None

    first_name = pull('first_name') or ''
    last_name = pull('last_name') or ''
    full_name = f'{first_name} {last_name}'.strip()
    role_name = _csv_row_role_name(row, mapping)
    return {
        'first_name': first_name,
        'last_name': last_name,
        'full_name': full_name,
        'role_name': role_name,
        'plt_id': pull('plt_id'),
        'ma_kennung': pull('ma_kennung'),
        'dag_id': pull('dag_id'),
        'email': pull('email'),
    }


def _csv_resolve_row_context(row, mapping, archiv_team, caches=None):
    """Liest eine CSV-Zeile wie der Import (ohne Schreiben)."""
    pylon = _csv_row_pylon_value(row, mapping)
    if not pylon:
        return None

    fs = _csv_import_row_strings(row, mapping)
    full_name = fs['full_name'] or pylon
    role_name = fs['role_name']

    project_name = _csv_mapped_cell_clean(row, mapping, 'project')
    team_name = _csv_mapped_cell_clean(row, mapping, 'team')

    project = None
    will_create_project = False
    proj_label = ''

    if project_name:
        if caches:
            project = caches.projects_by_name.get(project_name)
        else:
            project = Project.query.filter_by(name=project_name).first()
        if not project:
            will_create_project = True
            proj_label = f'{project_name} (wird neu angelegt)'
        else:
            proj_label = project.name
    else:
        if caches:
            project = caches.default_project
        else:
            project = Project.query.order_by(Project.id).first()
        if not project:
            return {
                'error': True,
                'pylon': pylon,
                'full_name': full_name,
                'role_name': role_name,
                'messages': ['Kein Projekt in der CSV und kein Standardprojekt in der Datenbank.'],
                'group_key': ('! Fehler', '! Fehler', role_name),
            }
        proj_label = project.name

    team = None
    will_create_team = False
    team_label = ''
    if not will_create_project and project:
        if team_name:
            if caches:
                team = caches.teams_by_proj_name.get((project.id, team_name))
            else:
                team = Team.query.filter_by(name=team_name, project_id=project.id).first()
            if not team:
                will_create_team = True
                team_label = f'{team_name} (wird neu angelegt)'
            else:
                team_label = team.name
        else:
            if caches:
                team = caches.first_team_by_project_id.get(project.id)
            else:
                team = Team.query.filter_by(project_id=project.id).order_by(Team.id).first()
            if not team:
                will_create_team = True
                team_label = 'Default (wird neu angelegt)'
            else:
                team_label = team.name
    else:
        team_label = team_name or '(Team folgt nach Projekt-Anlage)'

    is_active = _csv_row_active_flag(row, mapping)
    if caches:
        team_member = caches.members_by_pylon.get(pylon)
    else:
        team_member = TeamMember.query.filter_by(pylon=pylon).first()

    return {
        'pylon': pylon,
        'full_name': full_name,
        'role_name': role_name,
        'project': project,
        'project_name_csv': project_name,
        'will_create_project': will_create_project,
        'proj_label': proj_label,
        'team': team,
        'team_name_csv': team_name,
        'will_create_team': will_create_team,
        'team_label': team_label,
        'is_active': is_active,
        'team_member': team_member,
        'archiv_team': archiv_team,
        'plt_id': fs['plt_id'],
        'ma_kennung': fs['ma_kennung'],
        'dag_id': fs['dag_id'],
        'email': fs['email'],
        'group_key': (proj_label, team_label, role_name),
    }


def _csv_simulate_target_team_id(ctx):
    """Team-ID nach Importlogik; None wenn aktives Ziel-Team noch nicht in DB existiert."""
    archiv_team = ctx['archiv_team']
    team = ctx['team']
    team_member = ctx['team_member']
    is_active = ctx['is_active']
    will_create_project = ctx['will_create_project']
    will_create_team = ctx['will_create_team']

    if will_create_project or will_create_team:
        if not is_active:
            return archiv_team.id
        return None

    if not team:
        return archiv_team.id if not is_active else None

    if not team_member:
        if not is_active:
            return archiv_team.id
        return team.id

    if not is_active:
        return archiv_team.id

    if team_member.team_id == archiv_team.id:
        if team_member.original_team_id:
            return team_member.original_team_id
        return team.id
    return team.id


def _csv_member_snapshot(team_member, archiv_team, users_by_id=None):
    if not team_member:
        return None
    in_archiv = team_member.team_id == archiv_team.id
    proj_n = ''
    team_display = ''
    if in_archiv:
        # Vergleich mit CSV: inaktive PLs stehen in ARCHIV, die Schichtplan-Team/Projekt-Infos liegen an original_* oder am Platzhalter-Team.
        if team_member.original_team:
            ot = team_member.original_team
            team_display = (ot.name or '').strip()
            if ot.project:
                proj_n = (ot.project.name or '').strip()
        elif team_member.original_project:
            proj_n = (team_member.original_project.name or '').strip()
        if not proj_n and team_member.team and team_member.team.project:
            proj_n = (team_member.team.project.name or '').strip()
        if not team_display:
            team_display = ARCHIV_TEAM_NAME
    elif team_member.team:
        team_display = (team_member.team.name or '').strip()
        if team_member.team.project:
            proj_n = (team_member.team.project.name or '').strip()
    user = None
    if team_member.user_id:
        if users_by_id is not None:
            user = users_by_id.get(team_member.user_id)
        if user is None:
            user = User.query.get(team_member.user_id)
    role_n = user.role.name if user and user.role else None
    email_disp = _csv_display_cell_csv(user.email if user else None)
    return {
        'in_archiv': in_archiv,
        'project': proj_n,
        'team': team_display,
        'name': team_member.name or '',
        'plt_id': _norm_csv_cmp(team_member.plt_id),
        'ma_kennung': _norm_csv_cmp(team_member.ma_kennung),
        'dag_id': _norm_csv_cmp(team_member.dag_id),
        'role': role_n,
        'email': email_disp,
    }


def _csv_target_snapshot(ctx, preview_caches=None):
    archiv_team = ctx['archiv_team']
    tid = _csv_simulate_target_team_id(ctx)
    fs_name = ctx['full_name']
    role_name = ctx['role_name']
    teams_by_id = preview_caches.teams_by_id if preview_caches else None

    if tid == archiv_team.id:
        loc = f'Herkunft für ARCHIV: „{ctx["team_label"]}“ ({ctx["proj_label"]})'
        if ctx['team_member'] and ctx['team_member'].original_team_id and ctx['team_member'].original_team:
            ot = ctx['team_member'].original_team.name
            loc = f'ARCHIV (Reaktivierung möglich über Ursprungs-Team „{ot}“)'
        return {
            'in_archiv': True,
            'project': '',
            'team': loc,
            'name': fs_name,
            'plt_id': _norm_csv_cmp(ctx['plt_id']),
            'ma_kennung': _norm_csv_cmp(ctx['ma_kennung']),
            'dag_id': _norm_csv_cmp(ctx['dag_id']),
            'role': role_name,
        }

    if tid is not None and ctx['team'] and ctx['team'].id == tid:
        t = ctx['team']
        p = t.project.name if t.project else ''
        return {
            'in_archiv': False,
            'project': p,
            'team': t.name,
            'name': fs_name,
            'plt_id': _norm_csv_cmp(ctx['plt_id']),
            'ma_kennung': _norm_csv_cmp(ctx['ma_kennung']),
            'dag_id': _norm_csv_cmp(ctx['dag_id']),
            'role': role_name,
        }

    if tid is not None:
        t = teams_by_id.get(tid) if teams_by_id else None
        if t is None:
            t = Team.query.get(tid)
        if t:
            p = t.project.name if t.project else ''
            return {
                'in_archiv': False,
                'project': p,
                'team': t.name,
                'name': fs_name,
                'plt_id': _norm_csv_cmp(ctx['plt_id']),
                'ma_kennung': _norm_csv_cmp(ctx['ma_kennung']),
                'dag_id': _norm_csv_cmp(ctx['dag_id']),
                'role': role_name,
            }

    return {
        'in_archiv': False,
        'project': ctx['proj_label'],
        'team': ctx['team_label'],
        'name': fs_name,
        'plt_id': _norm_csv_cmp(ctx['plt_id']),
        'ma_kennung': _norm_csv_cmp(ctx['ma_kennung']),
        'dag_id': _norm_csv_cmp(ctx['dag_id']),
        'role': role_name,
    }


def _csv_cell_display(row, column_header):
    if not column_header:
        return ''
    v = row.get(column_header, '')
    if v is None:
        return ''
    return _csv_clean_cell_text(v, collapse_spaces=True)


def _csv_display_cell_csv(s):
    t = (s or '').strip()
    return t if t else '(leer)'


def _csv_name_split_parts(display_name):
    name = (display_name or '').strip()
    if not name:
        return '(leer)', '(leer)'
    parts = name.split()
    if len(parts) == 1:
        return parts[0], '(leer)'
    return parts[0], ' '.join(parts[1:])


def _csv_db_display_for_field(field, team_member, archiv_team, users_by_id=None):
    """Ist-Wert aus der DB, vergleichbar mit CSV-Zelle."""
    if not team_member:
        return '(leer)'
    snap = _csv_member_snapshot(team_member, archiv_team, users_by_id)
    if field == 'pylon':
        return _csv_display_cell_csv(team_member.pylon)
    if field == 'project':
        return _csv_display_cell_csv(snap['project'])
    if field == 'team':
        return _csv_display_cell_csv(snap['team'])
    if field == 'active_status':
        return '1' if not snap['in_archiv'] else '0'
    if field == 'role':
        return _csv_display_cell_csv(snap['role'])
    if field == 'agent_status':
        return '(leer)'
    if field == 'first_name':
        fn, _ln = _csv_name_split_parts(team_member.name)
        return fn
    if field == 'last_name':
        _fn, ln = _csv_name_split_parts(team_member.name)
        return ln
    if field == 'plt_id':
        return _csv_display_cell_csv(team_member.plt_id)
    if field == 'ma_kennung':
        return _csv_display_cell_csv(team_member.ma_kennung)
    if field == 'dag_id':
        return _csv_display_cell_csv(team_member.dag_id)
    if field == 'email':
        return snap['email']
    return '(leer)'


def _csv_review_cell_value(row, mapping, field_key):
    """Wert aus CSV-Zeile für Review; None wenn Spalte nicht zugeordnet."""
    if row is None:
        return '(leer)'
    col = mapping.get(field_key)
    if not col:
        return None
    return _csv_display_cell_csv(_csv_cell_display(row, col))


def _csv_build_review_comparison(new_row, mapping, team_member, archiv_team, users_by_id):
    """Zeilen für Alt/Neu-Tabelle: Alt = Live-Datenbank, Neu = hochgeladene CSV."""
    rows = []
    seen_cols = set()
    for field_key in CSV_REVIEW_DISPLAY_KEYS:
        col = mapping.get(field_key)
        if not col or col in seen_cols:
            continue
        seen_cols.add(col)
        new_val = _csv_review_cell_value(new_row, mapping, field_key)
        if new_val is None:
            continue
        if team_member:
            old_val = _csv_db_display_for_field(field_key, team_member, archiv_team, users_by_id)
        else:
            old_val = '—'
        changed = old_val != new_val
        rows.append({'label': col, 'old': old_val, 'new': new_val, 'changed': changed})

    if team_member and not mapping.get('active_status'):
        db_a = _csv_db_display_for_field('active_status', team_member, archiv_team, users_by_id)
        csv_a = '1' if _csv_row_active_flag(new_row, mapping) else '0'
        if db_a != csv_a:
            rows.append({
                'label': 'PLT aktiv (no column mapped)',
                'old': db_a, 'new': csv_a, 'changed': True,
            })

    if team_member and not mapping.get('role') and not mapping.get('agent_status'):
        db_r = _csv_db_display_for_field('role', team_member, archiv_team, users_by_id)
        if db_r != 'Mitarbeiter':
            rows.append({
                'label': 'Role (no column mapped)',
                'old': db_r, 'new': 'Mitarbeiter', 'changed': True,
            })

    _csv_review_drop_name_rows_when_full_name_matches(rows, new_row, mapping, team_member)
    _csv_review_suppress_inactive_archiv_org_fields(rows, new_row, mapping, team_member, archiv_team)
    return rows


def _csv_review_drop_name_rows_when_full_name_matches(rows, new_row, mapping, team_member):
    """
    DB speichert einen Vollnamen; die Vorschau splittet ihn für „Alt“ nur grob (erstes Wort / Rest).
    CSV liefert Vorname + Nachname separat — dieselbe Person kann dann künstlich als Änderung erscheinen,
    obwohl Vorname+Nachname zusammen dem DB-Vollnamen entsprechen.
    """
    if not team_member:
        return
    fn_col = mapping.get('first_name')
    ln_col = mapping.get('last_name')
    if not fn_col and not ln_col:
        return
    fs = _csv_import_row_strings(new_row, mapping)
    csv_full = _csv_normalize_full_name(fs.get('full_name') or '')
    db_full = _csv_normalize_full_name(team_member.name or '')
    if not csv_full or csv_full != db_full:
        return
    drop = {c for c in (fn_col, ln_col) if c}
    rows[:] = [r for r in rows if r.get('label') not in drop]


def _csv_review_suppress_inactive_archiv_org_fields(rows, new_row, mapping, team_member, archiv_team):
    """
    In CSV und DB ist PLT inaktiv (0): Projekt/Team aus dem Schichtplan weichen oft von ARCHIV/Platzhalter-Teams ab.
    Solche Differenzen sind für die Vorschau keine echten Import-Änderungen — sonst erscheinen alle Archiv-PLs dauernd.
    """
    if not team_member or not archiv_team:
        return
    if team_member.team_id != archiv_team.id:
        return
    if _csv_row_active_flag(new_row, mapping):
        return
    proj_col = mapping.get('project')
    team_col = mapping.get('team')
    for row in rows:
        lbl = row.get('label')
        if lbl and (lbl == proj_col or lbl == team_col):
            row['changed'] = False
            row['old'] = row['new']


def _csv_item_search_text(comparison, extra_lines):
    parts = []
    for c in comparison:
        parts.extend([c.get('label', ''), str(c.get('old', '')), str(c.get('new', ''))])
    if extra_lines:
        parts.extend(str(x) for x in extra_lines)
    return ' '.join(parts).lower()


def _csv_change_item_payload(ctx, comparison, change_kind, checkbox_disabled, error_messages=None):
    if error_messages is not None:
        lines = list(error_messages)
    else:
        lines = [f"{c['label']}: from {c['old']} to {c['new']}" for c in comparison if c['changed']]
    return {
        'pylon': ctx['pylon'],
        'full_name': ctx['full_name'],
        'comparison': comparison,
        'change_lines': lines,
        'group_key': ctx['group_key'],
        'change_kind': change_kind,
        'checkbox_disabled': checkbox_disabled,
        'search_text': _csv_item_search_text(comparison, lines),
    }


def _csv_build_change_item(row, mapping, archiv_team, preview_caches=None):
    """Vorschau: Alt (Live-DB) vs Neu (Import-CSV)."""
    ctx = _csv_resolve_row_context(row, mapping, archiv_team, preview_caches)
    if ctx is None:
        return None

    users_by_id = preview_caches.users_by_id if preview_caches else None
    pylon = ctx['pylon']

    if ctx.get('error'):
        tm = None
        if preview_caches:
            tm = preview_caches.members_by_pylon.get(pylon)
        comparison = _csv_build_review_comparison(row, mapping, tm, archiv_team, users_by_id)
        return _csv_change_item_payload(ctx, comparison, 'error', True, error_messages=ctx['messages'])

    team_member = ctx['team_member']
    after_snap = _csv_target_snapshot(ctx, preview_caches)
    comparison = _csv_build_review_comparison(row, mapping, team_member, archiv_team, users_by_id)

    if not any(c['changed'] for c in comparison):
        return None

    if team_member is None:
        return _csv_change_item_payload(ctx, comparison, 'create', False)

    before = _csv_member_snapshot(team_member, archiv_team, users_by_id)
    kind = 'archive' if after_snap['in_archiv'] and not before['in_archiv'] else (
        'restore' if before['in_archiv'] and not after_snap['in_archiv'] else 'update'
    )
    return _csv_change_item_payload(ctx, comparison, kind, False)


def _remove_baseline_csv_session(session):
    bpath = session.pop('csv_baseline_temp_file', None)
    if bpath and os.path.isfile(bpath):
        try:
            os.unlink(bpath)
        except OSError:
            pass
    session.pop('csv_baseline_delimiter', None)


def _csv_collect_last_row_by_pylon(temp_path, delimiter, mapping):
    from collections import OrderedDict
    last = OrderedDict()
    with open(temp_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        for row in reader:
            pylon = _csv_row_pylon_value(row, mapping)
            if pylon:
                last[pylon] = row
    return last


def _group_csv_preview_items(items):
    from collections import defaultdict
    tree = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for it in items:
        pk, tk, rk = it['group_key']
        tree[pk][tk][rk].append(it)
    return tree


def _run_csv_import_with_row_filter(temp_path, delimiter, mapping, archiv_team, included_pylons):
    """Führt den Import aus; nur Zeilen deren Pylon in included_pylons liegt (Set)."""
    stats = {
        'created_members': 0, 'updated_members': 0, 'archived_members': 0,
        'created_projects': 0, 'created_teams': 0, 'created_users': 0, 'created_roles': 0,
        'errors': 0, 'skipped': 0,
    }
    batch = []
    BATCH_SIZE = 400
    processed = 0
    ic = _CsvImportRunCaches(included_pylons)

    with open(temp_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        for row in reader:
            processed += 1
            try:
                pylon = _csv_row_pylon_value(row, mapping)
                if not pylon:
                    continue
                if pylon not in included_pylons:
                    stats['skipped'] += 1
                    continue

                fs_imp = _csv_import_row_strings(row, mapping)
                first_name = fs_imp.get('first_name') or ''
                last_name = fs_imp.get('last_name') or ''
                full_name = (fs_imp.get('full_name') or '').strip() or pylon

                plt_id = _csv_mapped_cell_clean(row, mapping, 'plt_id')
                ma_kennung = _csv_mapped_cell_clean(row, mapping, 'ma_kennung')
                dag_id = _csv_mapped_cell_clean(row, mapping, 'dag_id')
                email = _csv_mapped_cell_clean(row, mapping, 'email')

                role_name = _csv_row_role_name(row, mapping)

                role = ic.roles_by_name.get(role_name)
                if not role:
                    role = get_or_create_role(role_name)
                    ic.roles_by_name[role.name] = role
                    stats['created_roles'] += 1

                project_name = _csv_mapped_cell_clean(row, mapping, 'project')
                project = None
                if project_name:
                    project = ic.projects_by_name.get(project_name)
                    if not project:
                        project = Project(name=project_name)
                        db.session.add(project)
                        db.session.flush()
                        ic.projects_by_name[project.name] = project
                        stats['created_projects'] += 1
                else:
                    project = ic.default_project
                    if not project:
                        stats['errors'] += 1
                        continue

                team_name = _csv_mapped_cell_clean(row, mapping, 'team')
                team = None
                if team_name:
                    team = ic.teams_by_proj_name.get((project.id, team_name))
                    if not team:
                        team = Team(name=team_name, project_id=project.id)
                        db.session.add(team)
                        db.session.flush()
                        ic.teams_by_proj_name[(project.id, team.name)] = team
                        if project.id not in ic.first_team_by_project:
                            ic.first_team_by_project[project.id] = team
                        stats['created_teams'] += 1
                else:
                    team = ic.first_team_by_project.get(project.id)
                    if not team:
                        team = Team(name="Default", project_id=project.id)
                        db.session.add(team)
                        db.session.flush()
                        ic.teams_by_proj_name[(project.id, team.name)] = team
                        ic.first_team_by_project[project.id] = team
                        stats['created_teams'] += 1

                is_active = _csv_row_active_flag(row, mapping)
                team_member = ic.members_by_pylon.get(pylon)

                if team_member:
                    if not is_active:
                        if team_member.team_id != archiv_team.id:
                            team_member.original_team_id = team_member.team_id
                            team_member.original_project_id = team_member.team.project_id if team_member.team else None
                            team_member.team_id = archiv_team.id
                            stats['archived_members'] += 1
                    else:
                        if team_member.team_id == archiv_team.id:
                            if team_member.original_team_id:
                                team_member.team_id = team_member.original_team_id
                                team_member.original_team_id = None
                                team_member.original_project_id = None
                            else:
                                team_member.team_id = team.id
                        else:
                            team_member.team_id = team.id

                    team_member.name = full_name
                    if plt_id is not None:
                        team_member.plt_id = plt_id
                    if ma_kennung is not None:
                        team_member.ma_kennung = ma_kennung
                    if dag_id is not None:
                        team_member.dag_id = dag_id
                    stats['updated_members'] += 1

                    if team_member.user_id:
                        user = User.query.get(team_member.user_id)
                        if user and user.role_id != role.id:
                            user.role_id = role.id
                            db.session.add(user)
                else:
                    if is_active:
                        new_tid = team.id
                        orig_tid = None
                        orig_pid = None
                    else:
                        new_tid = archiv_team.id
                        orig_tid = team.id
                        orig_pid = project.id
                        stats['archived_members'] += 1
                    team_member = TeamMember(
                        name=full_name,
                        team_id=new_tid,
                        original_team_id=orig_tid,
                        original_project_id=orig_pid,
                        pylon=pylon,
                        plt_id=plt_id,
                        ma_kennung=ma_kennung,
                        dag_id=dag_id
                    )
                    db.session.add(team_member)
                    db.session.flush()
                    ic.members_by_pylon[pylon] = team_member
                    stats['created_members'] += 1

                    if not team_member.user_id:
                        first_part = first_name[:4].lower() if first_name else ''
                        last_part = last_name.lower() if last_name else ''
                        username_base = f"{first_part}{last_part}"
                        username_base = ''.join(c for c in username_base if c.isalnum() or c == '.')
                        if not username_base:
                            if email:
                                username_base = email.split('@')[0]
                            else:
                                username_base = pylon.lower()
                        username = username_base
                        existing = User.query.filter_by(username=username).first()
                        counter = 1
                        orig_username = username
                        while existing:
                            username = f"{orig_username}{counter}"
                            existing = User.query.filter_by(username=username).first()
                            counter += 1

                        user = User(
                            username=username,
                            email=email,
                            role_id=role.id,
                            project_id=project.id
                        )
                        user.set_password("Start123")
                        db.session.add(user)
                        db.session.flush()
                        team_member.user_id = user.id
                        stats['created_users'] += 1

                batch.append(team_member)
                if len(batch) >= BATCH_SIZE:
                    db.session.commit()
                    batch = []

            except Exception as e:
                db.session.rollback()
                current_app.logger.error(f"CSV Import Zeile ~{processed}: {e}")
                stats['errors'] += 1
                continue

    if batch:
        db.session.commit()
    return stats


# --- CSV Sync Route ---
@bp.route('/sync_from_csv', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def sync_from_csv():
    # Step 1: Upload CSV and preview
    if request.method == 'POST' and 'csv_file' in request.files:
        file = request.files['csv_file']
        if not file or not file.filename.endswith('.csv'):
            flash('Bitte eine CSV-Datei hochladen.', 'danger')
            return redirect(url_for('admin.sync_from_csv'))

        delimiter = request.form.get('delimiter', 'auto')

        prev_path = session.get('csv_temp_file')
        if prev_path:
            if os.path.isfile(prev_path):
                try:
                    os.unlink(prev_path)
                except OSError:
                    pass
            _remove_csv_preview_sidecar(prev_path)
        session.pop('csv_preview_truncated', None)
        _remove_baseline_csv_session(session)

        # Save uploaded file to a temporary file
        temp_fd, temp_path = tempfile.mkstemp(suffix='.csv')
        os.close(temp_fd)
        file.save(temp_path)
        
        # Read first few rows for preview
        with open(temp_path, 'r', encoding='utf-8-sig') as f:
            sample = f.read(1024)
            f.seek(0)
            if delimiter == 'auto':
                delimiter = ';' if ';' in sample else ','
            reader = csv.DictReader(f, delimiter=delimiter)
            headers = reader.fieldnames
            if headers and headers[0] == '':
                headers = headers[1:]
            
            preview_rows = []
            for row in reader:
                if row.get('Pylon-Nr', '').strip():
                    if '' in row:
                        del row['']
                    preview_rows.append(row)
                    if len(preview_rows) >= 5:
                        break
        
        # Store temp file path and delimiter in session
        session['csv_temp_file'] = temp_path
        session['csv_delimiter'] = delimiter

        mapping = {
            'pylon': 'Pylon-Nr',
            'plt_id': 'PLT-ID',
            'first_name': 'Vorname',
            'last_name': 'Nachname',
            'project': 'Projekt Schichtplan',
            'team': 'Team',
            'ma_kennung': 'MA-Kennung',
            'dag_id': 'DAG-ID',
            'email': 'eMail',
            'active_status': 'PLT aktiv?',
            'agent_status': 'Agent-Status',
            'role': 'Agent-Status'
        }

        return render_template(
            'admin/csv_mapping.html',
            headers=headers,
            preview_rows=preview_rows,
            mapping=mapping,
            delimiter=delimiter,
            config=current_app.config,
        )

    # Schritt 2a: Vorschau bauen (keine Änderungen an der DB)
    if request.method == 'POST' and 'preview_import' in request.form:
        mapping = _csv_mapping_from_request(request.form)
        if not mapping.get('pylon'):
            flash('Pylon-Nr muss zugeordnet sein.', 'danger')
            return redirect(url_for('admin.sync_from_csv'))
        delimiter = session.get('csv_delimiter', ';')
        temp_path = session.get('csv_temp_file')
        if not temp_path or not os.path.exists(temp_path):
            flash('Keine CSV-Daten gefunden. Bitte erneut hochladen.', 'danger')
            return redirect(url_for('admin.sync_from_csv'))

        archiv_team = get_or_create_archiv_team()
        last_rows = _csv_collect_last_row_by_pylon(temp_path, delimiter, mapping)
        total_with_pylon = len(last_rows)
        if not total_with_pylon:
            flash('Keine Zeilen mit Pylon-Nr in der CSV.', 'warning')
            return redirect(url_for('admin.sync_from_csv'))

        preview_caches = _CsvPreviewCaches(last_rows.keys())
        change_items = []
        for row in last_rows.values():
            item = _csv_build_change_item(row, mapping, archiv_team, preview_caches)
            if item:
                change_items.append(item)

        if not change_items:
            flash('Keine Abweichungen zur Live-Datenbank — der Import würde nichts ändern.', 'info')
            return redirect(url_for('admin.sync_from_csv'))

        preview_truncated = len(change_items) > CSV_PREVIEW_MAX_ROWS
        preview_items = change_items[:CSV_PREVIEW_MAX_ROWS]
        preview_pylons = [x['pylon'] for x in preview_items]
        sidecar = _csv_preview_sidecar_path(temp_path)
        if preview_truncated:
            session['csv_preview_truncated'] = True
            with open(sidecar, 'w', encoding='utf-8') as sf:
                json.dump({'preview_pylons': preview_pylons, 'total_changes': len(change_items)}, sf)
            flash(
                f'{len(change_items):,} Änderungen erkannt. Es werden die ersten {len(preview_items):,} zur Prüfung angezeigt. '
                f'Weitere Änderungen sind in dieser Runde nicht zur Auswahl — bitte danach erneut synchronisieren.',
                'warning',
            )
        else:
            session.pop('csv_preview_truncated', None)
            _remove_csv_preview_sidecar(temp_path)

        grouped = _group_csv_preview_items(preview_items)
        large_ui = len(change_items) >= CSV_PREVIEW_LARGE_UI_THRESHOLD
        return render_template(
            'admin/csv_import_preview.html',
            grouped=grouped,
            mapping=mapping,
            map_fields=CSV_IMPORT_MAP_FIELDS,
            delimiter=delimiter,
            preview_count=len(preview_items),
            total_csv_rows_with_pylon=total_with_pylon,
            total_changes=len(change_items),
            preview_truncated=preview_truncated,
            large_ui=large_ui,
            preview_max_rows=CSV_PREVIEW_MAX_ROWS,
            config=current_app.config,
        )

    # Schritt 2b: Nur ausgewählte Pylonen importieren
    if request.method == 'POST' and 'apply_import' in request.form:
        mapping = _csv_mapping_from_request(request.form)
        if not mapping.get('pylon'):
            flash('Ungültige Import-Anfrage (Zuordnung fehlt).', 'danger')
            return redirect(url_for('admin.sync_from_csv'))
        included = set(request.form.getlist('include_pylon'))
        delimiter = session.get('csv_delimiter', ';')
        temp_path = session.get('csv_temp_file')
        if not temp_path or not os.path.exists(temp_path):
            flash('Keine CSV-Daten gefunden. Bitte erneut hochladen.', 'danger')
            return redirect(url_for('admin.sync_from_csv'))

        preview_was_truncated = session.pop('csv_preview_truncated', False)
        sidecar = _csv_preview_sidecar_path(temp_path)
        extra_changes_not_shown = 0
        if preview_was_truncated:
            if os.path.isfile(sidecar):
                with open(sidecar, encoding='utf-8') as sf:
                    meta = json.load(sf)
                if isinstance(meta, dict):
                    extra_changes_not_shown = max(
                        0,
                        int(meta.get('total_changes', 0)) - len(meta.get('preview_pylons', [])),
                    )
                try:
                    os.unlink(sidecar)
                except OSError:
                    pass
            else:
                flash(
                    'Vorschau-Metadaten fehlten; es wurden nur die angehakten Änderungen importiert.',
                    'warning',
                )
        else:
            _remove_csv_preview_sidecar(temp_path)

        if not included:
            flash('Keine Zeilen ausgewählt.', 'warning')
            return redirect(url_for('admin.sync_from_csv'))

        archiv_team = get_or_create_archiv_team()
        stats = _run_csv_import_with_row_filter(temp_path, delimiter, mapping, archiv_team, included)
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        session.pop('csv_temp_file', None)
        _remove_baseline_csv_session(session)

        msg = (
            f'Import abgeschlossen: {stats["created_members"]} neu, {stats["updated_members"]} aktualisiert, '
            f'{stats["archived_members"]} archiviert, {stats["created_projects"]} Projekte, {stats["created_teams"]} Teams, '
            f'{stats["created_users"]} Benutzer, {stats["created_roles"]} neue Rollen, {stats["errors"]} Fehler, '
            f'{stats["skipped"]} CSV-Zeilen übersprungen (nicht zur Übernahme gewählt oder ohne Pylon).'
        )
        if extra_changes_not_shown:
            msg += f' Achtung: {extra_changes_not_shown:,} weitere Änderungen waren nicht in der Liste — bitte erneut synchronisieren.'
        flash(msg, 'success')
        return redirect(url_for('admin.sync_from_csv'))

    return render_template('admin/sync_from_csv.html', config=current_app.config)


# =====================================================================
# KPI (Demo) raw-data import  — standalone flow (separate from member sync)
# =====================================================================

_IMPORT_JOB_DIR = os.path.join(tempfile.gettempdir(), 'coaching_import_jobs')


def _import_job_path(job_id):
    return os.path.join(_IMPORT_JOB_DIR, f'{job_id}.json')


def _import_job_write(job_id, user_id, payload):
    os.makedirs(_IMPORT_JOB_DIR, exist_ok=True)
    data = {**payload, 'user_id': user_id, 'updated_at': time.time()}
    with open(_import_job_path(job_id), 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False)


def _import_job_read(job_id, user_id):
    path = _import_job_path(job_id)
    if not os.path.isfile(path):
        return None
    with open(path, 'r', encoding='utf-8') as fh:
        data = json.load(fh)
    if data.get('user_id') != user_id:
        return None
    return data


def _import_job_delete(job_id):
    path = _import_job_path(job_id)
    if os.path.isfile(path):
        try:
            os.unlink(path)
        except OSError:
            pass


def _import_json_temp(data):
    fd, path = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False)
    return path


def _spawn_revert_worker(command, batch_id, job_id, user_id):
    """Run revert in a separate OS process so gunicorn workers stay free."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    subprocess.Popen(
        [
            sys.executable, '-m', 'app.import_worker', command,
            str(batch_id), job_id, str(user_id),
        ],
        cwd=root,
        env=os.environ.copy(),
        start_new_session=True,
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _kpi_parse_date(value):
    s = (value or '').strip()
    if not s:
        return None
    for fmt in ('%d.%m.%Y', '%Y-%m-%d', '%d.%m.%y'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _kpi_detect_encoding(temp_path):
    """Pick a working text encoding (export tools often use cp1252/Latin-1, not UTF-8)."""
    for enc in ('utf-8-sig', 'utf-8', 'cp1252', 'latin-1'):
        try:
            with open(temp_path, 'r', encoding=enc) as fh:
                fh.read()
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return 'latin-1'  # decodes any byte sequence, used as last resort


def _kpi_normalize_row(row):
    """Match CSV headers case-insensitively (exports use e.g. Datensatz_id, Frage, Antwort)."""
    return {(k or '').strip().lstrip('\ufeff').lower(): v for k, v in row.items()}


def _kpi_read_surveys(temp_path):
    """Parse the KPI CSV grouped by datensatz_id. Returns list of survey dicts (no DB)."""
    surveys = {}
    order = []
    encoding = _kpi_detect_encoding(temp_path)
    with open(temp_path, 'r', encoding=encoding) as f:
        sample = f.read(4096)
        f.seek(0)
        delimiter = ';' if ';' in sample else ','
        reader = csv.DictReader(f, delimiter=delimiter)
        for raw_row in reader:
            row = _kpi_normalize_row(raw_row)
            dsid = (row.get('datensatz_id') or '').strip()
            if not dsid:
                continue
            s = surveys.get(dsid)
            if s is None:
                s = {
                    'datensatz_id': dsid[:64],
                    'interviewnummer': (row.get('interviewnummer') or '').strip()[:64],
                    'antwort_date': _kpi_parse_date(row.get('antwortdatum')),
                    'kontakt_date': _kpi_parse_date(row.get('kontaktdatum')),
                    'be4': kpi_logic.normalize_text(row.get('be4')),
                    'ma_kenner': kpi_logic.normalize_text(row.get('ma_kenner')),
                    'ospname': kpi_logic.normalize_text(row.get('ospname'))[:100],
                    'kampagne': kpi_logic.normalize_text(row.get('kampagne'))[:150],
                    'studie': kpi_logic.normalize_text(row.get('studie'))[:150],
                    'queue': kpi_logic.normalize_text(row.get('queue'))[:150],
                    'vorname': kpi_logic.normalize_text(row.get('vorname'))[:100],
                    'nachname': kpi_logic.normalize_text(row.get('nachname'))[:100],
                    'answers': [],
                    'nps_value': None,
                    'loesung_answer': None,
                    'info_positive': None,
                    'loesung_positive': None,
                    'fachkompetenz_stars': None,
                    'vertrieb_positive': None,
                }
                surveys[dsid] = s
                order.append(dsid)
            frage = row.get('frage') or ''
            antwort = row.get('antwort') or ''
            code = kpi_logic.question_code(frage)
            s['answers'].append({
                'code': code,
                'text': kpi_logic.question_text(frage),
                'antwort': kpi_logic.normalize_text(antwort),
            })
    # Default flags via auto-detection (no per-project mapping yet).
    result = [surveys[d] for d in order]
    for s in result:
        s['answers'] = _kpi_dedupe_answers_by_code(s['answers'])
        nps_v, loes_a, info_p, loes_p, fach_s, vert_p = kpi_logic.compute_survey_flags(s['answers'])
        s['nps_value'] = nps_v
        s['loesung_answer'] = loes_a
        s['info_positive'] = info_p
        s['loesung_positive'] = loes_p
        s['fachkompetenz_stars'] = fach_s
        s['vertrieb_positive'] = vert_p
    return result


def _kpi_apply_mappings(surveys):
    """Override default KPI flags using each survey's per-project question mapping.

    Surveys whose project has no mapping for their survey type keep the
    auto-detected defaults.
    """
    mappings = {}
    for m in KpiQuestionMapping.query.all():
        bucket = mappings.setdefault((m.project_id, m.survey_type), {})
        bucket[m.kpi_kind] = m.frage_code
    if not mappings:
        return
    for s in surveys:
        cfg = mappings.get((s.get('project_id'), s.get('studie')))
        if not cfg:
            continue
        nps_v, loes_a, info_p, loes_p, fach_s, vert_p = kpi_logic.compute_survey_flags(
            s['answers'],
            nps_code=cfg.get('nps'),
            loesung_code=cfg.get('loesung'),
            fachkompetenz_code=cfg.get('fachkompetenz'),
            vertrieb_code=cfg.get('vertrieb'),
        )
        s['nps_value'] = nps_v
        s['loesung_answer'] = loes_a
        s['info_positive'] = info_p
        s['loesung_positive'] = loes_p
        s['fachkompetenz_stars'] = fach_s
        s['vertrieb_positive'] = vert_p


def _kpi_resolve_links(surveys):
    """Resolve be4 -> team/project (globally unique name) and ma_kenner -> member."""
    team_map = {}
    for t in Team.query.all():
        if t.name:
            team_map.setdefault(t.name.strip(), (t.id, t.project_id))
    member_map = {}
    name_map = {}
    archiv_team = get_or_create_archiv_team()
    for m in TeamMember.query.options(joinedload(TeamMember.team)).all():
        if m.ma_kennung:
            member_map.setdefault(m.ma_kennung.strip(), []).append((m.id, m.team_id))
        if m.name:
            name_map.setdefault(m.name.strip().lower(), []).append(m)

    def _name_candidates(full_name, team_id):
        """Existing members matching a CSV name, preferring the resolved team; ARCHIV last."""
        members = name_map.get((full_name or '').strip().lower(), [])
        out = []
        for m in members:
            out.append({
                'id': m.id,
                'name': m.name,
                'team_name': m.team.name if m.team else '-',
                'has_kennung': bool(m.ma_kennung),
                'ma_kennung': m.ma_kennung or '',
                'in_resolved_team': (team_id is not None and m.team_id == team_id),
                'is_archiv': (m.team_id == archiv_team.id),
            })
        out.sort(key=lambda c: (not c['in_resolved_team'], c['is_archiv'], c['name']))
        return out[:5]

    matched_team = matched_member = unassigned = 0
    unknown_be4 = set()
    unknown_ma = set()
    unknown_ma_details = {}
    for s in surveys:
        tid = pid = None
        be4 = (s['be4'] or '').strip()
        if be4 and be4 in team_map:
            tid, pid = team_map[be4]
            matched_team += 1
        elif be4:
            unknown_be4.add(be4)
        s['team_id'] = tid
        s['project_id'] = pid

        mid = None
        cands = member_map.get((s['ma_kenner'] or '').strip())
        if cands:
            if tid is not None:
                for cmid, cmt in cands:
                    if cmt == tid:
                        mid = cmid
                        break
            if mid is None:
                mid = cands[0][0]
            matched_member += 1
        elif s['ma_kenner']:
            unknown_ma.add(s['ma_kenner'])
            key = s['ma_kenner'].strip()
            info = unknown_ma_details.get(key)
            if info is None:
                full_name = (str(s['vorname'] or '') + ' ' + str(s['nachname'] or '')).strip()
                unknown_ma_details[key] = {
                    'ma_kenner': key,
                    'name': full_name or '–',
                    'be4': be4 or '–',
                    'count': 1,
                    'candidates': _name_candidates(full_name, tid),
                }
            else:
                info['count'] += 1
        s['team_member_id'] = mid
        if mid is not None and pid is None:
            m = TeamMember.query.options(joinedload(TeamMember.team)).get(mid)
            if m and m.team:
                s['team_id'] = m.team_id
                s['project_id'] = m.team.project_id

        if tid is None:
            unassigned += 1
    return {
        'matched_team': matched_team,
        'matched_member': matched_member,
        'unassigned': unassigned,
        'unknown_be4': sorted(unknown_be4),
        'unknown_ma': sorted(unknown_ma),
        'unknown_ma_details': sorted(
            unknown_ma_details.values(), key=lambda d: (-d['count'], d['ma_kenner'])
        ),
    }


_EXCEL_CELL_ERRORS = frozenset({
    '#NAME?', '#REF!', '#VALUE!', '#N/A', '#DIV/0!', '#NULL?', '#NUM!',
})


def _kpi_is_excel_error(value):
    v = (value or '').strip().upper()
    if v in _EXCEL_CELL_ERRORS:
        return True
    return bool(v.startswith('#') and v.endswith('?'))


def _kpi_merge_answer_entry(a, b):
    """When duplicate question codes exist, prefer real text over Excel placeholders."""
    a_err = _kpi_is_excel_error(a.get('antwort'))
    b_err = _kpi_is_excel_error(b.get('antwort'))
    if a_err and not b_err:
        return b
    if b_err and not a_err:
        return a
    if not a_err and not b_err and len(b.get('antwort') or '') > len(a.get('antwort') or ''):
        return {**b, 'text': b.get('text') or a.get('text')}
    return {**a, 'text': a.get('text') or b.get('text')}


def _kpi_dedupe_answers_by_code(answers):
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
        prev = by_code.get(code)
        by_code[code] = _kpi_merge_answer_entry(prev, entry) if prev else entry
    return list(by_code.values())


def _kpi_answers_key_from_rows(answers):
    if not answers:
        return ()
    deduped = _kpi_dedupe_answers_by_code(answers)
    return tuple(sorted((a['code'], a['antwort']) for a in deduped))


def _kpi_survey_snapshot_from_dict(s):
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
        'answers': _kpi_answers_key_from_rows(s.get('answers') or []),
    }


def _kpi_survey_snapshot_from_db(sv, answers_key):
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


_KPI_DIFF_LABELS = (
    ('antwort_date', 'Antwortdatum'),
    ('be4', 'Team (be4)'),
    ('ma_kenner', 'MA-Kennung'),
    ('studie', 'Survey-Typ'),
    ('nps_value', 'NPS'),
    ('loesung_answer', 'Lösungsantwort'),
    ('info_positive', 'Informationsquote'),
    ('loesung_positive', 'Lösungsquote'),
    ('fachkompetenz_stars', 'Fachkompetenz'),
    ('vertrieb_positive', 'Vertriebliche Ansprache'),
    ('answers', 'Antworten'),
)


def _kpi_diff_fields(incoming_snap, existing_snap):
    diffs = []
    for key, label in _KPI_DIFF_LABELS:
        if incoming_snap.get(key) != existing_snap.get(key):
            diffs.append(label)
    return diffs


def _kpi_display_field_value(key, value):
    if key == 'antwort_date':
        return value.strftime('%d.%m.%Y') if value else '–'
    if key in ('info_positive', 'loesung_positive', 'vertrieb_positive'):
        if value is None:
            return '–'
        return 'Ja' if value else 'Nein'
    if value is None or value == '':
        return '–'
    return str(value)


def _kpi_answers_only_excel_noise(incoming_answers, db_answers):
    """True when answer diffs are only CSV-side Excel errors (#NAME? etc.) vs real DB text."""
    in_map = {a['code']: a['antwort'] for a in _kpi_dedupe_answers_by_code(incoming_answers)}
    ex_map = {a['code']: a['antwort'] for a in _kpi_dedupe_answers_by_code(db_answers)}
    has_diff = False
    for code in set(in_map) | set(ex_map):
        inc = in_map.get(code, '')
        exc = ex_map.get(code, '')
        if inc == exc:
            continue
        has_diff = True
        if not (_kpi_is_excel_error(inc) and exc and not _kpi_is_excel_error(exc)):
            return False
    return has_diff


def _kpi_surveys_data_equal(incoming, existing_snap, ex_rich_answers):
    """Compare incoming survey dict with DB snapshot; ignore Excel-error-only answer noise."""
    in_snap = _kpi_survey_snapshot_from_dict(incoming)
    if _kpi_build_field_diff_details(in_snap, existing_snap):
        return False
    if in_snap.get('answers') == existing_snap.get('answers'):
        return True
    return _kpi_answers_only_excel_noise(incoming.get('answers'), ex_rich_answers)


def _kpi_build_field_diff_details(incoming_snap, existing_snap):
    details = []
    for key, label in _KPI_DIFF_LABELS:
        if key == 'answers':
            continue
        if incoming_snap.get(key) != existing_snap.get(key):
            details.append({
                'key': key,
                'label': label,
                'before': _kpi_display_field_value(key, existing_snap.get(key)),
                'after': _kpi_display_field_value(key, incoming_snap.get(key)),
            })
    return details


def _kpi_load_rich_answers_by_survey_id(survey_ids):
    if not survey_ids:
        return {}
    buckets = {}
    rows = db.session.query(
        KpiAnswer.survey_id, KpiAnswer.frage_code, KpiAnswer.frage_text, KpiAnswer.antwort,
    ).filter(KpiAnswer.survey_id.in_(survey_ids)).all()
    for sid, code, ftext, antwort in rows:
        buckets.setdefault(sid, []).append({
            'code': (code or '').strip(),
            'text': (ftext or '').strip(),
            'antwort': (antwort or '').strip(),
        })
    return buckets


def _kpi_build_answer_diff_rows(incoming_answers, db_answers):
    in_deduped = _kpi_dedupe_answers_by_code(incoming_answers)
    ex_deduped = _kpi_dedupe_answers_by_code(db_answers)
    in_by_code = {a['code']: a for a in in_deduped}
    ex_by_code = {a['code']: a for a in ex_deduped}

    rows = []
    for code in sorted(set(in_by_code) | set(ex_by_code)):
        inc = in_by_code.get(code)
        ex = ex_by_code.get(code)
        before = (ex or {}).get('antwort') or '–'
        after = (inc or {}).get('antwort') or '–'
        if inc and ex and before == after:
            continue
        label = (inc or ex or {}).get('text') or code
        if inc and ex:
            kind = 'changed'
        elif inc:
            kind = 'new'
        else:
            kind = 'removed'
        rows.append({
            'code': code,
            'text': label[:100],
            'before': before,
            'after': after,
            'kind': kind,
            'after_is_excel_error': bool(inc and _kpi_is_excel_error(after)),
        })
    return rows


def _kpi_load_answers_by_survey_id(survey_ids):
    if not survey_ids:
        return {}
    buckets = {}
    rows = db.session.query(
        KpiAnswer.survey_id, KpiAnswer.frage_code, KpiAnswer.antwort,
    ).filter(KpiAnswer.survey_id.in_(survey_ids)).all()
    for sid, code, antwort in rows:
        buckets.setdefault(sid, []).append(((code or '').strip(), (antwort or '').strip()))
    return {sid: tuple(sorted(items)) for sid, items in buckets.items()}


def _kpi_analyze_import_conflicts(surveys):
    """Compare incoming surveys with DB: new, unchanged, changed, range orphans."""
    dates = [s['antwort_date'] for s in surveys if s['antwort_date']]
    date_from = min(dates) if dates else None
    date_to = max(dates) if dates else None
    incoming_by_dsid = {s['datensatz_id']: s for s in surveys}
    incoming_dsids = set(incoming_by_dsid)

    existing_rows = []
    if incoming_dsids:
        existing_rows = KpiSurvey.query.filter(
            KpiSurvey.datensatz_id.in_(incoming_dsids),
        ).all()
    existing_by_dsid = {sv.datensatz_id: sv for sv in existing_rows}
    survey_ids = [sv.id for sv in existing_rows]
    answers_by_id = _kpi_load_answers_by_survey_id(survey_ids)
    rich_answers_by_id = _kpi_load_rich_answers_by_survey_id(survey_ids)

    new_count = unchanged_count = changed_count = 0
    changed_samples = []
    for dsid, incoming in incoming_by_dsid.items():
        existing = existing_by_dsid.get(dsid)
        if not existing:
            new_count += 1
            continue
        in_snap = _kpi_survey_snapshot_from_dict(incoming)
        ex_snap = _kpi_survey_snapshot_from_db(existing, answers_by_id.get(existing.id))
        ex_rich = rich_answers_by_id.get(existing.id, [])
        if _kpi_surveys_data_equal(incoming, ex_snap, ex_rich):
            unchanged_count += 1
        else:
            changed_count += 1
            diffs = _kpi_diff_fields(in_snap, ex_snap)
            if len(changed_samples) < 15:
                answer_diffs = []
                if in_snap.get('answers') != ex_snap.get('answers'):
                    answer_diffs = _kpi_build_answer_diff_rows(
                        incoming.get('answers'),
                        ex_rich,
                    )
                changed_samples.append({
                    'datensatz_id': dsid,
                    'antwort_date': incoming.get('antwort_date'),
                    'existing_date': existing.antwort_date,
                    'be4': (incoming.get('be4') or '')[:40],
                    'diffs': diffs,
                    'diff_details': _kpi_build_field_diff_details(in_snap, ex_snap),
                    'answer_diffs': answer_diffs,
                })

    range_rows = []
    if date_from and date_to:
        range_rows = KpiSurvey.query.filter(
            KpiSurvey.antwort_date >= date_from,
            KpiSurvey.antwort_date <= date_to,
        ).all()
    existing_in_range_count = len(range_rows)
    orphan_rows = [sv for sv in range_rows if sv.datensatz_id not in incoming_dsids]
    orphan_in_range_count = len(orphan_rows)
    orphan_samples = [
        {
            'datensatz_id': sv.datensatz_id,
            'antwort_date': sv.antwort_date,
            'be4': (sv.be4 or '')[:40],
        }
        for sv in orphan_rows[:10]
    ]

    has_existing = bool(existing_by_dsid or range_rows)
    needs_overwrite_choice = (
        changed_count > 0 or unchanged_count > 0 or orphan_in_range_count > 0
    )
    excel_error_count = sum(
        1 for s in surveys for a in (s.get('answers') or []) if _kpi_is_excel_error(a.get('antwort'))
    )

    return {
        'date_from': date_from,
        'date_to': date_to,
        'date_from_fmt': date_from.strftime('%d.%m.%Y') if date_from else None,
        'date_to_fmt': date_to.strftime('%d.%m.%Y') if date_to else None,
        'new_count': new_count,
        'unchanged_count': unchanged_count,
        'changed_count': changed_count,
        'changed_samples': changed_samples,
        'existing_in_range_count': existing_in_range_count,
        'orphan_in_range_count': orphan_in_range_count,
        'orphan_samples': orphan_samples,
        'has_existing': has_existing,
        'needs_overwrite_choice': needs_overwrite_choice,
        'excel_error_count': excel_error_count,
    }


def _kpi_insert_survey(batch_id, s):
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


def _kpi_commit(filename, surveys, stats, overwrite=False, progress_cb=None):
    """Commit KPI import. overwrite=True replaces date range + updates existing datensatz_ids."""
    total = len(surveys)

    def _progress(done, message):
        if progress_cb:
            progress_cb(done, total, message)

    _progress(0, 'Konflikte prüfen…')
    conflicts = _kpi_analyze_import_conflicts(surveys)
    dates = [s['antwort_date'] for s in surveys if s['antwort_date']]
    date_from = min(dates) if dates else None
    date_to = max(dates) if dates else None

    batch = KpiImportBatch(
        filename=(filename or '')[:255],
        imported_by_id=current_user.id,
        date_from=date_from,
        date_to=date_to,
        surveys_total=len(surveys),
        surveys_matched_team=stats['matched_team'],
        surveys_matched_member=stats['matched_member'],
        surveys_unassigned=stats['unassigned'],
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

    if overwrite:
        ids_to_delete = set()
        if date_from and date_to:
            ids_to_delete.update(
                r[0] for r in db.session.query(KpiSurvey.id).filter(
                    KpiSurvey.antwort_date >= date_from,
                    KpiSurvey.antwort_date <= date_to,
                ).all()
            )
        incoming_dsids = {s['datensatz_id'] for s in surveys}
        if incoming_dsids:
            ids_to_delete.update(
                r[0] for r in db.session.query(KpiSurvey.id).filter(
                    KpiSurvey.datensatz_id.in_(incoming_dsids),
                ).all()
            )
        if ids_to_delete:
            KpiAnswer.query.filter(KpiAnswer.survey_id.in_(ids_to_delete)).delete(synchronize_session=False)
            KpiSurvey.query.filter(KpiSurvey.id.in_(ids_to_delete)).delete(synchronize_session=False)
            result['deleted'] = len(ids_to_delete)
        for i, s in enumerate(surveys):
            _kpi_insert_survey(batch.id, s)
            result['inserted'] += 1
            if i and i % 200 == 0:
                _progress(i, f'{i}/{total} Befragungen speichern…')
    else:
        existing_by_dsid = {}
        incoming_dsids = {s['datensatz_id'] for s in surveys}
        if incoming_dsids:
            for sv in KpiSurvey.query.filter(KpiSurvey.datensatz_id.in_(incoming_dsids)).all():
                existing_by_dsid[sv.datensatz_id] = sv
        answers_by_id = _kpi_load_answers_by_survey_id([sv.id for sv in existing_by_dsid.values()])
        rich_by_id = _kpi_load_rich_answers_by_survey_id([sv.id for sv in existing_by_dsid.values()])
        for i, s in enumerate(surveys):
            existing = existing_by_dsid.get(s['datensatz_id'])
            if existing:
                in_snap = _kpi_survey_snapshot_from_dict(s)
                ex_snap = _kpi_survey_snapshot_from_db(existing, answers_by_id.get(existing.id))
                if _kpi_surveys_data_equal(s, ex_snap, rich_by_id.get(existing.id, [])):
                    result['skipped_unchanged'] += 1
                else:
                    result['skipped_changed'] += 1
            else:
                _kpi_insert_survey(batch.id, s)
                result['inserted'] += 1
            if i and i % 200 == 0:
                _progress(i, f'{i}/{total} Befragungen prüfen…')

    _progress(total, 'Abschluss…')
    batch.surveys_total = result['inserted']
    _kpi_backfill_survey_links()
    db.session.commit()
    return batch, date_from, date_to, result


def _kpi_backfill_survey_links():
    """Set project_id/team_id from linked team member when be4 did not resolve."""
    rows = (
        KpiSurvey.query.filter(
            KpiSurvey.team_member_id.isnot(None),
            or_(KpiSurvey.project_id.is_(None), KpiSurvey.team_id.is_(None)),
        )
        .options(joinedload(KpiSurvey.team_member).joinedload(TeamMember.team))
        .all()
    )
    updated = 0
    for sv in rows:
        m = sv.team_member
        if not m or not m.team:
            continue
        changed = False
        if sv.project_id != m.team.project_id:
            sv.project_id = m.team.project_id
            changed = True
        if sv.team_id != m.team_id:
            sv.team_id = m.team_id
            changed = True
        if changed:
            updated += 1
    if updated:
        db.session.flush()
    return updated


def _kpi_cleanup_session_temp():
    temp_path = session.get('kpi_csv_temp_file')
    if temp_path and os.path.isfile(temp_path):
        try:
            os.unlink(temp_path)
        except OSError:
            pass
    session.pop('kpi_csv_temp_file', None)
    session.pop('kpi_csv_filename', None)
    ma_path = session.pop('kpi_unknown_ma_path', None)
    if ma_path and os.path.isfile(ma_path):
        try:
            os.unlink(ma_path)
        except OSError:
            pass


def _kpi_format_commit_flash(batch, commit_result):
    c = commit_result['conflicts']
    parts = [f'{commit_result["inserted"]} Befragungen importiert']
    if commit_result['skipped_unchanged']:
        parts.append(f'{commit_result["skipped_unchanged"]} unverändert übersprungen')
    if commit_result['skipped_changed']:
        parts.append(
            f'{commit_result["skipped_changed"]} geändert übersprungen '
            f'(zum Überschreiben beim Import „Vorhandene ersetzen“ aktivieren)'
        )
    if commit_result['deleted']:
        parts.append(f'{commit_result["deleted"]} vorhandene ersetzt/gelöscht')
    if commit_result['inserted'] == 0 and (c['changed_count'] or c['unchanged_count']):
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


def _kpi_run_import_job(app, job_id, user_id, temp_path, filename, overwrite):
    with app.app_context():
        try:
            _import_job_write(job_id, user_id, {
                'status': 'running', 'pct': 3, 'message': 'CSV lesen…',
            })
            surveys = _kpi_read_surveys(temp_path)
            stats = _kpi_resolve_links(surveys)
            _kpi_apply_mappings(surveys)

            def progress(done, total, message):
                pct = 5 + int((done / max(total, 1)) * 90)
                _import_job_write(job_id, user_id, {
                    'status': 'running', 'pct': pct, 'message': message,
                })

            batch, _df, _dt, commit_result = _kpi_commit(
                filename, surveys, stats, overwrite=overwrite, progress_cb=progress,
            )
            category, message = _kpi_format_commit_flash(batch, commit_result)
            _import_job_write(job_id, user_id, {
                'status': 'done',
                'pct': 100,
                'message': 'Import abgeschlossen.',
                'done_url': url_for('admin.import_kpi_csv_done', job_id=job_id),
                'flash_category': category,
                'flash_message': message,
            })
        except Exception as e:
            db.session.rollback()
            current_app.logger.exception('KPI import job failed')
            _import_job_write(job_id, user_id, {
                'status': 'error', 'pct': 0, 'message': str(e),
            })
        finally:
            db.session.remove()


def _prod_run_import_job(app, job_id, user_id, temp_path, filename, overwrite):
    with app.app_context():
        try:
            _import_job_write(job_id, user_id, {
                'status': 'running', 'pct': 3, 'message': 'CSV lesen…',
            })
            _headers, rows = _prod_read_csv_rows(temp_path)
            settings = productivity_logic.settings_dict(None)
            team_map, dag_map, name_map, ma_map = productivity_logic.build_link_maps(
                Team.query.all(), TeamMember.query.options(joinedload(TeamMember.team)).all(),
            )
            intervals, _m, _u, _unmatched = _prod_build_intervals(
                rows, settings, team_map, dag_map, name_map, ma_map,
            )

            def progress(done, total, message):
                pct = 5 + int((done / max(total, 1)) * 90)
                _import_job_write(job_id, user_id, {
                    'status': 'running', 'pct': pct, 'message': message,
                })

            batch, deleted = _prod_commit_intervals(
                filename, intervals, overwrite=overwrite, progress_cb=progress,
            )
            msg = f'{batch.intervals_stored} Intervalle importiert'
            if deleted:
                msg += f', {deleted} im Zeitraum ersetzt'
            msg += f'. {batch.matched_member} mit Agent, {batch.unmatched_member} ohne Zuordnung.'
            _import_job_write(job_id, user_id, {
                'status': 'done',
                'pct': 100,
                'message': 'Import abgeschlossen.',
                'done_url': url_for('admin.import_productivity_csv_done', job_id=job_id),
                'flash_category': 'success',
                'flash_message': msg,
            })
        except Exception as e:
            db.session.rollback()
            current_app.logger.exception('Productivity import job failed')
            _import_job_write(job_id, user_id, {
                'status': 'error', 'pct': 0, 'message': str(e),
            })
        finally:
            db.session.remove()


@bp.route('/import_kpi_csv', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def import_kpi_csv():
    # Step 1: upload + preview
    if request.method == 'POST' and 'kpi_csv_file' in request.files:
        file = request.files['kpi_csv_file']
        if not file or not file.filename.lower().endswith('.csv'):
            flash('Bitte eine CSV-Datei hochladen.', 'danger')
            return redirect(url_for('admin.import_kpi_csv'))

        _kpi_cleanup_session_temp()
        fd, temp_path = tempfile.mkstemp(suffix='.csv')
        os.close(fd)
        file.save(temp_path)
        session['kpi_csv_temp_file'] = temp_path
        session['kpi_csv_filename'] = file.filename

        try:
            surveys = _kpi_read_surveys(temp_path)
            stats = _kpi_resolve_links(surveys)
            _kpi_apply_mappings(surveys)
        except Exception as e:
            flash(f'CSV konnte nicht gelesen werden: {e}', 'danger')
            _kpi_cleanup_session_temp()
            return redirect(url_for('admin.import_kpi_csv'))

        if not surveys:
            flash(
                'Keine Datensätze mit "datensatz_id" in der CSV gefunden. '
                'Erwartet wird eine Spalte datensatz_id (Groß/Kleinschreibung egal).',
                'warning',
            )
            _kpi_cleanup_session_temp()
            return redirect(url_for('admin.import_kpi_csv'))

        dates = [s['antwort_date'] for s in surveys if s['antwort_date']]
        conflicts = _kpi_analyze_import_conflicts(surveys)
        unknown_ma_path = None
        if stats['unknown_ma_details']:
            unknown_ma_path = _import_json_temp(stats['unknown_ma_details'])
            session['kpi_unknown_ma_path'] = unknown_ma_path
        else:
            session.pop('kpi_unknown_ma_path', None)
        ma_page = 1
        ma_per_page = 50
        ma_details_page = stats['unknown_ma_details'][:ma_per_page]
        ma_total_pages = max(1, (len(stats['unknown_ma_details']) + ma_per_page - 1) // ma_per_page) if stats['unknown_ma_details'] else 1
        preview = {
            'total': len(surveys),
            'matched_team': stats['matched_team'],
            'matched_member': stats['matched_member'],
            'unassigned': stats['unassigned'],
            'date_from': conflicts['date_from_fmt'],
            'date_to': conflicts['date_to_fmt'],
            'nps_count': sum(1 for s in surveys if s['nps_value'] is not None),
            'loes_count': sum(1 for s in surveys if s['loesung_answer']),
            'no_date': sum(1 for s in surveys if not s['antwort_date']),
            'unknown_be4': stats['unknown_be4'][:30],
            'unknown_be4_count': len(stats['unknown_be4']),
            'unknown_ma_count': len(stats['unknown_ma']),
            'unknown_ma_details': ma_details_page,
            'unknown_ma_page': ma_page,
            'unknown_ma_total_pages': ma_total_pages,
            'conflicts': conflicts,
        }
        return render_template(
            'admin/import_kpi_preview.html',
            preview=preview,
            filename=file.filename,
            config=current_app.config,
        )

    # Step 2: confirm + commit
    if request.method == 'POST' and request.form.get('action') == 'confirm':
        temp_path = session.get('kpi_csv_temp_file')
        filename = session.get('kpi_csv_filename', 'import.csv')
        if not temp_path or not os.path.exists(temp_path):
            flash('Keine KPI-CSV-Daten gefunden. Bitte erneut hochladen.', 'danger')
            return redirect(url_for('admin.import_kpi_csv'))
        try:
            surveys = _kpi_read_surveys(temp_path)
            stats = _kpi_resolve_links(surveys)
            _kpi_apply_mappings(surveys)
            overwrite = request.form.get('confirm_overwrite') == '1'
            batch, date_from, date_to, commit_result = _kpi_commit(
                filename, surveys, stats, overwrite=overwrite,
            )
        except Exception as e:
            db.session.rollback()
            flash(f'Import fehlgeschlagen: {e}', 'danger')
            return redirect(url_for('admin.import_kpi_csv'))
        _kpi_cleanup_session_temp()
        c = commit_result['conflicts']
        parts = [f'{commit_result["inserted"]} Befragungen importiert']
        if commit_result['skipped_unchanged']:
            parts.append(f'{commit_result["skipped_unchanged"]} unverändert übersprungen')
        if commit_result['skipped_changed']:
            parts.append(
                f'{commit_result["skipped_changed"]} geändert übersprungen '
                f'(zum Überschreiben beim Import „Vorhandene ersetzen“ aktivieren)'
            )
        if commit_result['deleted']:
            parts.append(f'{commit_result["deleted"]} vorhandene ersetzt/gelöscht')
        if commit_result['inserted'] == 0 and (c['changed_count'] or c['unchanged_count']):
            flash(
                'Keine neuen Befragungen importiert. Alle Datensätze existieren bereits. '
                'Aktivieren Sie „Vorhandene KPI-Daten überschreiben“, um geänderte oder '
                'den Zeitraum zu ersetzen.',
                'warning',
            )
        else:
            flash(
                f'KPI-Import abgeschlossen: {", ".join(parts)}. '
                f'{batch.surveys_matched_team} mit Team, {batch.surveys_matched_member} mit Agent, '
                f'{batch.surveys_unassigned} ohne Team (unassigned).',
                'success',
            )
        return redirect(url_for('admin.import_kpi_csv'))

    recent_batches = KpiImportBatch.query.order_by(desc(KpiImportBatch.imported_at)).limit(10).all()
    return render_template(
        'admin/import_kpi_csv.html',
        recent_batches=recent_batches,
        config=current_app.config,
    )


@bp.route('/import_kpi_csv/preview/ma')
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def import_kpi_preview_ma_page():
    """Paginated MA-Kennung list for KPI import preview."""
    path = session.get('kpi_unknown_ma_path')
    if not path or not os.path.isfile(path):
        flash('Keine MA-Liste in der Session. Bitte CSV erneut hochladen.', 'warning')
        return redirect(url_for('admin.import_kpi_csv'))
    page = request.args.get('page', 1, type=int)
    per_page = 50
    with open(path, 'r', encoding='utf-8') as fh:
        all_rows = json.load(fh)
    total_pages = max(1, (len(all_rows) + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    rows = all_rows[start:start + per_page]
    return render_template(
        'admin/import_kpi_preview_ma.html',
        rows=rows,
        page=page,
        total_pages=total_pages,
        total=len(all_rows),
        config=current_app.config,
    )


@bp.route('/import_kpi_csv/job/start', methods=['POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def import_kpi_csv_job_start():
    temp_path = session.get('kpi_csv_temp_file')
    filename = session.get('kpi_csv_filename', 'import.csv')
    if not temp_path or not os.path.exists(temp_path):
        return jsonify({'error': 'Keine KPI-CSV-Daten gefunden. Bitte erneut hochladen.'}), 400
    overwrite = request.form.get('confirm_overwrite') == '1'
    job_id = uuid.uuid4().hex
    _import_job_write(job_id, current_user.id, {
        'status': 'pending', 'pct': 0, 'message': 'Import wird gestartet…',
    })
    app = current_app._get_current_object()
    thread = threading.Thread(
        target=_kpi_run_import_job,
        args=(app, job_id, current_user.id, temp_path, filename, overwrite),
        daemon=True,
    )
    thread.start()
    return jsonify({'job_id': job_id})


@bp.route('/import_kpi_csv/job/<job_id>/status')
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def import_kpi_csv_job_status(job_id):
    job = _import_job_read(job_id, current_user.id)
    if not job:
        return jsonify({'status': 'missing'}), 404
    return jsonify({
        'status': job.get('status'),
        'pct': job.get('pct', 0),
        'message': job.get('message', ''),
        'done_url': job.get('done_url'),
        'error': job.get('message') if job.get('status') == 'error' else None,
    })


@bp.route('/import_kpi_csv/done/<job_id>')
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def import_kpi_csv_done(job_id):
    job = _import_job_read(job_id, current_user.id)
    if job and job.get('flash_message'):
        flash(job['flash_message'], job.get('flash_category', 'success'))
    _import_job_delete(job_id)
    _kpi_cleanup_session_temp()
    session.pop('kpi_unknown_ma_path', None)
    return redirect(url_for('admin.import_kpi_csv'))


@bp.route('/import_kpi_csv/revert/<int:batch_id>/start', methods=['POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def revert_kpi_import_start(batch_id):
    if not KpiImportBatch.query.get(batch_id):
        return jsonify({'error': f'Import-Batch #{batch_id} nicht gefunden.'}), 404
    job_id = uuid.uuid4().hex
    _import_job_write(job_id, current_user.id, {
        'status': 'pending', 'pct': 0, 'message': 'Rückgängig machen wird gestartet…',
    })
    _spawn_revert_worker('kpi_revert', batch_id, job_id, current_user.id)
    return jsonify({'job_id': job_id})


@bp.route('/import_kpi_csv/revert/status/<job_id>')
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def import_kpi_csv_revert_status(job_id):
    """Poll page if revert was started without JS."""
    return render_template(
        'admin/import_job_status.html',
        job_id=job_id,
        status_url=url_for('admin.import_kpi_csv_job_status', job_id=job_id),
        done_url=url_for('admin.import_kpi_csv_done', job_id=job_id),
        title='Import rückgängig machen',
        config=current_app.config,
    )


def _kpi_recompute_flags(project_id=None):
    """Recompute precomputed KPI flags for stored surveys from raw answers + mappings.

    project_id None -> all surveys. Returns the number of surveys updated.
    """
    _kpi_backfill_survey_links()
    mappings = {}
    for m in KpiQuestionMapping.query.all():
        mappings.setdefault((m.project_id, m.survey_type), {})[m.kpi_kind] = m.frage_code

    # Batch-load answers grouped by survey (avoids per-survey N+1 queries).
    ans_q = db.session.query(
        KpiAnswer.survey_id, KpiAnswer.frage_code, KpiAnswer.frage_text, KpiAnswer.antwort,
    )
    if project_id:
        ans_q = ans_q.join(KpiSurvey, KpiSurvey.id == KpiAnswer.survey_id).filter(
            KpiSurvey.project_id == project_id
        )
    answers_by_survey = {}
    for sid, code, ftext, antwort in ans_q.yield_per(2000):
        answers_by_survey.setdefault(sid, []).append(
            {'code': code, 'text': ftext, 'antwort': antwort}
        )

    sv_q = KpiSurvey.query
    if project_id:
        sv_q = sv_q.filter(KpiSurvey.project_id == project_id)
    updated = 0
    for sv in sv_q.all():
        cfg = mappings.get((sv.project_id, sv.studie)) or {}
        nps_v, loes_a, info_p, loes_p, fach_s, vert_p = kpi_logic.compute_survey_flags(
            answers_by_survey.get(sv.id, []),
            nps_code=cfg.get('nps'),
            loesung_code=cfg.get('loesung'),
            fachkompetenz_code=cfg.get('fachkompetenz'),
            vertrieb_code=cfg.get('vertrieb'),
        )
        sv.nps_value = nps_v
        sv.loesung_answer = loes_a
        sv.info_positive = info_p
        sv.loesung_positive = loes_p
        sv.fachkompetenz_stars = fach_s
        sv.vertrieb_positive = vert_p
        updated += 1
    db.session.commit()
    return updated


def _prod_detect_encoding(temp_path):
    for enc in ('utf-8-sig', 'utf-8', 'cp1252', 'latin-1'):
        try:
            with open(temp_path, 'r', encoding=enc) as fh:
                fh.read(65536)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return 'latin-1'


def _prod_read_csv_rows(temp_path):
    encoding = _prod_detect_encoding(temp_path)
    with open(temp_path, 'r', encoding=encoding) as f:
        sample = f.read(4096)
        f.seek(0)
        delimiter = ';' if ';' in sample else ','
        reader = csv.DictReader(f, delimiter=delimiter)
        headers = reader.fieldnames or []
        rows = list(reader)
    return headers, rows


def _prod_cleanup_session_temp():
    temp_path = session.get('prod_csv_temp_file')
    if temp_path and os.path.exists(temp_path):
        try:
            os.unlink(temp_path)
        except OSError:
            pass
    session.pop('prod_csv_temp_file', None)
    session.pop('prod_csv_filename', None)
    session.pop('prod_csv_headers', None)
    pdata = session.pop('prod_preview', None)
    if pdata:
        for key in ('unmatched_path',):
            p = pdata.get(key)
            if p and os.path.isfile(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


def _prod_build_intervals(rows, settings, team_map, dag_map, name_map, ma_map):
    """Group rows by agent+slot, compute metrics, resolve links."""
    archiv_team = get_or_create_archiv_team()
    members_by_id = {
        m.id: m for m in TeamMember.query.options(joinedload(TeamMember.team)).all()
    }

    def _member_candidates(agent_name, dag_id, team_id):
        ids = set()
        dag_key = productivity_logic.normalize_member_id_key(dag_id)
        agent_lower = (agent_name or '').strip().lower()
        if dag_key and dag_key != '-1':
            for mid, _ in dag_map.get(dag_key, []):
                ids.add(mid)
            for mid, _ in ma_map.get(dag_key, []):
                ids.add(mid)
        if agent_lower:
            for mid, _ in name_map.get(agent_lower, []):
                ids.add(mid)
        out = []
        for mid in ids:
            m = members_by_id.get(mid)
            if not m:
                continue
            out.append({
                'id': m.id,
                'name': m.name,
                'team_name': m.team.name if m.team else '–',
                'dag_id': m.dag_id or '',
                'ma_kennung': m.ma_kennung or '',
                'is_archiv': m.team_id == archiv_team.id,
                'in_resolved_team': team_id is not None and m.team_id == team_id,
            })
        out.sort(key=lambda c: (not c['in_resolved_team'], c['is_archiv'], c['name']))
        return out[:8]

    def _unmatched_extra(be4, dag_id, agent_name, tid):
        be4_key = (be4 or '').strip()
        dag_key = productivity_logic.normalize_member_id_key(dag_id)
        team_entry = team_map.get(be4_key) if be4_key else None
        first_name, last_name = productivity_logic.split_agent_display_name(agent_name)
        dag_in_system = bool(dag_key and dag_key != '-1' and dag_key in dag_map)
        ma_in_system = bool(dag_key and dag_key in ma_map)
        suggest_ma = suggest_dag = ''
        hint = ''
        if dag_key and dag_key != '-1':
            if not dag_in_system and not ma_in_system:
                if productivity_logic.looks_like_numeric_id(dag_key):
                    suggest_ma = dag_key
                    hint = (
                        'DAG_ID ist numerisch und passt weder zu DAG-ID noch MA-Kennung '
                        'eines Mitglieds – vermutlich MA-Kennung aus Rohdaten.'
                    )
                else:
                    suggest_dag = dag_key
                    hint = 'DAG-ID aus CSV konnte keinem Mitglied zugeordnet werden.'
            elif ma_in_system and not dag_in_system:
                hint = 'Wert passt als MA-Kennung, nicht als DAG-ID.'
        return {
            'team_csv': be4_key or '–',
            'team_system_id': team_entry[0] if team_entry else None,
            'team_system_name': be4_key if team_entry else None,
            'team_unknown': bool(be4_key and be4_key != '–' and not team_entry),
            'first_name': first_name,
            'last_name': last_name,
            'suggest_ma_kennung': suggest_ma,
            'suggest_dag_id': suggest_dag,
            'hint': hint,
            'dag_in_system': dag_in_system,
            'ma_in_system': ma_in_system,
        }

    groups = {}
    for row in rows:
        slot_str = productivity_logic.combine_datum_zeit(row)
        dag = (row.get('DAG_ID') or '').strip()
        agent = (row.get('DAG_VN_NN') or '').strip()
        key = (dag or agent or '?', slot_str)
        groups.setdefault(key, []).append(row)

    intervals = []
    matched = unmatched = 0
    unmatched_details = {}
    for _key, grp in groups.items():
        slot = productivity_logic.merge_rows_to_slot(grp, settings)
        if not slot.get('slot_at'):
            continue
        tid, pid, mid = productivity_logic.resolve_member(
            slot.get('be4'), slot.get('dag_id'), slot.get('agent_name'),
            team_map, dag_map, name_map, ma_map,
        )
        if mid:
            matched += 1
        else:
            unmatched += 1
            ukey = (
                (slot.get('be4') or '').strip(),
                (slot.get('dag_id') or '').strip(),
                (slot.get('agent_name') or '').strip(),
            )
            u = unmatched_details.get(ukey)
            if u is None:
                u = {
                    'be4': slot.get('be4') or '–',
                    'dag_id': slot.get('dag_id') or '–',
                    'agent_name': slot.get('agent_name') or '–',
                    'count': 0,
                    'candidates': _member_candidates(
                        slot.get('agent_name'), slot.get('dag_id'), tid,
                    ),
                }
                u.update(_unmatched_extra(
                    slot.get('be4'), slot.get('dag_id'), slot.get('agent_name'), tid,
                ))
                unmatched_details[ukey] = u
            u['count'] += 1
        intervals.append({**slot, 'team_id': tid, 'project_id': pid, 'team_member_id': mid})
    unmatched_list = sorted(
        unmatched_details.values(), key=lambda d: (-d['count'], d['agent_name'], d['dag_id']),
    )
    return intervals, matched, unmatched, unmatched_list


def _prod_commit_intervals(filename, intervals, overwrite=False, progress_cb=None):
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
        q = ProductivityInterval.query.filter(
            ProductivityInterval.slot_at >= datetime.combine(date_from, time.min),
            ProductivityInterval.slot_at <= datetime.combine(date_to, time.max),
        )
        deleted = q.delete(synchronize_session=False)

    batch = ProductivityImportBatch(
        filename=filename,
        imported_by_id=current_user.id,
        date_from=date_from,
        date_to=date_to,
        rows_total=len(intervals),
        intervals_stored=0,
        matched_member=sum(1 for iv in intervals if iv.get('team_member_id')),
        unmatched_member=sum(1 for iv in intervals if not iv.get('team_member_id')),
    )
    db.session.add(batch)
    db.session.flush()

    chunk = 500
    stored = 0
    for i in range(0, len(intervals), chunk):
        part = intervals[i:i + chunk]
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
        stored += len(objs)
        _progress(stored, f'{stored}/{total} Intervalle speichern…')
    batch.intervals_stored = stored
    _progress(total, 'Abschluss…')
    db.session.commit()
    return batch, deleted


@bp.route('/import_productivity_csv', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def import_productivity_csv():
    if request.method == 'POST' and 'prod_csv_file' in request.files:
        file = request.files['prod_csv_file']
        if not file or not file.filename.lower().endswith('.csv'):
            flash('Bitte eine CSV-Datei hochladen.', 'danger')
            return redirect(url_for('admin.import_productivity_csv'))

        _prod_cleanup_session_temp()
        fd, temp_path = tempfile.mkstemp(suffix='.csv')
        os.close(fd)
        file.save(temp_path)
        session['prod_csv_temp_file'] = temp_path
        session['prod_csv_filename'] = file.filename

        try:
            headers, rows = _prod_read_csv_rows(temp_path)
            session['prod_csv_headers'] = headers
            settings = productivity_logic.settings_dict(None)
            team_map, dag_map, name_map, ma_map = productivity_logic.build_link_maps(
                Team.query.all(), TeamMember.query.options(joinedload(TeamMember.team)).all(),
            )
            intervals, matched, unmatched, unmatched_list = _prod_build_intervals(
                rows, settings, team_map, dag_map, name_map, ma_map,
            )
        except Exception as e:
            flash(f'CSV konnte nicht gelesen werden: {e}', 'danger')
            _prod_cleanup_session_temp()
            return redirect(url_for('admin.import_productivity_csv'))

        if not rows:
            flash('Keine Zeilen in der CSV gefunden.', 'warning')
            _prod_cleanup_session_temp()
            return redirect(url_for('admin.import_productivity_csv'))

        dates = [iv['slot_at'].date() for iv in intervals if iv.get('slot_at')]
        unmatched_path = _import_json_temp(unmatched_list) if unmatched_list else None
        session['prod_preview'] = {
            'filename': file.filename,
            'preview': {
                'rows_total': len(rows),
                'intervals': len(intervals),
                'matched_member': matched,
                'unmatched_member': unmatched,
                'unmatched_groups': len(unmatched_list),
                'date_from': min(dates).strftime('%d.%m.%Y') if dates else '-',
                'date_to': max(dates).strftime('%d.%m.%Y') if dates else '-',
                'headers_count': len(headers),
                'sample_headers': headers[:12],
            },
            'unmatched_path': unmatched_path,
        }
        return redirect(url_for('admin.import_productivity_preview'))

    if request.method == 'POST' and request.form.get('action') == 'confirm':
        temp_path = session.get('prod_csv_temp_file')
        filename = session.get('prod_csv_filename', 'import.csv')
        if not temp_path or not os.path.exists(temp_path):
            flash('Keine CSV-Daten gefunden. Bitte erneut hochladen.', 'danger')
            return redirect(url_for('admin.import_productivity_csv'))
        try:
            headers, rows = _prod_read_csv_rows(temp_path)
            settings = productivity_logic.settings_dict(None)
            team_map, dag_map, name_map, ma_map = productivity_logic.build_link_maps(
                Team.query.all(), TeamMember.query.options(joinedload(TeamMember.team)).all(),
            )
            intervals, _, _, _ = _prod_build_intervals(
                rows, settings, team_map, dag_map, name_map, ma_map,
            )
            overwrite = request.form.get('confirm_overwrite') == '1'
            batch, deleted = _prod_commit_intervals(filename, intervals, overwrite=overwrite)
        except Exception as e:
            db.session.rollback()
            flash(f'Import fehlgeschlagen: {e}', 'danger')
            return redirect(url_for('admin.import_productivity_csv'))
        _prod_cleanup_session_temp()
        msg = f'{batch.intervals_stored} Intervalle importiert'
        if deleted:
            msg += f', {deleted} im Zeitraum ersetzt'
        msg += f'. {batch.matched_member} mit Agent, {batch.unmatched_member} ohne Zuordnung.'
        flash(msg, 'success')
        session['prod_csv_headers'] = headers
        return redirect(url_for('admin.import_productivity_csv'))

    recent_batches = ProductivityImportBatch.query.order_by(desc(ProductivityImportBatch.imported_at)).limit(10).all()
    known_headers = session.get('prod_csv_headers') or []
    return render_template(
        'admin/import_productivity_csv.html',
        recent_batches=recent_batches,
        known_headers=known_headers,
        config=current_app.config,
    )


@bp.route('/import_productivity_csv/preview')
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def import_productivity_preview():
    pdata = session.get('prod_preview')
    if not pdata:
        flash('Keine Vorschau-Daten. Bitte CSV erneut hochladen.', 'warning')
        return redirect(url_for('admin.import_productivity_csv'))
    page = request.args.get('page', 1, type=int)
    per_page = 50
    unmatched_rows = []
    total_pages = 1
    unmatched_total = 0
    path = pdata.get('unmatched_path')
    if path and os.path.isfile(path):
        with open(path, 'r', encoding='utf-8') as fh:
            all_rows = json.load(fh)
        unmatched_total = len(all_rows)
        total_pages = max(1, (unmatched_total + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        start = (page - 1) * per_page
        unmatched_rows = all_rows[start:start + per_page]
    return render_template(
        'admin/import_productivity_preview.html',
        preview=pdata['preview'],
        filename=pdata['filename'],
        unmatched_rows=unmatched_rows,
        unmatched_total=unmatched_total,
        page=page,
        total_pages=total_pages,
        config=current_app.config,
    )


@bp.route('/import_productivity_csv/job/start', methods=['POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def import_productivity_csv_job_start():
    temp_path = session.get('prod_csv_temp_file')
    filename = session.get('prod_csv_filename', 'import.csv')
    if not temp_path or not os.path.exists(temp_path):
        return jsonify({'error': 'Keine CSV-Daten gefunden. Bitte erneut hochladen.'}), 400
    overwrite = request.form.get('confirm_overwrite') == '1'
    job_id = uuid.uuid4().hex
    _import_job_write(job_id, current_user.id, {
        'status': 'pending', 'pct': 0, 'message': 'Import wird gestartet…',
    })
    app = current_app._get_current_object()
    thread = threading.Thread(
        target=_prod_run_import_job,
        args=(app, job_id, current_user.id, temp_path, filename, overwrite),
        daemon=True,
    )
    thread.start()
    return jsonify({'job_id': job_id})


@bp.route('/import_productivity_csv/job/<job_id>/status')
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def import_productivity_csv_job_status(job_id):
    job = _import_job_read(job_id, current_user.id)
    if not job:
        return jsonify({'status': 'missing'}), 404
    return jsonify({
        'status': job.get('status'),
        'pct': job.get('pct', 0),
        'message': job.get('message', ''),
        'done_url': job.get('done_url'),
        'error': job.get('message') if job.get('status') == 'error' else None,
    })


@bp.route('/import_productivity_csv/done/<job_id>')
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def import_productivity_csv_done(job_id):
    job = _import_job_read(job_id, current_user.id)
    if job and job.get('flash_message'):
        flash(job['flash_message'], job.get('flash_category', 'success'))
    _import_job_delete(job_id)
    _prod_cleanup_session_temp()
    session.pop('prod_preview', None)
    return redirect(url_for('admin.import_productivity_csv'))


@bp.route('/import_productivity_csv/revert/<int:batch_id>/start', methods=['POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def import_productivity_csv_revert_start(batch_id):
    if not ProductivityImportBatch.query.get(batch_id):
        return jsonify({'error': f'Produktivitäts-Import #{batch_id} nicht gefunden.'}), 404
    job_id = uuid.uuid4().hex
    _import_job_write(job_id, current_user.id, {
        'status': 'pending', 'pct': 0, 'message': 'Zurücksetzen wird gestartet…',
    })
    _spawn_revert_worker('prod_revert', batch_id, job_id, current_user.id)
    return jsonify({'job_id': job_id})


@bp.route('/import_productivity_csv/revert/<int:batch_id>', methods=['POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def import_productivity_csv_revert(batch_id):
    """Start async revert (non-JS fallback redirects to status page)."""
    if not ProductivityImportBatch.query.get(batch_id):
        flash(f'Produktivitäts-Import #{batch_id} nicht gefunden.', 'danger')
        return redirect(url_for('admin.import_productivity_csv'))
    job_id = uuid.uuid4().hex
    _import_job_write(job_id, current_user.id, {
        'status': 'pending', 'pct': 0, 'message': 'Zurücksetzen wird gestartet…',
    })
    _spawn_revert_worker('prod_revert', batch_id, job_id, current_user.id)
    return redirect(url_for('admin.import_productivity_csv_revert_status', job_id=job_id))


@bp.route('/import_productivity_csv/revert/status/<job_id>')
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def import_productivity_csv_revert_status(job_id):
    return render_template(
        'admin/import_job_status.html',
        job_id=job_id,
        status_url=url_for('admin.import_productivity_csv_job_status', job_id=job_id),
        done_url=url_for('admin.import_productivity_csv_done', job_id=job_id),
        title='Produktivitäts-Import zurücksetzen',
        config=current_app.config,
    )


@bp.route('/kpi-verwaltung', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def kpi_verwaltung():
    """Per-project mapping of which question code defines NPS / Lösung-Info."""
    qual_pids = {
        r[0] for r in db.session.query(KpiSurvey.project_id).filter(
            KpiSurvey.project_id.isnot(None),
        ).distinct().all()
    }
    prod_pids = {
        r[0] for r in db.session.query(ProductivityInterval.project_id).filter(
            ProductivityInterval.project_id.isnot(None),
        ).distinct().all()
    }
    all_pids = qual_pids | prod_pids
    if all_pids:
        proj_rows = (
            db.session.query(Project.id, Project.name)
            .filter(Project.id.in_(all_pids))
            .order_by(Project.name).all()
        )
    else:
        proj_rows = []
    projects = [{'id': pid, 'name': pname} for pid, pname in proj_rows]

    def _form_int(name):
        try:
            return int(request.form.get(name))
        except (TypeError, ValueError):
            return None

    if request.method == 'POST':
        action = request.form.get('action')
        sel_project = _form_int('project_id')
        if action == 'save_global':
            from app.models import PlatformSettings
            row = PlatformSettings.query.get(1)
            if row is None:
                row = PlatformSettings(id=1)
                db.session.add(row)
            row.kpi_features_enabled = bool(request.form.get('kpi_features_enabled'))
            db.session.commit()
            state = 'aktiviert' if row.kpi_features_enabled else 'deaktiviert'
            flash(f'KPI-Funktionen in der Plattform wurden {state}.', 'success')
            return redirect(url_for('admin.kpi_verwaltung', project_id=sel_project) if sel_project
                            else url_for('admin.kpi_verwaltung'))
        if action == 'recompute':
            updated = _kpi_recompute_flags(sel_project)
            flash(f'KPIs neu berechnet: {updated} Befragungen aktualisiert.', 'success')
            return redirect(url_for('admin.kpi_verwaltung', project_id=sel_project) if sel_project
                            else url_for('admin.kpi_verwaltung'))
        if action == 'recompute_all':
            updated = _kpi_recompute_flags(None)
            flash(f'KPIs für alle Projekte neu berechnet: {updated} Befragungen.', 'success')
            return redirect(url_for('admin.kpi_verwaltung'))
        if action == 'save_categories':
            for cat in KpiCategory.query.order_by(KpiCategory.sort_order).all():
                label = (request.form.get(f'cat_label_{cat.id}') or '').strip()
                try:
                    sort_order = int(request.form.get(f'cat_sort_{cat.id}', cat.sort_order))
                except (TypeError, ValueError):
                    sort_order = cat.sort_order
                if label:
                    cat.label = label[:100]
                cat.sort_order = sort_order
            db.session.commit()
            flash('KPI-Kategorien gespeichert.', 'success')
            return redirect(url_for('admin.kpi_verwaltung', project_id=sel_project) if sel_project
                            else url_for('admin.kpi_verwaltung'))
        if action == 'save' and sel_project:
            st_list = request.form.getlist('survey_type')
            nps_list = request.form.getlist('nps')
            loes_list = request.form.getlist('loesung')
            fach_list = request.form.getlist('fachkompetenz')
            vert_list = request.form.getlist('vertrieb')
            KpiQuestionMapping.query.filter_by(project_id=sel_project).delete()
            for st, npsc, loesc, fachc, vertc in zip(st_list, nps_list, loes_list, fach_list, vert_list):
                st = (st or '').strip()
                if not st:
                    continue
                if npsc:
                    db.session.add(KpiQuestionMapping(
                        project_id=sel_project, survey_type=st, kpi_kind='nps', frage_code=npsc.strip()))
                if loesc:
                    db.session.add(KpiQuestionMapping(
                        project_id=sel_project, survey_type=st, kpi_kind='loesung', frage_code=loesc.strip()))
                if fachc:
                    db.session.add(KpiQuestionMapping(
                        project_id=sel_project, survey_type=st, kpi_kind='fachkompetenz', frage_code=fachc.strip()))
                if vertc:
                    db.session.add(KpiQuestionMapping(
                        project_id=sel_project, survey_type=st, kpi_kind='vertrieb', frage_code=vertc.strip()))

            type_list = request.form.getlist('kpi_source_type')
            mode_list = request.form.getlist('kpi_source_mode')
            ProjectKpiSource.query.filter_by(project_id=sel_project).delete()
            for st, mode in zip(type_list, mode_list):
                st = (st or '').strip()
                if not st:
                    continue
                if mode == 'count':
                    db.session.add(ProjectKpiSource(project_id=sel_project, survey_type=st, counts=True))
                elif mode == 'show':
                    db.session.add(ProjectKpiSource(project_id=sel_project, survey_type=st, counts=False))

            setting = ProjectKpiSetting.query.get(sel_project)
            if setting is None:
                setting = ProjectKpiSetting(project_id=sel_project)
                db.session.add(setting)
            setting.show_info = bool(request.form.get('show_info'))
            setting.show_loesung = bool(request.form.get('show_loesung'))
            setting.show_nps = bool(request.form.get('show_nps'))
            setting.show_fachkompetenz = bool(request.form.get('show_fachkompetenz'))
            setting.show_vertrieb = bool(request.form.get('show_vertrieb'))
            setting.dashboard_show_info = bool(request.form.get('dashboard_show_info'))
            setting.dashboard_show_loesung = bool(request.form.get('dashboard_show_loesung'))
            setting.dashboard_show_nps = bool(request.form.get('dashboard_show_nps'))
            setting.dashboard_show_fachkompetenz = bool(request.form.get('dashboard_show_fachkompetenz'))
            setting.dashboard_show_vertrieb = bool(request.form.get('dashboard_show_vertrieb'))

            pset = ProjectProductivitySetting.query.get(sel_project)
            if pset is None:
                pset = ProjectProductivitySetting(project_id=sel_project)
                db.session.add(pset)
            try:
                pset.interval_sec = max(60, int(request.form.get('prod_interval_sec', 1800)))
            except (TypeError, ValueError):
                pset.interval_sec = 1800
            pset.pause_col = (request.form.get('prod_pause_col') or 'IDLE_RC12_Bearbeitung').strip()[:80]
            pset.calls_col = (request.form.get('prod_calls_col') or 'Mex1').strip()[:80]
            pset.works_col = (request.form.get('prod_works_col') or 'Works_Beendet').strip()[:80]

            def _cols_from_form(prefix):
                return [c.strip() for c in request.form.getlist(prefix) if c.strip()]

            pset.sign_on_cols = json.dumps(_cols_from_form('prod_sign_on_cols'))
            pset.prod_cols = json.dumps(_cols_from_form('prod_prod_cols'))
            pset.nach_cols = json.dumps(_cols_from_form('prod_nach_cols'))
            pset.idle_cols = json.dumps(_cols_from_form('prod_idle_cols'))
            pset.excluded_cols = json.dumps(_cols_from_form('prod_excluded_cols'))
            pset.dashboard_show_sign_on = bool(request.form.get('dashboard_show_sign_on'))
            pset.dashboard_show_prod = bool(request.form.get('dashboard_show_prod'))
            pset.dashboard_show_nach = bool(request.form.get('dashboard_show_nach'))
            pset.dashboard_show_idle = bool(request.form.get('dashboard_show_idle'))
            pset.dashboard_show_calls = bool(request.form.get('dashboard_show_calls'))
            pset.dashboard_show_works = bool(request.form.get('dashboard_show_works'))
            pset.impact_show_sign_on = bool(request.form.get('impact_show_sign_on'))
            pset.impact_show_prod = bool(request.form.get('impact_show_prod'))
            pset.impact_show_nach = bool(request.form.get('impact_show_nach'))
            pset.impact_show_idle = bool(request.form.get('impact_show_idle'))
            pset.impact_show_calls = bool(request.form.get('impact_show_calls'))
            pset.impact_show_works = bool(request.form.get('impact_show_works'))
            pset.target_sign_on = _float_form('target_sign_on', 95.0)
            pset.target_prod = _float_form('target_prod', 85.0)
            pset.target_nach_per_call = _float_form('target_nach_per_call', 30.0)
            pset.target_idle_max = _float_form('target_idle_max', 10.0)
            pset.label_sign_on = _label_form('label_sign_on', productivity_logic.DEFAULT_LABELS['sign_on'])
            pset.label_prod = _label_form('label_prod', productivity_logic.DEFAULT_LABELS['prod'])
            pset.label_nach = _label_form('label_nach', productivity_logic.DEFAULT_LABELS['nach'])
            pset.label_idle = _label_form('label_idle', productivity_logic.DEFAULT_LABELS['idle'])
            pset.label_calls = _label_form('label_calls', productivity_logic.DEFAULT_LABELS['calls'])
            pset.label_works = _label_form('label_works', productivity_logic.DEFAULT_LABELS['works'])

            db.session.commit()
            flash('KPI-Einstellungen gespeichert. Tipp: „KPIs neu berechnen“ aktualisiert bestehende Daten.', 'success')
            return redirect(url_for('admin.kpi_verwaltung', project_id=sel_project))

    sel_project = request.args.get('project_id', type=int)
    if sel_project and sel_project not in {p['id'] for p in projects}:
        sel_project = None

    survey_types = []
    project_name = None
    visibility = {'info': True, 'loesung': True, 'nps': True, 'fachkompetenz': True, 'vertrieb': True}
    dashboard_visibility = dict(visibility)
    if sel_project:
        project_name = next((p['name'] for p in projects if p['id'] == sel_project), None)
        rows = (
            db.session.query(KpiSurvey.studie, KpiAnswer.frage_code, KpiAnswer.frage_text)
            .join(KpiAnswer, KpiAnswer.survey_id == KpiSurvey.id)
            .filter(KpiSurvey.project_id == sel_project)
            .distinct().all()
        )
        types = {}
        for studie, code, ftext in rows:
            studie = (studie or '').strip() or '(ohne Studie)'
            if not code:
                continue
            types.setdefault(studie, {}).setdefault(code, ftext or code)
        existing = {
            (m.survey_type, m.kpi_kind): m.frage_code
            for m in KpiQuestionMapping.query.filter_by(project_id=sel_project).all()
        }
        source_rows = ProjectKpiSource.query.filter_by(project_id=sel_project).all()
        has_source_config = bool(source_rows)
        row_by_type = {r.survey_type: r for r in source_rows}
        for studie in sorted(types):
            qs = [{'code': c, 'text': types[studie][c]} for c in sorted(types[studie])]
            row = row_by_type.get(studie)
            if not has_source_config:
                source_mode = 'count'
            elif row is None:
                source_mode = 'off'
            else:
                source_mode = 'count' if row.counts else 'show'
            survey_types.append({
                'name': studie,
                'questions': qs,
                'nps_code': existing.get((studie, 'nps'), ''),
                'loesung_code': existing.get((studie, 'loesung'), ''),
                'fachkompetenz_code': existing.get((studie, 'fachkompetenz'), ''),
                'vertrieb_code': existing.get((studie, 'vertrieb'), ''),
                'source_mode': source_mode,
            })
        setting = ProjectKpiSetting.query.get(sel_project)
        if setting:
            visibility = {
                'info': setting.show_info,
                'loesung': setting.show_loesung,
                'nps': setting.show_nps,
                'fachkompetenz': setting.show_fachkompetenz,
                'vertrieb': setting.show_vertrieb,
            }
            dashboard_visibility = {
                'info': setting.dashboard_show_info,
                'loesung': setting.dashboard_show_loesung,
                'nps': setting.dashboard_show_nps,
                'fachkompetenz': setting.dashboard_show_fachkompetenz,
                'vertrieb': setting.dashboard_show_vertrieb,
            }

    categories = KpiCategory.query.order_by(KpiCategory.sort_order, KpiCategory.id).all()
    if not categories:
        for key, label, order in (('qualitaet', 'Qualität', 1), ('produktivitaet', 'Produktivität', 2)):
            db.session.add(KpiCategory(key=key, label=label, sort_order=order, is_system=True))
        db.session.commit()
        categories = KpiCategory.query.order_by(KpiCategory.sort_order).all()

    prod_setting = ProjectProductivitySetting.query.get(sel_project) if sel_project else None
    prod_settings = productivity_logic.settings_dict(prod_setting)
    prod_dashboard_visibility = productivity_logic.dashboard_visibility_dict(prod_setting)
    prod_impact_visibility = productivity_logic.impact_visibility_dict(prod_setting)
    known_headers = session.get('prod_csv_headers') or []

    from app.kpi import kpi_features_enabled
    return render_template(
        'admin/kpi_verwaltung.html',
        projects=projects,
        sel_project=sel_project,
        project_name=project_name,
        survey_types=survey_types,
        visibility=visibility,
        dashboard_visibility=dashboard_visibility,
        categories=categories,
        prod_settings=prod_settings,
        prod_dashboard_visibility=prod_dashboard_visibility,
        prod_impact_visibility=prod_impact_visibility,
        known_headers=known_headers,
        kpi_features_enabled=kpi_features_enabled(),
        config=current_app.config,
    )


def _float_form(name, default):
    try:
        return float(request.form.get(name, default))
    except (TypeError, ValueError):
        return default


def _label_form(name, default):
    val = (request.form.get(name) or '').strip()
    return val[:80] if val else default


@bp.route('/team-view-kpis', methods=['GET', 'POST'])
@login_required
@role_required([ROLE_ADMIN, ROLE_BETRIEBSLEITER])
def team_view_kpis():
    """Configure /team-view member card metrics and color thresholds (Ziele)."""
    projects = Project.query.order_by(Project.name).all()

    def _form_int(name):
        try:
            return int(request.form.get(name))
        except (TypeError, ValueError):
            return None

    if request.method == 'POST':
        sel_project = _form_int('project_id')
        if sel_project:
            row = TeamViewCardSettings.query.get(sel_project)
            if row is None:
                row = TeamViewCardSettings(project_id=sel_project)
                db.session.add(row)
            row.show_nps = bool(request.form.get('show_nps'))
            row.show_loesung = bool(request.form.get('show_loesung'))
            row.show_info = bool(request.form.get('show_info'))
            row.show_performance = bool(request.form.get('show_performance'))
            row.show_fachkompetenz = bool(request.form.get('show_fachkompetenz'))
            row.show_vertrieb = bool(request.form.get('show_vertrieb'))
            row.target_nps = _float_form('target_nps', 50)
            row.target_loesung = _float_form('target_loesung', 80)
            row.target_info = _float_form('target_info', 80)
            row.target_performance = _float_form('target_performance', 80)
            row.target_fachkompetenz = _float_form('target_fachkompetenz', 4)
            row.target_vertrieb = _float_form('target_vertrieb', 80)
            row.warn_nps = _float_form('warn_nps', 0)
            row.warn_loesung = _float_form('warn_loesung', 60)
            row.warn_info = _float_form('warn_info', 60)
            row.warn_performance = _float_form('warn_performance', 50)
            row.warn_fachkompetenz = _float_form('warn_fachkompetenz', 3)
            row.warn_vertrieb = _float_form('warn_vertrieb', 60)
            db.session.commit()
            flash('Team-View KPI-Einstellungen gespeichert.', 'success')
            return redirect(url_for('admin.team_view_kpis', project_id=sel_project))

    sel_project = request.args.get('project_id', type=int)
    if sel_project and sel_project not in {p.id for p in projects}:
        sel_project = None
    settings = kpi_logic.team_view_card_settings_dict(
        TeamViewCardSettings.query.get(sel_project) if sel_project else None
    )
    project_name = next((p.name for p in projects if p.id == sel_project), None) if sel_project else None
    return render_template(
        'admin/team_view_kpis.html',
        projects=projects,
        sel_project=sel_project,
        project_name=project_name,
        settings=settings,
        config=current_app.config,
    )
