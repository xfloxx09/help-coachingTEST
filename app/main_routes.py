# app/main_routes.py
from flask import Blueprint, render_template, redirect, url_for, flash, request, session, current_app, jsonify, abort
from flask_login import login_required, current_user
from sqlalchemy import desc, or_, and_, false, exists, extract, cast, Date, func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload, selectinload, aliased
from app import db
from app.models import (
    User,
    Team,
    TeamMember,
    Coaching,
    Workshop,
    workshop_participants,
    Project,
    Role,
    AssignedCoaching,
    CoachingLeitfadenResponse,
    CoachingReview,
    PlannedCoaching,
    PlannedWorkshop,
    KpiSurvey,
    KpiAnswer,
    KpiImportBatch,
    ProjectKpiSource,
    ProjectKpiSetting,
    TeamViewCardSettings,
    KpiCategory,
    ProductivityInterval,
    ProjectProductivitySetting,
)
from app import kpi as kpi_logic
from app import productivity as productivity_logic
from app import kpi_time
from app import coaching_impact_wizard as ci_wizard
from app.forms import CoachingForm, WorkshopForm, PasswordChangeForm, CoachingReviewForm, AssignedCoachingForm
from app.utils import (
    bogen_layout_for_project,
    role_required,
    permission_required,
    any_permission_required,
    ROLE_ADMIN,
    ROLE_BETRIEBSLEITER,
    ROLE_PROJEKTLEITER,
    ROLE_TEAMLEITER,
    ROLE_ABTEILUNGSLEITER,
    ROLE_QM,
    ROLE_SALESCOACH,
    ROLE_TRAINER,
    get_or_create_archiv_team,
    ARCHIV_TEAM_NAME,
    get_accessible_project_ids,
    team_member_eligible_for_new_coaching,
    team_member_eligible_for_coaching_assignment,
    user_eligible_assignable_coach,
    users_for_assignment_coach_dropdown,
    users_for_assignment_coach_dropdown_multi,
    workshop_individual_rating_from_request,
    leitfaden_items_for_project,
    leitfaden_items_for_coaching_edit,
    today_athens_date,
    planned_coaching_can_start_today,
    create_planned_coaching_from_coaching_form,
    athens_calendar_day_utc_naive_bounds,
    utc_naive_or_aware_to_athens_date,
    can_view_kpi_qualitaet,
    can_view_kpi_produktivitaet,
)
from datetime import datetime, timezone, timedelta, date
from collections import defaultdict
import calendar

bp = Blueprint('main', __name__)
LEITFADEN_CHOICES = {'Ja', 'Nein', 'k.A.'}


def _try_create_planned_followup_from_request(coaching):
    """
    If „Nächstes Coaching planen“ has a date, create PlannedCoaching on save (no extra checkbox).
    Returns: None (nothing to do), 'bad_date', or 'created'.
    """
    if not current_user.has_permission('planned_coachings'):
        return None
    raw = (request.form.get('plan_next_date') or '').strip()
    if not raw:
        return None
    try:
        pdate = datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        return 'bad_date'
    notes = request.form.get('plan_next_notes', '')
    has_v = request.form.get('plan_next_verabredung') == '1'
    vtext = request.form.get('plan_next_verabredung_text', '')
    tm = TeamMember.query.get(coaching.team_member_id)
    create_planned_coaching_from_coaching_form(
        current_user.id,
        coaching.team_member_id,
        pdate,
        coaching.project_id,
        tm.team_id if tm else None,
        notes,
        has_v,
        vtext,
        coaching.id,
    )
    return 'created'


def _effective_planned_coaching_for_fulfill(team_member_id, project_id, fulfill_planned_id):
    """Planned row if submission may fulfill it (same rules as persist step)."""
    if not fulfill_planned_id:
        return None
    pc = PlannedCoaching.query.get(fulfill_planned_id)
    if not pc or pc.coach_id != current_user.id or pc.status != 'open':
        return None
    if not planned_coaching_can_start_today(pc.planned_for_date):
        return None
    if pc.team_member_id != team_member_id:
        return None
    acc = get_accessible_project_ids()
    if acc is not None and pc.project_id and pc.project_id not in acc:
        return None
    return pc


def _parse_fulfill_planned_submission(team_member_id, project_id):
    """
    Returns (fulfill_planned_id, verabredung_erfuellt, error_message).
    verabredung_erfuellt: True/False if plan has agreement and will be fulfilled; else None.
    """
    raw = (request.form.get('fulfill_planned_id') or '').strip()
    fulfill_pid = int(raw) if raw.isdigit() else None
    if not fulfill_pid:
        return None, None, None
    pc = _effective_planned_coaching_for_fulfill(team_member_id, project_id, fulfill_pid)
    if not pc:
        return fulfill_pid, None, None
    if pc.has_verabredung:
        raw_ve = (request.form.get('planned_verabredung_erfuellt') or '').strip()
        if raw_ve == '1':
            return fulfill_pid, True, None
        if raw_ve == '0':
            return fulfill_pid, False, None
        return fulfill_pid, None, 'Bitte wählen Sie, ob die Vereinbarung erfüllt wurde oder nicht.'
    return fulfill_pid, None, None


def _maybe_fulfill_planned_coaching(coaching, fulfill_planned_id, verabredung_erfuellt=None):
    if not fulfill_planned_id:
        return
    pc = PlannedCoaching.query.get(fulfill_planned_id)
    if not pc or pc.coach_id != current_user.id or pc.status != 'open':
        return
    if not planned_coaching_can_start_today(pc.planned_for_date):
        return
    if pc.team_member_id != coaching.team_member_id:
        return
    acc = get_accessible_project_ids()
    if acc is not None and pc.project_id and pc.project_id not in acc:
        return
    pc.fulfilled_coaching_id = coaching.id
    pc.status = 'fulfilled'
    if pc.has_verabredung:
        pc.verabredung_erfuellt = verabredung_erfuellt
    else:
        pc.verabredung_erfuellt = None


def _coaching_has_fulfilled_planned_row(coaching_id):
    """True if this coaching closed a planned slot (Bericht ist archiviert / nicht mehr editierbar)."""
    return (
        PlannedCoaching.query.filter(
            PlannedCoaching.fulfilled_coaching_id == coaching_id,
            PlannedCoaching.status == 'fulfilled',
        ).first()
        is not None
    )


def _user_may_view_fulfilled_plan_bericht(coaching):
    """Bericht lesen: eigener Coach, Admin/BL, oder PL/QM/Zuweiser im selben Projektbereich wie das Coaching-Dashboard."""
    if coaching is None:
        return False
    if current_user.role_name in (ROLE_ADMIN, ROLE_BETRIEBSLEITER):
        return True
    if coaching.coach_id == current_user.id:
        return True
    if not (
        current_user.has_permission('view_coaching_dashboard')
        or current_user.has_permission('view_pl_qm_dashboard')
        or current_user.has_permission('assign_coachings')
    ):
        return False
    acc = get_accessible_project_ids()
    if acc is not None:
        if len(acc) == 0:
            return False
        pid = coaching.project_id
        if not pid or pid not in acc:
            return False
    if _user_sees_all_teams_coaching_dashboard():
        return True
    if current_user.has_permission('view_pl_qm_dashboard') or current_user.has_permission('assign_coachings'):
        return True
    tm = coaching.team_member
    if not tm or not tm.team_id:
        return False
    return tm.team_id in set(_dashboard_my_team_ids())


def _user_may_edit_planned_coaching(pc):
    """Coach owns the row, still open, and project is in scope (same rules as list)."""
    if pc is None or pc.coach_id != current_user.id or pc.status != 'open':
        return False
    acc = get_accessible_project_ids()
    if acc is not None:
        if len(acc) == 0:
            return False
        if not pc.project_id or pc.project_id not in acc:
            return False
    return True


def _user_may_edit_planned_workshop(pw):
    """Coach owns the row, still open, project in scope (None = global)."""
    if pw is None or pw.coach_id != current_user.id or pw.status != 'open':
        return False
    acc = get_accessible_project_ids()
    if acc is not None:
        if len(acc) == 0:
            return False
        if pw.project_id is not None and pw.project_id not in acc:
            return False
    return True


def _split_planned_open_by_overdue(rows, today):
    """Open plans with planned_for_date before today vs today and future."""
    overdue = []
    current = []
    for row in rows or []:
        d = row.planned_for_date or today
        if d < today:
            overdue.append(row)
        else:
            current.append(row)
    overdue.sort(key=lambda r: (r.planned_for_date or date.min, r.id))
    return current, overdue


def _redirect_planned_coachings_list(tab=None):
    tab = (tab or request.form.get('return_tab') or request.args.get('tab') or '').strip()
    if tab in ('offen', 'ueberfaellig', 'geschlossen'):
        return redirect(url_for('main.planned_coachings_list', tab=tab))
    return redirect(url_for('main.planned_coachings_list'))


def _can_view_others_planned_in_scope():
    """See other coaches' open planned coachings/workshops (read-only); own plans use planned_coachings/add_workshop."""
    if not current_user.is_authenticated:
        return False
    return current_user.has_permission('view_others_planned_coachings')


def _count_open_planned_for_index():
    """Badge count: own open plans plus others' open plans in accessible projects (for oversight roles)."""
    u = current_user
    acc = get_accessible_project_ids()
    can_pc = u.has_permission('planned_coachings')
    can_pw = u.has_permission('add_workshop')
    can_vo = _can_view_others_planned_in_scope()
    total = 0

    parts_c = []
    if can_pc:
        mine_c = PlannedCoaching.coach_id == u.id
        if acc is not None:
            if len(acc) == 0:
                mine_c = and_(mine_c, false())
            else:
                mine_c = and_(mine_c, PlannedCoaching.project_id.in_(acc))
        parts_c.append(mine_c)
    if can_vo:
        other_c = PlannedCoaching.coach_id != u.id
        if acc is not None:
            if len(acc) == 0:
                other_c = and_(other_c, false())
            else:
                other_c = and_(other_c, PlannedCoaching.project_id.in_(acc))
        parts_c.append(other_c)
    if parts_c:
        total += PlannedCoaching.query.filter(
            PlannedCoaching.status == 'open',
            or_(*parts_c),
        ).count()

    parts_w = []
    if can_pw:
        mine_w = PlannedWorkshop.coach_id == u.id
        if acc is not None:
            if len(acc) == 0:
                mine_w = and_(mine_w, false())
            else:
                mine_w = and_(
                    mine_w,
                    or_(PlannedWorkshop.project_id.in_(acc), PlannedWorkshop.project_id.is_(None)),
                )
        parts_w.append(mine_w)
    if can_vo:
        other_w = PlannedWorkshop.coach_id != u.id
        if acc is not None:
            if len(acc) == 0:
                other_w = and_(other_w, false())
            else:
                other_w = and_(
                    other_w,
                    or_(PlannedWorkshop.project_id.in_(acc), PlannedWorkshop.project_id.is_(None)),
                )
        parts_w.append(other_w)
    if parts_w:
        total += PlannedWorkshop.query.filter(
            PlannedWorkshop.status == 'open',
            or_(*parts_w),
        ).count()

    return total


def _resolve_planned_workshop_fulfill_for_form(project_id):
    """Offener Plan des Users, passend zum gewählten Projekt (planned project leer = egal)."""
    pw_id = request.form.get('planned_workshop_id', type=int) if request.method == 'POST' else request.args.get(
        'planned_workshop', type=int
    )
    if not pw_id:
        return None
    cand = PlannedWorkshop.query.get(pw_id)
    if not cand or cand.coach_id != current_user.id or cand.status != 'open':
        return None
    if cand.project_id is not None and cand.project_id != project_id:
        return None
    return cand


def _safe_internal_path(path_val):
    """Only allow same-app relative paths (no open redirects)."""
    if not path_val or not isinstance(path_val, str):
        return None
    s = path_val.strip()
    if not s.startswith('/') or s.startswith('//'):
        return None
    if any(c in s for c in '\n\r\t'):
        return None
    return s


def _may_view_assigned_rejection_bericht(assignment):
    """Zuweiser, QM/Scope-Bericht, Coach der abgelehnt hat, Admin/BL."""
    if not assignment or assignment.status != 'rejected':
        return False
    if not (assignment.rejection_reason or '').strip():
        return False
    tm = assignment.team_member
    if not tm or not tm.team:
        return False
    project_id = tm.team.project_id
    acc = get_accessible_project_ids()
    if acc is not None:
        if len(acc) == 0 or project_id not in acc:
            return False
    if current_user.role_name in (ROLE_ADMIN, ROLE_BETRIEBSLEITER):
        return True
    if assignment.coach_id == current_user.id:
        return True
    is_pl_owner = assignment.project_leader_id == current_user.id
    if is_pl_owner and current_user.has_permission('assign_coachings'):
        return True
    if current_user.has_permission('view_assigned_coaching_report'):
        return True
    return False


def _redirect_after_coaching_review(form, my_coachings_query_args):
    target = _safe_internal_path((form.next.data or '').strip()) if getattr(form, 'next', None) else None
    if target:
        return redirect(target)
    return redirect(url_for('main.my_coachings', **my_coachings_query_args))


# Helper to get the active project for the current user
def get_visible_project_id():
    if current_user.is_authenticated:
        if current_user.role_name in [ROLE_ADMIN, ROLE_BETRIEBSLEITER]:
            project_id = session.get('active_project')
            if project_id:
                return project_id
            first = Project.query.first()
            return first.id if first else None
        elif current_user.role_name == ROLE_ABTEILUNGSLEITER:
            project_id = session.get('active_project')
            allowed = [p.id for p in current_user.projects]
            if project_id and project_id in allowed:
                return project_id
            first = current_user.projects.first()
            return first.id if first else None
        allowed = get_accessible_project_ids()
        if not allowed:
            return current_user.project_id
        if len(allowed) == 1:
            return allowed[0]
        project_id = session.get('active_project')
        if project_id and project_id in allowed:
            return project_id
        if current_user.project_id and current_user.project_id in allowed:
            return current_user.project_id
        return allowed[0]
    return None


def _apply_query_project_to_session():
    """If ?project=<id> is present and allowed, persist to session (same rules as set_project)."""
    pid = request.args.get('project', type=int)
    if pid is None:
        return
    project = Project.query.get(pid)
    if not project:
        return
    if current_user.role_name in [ROLE_ADMIN, ROLE_BETRIEBSLEITER]:
        session['active_project'] = pid
        session.modified = True
        return
    if current_user.role_name == ROLE_ABTEILUNGSLEITER and project in current_user.projects:
        session['active_project'] = pid
        session.modified = True
        return
    allowed = get_accessible_project_ids()
    if allowed and pid in allowed:
        session['active_project'] = pid
        session.modified = True


def _projects_for_coaching_workshop_picker():
    """Projects the user may target when adding a coaching or workshop."""
    accessible = get_accessible_project_ids()
    if accessible is None:
        return Project.query.order_by(Project.name).all()
    if not accessible:
        return []
    return Project.query.filter(Project.id.in_(accessible)).order_by(Project.name).all()


def _resolve_coaching_workshop_project_id():
    """
    Active project for add-coaching / add-workshop.
    Uses ?project= on GET, project_id on POST; must fall within get_accessible_project_ids() for non-admin.
    """
    accessible = get_accessible_project_ids()
    chosen = request.args.get('project', type=int)
    if request.method == 'POST':
        chosen = request.form.get('project_id', type=int) or chosen
    if accessible is None:
        if chosen and Project.query.get(chosen):
            return chosen
        return get_visible_project_id()
    if not accessible:
        return None
    if chosen and chosen in accessible:
        return chosen
    return get_visible_project_id()


def _sync_assigned_coaching_status_from_progress(assignment):
    """Mark assignment completed when expected_coaching_count is reached; reopen if count drops below (e.g. delete)."""
    if not assignment:
        return
    exp = assignment.expected_coaching_count or 0
    if exp <= 0:
        return
    st = assignment.status
    if st in ('cancelled', 'rejected', 'expired'):
        return
    done = Coaching.query.filter_by(assigned_coaching_id=assignment.id).count()
    if done >= exp:
        if st in ('pending', 'accepted', 'in_progress'):
            assignment.status = 'completed'
            _snapshot_assignment_end_kpis(assignment)
    elif done > 0:
        if st in ('pending', 'accepted'):
            assignment.status = 'in_progress'
    elif st == 'completed':
        assignment.status = 'accepted'


def _assignment_eligible_to_link_coaching(assignment):
    """Coach may link a coaching only to open assignments whose deadline has not passed."""
    if not assignment:
        return False
    if assignment.status not in ('pending', 'accepted', 'in_progress'):
        return False
    if not assignment.deadline:
        return True
    now = datetime.now(timezone.utc)
    dl = assignment.deadline
    if dl.tzinfo is not None:
        return dl >= now
    return dl >= datetime.utcnow()


def _user_can_assign_coachings():
    return current_user.has_permission('assign_coachings')


def _active_assignment_counts_for_members(member_ids):
    if not member_ids:
        return {}
    rows = db.session.query(
        AssignedCoaching.team_member_id,
        db.func.count(AssignedCoaching.id),
    ).filter(
        AssignedCoaching.team_member_id.in_(member_ids),
        AssignedCoaching.status.in_(['pending', 'accepted', 'in_progress']),
    ).group_by(AssignedCoaching.team_member_id).all()
    return {mid: int(cnt or 0) for mid, cnt in rows}


def _member_ids_from_assign_request():
    """Parse team member ids from ?member_ids= / ?member_id= (GET) or form list (POST)."""
    if request.method == 'POST':
        ids = request.form.getlist('team_member_ids')
    else:
        ids = request.args.getlist('member_ids')
    out = []
    for raw in ids:
        if raw is None:
            continue
        s = str(raw).strip()
        if not s:
            continue
        for part in s.split(','):
            part = part.strip()
            if part.isdigit():
                out.append(int(part))
    if not out and request.method != 'POST':
        single = request.args.get('member_id', type=int)
        if single:
            out.append(single)
    seen = set()
    deduped = []
    for i in out:
        if i not in seen:
            seen.add(i)
            deduped.append(i)
    return deduped


def _member_performance_for_assigned_page(project_id, kpi_period='this_month', kpi_from=None, kpi_to=None):
    members = TeamMember.query.join(Team, TeamMember.team_id == Team.id).filter(
        Team.project_id == project_id,
        Team.name != ARCHIV_TEAM_NAME,
        or_(Team.active_for_coaching.is_(True), Team.visible_for_coaching_assignment.is_(True)),
    ).all()
    member_ids = [m.id for m in members]
    active_counts = _active_assignment_counts_for_members(member_ids)
    raw = []
    for m in members:
        stats = db.session.query(
            db.func.count(Coaching.id),
            db.func.avg(Coaching.performance_mark),
            db.func.sum(Coaching.time_spent),
            db.func.max(Coaching.coaching_date),
        ).filter(
            Coaching.team_member_id == m.id,
            Coaching.project_id == project_id,
        ).first()
        cnt = int(stats[0] or 0)
        avg_m = stats[1]
        total_t = int(stats[2] or 0)
        last_d = stats[3]
        avg_score = round(float(avg_m or 0) * 10, 1) if cnt > 0 else 0.0
        raw.append({
            'member': m,
            'coaching_count': cnt,
            'avg_score': avg_score,
            'total_time': total_t,
            'last_coaching_date': last_d,
        })
    if not raw:
        return []
    out = []
    for r in raw:
        m = r['member']
        out.append({
            'id': m.id,
            'name': m.name,
            'team_name': m.team.name if m.team else '',
            'avg_score': r['avg_score'],
            'coaching_count': r['coaching_count'],
            'total_time': r['total_time'],
            'last_coaching_date': r['last_coaching_date'],
            'active_assignment_count': active_counts.get(m.id, 0),
        })
    kpi_map = {}
    if kpi_logic.kpi_features_enabled():
        kpi_map = _members_kpi_map(
            project_id, member_ids, kpi_period=kpi_period, kpi_from=kpi_from, kpi_to=kpi_to,
        )
    for row in out:
        k = kpi_map.get(row['id'], {})
        row['nps'] = k.get('nps')
        row['nps_count'] = k.get('nps_count', 0)
        row['loesung_quote'] = k.get('loes_quote')
        row['loesung_count'] = k.get('loes_count', 0)
        row['info_quote'] = k.get('info_quote')
        row['info_count'] = k.get('info_count', 0)
        row['surveys_total'] = k.get('surveys_total', 0)
    return out


# Helper for date ranges
def calculate_date_range(period_arg):
    today = datetime.now(timezone.utc).date()
    if period_arg == 'today':
        start = datetime.combine(today, datetime.min.time())
        end = datetime.combine(today, datetime.max.time())
    elif period_arg == 'yesterday':
        yesterday = today - timedelta(days=1)
        start = datetime.combine(yesterday, datetime.min.time())
        end = datetime.combine(yesterday, datetime.max.time())
    elif period_arg == 'this_week':
        start_of_week = today - timedelta(days=today.weekday())
        start = datetime.combine(start_of_week, datetime.min.time())
        end = datetime.combine(today, datetime.max.time())
    elif period_arg == 'last_week':
        start_of_last_week = today - timedelta(days=today.weekday() + 7)
        end_of_last_week = start_of_last_week + timedelta(days=6)
        start = datetime.combine(start_of_last_week, datetime.min.time())
        end = datetime.combine(end_of_last_week, datetime.max.time())
    elif period_arg == 'this_month':
        start = datetime.combine(today.replace(day=1), datetime.min.time())
        end = datetime.combine(today, datetime.max.time())
    elif period_arg == 'last_month':
        first_of_this_month = today.replace(day=1)
        last_of_last_month = first_of_this_month - timedelta(days=1)
        first_of_last_month = last_of_last_month.replace(day=1)
        start = datetime.combine(first_of_last_month, datetime.min.time())
        end = datetime.combine(last_of_last_month, datetime.max.time())
    elif period_arg == '7days':
        start_day = today - timedelta(days=6)
        start = datetime.combine(start_day, datetime.min.time())
        end = datetime.combine(today, datetime.max.time())
    elif period_arg == '30days':
        start_day = today - timedelta(days=29)
        start = datetime.combine(start_day, datetime.min.time())
        end = datetime.combine(today, datetime.max.time())
    elif period_arg == 'current_quarter':
        q = (today.month - 1) // 3
        first_month = q * 3 + 1
        start = datetime.combine(date(today.year, first_month, 1), datetime.min.time())
        end = datetime.combine(today, datetime.max.time())
    elif period_arg == 'current_year':
        start = datetime.combine(date(today.year, 1, 1), datetime.min.time())
        end = datetime.combine(today, datetime.max.time())
    elif period_arg and len(period_arg) == 7 and period_arg[4] == '-':
        try:
            y = int(period_arg[0:4])
            mo = int(period_arg[5:7])
            last_d = calendar.monthrange(y, mo)[1]
            start = datetime.combine(date(y, mo, 1), datetime.min.time())
            end = datetime.combine(date(y, mo, last_d), datetime.max.time())
        except ValueError:
            start = None
            end = None
    elif period_arg == 'vonbis':
        start = None
        end = None
    else:
        start = None
        end = None
    return start, end


def _parse_coaching_dashboard_von_bis(date_from_str, date_to_str):
    """UTC-naive day bounds from YYYY-MM-DD; None if invalid."""
    if not date_from_str or not date_to_str:
        return None, None
    try:
        d0 = datetime.strptime(date_from_str.strip(), '%Y-%m-%d').date()
        d1 = datetime.strptime(date_to_str.strip(), '%Y-%m-%d').date()
    except ValueError:
        return None, None
    if d0 > d1:
        d0, d1 = d1, d0
    start = datetime.combine(d0, datetime.min.time())
    end = datetime.combine(d1, datetime.max.time())
    return start, end


def get_month_name_german(month_num):
    return ['Januar', 'Februar', 'März', 'April', 'Mai', 'Juni',
            'Juli', 'August', 'September', 'Oktober', 'November', 'Dezember'][month_num-1]


def _month_series_inclusive(start_d: date, end_d: date):
    """Calendar months from start_d's month through end_d's month, inclusive."""
    y, m = start_d.year, start_d.month
    ey, em = end_d.year, end_d.month
    out = []
    while (y, m) <= (ey, em):
        out.append((y, m))
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return out


def _day_series_inclusive(start_d: date, end_d: date):
    out = []
    d = start_d
    while d <= end_d:
        out.append(d)
        d += timedelta(days=1)
    return out


def _monday_on_or_before(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _week_start_series_inclusive(start_d: date, end_d: date):
    w0 = _monday_on_or_before(start_d)
    w1 = _monday_on_or_before(end_d)
    out = []
    w = w0
    while w <= w1:
        out.append(w)
        w += timedelta(days=7)
    return out


def _coaching_timeline_bucket(period_arg, cal_date_active, start_date, end_date):
    """hour | day | week | month — aligned with Zeitraum filter."""
    if cal_date_active:
        return 'hour'
    pa = (period_arg or '').strip()
    if pa == 'all' or pa == '':
        return 'month'
    if pa in (
        '7days',
        '30days',
        'today',
        'yesterday',
        'this_week',
        'last_week',
        'this_month',
        'last_month',
    ):
        return 'day'
    if len(pa) == 7 and pa[4] == '-':
        return 'day'
    if pa == 'current_quarter':
        return 'week'
    if pa == 'current_year':
        return 'month'
    if start_date and end_date:
        sd = start_date.date() if isinstance(start_date, datetime) else start_date
        ed = end_date.date() if isinstance(end_date, datetime) else end_date
        span = (ed - sd).days + 1
        if span <= 31:
            return 'day'
        if span <= 120:
            return 'week'
        return 'month'
    return 'month'


def _coaching_dashboard_zeitraum_series(
    period_arg, cal_date_active, graph_filters, start_date, end_date
):
    """
    Labels and counts for „Coachings / Zeitraum“: bucket size follows the selected period
    (days for 7/30 Tage & Monate, weeks for Quartal, months for Jahr & „Alles“, hours for Kalendertag).
    """

    def _gq(*entities):
        return (
            db.session.query(*entities)
            .select_from(Coaching)
            .join(TeamMember, Coaching.team_member_id == TeamMember.id)
            .join(Team, TeamMember.team_id == Team.id)
            .outerjoin(User, Coaching.coach_id == User.id)
            .filter(*graph_filters)
        )

    bucket = _coaching_timeline_bucket(period_arg, cal_date_active, start_date, end_date)

    if bucket == 'hour':
        hr = extract('hour', Coaching.coaching_date)
        rows = (
            _gq(hr, db.func.count(Coaching.id))
            .group_by(hr)
            .order_by(hr)
            .all()
        )
        counts = {int(r[0]): int(r[1] or 0) for r in rows if r[0] is not None}
        labels = [f"{h:02d}:00" for h in range(24)]
        values = [counts.get(h, 0) for h in range(24)]
        return labels, values

    if bucket == 'day':
        day_col = cast(Coaching.coaching_date, Date)
        rows = (
            _gq(day_col, db.func.count(Coaching.id))
            .group_by(day_col)
            .order_by(day_col)
            .all()
        )
        counts_map = {}
        for r in rows:
            d0 = r[0]
            if d0 is None:
                continue
            if isinstance(d0, datetime):
                d0 = d0.date()
            counts_map[d0] = int(r[1] or 0)
        if start_date and end_date:
            sd = start_date.date() if isinstance(start_date, datetime) else start_date
            ed = end_date.date() if isinstance(end_date, datetime) else end_date
            days = _day_series_inclusive(sd, ed)
        elif counts_map:
            keys_sorted = sorted(counts_map.keys())
            days = _day_series_inclusive(keys_sorted[0], keys_sorted[-1])
        else:
            return [], []
        labels = [d.strftime('%d.%m.%Y') for d in days]
        values = [counts_map.get(d, 0) for d in days]
        return labels, values

    if bucket == 'week':
        wk = db.func.date_trunc('week', Coaching.coaching_date)
        rows = (
            _gq(wk, db.func.count(Coaching.id))
            .group_by(wk)
            .order_by(wk)
            .all()
        )
        counts_map = {}
        for r in rows:
            ts = r[0]
            if ts is None:
                continue
            d0 = ts.date() if isinstance(ts, datetime) else ts
            counts_map[d0] = int(r[1] or 0)
        if start_date and end_date:
            sd = start_date.date() if isinstance(start_date, datetime) else start_date
            ed = end_date.date() if isinstance(end_date, datetime) else end_date
            weeks = _week_start_series_inclusive(sd, ed)
        elif counts_map:
            keys_sorted = sorted(counts_map.keys())
            weeks = _week_start_series_inclusive(keys_sorted[0], keys_sorted[-1])
        else:
            return [], []
        labels = []
        for w0 in weeks:
            iso = w0.isocalendar()
            labels.append(f"KW {iso[1]}/{iso[0]}")
        values = [counts_map.get(w0, 0) for w0 in weeks]
        return labels, values

    # month
    cy = extract('year', Coaching.coaching_date)
    cm = extract('month', Coaching.coaching_date)
    rows = (
        _gq(cy, cm, db.func.count(Coaching.id))
        .group_by(cy, cm)
        .order_by(cy, cm)
        .all()
    )
    counts_map = {(int(r[0]), int(r[1])): int(r[2] or 0) for r in rows}
    if start_date and end_date:
        sd = start_date.date() if isinstance(start_date, datetime) else start_date
        ed = end_date.date() if isinstance(end_date, datetime) else end_date
        months = _month_series_inclusive(sd, ed)
    elif counts_map:
        keys_sorted = sorted(counts_map.keys())
        y0, m0 = keys_sorted[0]
        y1, m1 = keys_sorted[-1]
        months = _month_series_inclusive(date(y0, m0, 1), date(y1, m1, 1))
    else:
        return [], []
    labels = [f"{get_month_name_german(m)} {y}" for y, m in months]
    values = [counts_map.get((y, m), 0) for y, m in months]
    return labels, values


def get_allowed_project_ids_for_reviews():
    """Projects a user may see when using view_all_reviews."""
    ids = get_accessible_project_ids()
    if ids is None:
        ap = session.get('active_project')
        if ap:
            return [ap]
        return [p.id for p in Project.query.order_by(Project.name).all()]
    return ids


def apply_coaching_date_filters(query, period_arg, year, month, day):
    """Preset period and/or explicit Jahr/Monat/Tag (UTC day boundaries). Query must be on Coaching."""
    if year is not None:
        try:
            if month is not None and day is not None:
                d0 = date(year, month, day)
                start = datetime.combine(d0, datetime.min.time()).replace(tzinfo=timezone.utc)
                end = datetime.combine(d0, datetime.max.time()).replace(tzinfo=timezone.utc)
            elif month is not None:
                last_d = calendar.monthrange(year, month)[1]
                start = datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc)
                end = datetime(year, month, last_d, 23, 59, 59, 999999, tzinfo=timezone.utc)
            else:
                start = datetime(year, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
                end = datetime(year, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
            query = query.filter(Coaching.coaching_date >= start, Coaching.coaching_date <= end)
        except ValueError:
            pass
    else:
        start, end = calculate_date_range(period_arg)
        if start:
            query = query.filter(Coaching.coaching_date >= start)
        if end:
            query = query.filter(Coaching.coaching_date <= end)
    return query


def _get_teams_for_team_view():
    """Teams for /team-view: PL/QM = aktive Projektteams mit mind. einem Mitglied (ohne ARCHIV); view_own_team = eigene Teams ohne ARCHIV/inaktiv."""
    archiv = get_or_create_archiv_team()
    archiv_id = archiv.id
    has_members = exists().where(TeamMember.team_id == Team.id)
    if current_user.has_permission('view_pl_qm_dashboard'):
        project_id = get_visible_project_id()
        if not project_id:
            return []
        return Team.query.filter(
            Team.project_id == project_id,
            Team.id != archiv_id,
            Team.name != ARCHIV_TEAM_NAME,
            Team.active_for_coaching.is_(True),
            has_members,
        ).order_by(Team.name).all()
    if not current_user.has_permission('view_own_team'):
        return []
    seen = set()
    teams = []
    for tm in current_user.team_members:
        if not tm.team_id or tm.team_id == archiv_id or tm.team_id in seen:
            continue
        team = Team.query.get(tm.team_id)
        if not team or team.name == ARCHIV_TEAM_NAME or not team.active_for_coaching:
            continue
        teams.append(team)
        seen.add(team.id)
    teams.sort(key=lambda x: x.name)
    return teams


def _teams_for_assigned_coaching_filters(project_id_single=None, gesamt_acc=None, gesamt_project_filter=None):
    """
    Teams für die Team-Auswahl auf „Zugewiesene Coachings“ und Gesamtbericht — nur was die Rolle sehen darf
    (wie Mein Team / PL-Dashboard: nicht alle Projektteams für Teamleiter/Coach mit coach_own_team_only).
    """
    archiv = get_or_create_archiv_team()
    archiv_id = archiv.id
    has_members = exists().where(TeamMember.team_id == Team.id)

    if project_id_single is not None:
        proj_ids = [project_id_single]
    else:
        if gesamt_project_filter is not None:
            proj_ids = [gesamt_project_filter]
        elif gesamt_acc is None:
            proj_ids = None
        else:
            proj_ids = list(gesamt_acc) if gesamt_acc else []

    q = Team.query.filter(
        Team.id != archiv_id,
        Team.name != ARCHIV_TEAM_NAME,
    )
    if proj_ids is not None:
        if not proj_ids:
            return []
        q = q.filter(Team.project_id.in_(proj_ids))

    if current_user.role_name in (ROLE_ADMIN, ROLE_BETRIEBSLEITER):
        q = q.filter(Team.active_for_coaching.is_(True), has_members)
        return q.order_by(Team.name).all()

    _led_team_ids = {tm.team_id for tm in current_user.team_members if tm.team_id}
    if current_user.has_permission('coach_own_team_only') or current_user.role_name == ROLE_TEAMLEITER:
        if not _led_team_ids:
            return []
        q = q.filter(Team.id.in_(_led_team_ids), Team.active_for_coaching.is_(True))
        return q.order_by(Team.name).all()

    if current_user.has_permission('view_pl_qm_dashboard') or current_user.has_permission('assign_coachings'):
        q = q.filter(Team.active_for_coaching.is_(True), has_members)
        return q.order_by(Team.name).all()

    q = q.filter(Team.active_for_coaching.is_(True), has_members)
    return q.order_by(Team.name).all()


def _user_sees_all_teams_coaching_dashboard():
    if current_user.role_name in (ROLE_ADMIN, ROLE_BETRIEBSLEITER):
        return True
    return current_user.has_permission('view_coaching_dashboard_all_teams')


def _dashboard_my_team_ids():
    """Team IDs where the user has a TeamMember row (Mein Team basis), excluding ARCHIV."""
    archiv = get_or_create_archiv_team()
    archiv_id = archiv.id
    seen = set()
    out = []
    for tm in current_user.team_members:
        if not tm.team_id or tm.team_id == archiv_id or tm.team_id in seen:
            continue
        team = tm.team
        if team and team.name != ARCHIV_TEAM_NAME:
            out.append(tm.team_id)
            seen.add(tm.team_id)
    return out


def _coaching_dashboard_resolve_member_filter(
    member_id,
    accessible,
    dashboard_project_id,
    team_arg,
    sees_all_teams,
    my_dash_team_ids,
):
    """
    Teammitglied aus ?member_id= — nur wenn sichtbar im Dashboard-Scope (Projekt, Team, Rechte, kein ARCHIV).
    """
    if not member_id:
        return None
    tm = TeamMember.query.options(joinedload(TeamMember.team)).get(member_id)
    if not tm or not tm.team:
        return None
    team = tm.team
    if team.name == ARCHIV_TEAM_NAME or not team.active_for_coaching:
        return None
    if accessible is not None:
        if not accessible or team.project_id not in accessible:
            return None
    if dashboard_project_id == -1:
        return None
    if dashboard_project_id is not None and team.project_id != dashboard_project_id:
        return None
    if team_arg != 'all' and team_arg.isdigit():
        if int(team_arg) != team.id:
            return None
    if not sees_all_teams:
        if not my_dash_team_ids or team.id not in my_dash_team_ids:
            return None
    return tm


def _coaching_dashboard_query_joined(base_query):
    """Join path required whenever filters reference TeamMember, Team, or coach User."""
    return base_query.join(
        TeamMember, Coaching.team_member_id == TeamMember.id
    ).join(
        Team, TeamMember.team_id == Team.id
    ).outerjoin(
        User, Coaching.coach_id == User.id
    )


def _build_coaching_dashboard_bar_charts(graph_filters, group_by='team'):
    """Bar-chart series by team or by project (same filters as dashboard graphs)."""
    if group_by == 'project':
        entities = (
            db.session.query(Project.id, Project.name)
            .select_from(Coaching)
            .join(TeamMember, Coaching.team_member_id == TeamMember.id)
            .join(Team, TeamMember.team_id == Team.id)
            .join(Project, Coaching.project_id == Project.id)
            .outerjoin(User, Coaching.coach_id == User.id)
            .filter(*graph_filters)
            .distinct()
            .order_by(Project.name)
            .all()
        )
    else:
        entities = (
            db.session.query(Team.id, Team.name)
            .select_from(Coaching)
            .join(TeamMember, Coaching.team_member_id == TeamMember.id)
            .join(Team, TeamMember.team_id == Team.id)
            .outerjoin(User, Coaching.coach_id == User.id)
            .filter(*graph_filters)
            .distinct()
            .order_by(Team.name)
            .all()
        )

    labels = []
    avg_performance = []
    total_time = []
    coachings_count = []
    for ent in entities:
        if group_by == 'project':
            ent_filters = [Coaching.project_id == ent.id] + list(graph_filters)
        else:
            ent_filters = [TeamMember.team_id == ent.id] + list(graph_filters)
        stats = (
            db.session.query(
                db.func.avg(Coaching.performance_mark),
                db.func.sum(Coaching.time_spent),
                db.func.count(Coaching.id),
            )
            .select_from(Coaching)
            .join(TeamMember, Coaching.team_member_id == TeamMember.id)
            .join(Team, TeamMember.team_id == Team.id)
            .outerjoin(User, Coaching.coach_id == User.id)
            .filter(*ent_filters)
            .first()
        )
        labels.append(ent.name)
        avg_performance.append(round((stats[0] or 0) * 10, 1))
        total_time.append(stats[1] or 0)
        coachings_count.append(stats[2] or 0)
    return labels, avg_performance, total_time, coachings_count


def _coaching_dashboard_chart_group_arg(param_name, has_teams, has_projects):
    raw = (request.args.get(param_name) or 'teams').strip()
    if raw not in ('teams', 'projects'):
        raw = 'teams'
    if raw == 'projects' and not has_projects:
        raw = 'teams'
    if raw == 'teams' and not has_teams and has_projects:
        raw = 'projects'
    return raw


def _build_team_members_performance(team):
    project_id = team.project_id
    members = TeamMember.query.filter_by(team_id=team.id).order_by(TeamMember.name).all()
    member_ids = [m.id for m in members]
    kpi_map = {}
    prod_map = {}
    if kpi_logic.kpi_features_enabled():
        if can_view_kpi_qualitaet(current_user):
            kpi_map = _members_kpi_map(project_id, member_ids, kpi_period='all')
        if can_view_kpi_produktivitaet(current_user):
            prod_map = _members_productivity_map(project_id, member_ids)
    team_members_performance = []
    for member in members:
        m_stats = db.session.query(
            db.func.count(Coaching.id),
            db.func.avg(Coaching.performance_mark),
            db.func.sum(Coaching.time_spent)
        ).filter(Coaching.team_member_id == member.id, Coaching.project_id == project_id).first()
        total_c = m_stats[0] or 0
        avg_perf = round((m_stats[1] or 0) * 10, 1) if total_c > 0 else 0
        total_t = m_stats[2] or 0
        hours = total_t // 60
        mins = total_t % 60
        formatted_time = f"{hours}h {mins}m" if hours > 0 else f"{mins}m"

        if total_c > 0:
            member_coachings = Coaching.query.filter_by(team_member_id=member.id, project_id=project_id).all()
            total_checks = 0
            positive_checks = 0
            for c in member_coachings:
                for _, val in c.leitfaden_fields_list:
                    if val and val != 'k.A.':
                        total_checks += 1
                        if str(val).lower() in ['ja', 'yes', '1', 'true']:
                            positive_checks += 1
            avg_leitfaden = round((positive_checks / total_checks * 100), 1) if total_checks > 0 else 0
        else:
            avg_leitfaden = 0

        kpi = kpi_map.get(member.id, {})
        prod = prod_map.get(member.id, {})
        team_members_performance.append({
            'id': member.id,
            'name': member.name,
            'total_coachings': total_c,
            'avg_score': avg_perf,
            'total_time': total_t,
            'formatted_total_coaching_time': formatted_time,
            'avg_leitfaden_adherence': avg_leitfaden,
            'nps': kpi.get('nps'),
            'nps_count': kpi.get('nps_count', 0),
            'loesung_quote': kpi.get('loes_quote'),
            'loesung_count': kpi.get('loes_count', 0),
            'info_quote': kpi.get('info_quote'),
            'info_count': kpi.get('info_count', 0),
            'fachkompetenz': kpi.get('fachkompetenz'),
            'fachkompetenz_count': kpi.get('fachkompetenz_count', 0),
            'vertrieb_quote': kpi.get('vertrieb_quote'),
            'vertrieb_count': kpi.get('vertrieb_count', 0),
            'sign_on_pct': prod.get('sign_on_pct'),
            'prod_pct': prod.get('prod_pct'),
            'nach_per_call': prod.get('nach_per_call'),
            'idle_pct': prod.get('idle_pct'),
            'prod_calls': prod.get('calls'),
            'prod_works': prod.get('works'),
            'prod_intervals': prod.get('intervals', 0),
        })
    return team_members_performance


def _team_leaders_for_team_card(team):
    """Auf der Karte als Teamleiter: im Team als Mitglied zugeordnet ``TeamMember.user_id`` und Berechtigung view_own_team."""
    users = (
        User.query.options(
            joinedload(User.role).joinedload(Role.permissions),
            selectinload(User.team_members),
        )
        .join(TeamMember, TeamMember.user_id == User.id)
        .filter(TeamMember.team_id == team.id, TeamMember.user_id.isnot(None))
        .distinct()
        .all()
    )
    eligible = [u for u in users if u.has_permission('view_own_team')]
    return sorted(eligible, key=lambda u: (u.coach_display_name or u.username or '').lower())


def filter_reviews_by_coaching_date(query, period_arg, year, month, day):
    """CoachingReview query already joined to Coaching; filter on coaching_date."""
    if year is not None:
        try:
            if month is not None and day is not None:
                d0 = date(year, month, day)
                start = datetime.combine(d0, datetime.min.time()).replace(tzinfo=timezone.utc)
                end = datetime.combine(d0, datetime.max.time()).replace(tzinfo=timezone.utc)
            elif month is not None:
                last_d = calendar.monthrange(year, month)[1]
                start = datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc)
                end = datetime(year, month, last_d, 23, 59, 59, 999999, tzinfo=timezone.utc)
            else:
                start = datetime(year, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
                end = datetime(year, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
            query = query.filter(Coaching.coaching_date >= start, Coaching.coaching_date <= end)
        except ValueError:
            pass
    else:
        start, end = calculate_date_range(period_arg)
        if start:
            query = query.filter(Coaching.coaching_date >= start)
        if end:
            query = query.filter(Coaching.coaching_date <= end)
    return query


def my_coachings_filter_query_args():
    """Preserve filters when redirecting after POST."""
    d = {}
    for key in ('period', 'year', 'month', 'day'):
        v = request.args.get(key)
        if v is not None and v != '':
            d[key] = v
    return d


def build_filter_args(period_arg, year, month, day, extra=None):
    args = {'period': period_arg}
    if year is not None:
        args['year'] = year
    if month is not None:
        args['month'] = month
    if day is not None:
        args['day'] = day
    if extra:
        args.update(extra)
    return args


def url_for_paginated(endpoint, page, filter_args):
    kw = dict(filter_args)
    kw['page'] = page
    return url_for(endpoint, **kw)


def _assigned_coachings_index_badge_count(user):
    """
    Offene zugewiesene Coachings für die Startseiten-Kachel: Status pending/accepted/in_progress.
    Zählt für den eingeloggten Nutzer als Coach und/oder als zuweisende Person (PL/QM-Ansicht),
    über alle Projekte aus get_accessible_project_ids().
    """
    acc = get_accessible_project_ids()
    if acc is not None and len(acc) == 0:
        return 0
    role_filters = []
    if user.has_permission('view_assigned_coachings'):
        role_filters.append(AssignedCoaching.coach_id == user.id)
    if user.has_permission('assign_coachings'):
        role_filters.append(AssignedCoaching.project_leader_id == user.id)
    if not role_filters:
        return 0
    q = (
        AssignedCoaching.query.join(TeamMember, AssignedCoaching.team_member_id == TeamMember.id)
        .join(Team, TeamMember.team_id == Team.id)
        .filter(
            AssignedCoaching.status.in_(['pending', 'accepted', 'in_progress']),
            or_(*role_filters),
        )
    )
    if acc is not None:
        q = q.filter(Team.project_id.in_(acc))
    return q.count()


def _assigned_coachings_scope_query(project_filter_id=None):
    """
    AssignedCoachings in Projekten aus get_accessible_project_ids() (inkl. Abteilungs-Scope).
    project_filter_id: optional eine der erlaubten Projekt-IDs.
    Returns None wenn keine Projekte sichtbar.
    """
    acc = get_accessible_project_ids()
    if acc is not None and len(acc) == 0:
        return None
    if project_filter_id is not None:
        if acc is not None and project_filter_id not in acc:
            return None
    q = (
        AssignedCoaching.query.join(TeamMember, AssignedCoaching.team_member_id == TeamMember.id)
        .join(Team, TeamMember.team_id == Team.id)
    )
    if acc is not None:
        q = q.filter(Team.project_id.in_(acc))
    if project_filter_id is not None:
        q = q.filter(Team.project_id == project_filter_id)
    return q


def _gesamtbericht_project_bar_extra(
    tab_active,
    team_filter,
    coach_filter,
    member_filter,
    search_term,
    sort_by,
    sort_dir,
    project_leader_filter=None,
):
    """Hidden Felder für Projektwechsel-Leiste auf dem Gesamtbericht."""
    d = {'status': tab_active}
    if team_filter:
        d['team'] = team_filter
    if coach_filter:
        d['coach'] = coach_filter
    if member_filter:
        d['member'] = member_filter
    if search_term:
        d['search'] = search_term
    if project_leader_filter:
        d['project_leader'] = project_leader_filter
    if sort_by != 'deadline':
        d['sort_by'] = sort_by
    if sort_dir != 'asc':
        d['sort_dir'] = sort_dir
    return d


@bp.route('/')
@login_required
def index():
    u = current_user
    index_tile_count = sum([
        1 if u.has_permission('view_coaching_dashboard') else 0,
        1 if u.has_permission('view_workshop_dashboard') else 0,
        1 if (
            u.has_permission('view_assigned_coachings')
            or u.has_permission('assign_coachings')
            or u.has_permission('view_pl_qm_dashboard')
        ) else 0,
        1 if u.has_permission('terminkalender') else 0,
        1 if (
            u.has_permission('planned_coachings')
            or u.has_permission('add_workshop')
            or u.has_permission('view_others_planned_coachings')
        ) else 0,
        1 if (u.has_permission('view_own_coachings') or u.has_permission('leave_coaching_review')) else 0,
        1 if u.has_permission('view_review') else 0,
        1 if u.has_permission('view_all_reviews') else 0,
    ])
    open_planned_coachings_count = 0
    if (
        u.has_permission('planned_coachings')
        or u.has_permission('add_workshop')
        or _can_view_others_planned_in_scope()
    ):
        open_planned_coachings_count = _count_open_planned_for_index()

    assigned_coachings_notify_count = 0
    if (
        u.has_permission('view_assigned_coachings')
        or u.has_permission('assign_coachings')
        or u.has_permission('view_pl_qm_dashboard')
    ):
        assigned_coachings_notify_count = _assigned_coachings_index_badge_count(u)

    return render_template(
        'main/index_choice.html',
        config=current_app.config,
        index_tile_count=index_tile_count,
        open_planned_coachings_count=open_planned_coachings_count,
        assigned_coachings_notify_count=assigned_coachings_notify_count,
    )


@bp.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    form = PasswordChangeForm()
    if form.validate_on_submit():
        if current_user.check_password(form.old_password.data):
            current_user.set_password(form.new_password.data)
            db.session.commit()
            flash('Passwort erfolgreich geändert.', 'success')
            return redirect(url_for('main.profile'))
        else:
            flash('Aktuelles Passwort ist falsch.', 'danger')
    return render_template('main/profile.html', form=form, config=current_app.config)


# --- Coaching Dashboard (your main dashboard) ---
@bp.route('/coaching-dashboard')
@login_required
@permission_required('view_coaching_dashboard')
def coaching_dashboard():
    page = request.args.get('page', 1, type=int)
    period_arg = (request.args.get('period') or '').strip() or '7days'
    team_arg = request.args.get('team', 'all')
    search_arg = request.args.get('search', default='', type=str).strip()
    project_raw = (request.args.get('project') or '').strip()
    project_filter_int = None
    project_scope_all = False

    accessible = get_accessible_project_ids()
    if project_raw.lower() == 'all':
        project_scope_all = True
    elif project_raw.isdigit():
        project_filter_int = int(project_raw)
    elif accessible is not None and len(accessible) > 1 and not project_raw:
        # Mehrere Projekte sichtbar, kein ?project= → standardmäßig „alle Projekte“
        project_scope_all = True

    sees_all_teams = _user_sees_all_teams_coaching_dashboard()
    my_dash_team_ids = _dashboard_my_team_ids() if not sees_all_teams else []

    # Scope filters: Projekt + Zeitraum + Team-Dropdown. KPI-Karten zählen inkl. ARCHIV; Grafiken & Coaching-Liste ohne ARCHIV-Coachees.
    scope_filters = []
    if accessible is None:
        if project_filter_int is not None:
            scope_filters.append(Coaching.project_id == project_filter_int)
    elif not accessible:
        scope_filters.append(Coaching.project_id == -1)
    else:
        if project_filter_int is not None and project_filter_int not in accessible:
            project_filter_int = None
        if project_scope_all:
            scope_filters.append(Coaching.project_id.in_(accessible))
        elif project_filter_int is not None:
            scope_filters.append(Coaching.project_id == project_filter_int)
        elif len(accessible) == 1:
            scope_filters.append(Coaching.project_id == accessible[0])
        else:
            vid = get_visible_project_id()
            if vid and vid in accessible:
                scope_filters.append(Coaching.project_id == vid)
            else:
                scope_filters.append(Coaching.project_id == accessible[0])

    if accessible is None:
        dashboard_project_id = project_filter_int
    elif not accessible:
        dashboard_project_id = -1
    else:
        if project_scope_all:
            dashboard_project_id = None
        elif project_filter_int is not None:
            dashboard_project_id = project_filter_int
        elif len(accessible) == 1:
            dashboard_project_id = accessible[0]
        else:
            vid = get_visible_project_id()
            dashboard_project_id = vid if (vid and vid in accessible) else accessible[0]

    cal_date_str = (request.args.get('cal_date') or '').strip()
    cal_date_active = None
    if cal_date_str:
        try:
            cal_date_active = datetime.strptime(cal_date_str, '%Y-%m-%d').date()
        except ValueError:
            cal_date_active = None
            cal_date_str = ''

    date_from_str = (request.args.get('date_from') or '').strip()
    date_to_str = (request.args.get('date_to') or '').strip()

    if cal_date_active:
        start_date, end_date = athens_calendar_day_utc_naive_bounds(cal_date_active)
    elif period_arg == 'vonbis':
        start_date, end_date = _parse_coaching_dashboard_von_bis(date_from_str, date_to_str)
        if not start_date:
            flash('Bitte gültiges von- und bis-Datum wählen (Zeitraum von-bis).', 'warning')
            period_arg = '7days'
            start_date, end_date = calculate_date_range('7days')
    else:
        start_date, end_date = calculate_date_range(period_arg)
    if start_date:
        scope_filters.append(Coaching.coaching_date >= start_date)
    if end_date:
        scope_filters.append(Coaching.coaching_date <= end_date)

    if team_arg != 'all' and team_arg.isdigit():
        tid = int(team_arg)
        team_row = Team.query.filter_by(id=tid).first()
        if (
            team_row
            and team_row.name != ARCHIV_TEAM_NAME
            and team_row.active_for_coaching
            and dashboard_project_id != -1
            and (accessible is None or team_row.project_id in accessible)
            and (dashboard_project_id is None or team_row.project_id == dashboard_project_id)
        ):
            scope_filters.append(Team.id == tid)

    member_arg = request.args.get('member_id', type=int)
    dashboard_member = None
    if member_arg:
        dashboard_member = _coaching_dashboard_resolve_member_filter(
            member_arg,
            accessible,
            dashboard_project_id,
            team_arg,
            sees_all_teams,
            my_dash_team_ids,
        )
        if not dashboard_member:
            flash('Mitarbeiter-Filter ungültig oder keine Berechtigung.', 'warning')
        else:
            scope_filters.append(Coaching.team_member_id == dashboard_member.id)

    archiv_team = get_or_create_archiv_team()
    # Graphs must hide every ARCHIV team row, not only the default ARCHIV team id.
    graph_filters = scope_filters + [Team.name != ARCHIV_TEAM_NAME]

    list_filters = list(scope_filters)
    list_filters.append(TeamMember.team_id != archiv_team.id)

    if search_arg:
        pattern = f"%{search_arg}%"
        list_filters.append(
            or_(
                TeamMember.name.ilike(pattern),
                User.username.ilike(pattern),
                Coaching.coaching_subject.ilike(pattern),
                Coaching.coach_notes.ilike(pattern),
            )
        )

    if not sees_all_teams:
        if my_dash_team_ids:
            list_filters.append(TeamMember.team_id.in_(my_dash_team_ids))
        else:
            list_filters.append(false())

    list_query = _coaching_dashboard_query_joined(
        Coaching.query.options(
            joinedload(Coaching.employee_review),
            selectinload(Coaching.coach).selectinload(User.team_members),
        )
    ).filter(*list_filters)

    coachings_paginated = list_query.order_by(desc(Coaching.coaching_date)).paginate(page=page, per_page=15, error_out=False)

    can_leave_review = current_user.has_permission('leave_coaching_review')
    review_form_dashboard = None
    review_redirect_next = ''
    if can_leave_review:
        qv = request.query_string.decode()
        review_redirect_next = request.path + (('?' + qv) if qv else '')
        review_form_dashboard = CoachingReviewForm()
        review_form_dashboard.next.data = review_redirect_next

    total_coachings = list_query.count()

    chart_labels, chart_avg_performance, chart_total_time, chart_coachings_count = (
        _build_coaching_dashboard_bar_charts(graph_filters, 'team')
    )
    (
        chart_project_labels,
        chart_project_avg_performance,
        chart_project_total_time,
        chart_project_coachings_count,
    ) = _build_coaching_dashboard_bar_charts(graph_filters, 'project')

    has_team_chart_data = bool(chart_labels)
    has_project_chart_data = bool(chart_project_labels)
    show_chart_group_toggle = len(chart_labels) > 1 or len(chart_project_labels) > 1
    chart_group_coachings = _coaching_dashboard_chart_group_arg(
        'chart_group_coachings', has_team_chart_data, has_project_chart_data
    )
    chart_group_time = _coaching_dashboard_chart_group_arg(
        'chart_group_time', has_team_chart_data, has_project_chart_data
    )
    chart_group_perf = _coaching_dashboard_chart_group_arg(
        'chart_group_perf', has_team_chart_data, has_project_chart_data
    )

    subject_counts = (
        db.session.query(Coaching.coaching_subject, db.func.count(Coaching.id))
        .select_from(Coaching)
        .join(TeamMember, Coaching.team_member_id == TeamMember.id)
        .join(Team, TeamMember.team_id == Team.id)
        .outerjoin(User, Coaching.coach_id == User.id)
        .filter(*graph_filters)
        .group_by(Coaching.coaching_subject)
        .all()
    )
    subject_chart_labels = [s[0] or 'Unbekannt' for s in subject_counts]
    subject_chart_values = [s[1] for s in subject_counts]

    chart_coaching_zeitraum_labels, chart_coaching_zeitraum_counts = (
        _coaching_dashboard_zeitraum_series(
            period_arg, cal_date_active, graph_filters, start_date, end_date
        )
    )

    global_stats = (
        db.session.query(db.func.count(Coaching.id), db.func.sum(Coaching.time_spent))
        .select_from(Coaching)
        .join(TeamMember, Coaching.team_member_id == TeamMember.id)
        .join(Team, TeamMember.team_id == Team.id)
        .outerjoin(User, Coaching.coach_id == User.id)
        .filter(*scope_filters)
        .first()
    )
    global_total_coachings_count = global_stats[0] or 0
    total_minutes = global_stats[1] or 0
    hours = total_minutes // 60
    minutes = total_minutes % 60
    global_time_coached_display = f"{hours} Std. {minutes} Min. ({total_minutes} Min. gesamt)"
    
    # Team-Dropdown: „sichtbare“ Projektteams (nicht ARCHIV, aktiv, mindestens ein TeamMember — leere Teams ausblenden).
    team_dropdown_q = Team.query.filter(
        Team.name != ARCHIV_TEAM_NAME,
        Team.active_for_coaching.is_(True),
        exists().where(TeamMember.team_id == Team.id),
    )
    if dashboard_project_id is not None and dashboard_project_id != -1:
        all_teams_for_filter = team_dropdown_q.filter(Team.project_id == dashboard_project_id).order_by(Team.name).all()
    elif dashboard_project_id == -1:
        all_teams_for_filter = []
    elif accessible is not None and project_scope_all:
        all_teams_for_filter = team_dropdown_q.filter(Team.project_id.in_(accessible)).order_by(Team.name).all()
    else:
        all_teams_for_filter = team_dropdown_q.order_by(Team.name).all()

    # Month options
    now = datetime.now(timezone.utc)
    current_year = now.year
    previous_year = current_year - 1
    month_options = []
    for m in range(12, 0, -1):
        month_options.append({'value': f"{previous_year}-{m:02d}", 'text': f"{get_month_name_german(m)} {previous_year}"})
    for m in range(now.month, 0, -1):
        month_options.append({'value': f"{current_year}-{m:02d}", 'text': f"{get_month_name_german(m)} {current_year}"})
    
    # Project filter dropdown: all (admin) or only accessible
    if accessible is None:
        all_projects = Project.query.order_by(Project.name).all()
    elif len(accessible) > 1:
        all_projects = Project.query.filter(Project.id.in_(accessible)).order_by(Project.name).all()
    else:
        all_projects = []

    show_global_all_projects_option = accessible is None or (accessible is not None and len(accessible) > 1)
    coaching_dashboard_project_all_is_blank = accessible is None

    if accessible is None:
        current_project_filter = project_filter_int
    elif not accessible:
        current_project_filter = None
    else:
        if project_scope_all:
            current_project_filter = 'all'
        elif project_filter_int is not None:
            current_project_filter = project_filter_int
        elif len(accessible) == 1:
            current_project_filter = accessible[0]
        else:
            current_project_filter = dashboard_project_id

    coaching_dashboard_url_project = None
    if all_projects:
        if accessible is None:
            coaching_dashboard_url_project = project_filter_int
        elif not accessible:
            coaching_dashboard_url_project = None
        else:
            if project_scope_all:
                coaching_dashboard_url_project = 'all'
            elif project_filter_int is not None:
                coaching_dashboard_url_project = project_filter_int
            else:
                coaching_dashboard_url_project = dashboard_project_id

    cal_day_label = cal_date_active.strftime('%d.%m.%Y') if cal_date_active else None

    coaching_dashboard_persist_query = {'period': period_arg, 'team': team_arg}
    if show_chart_group_toggle:
        for _cg_key, _cg_val in (
            ('chart_group_coachings', chart_group_coachings),
            ('chart_group_time', chart_group_time),
            ('chart_group_perf', chart_group_perf),
        ):
            if _cg_val != 'teams':
                coaching_dashboard_persist_query[_cg_key] = _cg_val
    if search_arg:
        coaching_dashboard_persist_query['search'] = search_arg
    if cal_date_active and cal_date_str:
        coaching_dashboard_persist_query['cal_date'] = cal_date_str
    if period_arg == 'vonbis' and date_from_str and date_to_str:
        coaching_dashboard_persist_query['date_from'] = date_from_str
        coaching_dashboard_persist_query['date_to'] = date_to_str
    if dashboard_member:
        coaching_dashboard_persist_query['member_id'] = dashboard_member.id
    if coaching_dashboard_url_project is not None:
        coaching_dashboard_persist_query['project'] = coaching_dashboard_url_project

    coaching_dashboard_clear_member_href = url_for(
        'main.coaching_dashboard',
        **{k: v for k, v in coaching_dashboard_persist_query.items() if k != 'member_id'},
    )
    coaching_dashboard_search_reset_href = url_for(
        'main.coaching_dashboard',
        **{k: v for k, v in coaching_dashboard_persist_query.items() if k != 'search'},
    )

    return render_template('main/index.html',
                           title='Coaching Dashboard',
                           coachings_paginated=coachings_paginated,
                           total_coachings=total_coachings,
                           chart_labels=chart_labels,
                           chart_avg_performance_mark_percentage=chart_avg_performance,
                           chart_total_time_spent=chart_total_time,
                           chart_coachings_done=chart_coachings_count,
                           chart_project_labels=chart_project_labels,
                           chart_project_avg_performance=chart_project_avg_performance,
                           chart_project_total_time=chart_project_total_time,
                           chart_project_coachings_count=chart_project_coachings_count,
                           chart_group_coachings=chart_group_coachings,
                           chart_group_time=chart_group_time,
                           chart_group_perf=chart_group_perf,
                           show_chart_group_toggle=show_chart_group_toggle,
                           subject_chart_labels=subject_chart_labels,
                           subject_chart_values=subject_chart_values,
                           chart_coaching_zeitraum_labels=chart_coaching_zeitraum_labels,
                           chart_coaching_zeitraum_counts=chart_coaching_zeitraum_counts,
                           global_total_coachings_count=global_total_coachings_count,
                           global_time_coached_display=global_time_coached_display,
                           all_teams_for_filter=all_teams_for_filter,
                           all_projects=all_projects,
                           current_period_filter=period_arg,
                           current_team_id_filter=team_arg,
                           current_project_filter=current_project_filter,
                           show_global_all_projects_option=show_global_all_projects_option,
                           coaching_dashboard_project_all_is_blank=coaching_dashboard_project_all_is_blank,
                           coaching_dashboard_url_project=coaching_dashboard_url_project,
                           current_search_term=search_arg,
                           month_options=month_options,
                           can_leave_review=can_leave_review,
                           review_form_dashboard=review_form_dashboard,
                           review_redirect_next=review_redirect_next,
                           cal_date_filter=cal_date_str if cal_date_active else None,
                           cal_day_label=cal_day_label,
                           coaching_dashboard_persist_query=coaching_dashboard_persist_query,
                           coaching_dashboard_clear_member_href=coaching_dashboard_clear_member_href,
                           coaching_dashboard_search_reset_href=coaching_dashboard_search_reset_href,
                           dashboard_member_id=dashboard_member.id if dashboard_member else None,
                           dashboard_member_name=dashboard_member.name if dashboard_member else None,
                           coaching_dashboard_date_from=date_from_str if period_arg == 'vonbis' else '',
                           coaching_dashboard_date_to=date_to_str if period_arg == 'vonbis' else '',
                           config=current_app.config)


def _team_members_for_planned_coaching_picker(project_id=None):
    """Teammitglied-Auswahl für geplante Coachings (optional projektübergreifend im sichtbaren Scope)."""
    query = (
        TeamMember.query.join(Team, TeamMember.team_id == Team.id)
        .filter(
            Team.name != ARCHIV_TEAM_NAME,
            Team.active_for_coaching.is_(True),
        )
    )
    if project_id:
        query = query.filter(Team.project_id == project_id)
    else:
        accessible = get_accessible_project_ids()
        if accessible is not None:
            if not accessible:
                return []
            query = query.filter(Team.project_id.in_(accessible))

    if current_user.has_permission('coach_own_team_only'):
        coach_team_member = current_user.team_members[0] if current_user.team_members else None
        if coach_team_member:
            query = query.filter(TeamMember.team_id == coach_team_member.team_id)
        else:
            query = query.filter(false())
    members = query.order_by(Team.name, TeamMember.name).all()
    return [m for m in members if team_member_eligible_for_new_coaching(m)]


def _terminkalender_coaching_dashboard_project_kw():
    """project=… für Links zum Coaching-Dashboard (Kalenderfilter)."""
    acc = get_accessible_project_ids()
    raw = (request.args.get('project') or '').strip()
    if raw.lower() == 'all':
        if acc is not None and len(acc) > 1:
            return {'project': 'all'}
        return {}
    if raw.isdigit():
        pid = int(raw)
        if acc is None or (acc and pid in acc):
            return {'project': pid}
    vid = get_visible_project_id()
    if acc is None:
        return {'project': vid} if vid else {}
    if not acc:
        return {}
    if len(acc) == 1:
        return {'project': acc[0]}
    if vid and vid in acc:
        return {'project': vid}
    return {'project': 'all'}


@bp.route('/terminkalender')
@login_required
@permission_required('terminkalender')
def terminkalender():
    today = today_athens_date()
    try:
        year = request.args.get('year', type=int) or today.year
        month = request.args.get('month', type=int) or today.month
        date(year, month, 1)
    except ValueError:
        year, month = today.year, today.month

    acc = get_accessible_project_ids()
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    lo, _ = athens_calendar_day_utc_naive_bounds(first)
    _, hi = athens_calendar_day_utc_naive_bounds(last)

    empty_bucket = {
        'done_me': 0,
        'done_others': 0,
        'planned': 0,
        'assigned': 0,
        'ws_me': 0,
        'ws_others': 0,
        'planned_ws': 0,
    }
    counts = defaultdict(lambda: {k: v for k, v in empty_bucket.items()})
    cal_show_own_coachings = current_user.has_permission('add_coaching') or current_user.has_permission(
        'coach'
    )
    cal_show_own_workshops = current_user.has_permission('add_workshop') or current_user.has_permission(
        'coach'
    )

    archiv_team = get_or_create_archiv_team()
    sees_all_teams = _user_sees_all_teams_coaching_dashboard()
    my_dash_team_ids = _dashboard_my_team_ids() if not sees_all_teams else None

    if current_user.has_permission('view_coaching_dashboard'):
        q_done = (
            Coaching.query.filter(
                Coaching.coaching_date >= lo,
                Coaching.coaching_date <= hi,
            )
            .join(TeamMember, Coaching.team_member_id == TeamMember.id)
            .join(Team, TeamMember.team_id == Team.id)
            .filter(TeamMember.team_id != archiv_team.id)
        )
        if acc is not None:
            if acc:
                q_done = q_done.filter(Team.project_id.in_(acc))
            else:
                q_done = q_done.filter(false())
        if not sees_all_teams:
            if my_dash_team_ids:
                q_done = q_done.filter(TeamMember.team_id.in_(my_dash_team_ids))
            else:
                q_done = q_done.filter(false())
        for row in q_done.options(joinedload(Coaching.team_member)).all():
            d = utc_naive_or_aware_to_athens_date(row.coaching_date)
            if cal_show_own_coachings and row.coach_id == current_user.id:
                counts[d]['done_me'] += 1
            else:
                counts[d]['done_others'] += 1

    if current_user.has_permission('view_workshop_dashboard'):
        q_ws = Workshop.query.filter(
            Workshop.workshop_date >= lo,
            Workshop.workshop_date <= hi,
        )
        if acc is not None:
            if acc:
                q_ws = q_ws.filter(Workshop.project_id.in_(acc))
            else:
                q_ws = q_ws.filter(false())
        if not sees_all_teams:
            if my_dash_team_ids:
                q_ws = (
                    q_ws.join(workshop_participants, workshop_participants.c.workshop_id == Workshop.id)
                    .join(TeamMember, TeamMember.id == workshop_participants.c.team_member_id)
                    .filter(TeamMember.team_id.in_(my_dash_team_ids))
                    .distinct()
                )
            else:
                q_ws = q_ws.filter(false())
        for wrow in q_ws.all():
            wd = utc_naive_or_aware_to_athens_date(wrow.workshop_date)
            if cal_show_own_workshops and wrow.coach_id == current_user.id:
                counts[wd]['ws_me'] += 1
            else:
                counts[wd]['ws_others'] += 1

    planned_ws_capture_by_date = {}
    if current_user.has_permission('add_workshop'):
        q_pws = PlannedWorkshop.query.filter(
            PlannedWorkshop.coach_id == current_user.id,
            PlannedWorkshop.status == 'open',
            PlannedWorkshop.planned_for_date >= first,
            PlannedWorkshop.planned_for_date <= last,
        )
        if acc is not None:
            if acc:
                q_pws = q_pws.filter(
                    or_(PlannedWorkshop.project_id.in_(acc), PlannedWorkshop.project_id.is_(None))
                )
            else:
                q_pws = q_pws.filter(false())
        pws_rows = q_pws.order_by(PlannedWorkshop.planned_for_date, PlannedWorkshop.id).all()
        for pwr in pws_rows:
            counts[pwr.planned_for_date]['planned_ws'] += 1
            if pwr.planned_for_date <= today and pwr.planned_for_date not in planned_ws_capture_by_date:
                planned_ws_capture_by_date[pwr.planned_for_date] = (pwr.id, pwr.project_id)

    can_view_others_planned_cal = _can_view_others_planned_in_scope()
    if can_view_others_planned_cal:
        q_pws_o = PlannedWorkshop.query.filter(
            PlannedWorkshop.coach_id != current_user.id,
            PlannedWorkshop.status == 'open',
            PlannedWorkshop.planned_for_date >= first,
            PlannedWorkshop.planned_for_date <= last,
        )
        if acc is not None:
            if acc:
                q_pws_o = q_pws_o.filter(
                    or_(PlannedWorkshop.project_id.in_(acc), PlannedWorkshop.project_id.is_(None))
                )
            else:
                q_pws_o = q_pws_o.filter(false())
        for pwr in q_pws_o.all():
            counts[pwr.planned_for_date]['planned_ws'] += 1

    if current_user.has_permission('planned_coachings'):
        q_pl = PlannedCoaching.query.filter(
            PlannedCoaching.coach_id == current_user.id,
            PlannedCoaching.status == 'open',
            PlannedCoaching.planned_for_date >= first,
            PlannedCoaching.planned_for_date <= last,
        )
        if acc is not None:
            if acc:
                q_pl = q_pl.filter(
                    or_(PlannedCoaching.project_id.in_(acc), PlannedCoaching.project_id.is_(None))
                )
            else:
                q_pl = q_pl.filter(false())
        for pc in q_pl.all():
            counts[pc.planned_for_date]['planned'] += 1

    if can_view_others_planned_cal:
        q_pl_o = PlannedCoaching.query.filter(
            PlannedCoaching.coach_id != current_user.id,
            PlannedCoaching.status == 'open',
            PlannedCoaching.planned_for_date >= first,
            PlannedCoaching.planned_for_date <= last,
        )
        if acc is not None:
            if acc:
                q_pl_o = q_pl_o.filter(PlannedCoaching.project_id.in_(acc))
            else:
                q_pl_o = q_pl_o.filter(false())
        for pc in q_pl_o.all():
            counts[pc.planned_for_date]['planned'] += 1

    if current_user.has_permission('view_assigned_coachings'):
        q_as = (
            AssignedCoaching.query.filter(
                AssignedCoaching.coach_id == current_user.id,
                AssignedCoaching.status.in_(['pending', 'accepted', 'in_progress']),
                AssignedCoaching.deadline >= lo,
                AssignedCoaching.deadline <= hi,
            )
            .join(TeamMember, AssignedCoaching.team_member_id == TeamMember.id)
            .join(Team, TeamMember.team_id == Team.id)
        )
        if acc is not None:
            if acc:
                q_as = q_as.filter(Team.project_id.in_(acc))
            else:
                q_as = q_as.filter(false())
        for asn in q_as.all():
            ad = utc_naive_or_aware_to_athens_date(asn.deadline)
            if first <= ad <= last:
                counts[ad]['assigned'] += 1

    can_plan_c = current_user.has_permission('planned_coachings')
    can_plan_w = current_user.has_permission('add_workshop')
    show_terminkalender_planned = can_plan_c or can_view_others_planned_cal
    show_terminkalender_planned_ws = can_plan_w or can_view_others_planned_cal

    def enrich_day(d):
        z = counts[d]
        is_past = d < today
        is_future = d > today
        cap = planned_ws_capture_by_date.get(d)
        return {
            'date': d,
            'in_month': d.month == month,
            'is_today': d == today,
            'is_past': is_past,
            'is_future': is_future,
            'done_me': z['done_me'],
            'done_others': z['done_others'],
            'planned': z['planned'],
            'assigned': z['assigned'],
            'ws_me': z['ws_me'],
            'ws_others': z['ws_others'],
            'planned_ws': z['planned_ws'],
            'planned_workshop_capture_id': cap[0] if cap else None,
            'planned_workshop_capture_project_id': cap[1] if cap else None,
            'show_add': not is_past
            and (
                can_plan_c
                or can_plan_w
                or (d == today and current_user.has_permission('add_coaching'))
            ),
        }

    cal = calendar.Calendar(firstweekday=calendar.MONDAY)
    month_weeks = [[enrich_day(d) for d in wk] for wk in cal.monthdatescalendar(year, month)]

    week_start_raw = (request.args.get('week_start') or '').strip()
    try:
        week_start = datetime.strptime(week_start_raw, '%Y-%m-%d').date() if week_start_raw else today
    except ValueError:
        week_start = today
    week_start = week_start - timedelta(days=week_start.weekday())
    week_days = [enrich_day(week_start + timedelta(days=i)) for i in range(7)]

    dash_kw = _terminkalender_coaching_dashboard_project_kw()
    proj_raw = (request.args.get('project') or '').strip()

    add_coaching_project_id = _resolve_coaching_workshop_project_id() if current_user.has_permission('add_coaching') else None
    if not add_coaching_project_id and proj_raw.isdigit():
        add_coaching_project_id = int(proj_raw) if (acc is None or int(proj_raw) in acc) else None

    month_title = f'{get_month_name_german(month)} {year}'
    if month == 1:
        prev_y, prev_m = year - 1, 12
    else:
        prev_y, prev_m = year, month - 1
    if month == 12:
        next_y, next_m = year + 1, 1
    else:
        next_y, next_m = year, month + 1
    week_prev_start = week_start - timedelta(days=7)
    week_next_start = week_start + timedelta(days=7)
    week_end = week_start + timedelta(days=6)

    month_total_done_me = 0
    month_total_done_others = 0
    month_total_planned = 0
    month_total_assigned = 0
    month_total_ws_me = 0
    month_total_ws_others = 0
    month_total_planned_ws = 0
    d_agg = first
    while d_agg <= last:
        bucket = counts.get(d_agg, {})
        month_total_done_me += bucket.get('done_me', 0)
        month_total_done_others += bucket.get('done_others', 0)
        month_total_planned += bucket.get('planned', 0)
        month_total_assigned += bucket.get('assigned', 0)
        month_total_ws_me += bucket.get('ws_me', 0)
        month_total_ws_others += bucket.get('ws_others', 0)
        month_total_planned_ws += bucket.get('planned_ws', 0)
        d_agg += timedelta(days=1)

    return render_template(
        'main/terminkalender.html',
        title='Terminkalender',
        year=year,
        month=month,
        month_title=month_title,
        prev_y=prev_y,
        prev_m=prev_m,
        next_y=next_y,
        next_m=next_m,
        week_prev_start=week_prev_start,
        week_next_start=week_next_start,
        week_end=week_end,
        month_weeks=month_weeks,
        week_days=week_days,
        week_start=week_start,
        today=today,
        dash_kw=dash_kw,
        proj_query=proj_raw,
        can_plan=can_plan_c,
        can_plan_workshop=can_plan_w,
        can_add_coaching=current_user.has_permission('add_coaching'),
        add_coaching_project_id=add_coaching_project_id,
        has_perm_planned=can_plan_c,
        show_terminkalender_planned=show_terminkalender_planned,
        show_terminkalender_planned_ws=show_terminkalender_planned_ws,
        has_perm_assigned=current_user.has_permission('view_assigned_coachings'),
        has_perm_dash=current_user.has_permission('view_coaching_dashboard'),
        has_perm_workshop=current_user.has_permission('view_workshop_dashboard'),
        calendar_dash_project=dash_kw.get('project'),
        month_total_done_me=month_total_done_me,
        month_total_done_others=month_total_done_others,
        month_total_planned=month_total_planned,
        month_total_assigned=month_total_assigned,
        month_total_ws_me=month_total_ws_me,
        month_total_ws_others=month_total_ws_others,
        month_total_planned_ws=month_total_planned_ws,
        cal_show_own_coachings=cal_show_own_coachings,
        cal_show_own_workshops=cal_show_own_workshops,
        config=current_app.config,
    )


@bp.route('/terminkalender/plan-menu')
@login_required
def terminkalender_plan_menu():
    today = today_athens_date()
    from_planned_list = (request.args.get('source') or '').strip() == 'geplante-coachings'
    day_str = (request.args.get('day') or '').strip()
    plan_date = None
    if day_str:
        try:
            plan_date = datetime.strptime(day_str, '%Y-%m-%d').date()
        except ValueError:
            flash('Ungültiges Datum.', 'warning')
            if from_planned_list:
                return redirect(url_for('main.planned_coachings_list'))
            return redirect(url_for('main.terminkalender'))
        if plan_date < today:
            flash('Ein Termin kann nicht in der Vergangenheit liegen.', 'warning')
            if from_planned_list:
                return redirect(url_for('main.planned_coachings_list'))
            return redirect(url_for('main.terminkalender'))
    elif not from_planned_list:
        flash('Ungültiges Datum.', 'warning')
        return redirect(url_for('main.terminkalender'))

    can_plan_c = current_user.has_permission('planned_coachings')
    can_plan_w = current_user.has_permission('add_workshop')
    add_coaching_project_id = None
    if current_user.has_permission('add_coaching'):
        add_coaching_project_id = _resolve_coaching_workshop_project_id()
    workshop_project_id = None
    if can_plan_w:
        workshop_project_id = _resolve_coaching_workshop_project_id()
    acc = get_accessible_project_ids()
    proj_raw = (request.args.get('project') or '').strip()
    if not add_coaching_project_id and proj_raw.isdigit():
        pid = int(proj_raw)
        if acc is None or pid in acc:
            add_coaching_project_id = pid
    if can_plan_w and not workshop_project_id and proj_raw.isdigit():
        pid = int(proj_raw)
        if acc is None or pid in acc:
            workshop_project_id = pid

    is_today = plan_date == today if plan_date else False
    can_capture_today = (
        not from_planned_list
        and is_today
        and current_user.has_permission('add_coaching')
        and bool(add_coaching_project_id)
    )
    can_workshop_capture_today = (
        not from_planned_list
        and is_today
        and can_plan_w
        and bool(workshop_project_id)
    )
    if from_planned_list:
        show_plan_coaching = can_plan_c
        show_plan_workshop = can_plan_w
    else:
        show_plan_coaching = can_plan_c and not is_today
        show_plan_workshop = can_plan_w and not is_today

    if not (show_plan_coaching or show_plan_workshop or can_capture_today or can_workshop_capture_today):
        flash('Keine passende Berechtigung für diese Aktion.', 'danger')
        if from_planned_list:
            return redirect(url_for('main.planned_coachings_list'))
        return redirect(url_for('main.terminkalender'))

    return render_template(
        'main/terminkalender_plan_menu.html',
        title='Termin anlegen',
        plan_date=plan_date,
        is_today=is_today,
        show_plan_coaching=show_plan_coaching,
        show_plan_workshop=show_plan_workshop,
        can_capture_today=can_capture_today,
        can_workshop_capture_today=can_workshop_capture_today,
        add_coaching_project_id=add_coaching_project_id,
        workshop_project_id=workshop_project_id,
        from_planned_list=from_planned_list,
        config=current_app.config,
    )


@bp.route('/terminkalender/plan-workshop', methods=['GET', 'POST'])
@login_required
@permission_required('add_workshop')
def terminkalender_plan_workshop():
    today = today_athens_date()
    plan_date_str = (request.args.get('day') if request.method == 'GET' else request.form.get('plan_date')) or ''
    if request.method == 'GET' and not plan_date_str.strip():
        plan_date_str = (today + timedelta(days=1)).isoformat()
    try:
        plan_date = datetime.strptime(plan_date_str.strip(), '%Y-%m-%d').date()
    except ValueError:
        flash('Ungültiges Datum.', 'warning')
        return redirect(url_for('main.terminkalender'))

    if plan_date < today:
        flash('Ein Termin kann nicht in der Vergangenheit liegen.', 'warning')
        return redirect(url_for('main.terminkalender'))

    accessible = get_accessible_project_ids()
    if request.method == 'POST':
        project_id = request.form.get('project_id', type=int)
    else:
        project_id = _resolve_coaching_workshop_project_id()

    if accessible is not None:
        if not project_id or project_id not in accessible:
            project_id = get_visible_project_id()
        if not project_id or project_id not in accessible:
            flash('Bitte ein gültiges Projekt wählen.', 'danger')
            return redirect(url_for('main.terminkalender', year=plan_date.year, month=plan_date.month))
    elif not project_id:
        flash('Kein Projekt ausgewählt.', 'danger')
        return redirect(url_for('main.terminkalender', year=plan_date.year, month=plan_date.month))

    if request.method == 'POST':
        title = (request.form.get('title') or '').strip()
        if not title:
            flash('Bitte einen Workshop-Titel angeben.', 'warning')
            return redirect(url_for('main.terminkalender_plan_workshop', day=plan_date.isoformat()))
        notes = (request.form.get('notes') or '').strip()
        db.session.add(
            PlannedWorkshop(
                coach_id=current_user.id,
                project_id=project_id,
                title=title,
                planned_for_date=plan_date,
                notes=notes or None,
                status='open',
            )
        )
        db.session.commit()
        flash('Geplanter Workshop wurde angelegt.', 'success')
        return redirect(url_for('main.terminkalender', year=plan_date.year, month=plan_date.month))

    return render_template(
        'main/terminkalender_plan_workshop.html',
        title='Geplanten Workshop anlegen',
        plan_date=plan_date,
        project_id=project_id,
        today=today,
        config=current_app.config,
    )


@bp.route('/terminkalender/plan', methods=['GET', 'POST'])
@login_required
@permission_required('planned_coachings')
def terminkalender_plan():
    today = today_athens_date()
    plan_date_str = (request.args.get('day') if request.method == 'GET' else request.form.get('plan_date')) or ''
    if request.method == 'GET' and not plan_date_str.strip():
        plan_date_str = (today + timedelta(days=1)).isoformat()
    try:
        plan_date = datetime.strptime(plan_date_str.strip(), '%Y-%m-%d').date()
    except ValueError:
        flash('Ungültiges Datum.', 'warning')
        return redirect(url_for('main.terminkalender'))

    if plan_date < today:
        flash('Ein Termin kann nicht in der Vergangenheit liegen.', 'warning')
        return redirect(url_for('main.terminkalender'))

    accessible = get_accessible_project_ids()
    if request.method == 'POST':
        project_id = request.form.get('project_id', type=int)
    else:
        project_raw = (request.args.get('project') or '').strip().lower()
        if project_raw == 'all':
            project_id = None
        elif project_raw.isdigit():
            project_id = int(project_raw)
        else:
            project_id = _resolve_coaching_workshop_project_id()
            # With multiple visible projects, default picker scope should include all visible projects.
            if accessible is not None and len(accessible) > 1:
                project_id = None

    if accessible is not None:
        if project_id and project_id not in accessible:
            flash('Bitte ein gültiges Projekt wählen.', 'danger')
            return redirect(url_for('main.terminkalender', year=plan_date.year, month=plan_date.month))
        if not project_id and len(accessible) == 1:
            project_id = accessible[0]

    if request.method == 'POST':
        member_id = request.form.get('team_member_id', type=int)
        tm = TeamMember.query.get(member_id) if member_id else None
        allowed_ids = {m.id for m in _team_members_for_planned_coaching_picker(project_id)}
        if not tm or tm.id not in allowed_ids:
            flash('Bitte ein gültiges Teammitglied wählen.', 'warning')
            kw = {'day': plan_date.isoformat()}
            kw['project'] = project_id if project_id else 'all'
            return redirect(url_for('main.terminkalender_plan', **kw))
        notes = (request.form.get('notes') or '').strip()
        has_v = request.form.get('has_verabredung') == '1'
        vtext = (request.form.get('verabredung_text') or '').strip()
        create_planned_coaching_from_coaching_form(
            coach_user_id=current_user.id,
            team_member_id=member_id,
            planned_for_date=plan_date,
            project_id=tm.team.project_id if tm and tm.team else project_id,
            team_id=tm.team_id,
            notes=notes,
            has_verabredung=has_v,
            verabredung_text=vtext if has_v else '',
            source_coaching_id=None,
        )
        db.session.commit()
        flash('Geplantes Coaching wurde angelegt.', 'success')
        return redirect(url_for('main.terminkalender', year=plan_date.year, month=plan_date.month))

    members = _team_members_for_planned_coaching_picker(project_id)
    filter_projects = _projects_for_coaching_workshop_picker()
    selected_member_id = request.args.get('suggested_member_id', type=int)
    allowed_member_ids = {m.id for m in members}
    if selected_member_id not in allowed_member_ids:
        selected_member_id = None
    return render_template(
        'main/terminkalender_plan.html',
        title='Geplantes Coaching anlegen',
        plan_date=plan_date,
        project_id=project_id,
        filter_projects=filter_projects,
        members=members,
        selected_member_id=selected_member_id,
        today=today,
        config=current_app.config,
    )


@bp.route('/my-coachings')
@login_required
@any_permission_required('view_own_coachings', 'leave_coaching_review')
def my_coachings():
    page = request.args.get('page', 1, type=int)
    period_arg = request.args.get('period', 'all')
    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)
    day = request.args.get('day', type=int)

    query = Coaching.query.options(
        joinedload(Coaching.employee_review),
        selectinload(Coaching.coach).selectinload(User.team_members),
    ).join(TeamMember, Coaching.team_member_id == TeamMember.id).filter(
        TeamMember.user_id == current_user.id
    )
    query = apply_coaching_date_filters(query, period_arg, year, month, day)
    coachings = query.order_by(desc(Coaching.coaching_date)).paginate(page=page, per_page=15, error_out=False)

    now = datetime.now(timezone.utc)
    year_options = list(range(now.year, now.year - 6, -1))
    month_options_list = [{'value': m, 'text': get_month_name_german(m)} for m in range(1, 13)]
    day_options = list(range(1, 32))

    review_form = CoachingReviewForm()
    filter_args = build_filter_args(period_arg, year, month, day)
    can_leave_review = current_user.has_permission('leave_coaching_review')
    has_team_member_link = (
        db.session.query(TeamMember.id).filter(TeamMember.user_id == current_user.id).first()
        is not None
    )
    return render_template(
        'main/my_coachings.html',
        title='Meine Coachings',
        coachings=coachings,
        current_period=period_arg,
        filter_year=year,
        filter_month=month,
        filter_day=day,
        year_options=year_options,
        month_options_list=month_options_list,
        day_options=day_options,
        filter_args=filter_args,
        page_url=lambda p: url_for_paginated('main.my_coachings', p, filter_args),
        review_form=review_form,
        can_leave_review=can_leave_review,
        has_team_member_link=has_team_member_link,
        config=current_app.config
    )


@bp.route('/my-coachings/review', methods=['POST'])
@login_required
@permission_required('leave_coaching_review')
def submit_coaching_review():
    form = CoachingReviewForm()
    cid_raw = (request.form.get('review_coaching_pk') or '').strip()
    if not cid_raw:
        flash('Coaching konnte nicht zugeordnet werden. Bitte „Bewertung abgeben“ erneut anklicken.', 'danger')
        t = _safe_internal_path((request.form.get('next') or '').strip())
        if t:
            return redirect(t)
        return redirect(url_for('main.my_coachings', **my_coachings_filter_query_args()))

    if not form.validate_on_submit():
        for _field, errors in form.errors.items():
            for err in errors:
                flash(err, 'danger')
        t = _safe_internal_path((request.form.get('next') or '').strip())
        if t:
            return redirect(t)
        return redirect(url_for('main.my_coachings', **my_coachings_filter_query_args()))

    try:
        cid = int(cid_raw)
    except (TypeError, ValueError):
        flash('Ungültige Coaching-ID.', 'danger')
        return _redirect_after_coaching_review(form, my_coachings_filter_query_args())

    coaching = Coaching.query.get_or_404(cid)
    member = coaching.team_member
    if not member or member.user_id != current_user.id:
        flash('Keine Berechtigung für dieses Coaching.', 'danger')
        return _redirect_after_coaching_review(form, my_coachings_filter_query_args())

    existing = CoachingReview.query.filter_by(coaching_id=coaching.id).first()
    if existing:
        flash('Ihre Bewertung wurde bereits abgegeben und kann nicht mehr geändert werden.', 'warning')
        return _redirect_after_coaching_review(form, my_coachings_filter_query_args())

    db.session.add(CoachingReview(
        coaching_id=coaching.id,
        reviewer_user_id=current_user.id,
        rating=form.rating.data,
        comment=(form.comment.data or '').strip() or None,
        visible_to_coach=bool(form.visible_to_coach.data),
        visible_to_manager=bool(form.visible_to_manager.data),
    ))
    db.session.commit()
    flash('Vielen Dank! Ihre Bewertung wurde gespeichert.', 'success')
    return _redirect_after_coaching_review(form, my_coachings_filter_query_args())


@bp.route('/reviews/for-me')
@login_required
@permission_required('view_review')
def coach_received_reviews():
    page = request.args.get('page', 1, type=int)
    period_arg = request.args.get('period', 'all')
    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)
    day = request.args.get('day', type=int)

    query = CoachingReview.query.join(Coaching, CoachingReview.coaching_id == Coaching.id).filter(
        Coaching.coach_id == current_user.id
    ).filter(CoachingReview.visible_to_coach.is_(True))
    query = filter_reviews_by_coaching_date(query, period_arg, year, month, day)
    reviews = query.order_by(desc(CoachingReview.created_at)).paginate(page=page, per_page=20, error_out=False)

    now = datetime.now(timezone.utc)
    year_options = list(range(now.year, now.year - 6, -1))
    month_options_list = [{'value': m, 'text': get_month_name_german(m)} for m in range(1, 13)]
    day_options = list(range(1, 32))

    filter_args = build_filter_args(period_arg, year, month, day)
    return render_template(
        'main/coach_received_reviews.html',
        title='Bewertungen über mich',
        reviews=reviews,
        current_period=period_arg,
        filter_year=year,
        filter_month=month,
        filter_day=day,
        year_options=year_options,
        month_options_list=month_options_list,
        day_options=day_options,
        filter_args=filter_args,
        page_url=lambda p: url_for_paginated('main.coach_received_reviews', p, filter_args),
        config=current_app.config
    )


@bp.route('/reviews/all')
@login_required
@permission_required('view_all_reviews')
def all_coaching_reviews():
    project_ids = get_allowed_project_ids_for_reviews()
    if not project_ids:
        flash('Kein Projekt für die Bewertungsübersicht verfügbar.', 'warning')
        return redirect(url_for('main.index'))

    page = request.args.get('page', 1, type=int)
    period_arg = request.args.get('period', 'all')
    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)
    day = request.args.get('day', type=int)
    project_filter = request.args.get('project', type=int)
    if project_filter and project_filter not in project_ids:
        project_filter = None

    team_filter = request.args.get('team', type=int)
    coach_filter = request.args.get('coach', type=int)

    if team_filter:
        t = Team.query.filter_by(id=team_filter).first()
        if not t or t.project_id not in project_ids:
            team_filter = None
        elif project_filter and t.project_id != project_filter:
            team_filter = None

    if coach_filter:
        cq_exists = Coaching.query.filter(
            Coaching.coach_id == coach_filter,
            Coaching.project_id.in_(project_ids),
        )
        if project_filter:
            cq_exists = cq_exists.filter(Coaching.project_id == project_filter)
        if not cq_exists.first():
            coach_filter = None

    q = CoachingReview.query.join(Coaching, CoachingReview.coaching_id == Coaching.id).filter(
        Coaching.project_id.in_(project_ids)
    ).filter(CoachingReview.visible_to_manager.is_(True))
    if project_filter:
        q = q.filter(Coaching.project_id == project_filter)
    if team_filter:
        q = q.join(TeamMember, Coaching.team_member_id == TeamMember.id).filter(
            TeamMember.team_id == team_filter
        )
    if coach_filter:
        q = q.filter(Coaching.coach_id == coach_filter)
    q = filter_reviews_by_coaching_date(q, period_arg, year, month, day)
    reviews = q.order_by(desc(CoachingReview.created_at)).paginate(page=page, per_page=25, error_out=False)

    now = datetime.now(timezone.utc)
    year_options = list(range(now.year, now.year - 6, -1))
    month_options_list = [{'value': m, 'text': get_month_name_german(m)} for m in range(1, 13)]
    day_options = list(range(1, 32))
    all_projects = Project.query.filter(Project.id.in_(project_ids)).order_by(Project.name).all()

    team_project_scope = [project_filter] if project_filter else project_ids
    filter_teams = (
        Team.query.filter(Team.project_id.in_(team_project_scope), Team.name != ARCHIV_TEAM_NAME)
        .order_by(Team.name)
        .all()
    )

    coach_q = (
        db.session.query(User)
        .options(selectinload(User.team_members))
        .join(Coaching, Coaching.coach_id == User.id)
        .filter(Coaching.project_id.in_(project_ids), Coaching.coach_id.isnot(None))
    )
    if project_filter:
        coach_q = coach_q.filter(Coaching.project_id == project_filter)
    filter_coaches = coach_q.distinct().order_by(User.username).all()

    extra_filters = {}
    if project_filter:
        extra_filters['project'] = project_filter
    if team_filter:
        extra_filters['team'] = team_filter
    if coach_filter:
        extra_filters['coach'] = coach_filter
    filter_args = build_filter_args(period_arg, year, month, day, extra=extra_filters)
    return render_template(
        'main/all_coaching_reviews.html',
        title='Alle Bewertungen',
        reviews=reviews,
        current_period=period_arg,
        filter_year=year,
        filter_month=month,
        filter_day=day,
        filter_project=project_filter,
        filter_team=team_filter,
        filter_coach=coach_filter,
        filter_teams=filter_teams,
        filter_coaches=filter_coaches,
        year_options=year_options,
        month_options_list=month_options_list,
        day_options=day_options,
        filter_projects=all_projects,
        filter_args=filter_args,
        page_url=lambda p: url_for_paginated('main.all_coaching_reviews', p, filter_args),
        config=current_app.config
    )


# --- Add Coaching (with the permission restriction only) ---
@bp.route('/add-coaching', methods=['GET', 'POST'])
@login_required
@permission_required('add_coaching')
def add_coaching():
    coaching_projects = _projects_for_coaching_workshop_picker()
    project_id = _resolve_coaching_workshop_project_id()
    if not project_id:
        flash('Kein Projekt ausgewählt oder zugeordnet.', 'danger')
        return redirect(url_for('main.index'))
    accessible = get_accessible_project_ids()
    if accessible is not None and project_id not in accessible:
        flash('Ungültiges oder nicht freigegebenes Projekt.', 'danger')
        return redirect(url_for('main.add_coaching'))
    if accessible is None and not Project.query.get(project_id):
        flash('Ungültiges Projekt.', 'danger')
        return redirect(url_for('main.add_coaching'))

    show_coaching_project_picker = len(coaching_projects) > 1

    current_user_role = current_user.role_name
    current_user_team_ids = (
        sorted({tm.team_id for tm in current_user.team_members if tm.team_id})
        if current_user_role == ROLE_TEAMLEITER else []
    )
    form = CoachingForm(current_user_role=current_user_role, current_user_team_ids=current_user_team_ids)
    assignment_member_ids = [
        row[0]
        for row in (
            db.session.query(AssignedCoaching.team_member_id)
            .join(TeamMember, AssignedCoaching.team_member_id == TeamMember.id)
            .join(Team, TeamMember.team_id == Team.id)
            .filter(
                AssignedCoaching.coach_id == current_user.id,
                AssignedCoaching.status.in_(['pending', 'accepted', 'in_progress']),
                Team.project_id == project_id,
                Team.name != ARCHIV_TEAM_NAME,
            )
            .distinct()
            .all()
        )
    ]
    filter_teams = form.teams_for_member_pick_filter(
        exclude_archiv=True,
        project_id=project_id,
        include_member_ids=assignment_member_ids,
    )
    allowed_team_ids = {t.id for t in filter_teams}
    team_filter_id = None
    team_filter_feature = current_app.config.get('ENABLE_ADD_COACHING_TEAM_FILTER', True)
    if team_filter_feature:
        if request.method == 'POST':
            raw_tf = (request.form.get('team_filter') or '').strip()
            if raw_tf.isdigit():
                cand = int(raw_tf)
                if cand in allowed_team_ids:
                    team_filter_id = cand
        else:
            arg_team = request.args.get('team', type=int)
            if arg_team and arg_team in allowed_team_ids:
                team_filter_id = arg_team

    form.update_team_member_choices(
        exclude_archiv=True,
        project_id=project_id,
        include_member_ids=assignment_member_ids,
        team_filter_id=team_filter_id,
    )
    form.apply_bogen(project_id)
    leitfaden_items = leitfaden_items_for_project(project_id)
    bogen_layout = bogen_layout_for_project(project_id)

    initial_fulfill_planned_id = None
    if request.method == 'GET':
        suggested_member_id = request.args.get('suggested_member_id', type=int)
        planned_id_arg = request.args.get('planned_id', type=int)
        if planned_id_arg:
            pc = PlannedCoaching.query.get(planned_id_arg)
            if pc and pc.coach_id == current_user.id and pc.status == 'open':
                acc = get_accessible_project_ids()
                if acc is None or not pc.project_id or pc.project_id in acc:
                    tm_pc = TeamMember.query.get(pc.team_member_id)
                    if tm_pc and team_member_eligible_for_new_coaching(tm_pc):
                        if pc.project_id and pc.project_id != project_id:
                            return redirect(url_for(
                                'main.add_coaching',
                                project=pc.project_id,
                                planned_id=planned_id_arg,
                            ))
                        form.team_member_id.data = pc.team_member_id
                        if planned_coaching_can_start_today(pc.planned_for_date):
                            initial_fulfill_planned_id = pc.id
        elif suggested_member_id:
            try:
                valid_ids = {int(choice[0]) for choice in (form.team_member_id.choices or []) if int(choice[0]) != 0}
            except (TypeError, ValueError):
                valid_ids = set()
            if suggested_member_id in valid_ids:
                form.team_member_id.data = suggested_member_id

    if form.validate_on_submit():
        team_member = TeamMember.query.get(form.team_member_id.data)
        if not team_member:
            flash('Teammitglied nicht gefunden.', 'danger')
            return redirect(url_for('main.add_coaching', project=project_id))
        if team_filter_id and team_member.team_id != team_filter_id:
            flash('Ungültige Kombination aus Teamfilter und Teammitglied.', 'danger')
            return redirect(url_for('main.add_coaching', project=project_id, team=team_filter_id))
        if not team_member.team or team_member.team.project_id != project_id:
            flash('Teammitglied passt nicht zum gewählten Projekt.', 'danger')
            return redirect(url_for('main.add_coaching', project=project_id))
        if not team_member_eligible_for_new_coaching(team_member):
            flash('Dieses Team ist für neue Coachings deaktiviert. Wählen Sie ein anderes Teammitglied.', 'danger')
            return redirect(url_for('main.add_coaching', project=project_id))

        fulfill_pid, verab_erfuellt, fulfill_err = _parse_fulfill_planned_submission(
            form.team_member_id.data, project_id
        )
        if fulfill_err:
            flash(fulfill_err, 'warning')
            if fulfill_pid:
                return redirect(url_for(
                    'main.add_coaching', project=project_id, planned_id=fulfill_pid,
                ))
            return redirect(url_for('main.add_coaching', project=project_id))

        if form.coaching_style.data == 'TCAP' and not getattr(bogen_layout, 'allow_tcap', True):
            flash('TCAP ist für dieses Projekt nicht freigegeben.', 'danger')
            return redirect(url_for('main.add_coaching', project=project_id))
        coaching = Coaching(
            team_member_id=form.team_member_id.data,
            coach_id=current_user.id,
            coaching_style=form.coaching_style.data,
            tcap_id=form.tcap_id.data if form.coaching_style.data == 'TCAP' else None,
            coaching_subject=form.coaching_subject.data,
            leitfaden_begruessung=form.leitfaden_begruessung.data,
            leitfaden_legitimation=form.leitfaden_legitimation.data,
            leitfaden_pka=form.leitfaden_pka.data,
            leitfaden_kek=form.leitfaden_kek.data,
            leitfaden_angebot=form.leitfaden_angebot.data,
            leitfaden_zusammenfassung=form.leitfaden_zusammenfassung.data,
            leitfaden_kzb=form.leitfaden_kzb.data,
            performance_mark=form.performance_mark.data,
            time_spent=form.time_spent.data,
            coach_notes=form.coach_notes.data,
            project_id=project_id,
            team_id=team_member.team_id
        )
        linked_assignment = None
        if form.assigned_coaching_id.data and form.assigned_coaching_id.data != 0:
            cand = AssignedCoaching.query.get(form.assigned_coaching_id.data)
            if (
                cand
                and cand.coach_id == current_user.id
                and cand.team_member_id == form.team_member_id.data
                and _assignment_eligible_to_link_coaching(cand)
            ):
                coaching.assigned_coaching_id = cand.id
                linked_assignment = cand
            elif cand:
                flash(
                    'Die gewählte Aufgabe ist abgelaufen oder nicht mehr gültig und wurde nicht verknüpft.',
                    'warning',
                )

        db.session.add(coaching)
        db.session.flush()

        for item in leitfaden_items:
            selected_value = request.form.get(f'leitfaden_item_{item.id}', 'k.A.')
            value = selected_value if selected_value in LEITFADEN_CHOICES else 'k.A.'
            db.session.add(CoachingLeitfadenResponse(
                coaching_id=coaching.id,
                item_id=item.id,
                value=value
            ))
        if linked_assignment:
            _sync_assigned_coaching_status_from_progress(linked_assignment)
        _maybe_fulfill_planned_coaching(coaching, fulfill_pid, verab_erfuellt)
        plan_result = _try_create_planned_followup_from_request(coaching)
        db.session.commit()
        flash('Coaching erfolgreich gespeichert!', 'success')
        if plan_result == 'bad_date':
            flash('Geplantes Folgecoaching: bitte ein gültiges Datum wählen.', 'warning')
        elif plan_result == 'created':
            flash('Folgetermin wurde gespeichert.', 'info')
        return redirect(url_for('main.coaching_dashboard'))

    assigned_id = request.args.get('assigned_id', type=int)
    if assigned_id:
        assignment = AssignedCoaching.query.get(assigned_id)
        if (
            assignment
            and assignment.coach_id == current_user.id
            and assignment.status in ('pending', 'accepted', 'in_progress')
            and _assignment_eligible_to_link_coaching(assignment)
        ):
            tm_a = TeamMember.query.get(assignment.team_member_id)
            if not team_member_eligible_for_new_coaching(tm_a):
                flash('Diese Aufgabe kann nicht angenommen werden: Das Team ist für neue Coachings deaktiviert.', 'danger')
            else:
                form.assigned_coaching_id.data = assigned_id
                form.team_member_id.data = assignment.team_member_id
                if assignment.desired_performance_note:
                    form.performance_mark.data = assignment.desired_performance_note
                was_pending = assignment.status == 'pending'
                assignment.status = 'accepted'
                if was_pending:
                    db.session.commit()
                    flash('Coaching-Aufgabe angenommen.', 'success')
        else:
            flash('Ungültige oder nicht verfügbare Aufgabe.', 'danger')

    show_team_member_team_filter = team_filter_feature and len(filter_teams) > 1
    if team_filter_feature and show_team_member_team_filter and form.team_member_id.data:
        try:
            valid_member_choice_ids = {int(c[0]) for c in (form.team_member_id.choices or [])}
        except (TypeError, ValueError):
            valid_member_choice_ids = set()
        sel_mid = form.team_member_id.data
        if sel_mid and int(sel_mid) not in valid_member_choice_ids:
            tm_sel = TeamMember.query.get(sel_mid)
            if tm_sel and tm_sel.team_id in allowed_team_ids:
                team_filter_id = tm_sel.team_id
                form.update_team_member_choices(
                    exclude_archiv=True,
                    project_id=project_id,
                    include_member_ids=assignment_member_ids,
                    team_filter_id=team_filter_id,
                )

    return render_template(
        'main/add_coaching.html',
        title='Coaching erfassen',
        form=form,
        leitfaden_items=leitfaden_items,
        selected_leitfaden_values={},
        coaching_projects=coaching_projects,
        selected_coaching_project_id=project_id,
        show_coaching_project_picker=show_coaching_project_picker,
        add_coaching_filter_teams=filter_teams,
        add_coaching_selected_team_id=team_filter_id,
        show_team_member_team_filter=show_team_member_team_filter,
        bogen_layout=bogen_layout,
        config=current_app.config,
        initial_fulfill_planned_id=initial_fulfill_planned_id,
    )


# --- Read-only Bericht (abgeschlossenes geplantes Coaching) ---
@bp.route('/coaching-bericht/<int:coaching_id>')
@login_required
@any_permission_required(
    'edit_coaching',
    'add_coaching',
    'view_coaching_dashboard',
    'view_pl_qm_dashboard',
    'assign_coachings',
)
def view_fulfilled_plan_bericht(coaching_id):
    coaching = (
        Coaching.query.options(
            joinedload(Coaching.team_member).joinedload(TeamMember.team),
            joinedload(Coaching.coach),
            selectinload(Coaching.leitfaden_responses).joinedload(CoachingLeitfadenResponse.item),
            joinedload(Coaching.employee_review),
        ).get(coaching_id)
    )
    if coaching is None:
        abort(404)
    if not _user_may_view_fulfilled_plan_bericht(coaching):
        flash('Sie haben keine Berechtigung, diesen Bericht einzusehen.', 'danger')
        if current_user.has_permission('view_coaching_dashboard'):
            return redirect(url_for('main.coaching_dashboard'))
        return redirect(url_for('main.index'))
    if not _coaching_has_fulfilled_planned_row(coaching_id):
        flash('Dieses Coaching ist kein abgeschlossenes geplantes Coaching.', 'warning')
        if current_user.has_permission('edit_coaching'):
            return redirect(url_for('main.edit_coaching', coaching_id=coaching_id))
        return redirect(url_for('main.index'))

    planned_ctx = (
        PlannedCoaching.query.filter_by(
            fulfilled_coaching_id=coaching_id,
            status='fulfilled',
        )
        .options(
            joinedload(PlannedCoaching.team_member),
            joinedload(PlannedCoaching.project),
        )
        .first()
    )

    return render_template(
        'main/coaching_bericht_quick.html',
        title='Coaching-Bericht',
        coaching=coaching,
        planned_ctx=planned_ctx,
        config=current_app.config,
    )


# --- Edit Coaching ---
@bp.route('/edit-coaching/<int:coaching_id>', methods=['GET', 'POST'])
@login_required
@permission_required('edit_coaching')
def edit_coaching(coaching_id):
    coaching = Coaching.query.get_or_404(coaching_id)
    if current_user.role_name not in [ROLE_ADMIN, ROLE_BETRIEBSLEITER] and coaching.coach_id != current_user.id:
        flash('Sie haben keine Berechtigung, dieses Coaching zu bearbeiten.', 'danger')
        return redirect(url_for('main.coaching_dashboard'))

    if _coaching_has_fulfilled_planned_row(coaching_id):
        if request.method == 'POST':
            flash(
                'Abgeschlossene geplante Coachings sind nur als Bericht einsehbar und können nicht geändert werden.',
                'info',
            )
        return redirect(url_for('main.view_fulfilled_plan_bericht', coaching_id=coaching_id))

    cut = (
        sorted({tm.team_id for tm in current_user.team_members if tm.team_id})
        if current_user.role_name == ROLE_TEAMLEITER else []
    )
    form = CoachingForm(obj=coaching, current_user_role=current_user.role_name, current_user_team_ids=cut)
    form.update_team_member_choices(
        exclude_archiv=True,
        project_id=coaching.project_id,
        include_member_ids=[coaching.team_member_id],
    )
    form.apply_bogen(coaching.project_id, coaching=coaching)
    bogen_layout = bogen_layout_for_project(coaching.project_id)
    leitfaden_items = leitfaden_items_for_coaching_edit(coaching)
    selected_leitfaden_values = {}
    if leitfaden_items:
        try:
            selected_leitfaden_values = {response.item_id: response.value for response in coaching.leitfaden_responses}
        except SQLAlchemyError:
            db.session.rollback()
            selected_leitfaden_values = {}

    if form.validate_on_submit():
        tm_new = TeamMember.query.get(form.team_member_id.data)
        if not tm_new or not team_member_eligible_for_new_coaching(tm_new):
            flash('Ungültiges Teammitglied oder Team für neue Coachings deaktiviert.', 'danger')
            return redirect(url_for('main.edit_coaching', coaching_id=coaching_id))
        if form.coaching_style.data == 'TCAP' and not getattr(bogen_layout, 'allow_tcap', True):
            flash('TCAP ist für dieses Projekt nicht freigegeben.', 'danger')
            return redirect(url_for('main.edit_coaching', coaching_id=coaching_id))
        prev_assigned_id = coaching.assigned_coaching_id
        form.populate_obj(coaching)
        # Form uses 0 as "no assignment" sentinel; DB FK requires NULL instead of 0.
        if not coaching.assigned_coaching_id:
            coaching.assigned_coaching_id = None
        if coaching.assigned_coaching_id:
            ac = AssignedCoaching.query.get(coaching.assigned_coaching_id)
            if not ac or ac.coach_id != coaching.coach_id or ac.team_member_id != coaching.team_member_id:
                flash('Ungültige zugewiesene Aufgabe.', 'danger')
                return redirect(url_for('main.edit_coaching', coaching_id=coaching_id))
            if not _assignment_eligible_to_link_coaching(ac):
                flash(
                    'Die gewählte Aufgabe ist abgelaufen oder nicht mehr gültig. Verknüpfung wurde entfernt.',
                    'warning',
                )
                coaching.assigned_coaching_id = None
        if form.coaching_style.data != 'TCAP':
            coaching.tcap_id = None
        if leitfaden_items:
            CoachingLeitfadenResponse.query.filter_by(coaching_id=coaching.id).delete()
            for item in leitfaden_items:
                selected_value = request.form.get(f'leitfaden_item_{item.id}', 'k.A.')
                value = selected_value if selected_value in LEITFADEN_CHOICES else 'k.A.'
                db.session.add(CoachingLeitfadenResponse(
                    coaching_id=coaching.id,
                    item_id=item.id,
                    value=value
                ))
        db.session.flush()
        for aid in {a for a in (prev_assigned_id, coaching.assigned_coaching_id) if a}:
            _sync_assigned_coaching_status_from_progress(AssignedCoaching.query.get(aid))
        plan_result = _try_create_planned_followup_from_request(coaching)
        db.session.commit()
        flash('Coaching erfolgreich aktualisiert.', 'success')
        if plan_result == 'bad_date':
            flash('Geplantes Folgecoaching: bitte ein gültiges Datum wählen.', 'warning')
        elif plan_result == 'created':
            flash('Folgetermin wurde gespeichert.', 'info')
        return redirect(url_for('main.coaching_dashboard'))

    return render_template(
        'main/add_coaching.html',
        title='Coaching bearbeiten',
        form=form,
        is_edit_mode=True,
        coaching=coaching,
        leitfaden_items=leitfaden_items,
        selected_leitfaden_values=selected_leitfaden_values,
        add_coaching_filter_teams=[],
        add_coaching_selected_team_id=None,
        show_team_member_team_filter=False,
        bogen_layout=bogen_layout,
        config=current_app.config,
        initial_fulfill_planned_id=None,
    )


# --- Delete Coaching ---
@bp.route('/delete-coaching/<int:coaching_id>', methods=['POST'])
@login_required
@permission_required('edit_coaching')
def delete_coaching(coaching_id):
    coaching = Coaching.query.get_or_404(coaching_id)
    if current_user.role_name not in [ROLE_ADMIN, ROLE_BETRIEBSLEITER] and coaching.coach_id != current_user.id:
        flash('Keine Berechtigung.', 'danger')
        return redirect(url_for('main.coaching_dashboard'))
    if _coaching_has_fulfilled_planned_row(coaching_id):
        flash(
            'Dieses Coaching gehört zu einem abgeschlossenen geplanten Coaching und kann nicht gelöscht werden.',
            'warning',
        )
        return redirect(url_for('main.view_fulfilled_plan_bericht', coaching_id=coaching_id))
    assigned_ref = coaching.assigned_coaching_id
    db.session.delete(coaching)
    db.session.flush()
    if assigned_ref:
        _sync_assigned_coaching_status_from_progress(AssignedCoaching.query.get(assigned_ref))
    db.session.commit()
    flash('Coaching gelöscht.', 'success')
    return redirect(url_for('main.coaching_dashboard'))


# --- Workshop routes (keep as you had) ---
@bp.route('/add-workshop', methods=['GET', 'POST'])
@login_required
@permission_required('add_workshop')
def add_workshop():
    workshop_projects = _projects_for_coaching_workshop_picker()
    project_id = _resolve_coaching_workshop_project_id()
    if not project_id:
        flash('Kein Projekt ausgewählt.', 'danger')
        return redirect(url_for('main.index'))
    accessible = get_accessible_project_ids()
    if accessible is not None and project_id not in accessible:
        flash('Ungültiges oder nicht freigegebenes Projekt.', 'danger')
        return redirect(url_for('main.add_workshop'))
    if accessible is None and not Project.query.get(project_id):
        flash('Ungültiges Projekt.', 'danger')
        return redirect(url_for('main.add_workshop'))

    show_workshop_project_picker = len(workshop_projects) > 1

    fulfill_pw = _resolve_planned_workshop_fulfill_for_form(project_id)

    current_user_team_ids = (
        sorted({tm.team_id for tm in current_user.team_members if tm.team_id})
        if current_user.role_name == ROLE_TEAMLEITER else []
    )
    form = WorkshopForm(current_user_role=current_user.role_name, current_user_team_ids=current_user_team_ids)
    form.update_participant_choices(project_id=project_id)
    if request.method == 'GET' and fulfill_pw:
        if fulfill_pw.title:
            form.title.data = fulfill_pw.title
        if fulfill_pw.notes:
            form.notes.data = fulfill_pw.notes
    if form.validate_on_submit():
        fulfill_pw_post = _resolve_planned_workshop_fulfill_for_form(project_id)
        for member_id in form.team_member_ids.data:
            wm = TeamMember.query.get(member_id)
            if not wm or not wm.team or wm.team.project_id != project_id:
                flash('Mindestens ein Teilnehmer gehört nicht zum gewählten Projekt.', 'danger')
                redir = url_for('main.add_workshop', project=project_id)
                if fulfill_pw_post:
                    redir = url_for('main.add_workshop', project=project_id, planned_workshop=fulfill_pw_post.id)
                return redirect(redir)
            if not team_member_eligible_for_new_coaching(wm):
                flash('Mindestens ein Teilnehmer gehört zu einem Team, das für neue Workshops deaktiviert ist.', 'danger')
                redir = url_for('main.add_workshop', project=project_id)
                if fulfill_pw_post:
                    redir = url_for('main.add_workshop', project=project_id, planned_workshop=fulfill_pw_post.id)
                return redirect(redir)
        workshop = Workshop(
            title=form.title.data,
            coach_id=current_user.id,
            overall_rating=form.overall_rating.data,
            time_spent=form.time_spent.data,
            notes=form.notes.data,
            project_id=project_id
        )
        db.session.add(workshop)
        db.session.flush()
        for member_id in form.team_member_ids.data:
            individual_rating = workshop_individual_rating_from_request(member_id)
            stmt = workshop_participants.insert().values(
                workshop_id=workshop.id,
                team_member_id=member_id,
                individual_rating=individual_rating,
                original_team_id=None
            )
            db.session.execute(stmt)
        if fulfill_pw_post:
            fulfill_pw_post.fulfilled_workshop_id = workshop.id
            fulfill_pw_post.status = 'fulfilled'
        db.session.commit()
        flash('Workshop erfolgreich gespeichert.', 'success')
        return redirect(url_for('main.workshop_dashboard'))
    return render_template(
        'main/add_workshop.html',
        form=form,
        workshop_projects=workshop_projects,
        selected_workshop_project_id=project_id,
        show_workshop_project_picker=show_workshop_project_picker,
        planned_workshop_fulfill=fulfill_pw,
        config=current_app.config,
    )


@bp.route('/workshop-dashboard')
@login_required
@permission_required('view_workshop_dashboard')
def workshop_dashboard():
    page = request.args.get('page', 1, type=int)
    period_arg = request.args.get('period', 'all')
    search_arg = request.args.get('search', default="", type=str).strip()

    accessible = get_accessible_project_ids()
    project_raw = (request.args.get('project') or '').strip()
    project_filter_int = None
    project_scope_all = False

    if project_raw.lower() == 'all':
        project_scope_all = True
    elif project_raw.isdigit():
        project_filter_int = int(project_raw)
    elif accessible is not None and len(accessible) > 1 and not project_raw:
        project_scope_all = True

    ws_project_filters = []
    if accessible is None:
        if project_filter_int is not None:
            ws_project_filters.append(Workshop.project_id == project_filter_int)
    elif not accessible:
        ws_project_filters.append(Workshop.project_id == -1)
    else:
        if project_filter_int is not None and project_filter_int not in accessible:
            project_filter_int = None
        if project_scope_all:
            ws_project_filters.append(Workshop.project_id.in_(accessible))
        elif project_filter_int is not None:
            ws_project_filters.append(Workshop.project_id == project_filter_int)
        elif len(accessible) == 1:
            ws_project_filters.append(Workshop.project_id == accessible[0])
        else:
            vid = get_visible_project_id()
            if vid and vid in accessible:
                ws_project_filters.append(Workshop.project_id == vid)
            else:
                ws_project_filters.append(Workshop.project_id == accessible[0])

    if accessible is None:
        dashboard_project_id = project_filter_int
    elif not accessible:
        dashboard_project_id = -1
    else:
        if project_scope_all:
            dashboard_project_id = None
        elif project_filter_int is not None:
            dashboard_project_id = project_filter_int
        elif len(accessible) == 1:
            dashboard_project_id = accessible[0]
        else:
            vid = get_visible_project_id()
            dashboard_project_id = vid if (vid and vid in accessible) else accessible[0]

    cal_date_str = (request.args.get('cal_date') or '').strip()
    cal_date_active = None
    if cal_date_str:
        try:
            cal_date_active = datetime.strptime(cal_date_str, '%Y-%m-%d').date()
        except ValueError:
            cal_date_active = None
            cal_date_str = ''

    ws_filters = list(ws_project_filters)
    if cal_date_active:
        start_date, end_date = athens_calendar_day_utc_naive_bounds(cal_date_active)
    else:
        start_date, end_date = calculate_date_range(period_arg)
    if start_date:
        ws_filters.append(Workshop.workshop_date >= start_date)
    if end_date:
        ws_filters.append(Workshop.workshop_date <= end_date)

    workshops_query = Workshop.query
    if search_arg:
        pattern = f"%{search_arg}%"
        ws_filters.append(
            or_(
                Workshop.title.ilike(pattern),
                Workshop.notes.ilike(pattern),
                User.username.ilike(pattern)
            )
        )
        workshops_query = workshops_query.join(User, Workshop.coach_id == User.id)

    workshops_query = workshops_query.filter(*ws_filters)
    workshops_paginated = workshops_query.order_by(desc(Workshop.workshop_date)).paginate(page=page, per_page=15, error_out=False)

    total_workshops = workshops_query.count()
    total_time = db.session.query(
        db.func.coalesce(db.func.sum(Workshop.time_spent), 0)
    ).filter(*ws_filters).scalar()
    avg_rating_val = db.session.query(
        db.func.avg(Workshop.overall_rating)
    ).filter(*ws_filters).scalar()
    avg_rating = round(avg_rating_val, 1) if avg_rating_val else 0

    now = datetime.now(timezone.utc)
    current_year = now.year
    previous_year = current_year - 1
    month_options = []
    for m in range(12, 0, -1):
        month_options.append({'value': f"{previous_year}-{m:02d}", 'text': f"{get_month_name_german(m)} {previous_year}"})
    for m in range(now.month, 0, -1):
        month_options.append({'value': f"{current_year}-{m:02d}", 'text': f"{get_month_name_german(m)} {current_year}"})

    if accessible is None:
        all_projects = Project.query.order_by(Project.name).all()
    elif len(accessible) > 1:
        all_projects = Project.query.filter(Project.id.in_(accessible)).order_by(Project.name).all()
    else:
        all_projects = []

    show_global_all_projects_option = accessible is None or (accessible is not None and len(accessible) > 1)
    workshop_dashboard_project_all_is_blank = accessible is None

    if accessible is None:
        current_project_filter = project_filter_int
    elif not accessible:
        current_project_filter = None
    else:
        if project_scope_all:
            current_project_filter = 'all'
        elif project_filter_int is not None:
            current_project_filter = project_filter_int
        elif len(accessible) == 1:
            current_project_filter = accessible[0]
        else:
            current_project_filter = dashboard_project_id

    workshop_dashboard_url_project = None
    if all_projects:
        if accessible is None:
            workshop_dashboard_url_project = project_filter_int
        elif not accessible:
            workshop_dashboard_url_project = None
        else:
            if project_scope_all:
                workshop_dashboard_url_project = 'all'
            elif project_filter_int is not None:
                workshop_dashboard_url_project = project_filter_int
            else:
                workshop_dashboard_url_project = dashboard_project_id

    cal_day_label = ''
    if cal_date_active:
        cal_day_label = cal_date_active.strftime('%d.%m.%Y')

    return render_template('main/workshop_dashboard.html',
                           title='Workshop Dashboard',
                           workshops_paginated=workshops_paginated,
                           total_workshops=total_workshops,
                           total_time=total_time,
                           avg_rating=avg_rating,
                           current_search=search_arg,
                           current_period_filter=period_arg,
                           month_options=month_options,
                           cal_date_filter=cal_date_str if cal_date_active else None,
                           cal_day_label=cal_day_label,
                           all_projects=all_projects,
                           show_global_all_projects_option=show_global_all_projects_option,
                           workshop_dashboard_project_all_is_blank=workshop_dashboard_project_all_is_blank,
                           current_project_filter=current_project_filter,
                           workshop_dashboard_url_project=workshop_dashboard_url_project,
                           config=current_app.config,
                           db=db,
                           workshop_participants=workshop_participants)


# --- Team View (team leaders + members with view_own_team; PL/QM via view_pl_qm_dashboard) ---
@bp.route('/team-view')
@login_required
@any_permission_required('view_own_team', 'view_pl_qm_dashboard')
def team_view():
    all_teams_list = _get_teams_for_team_view()
    if not all_teams_list:
        flash('Kein Team für diese Ansicht verfügbar. Prüfen Sie die Berechtigung und die Zuordnung (Teamleiter-Teams oder Teammitglied).', 'info')
        return redirect(url_for('main.index'))

    requested_id = request.args.get('team_id', type=int)
    team = None
    if requested_id:
        team = next((t for t in all_teams_list if t.id == requested_id), None)
        if not team:
            flash('Kein Zugriff auf das angeforderte Team.', 'warning')
    if not team:
        team = all_teams_list[0]

    team_members_performance = _build_team_members_performance(team)
    card_settings = _team_view_card_settings(team.project_id)
    prod_visibility = _prod_dashboard_visibility(team.project_id)
    prod_labels = _prod_labels(team.project_id)
    prod_targets = _prod_settings(team.project_id)
    has_productivity_data = bool(
        db.session.query(ProductivityInterval.id)
        .filter(ProductivityInterval.project_id == team.project_id)
        .limit(1)
        .first()
    )
    member_ids = [m.id for m in TeamMember.query.filter_by(team_id=team.id).all()]
    team_total_coachings = 0
    team_avg_time_minutes = 0
    team_avg_score_percent = 0
    if member_ids:
        team_scope = [
            Coaching.team_member_id.in_(member_ids),
            Coaching.project_id == team.project_id,
        ]
        team_total_coachings = (
            db.session.query(db.func.count(Coaching.id))
            .filter(*team_scope)
            .scalar()
            or 0
        )
        avg_time_val = (
            db.session.query(db.func.avg(Coaching.time_spent))
            .filter(*team_scope)
            .scalar()
        )
        avg_mark_val = (
            db.session.query(db.func.avg(Coaching.performance_mark))
            .filter(*team_scope)
            .scalar()
        )
        team_avg_time_minutes = int(round(avg_time_val or 0))
        team_avg_score_percent = round((avg_mark_val or 0) * 10, 1)

    members = TeamMember.query.filter_by(team_id=team.id).order_by(TeamMember.name).all()
    team_leaders_display = _team_leaders_for_team_card(team)
    return render_template(
        'main/team_view.html',
        title='Mein Team',
        team=team,
        members=members,
        team_leaders_display=team_leaders_display,
        team_members_performance=team_members_performance,
        team_total_coachings=team_total_coachings,
        team_avg_time_minutes=team_avg_time_minutes,
        team_avg_score_percent=team_avg_score_percent,
        all_teams_list=all_teams_list,
        card_settings=card_settings,
        prod_visibility=prod_visibility,
        prod_labels=prod_labels,
        prod_targets=prod_targets,
        has_productivity_data=has_productivity_data,
        kpi_category_labels=_kpi_category_labels(),
        config=current_app.config,
    )


# --- PL/QM Dashboard ---
@bp.route('/pl-qm-dashboard')
@login_required
@permission_required('view_pl_qm_dashboard')
def pl_qm_dashboard():
    _apply_query_project_to_session()
    project_id = get_visible_project_id()
    if not project_id:
        flash('Kein Projekt ausgewählt.', 'danger')
        return redirect(url_for('main.index'))

    page = request.args.get('page', 1, type=int)
    selected_team_id_filter = request.args.get('team_id_filter', default='', type=str)

    project = Project.query.get(project_id)
    all_teams = (
        Team.query.filter_by(project_id=project_id)
        .filter(
            Team.name != ARCHIV_TEAM_NAME,
            Team.active_for_coaching.is_(True),
            exists().where(TeamMember.team_id == Team.id),
        )
        .order_by(Team.name)
        .all()
    )
    allowed_pl_qm_team_ids = {t.id for t in all_teams}

    # Compute per-team stats
    teams_stats = []
    for team in all_teams:
        stats = db.session.query(
            db.func.count(Coaching.id),
            db.func.avg(Coaching.performance_mark),
            db.func.sum(Coaching.time_spent)
        ).join(TeamMember, Coaching.team_member_id == TeamMember.id).filter(
            TeamMember.team_id == team.id,
            Coaching.project_id == project_id
        ).first()
        num_coachings = stats[0] or 0
        avg_score = round((stats[1] or 0) * 10, 1)
        total_time = stats[2] or 0
        teams_stats.append({
            'id': team.id,
            'name': team.name,
            'num_coachings': num_coachings,
            'avg_score': avg_score,
            'total_time': total_time
        })

    # Overall stats
    overall = db.session.query(
        db.func.count(Coaching.id),
        db.func.sum(Coaching.time_spent),
        db.func.avg(Coaching.performance_mark)
    ).filter(Coaching.project_id == project_id).first()
    total_coachings_overall = overall[0] or 0
    total_time_overall = overall[1] or 0
    avg_score_overall = round((overall[2] or 0) * 10, 1)

    # Chart data
    chart_labels = [t['name'] for t in teams_stats if t['num_coachings'] > 0]
    chart_avg_performance_values = [t['avg_score'] for t in teams_stats if t['num_coachings'] > 0]

    subject_counts = db.session.query(
        Coaching.coaching_subject, db.func.count(Coaching.id)
    ).filter(Coaching.project_id == project_id).group_by(Coaching.coaching_subject).all()
    subject_labels = [s[0] or 'Unbekannt' for s in subject_counts]
    subject_values = [s[1] for s in subject_counts]

    # Top 3 and Flop 3 teams
    teams_with_coachings = [t for t in teams_stats if t['num_coachings'] > 0]
    sorted_by_score = sorted(teams_with_coachings, key=lambda x: x['avg_score'], reverse=True)
    top_3_teams = sorted_by_score[:3]
    flop_3_teams = sorted_by_score[-3:][::-1] if len(sorted_by_score) > 3 else []

    # Member cards for selected team
    selected_team_object_for_cards = None
    members_data_for_cards = []
    if selected_team_id_filter and selected_team_id_filter.isdigit():
        selected_team_object_for_cards = Team.query.get(int(selected_team_id_filter))
        if not selected_team_object_for_cards or selected_team_object_for_cards.id not in allowed_pl_qm_team_ids:
            selected_team_object_for_cards = None
        if selected_team_object_for_cards:
            team_members = TeamMember.query.filter_by(team_id=selected_team_object_for_cards.id).order_by(TeamMember.name).all()
            for member in team_members:
                m_stats = db.session.query(
                    db.func.count(Coaching.id),
                    db.func.avg(Coaching.performance_mark),
                    db.func.sum(Coaching.time_spent)
                ).filter(Coaching.team_member_id == member.id, Coaching.project_id == project_id).first()
                total_c = m_stats[0] or 0
                avg_perf = round((m_stats[1] or 0) * 10, 1) if total_c > 0 else 0
                total_t = m_stats[2] or 0
                hours = total_t // 60
                mins = total_t % 60
                formatted_time = f"{hours}h {mins}m" if hours > 0 else f"{mins}m"

                if total_c > 0:
                    member_coachings = Coaching.query.filter_by(team_member_id=member.id, project_id=project_id).all()
                    total_checks = 0
                    positive_checks = 0
                    for c in member_coachings:
                        for _, val in c.leitfaden_fields_list:
                            if val and val != 'k.A.':
                                total_checks += 1
                                if val.lower() in ['ja', 'yes', '1', 'true']:
                                    positive_checks += 1
                    avg_leitfaden = round((positive_checks / total_checks * 100), 1) if total_checks > 0 else 0
                else:
                    avg_leitfaden = 0

                members_data_for_cards.append({
                    'id': member.id,
                    'name': member.name,
                    'total_coachings': total_c,
                    'avg_score': avg_perf,
                    'total_time': total_t,
                    'formatted_total_coaching_time': formatted_time,
                    'avg_leitfaden_adherence': avg_leitfaden
                })

    coachings_paginated = Coaching.query.filter_by(project_id=project_id).order_by(
        desc(Coaching.coaching_date)
    ).paginate(page=page, per_page=15, error_out=False)

    return render_template('main/projektleiter_dashboard.html',
                           title='Teams',
                           project_bar_endpoint='main.pl_qm_dashboard',
                           project_bar_extra_hidden={},
                           project=project,
                           total_coachings_overall=total_coachings_overall,
                           total_time_overall=total_time_overall,
                           avg_score_overall=avg_score_overall,
                           teams_stats=teams_stats,
                           chart_labels=chart_labels,
                           chart_avg_performance_values=chart_avg_performance_values,
                           subject_labels=subject_labels,
                           subject_values=subject_values,
                           all_teams_for_filter=all_teams,
                           selected_team_id_filter=selected_team_id_filter,
                           selected_team_object_for_cards=selected_team_object_for_cards,
                           members_data_for_cards=members_data_for_cards,
                           coachings_paginated=coachings_paginated,
                           top_3_teams=top_3_teams,
                           flop_3_teams=flop_3_teams,
                           config=current_app.config)


@bp.route('/api/available_assignments')
@login_required
@permission_required('add_coaching')
def available_assignments():
    """Offene/aktive zugewiesene Aufgaben für Coach + gewähltes Teammitglied (Coaching-Formular)."""
    member_id = request.args.get('member_id', type=int)
    if not member_id:
        return jsonify({'assignments': []})

    ensure_raw = (request.args.get('ensure_assignment_ids') or '').strip()
    ensure_ids = []
    for part in ensure_raw.split(','):
        part = part.strip()
        if part.isdigit():
            ensure_ids.append(int(part))

    now_cut = datetime.utcnow()
    base = (
        AssignedCoaching.query.filter(
            AssignedCoaching.team_member_id == member_id,
            AssignedCoaching.coach_id == current_user.id,
            AssignedCoaching.status.in_(['pending', 'accepted', 'in_progress']),
            or_(
                AssignedCoaching.deadline.is_(None),
                AssignedCoaching.deadline >= now_cut,
            ),
        )
        .order_by(AssignedCoaching.deadline)
    )

    seen = set()
    out = []
    for a in base.all():
        if not _assignment_eligible_to_link_coaching(a):
            continue
        seen.add(a.id)
        out.append({
            'id': a.id,
            'deadline': a.deadline.strftime('%d.%m.%y') if a.deadline else '',
            'progress': a.progress,
        })

    ensure_allow_stale = len(ensure_ids) == 1
    for eid in ensure_ids:
        if eid in seen:
            continue
        a = AssignedCoaching.query.get(eid)
        if not a or a.team_member_id != member_id or a.coach_id != current_user.id:
            continue
        if _assignment_eligible_to_link_coaching(a) or (ensure_allow_stale and eid == ensure_ids[0]):
            seen.add(a.id)
            out.append({
                'id': a.id,
                'deadline': a.deadline.strftime('%d.%m.%y') if a.deadline else '',
                'progress': a.progress,
            })

    return jsonify({'assignments': out})


@bp.route('/api/open_planned_coachings')
@login_required
@permission_required('add_coaching')
def open_planned_coachings_for_member():
    """Offene geplante Coachings Coach + Mitglied (Auswahl im Bogen)."""
    member_id = request.args.get('member_id', type=int)
    if not member_id:
        return jsonify({'plans': []})
    today = today_athens_date()
    q = PlannedCoaching.query.filter(
        PlannedCoaching.team_member_id == member_id,
        PlannedCoaching.coach_id == current_user.id,
        PlannedCoaching.status == 'open',
    ).order_by(PlannedCoaching.planned_for_date)
    plans = []
    for p in q.all():
        plans.append({
            'id': p.id,
            'date': p.planned_for_date.strftime('%d.%m.%Y'),
            'date_iso': p.planned_for_date.isoformat(),
            'can_start': p.planned_for_date <= today,
            'notes': p.notes or '',
            'has_verabredung': p.has_verabredung,
            'verabredung_text': p.verabredung_text or '',
        })
    return jsonify({'plans': plans})


@bp.route('/geplante-coachings')
@login_required
@any_permission_required(
    'planned_coachings', 'add_workshop', 'view_others_planned_coachings'
)
def planned_coachings_list():
    sort_today = today_athens_date()
    next_week_end = sort_today + timedelta(days=7)
    next_month_end = sort_today + timedelta(days=31)
    last_week_start = sort_today - timedelta(days=7)
    last_month_start = sort_today - timedelta(days=31)
    can_pc = current_user.has_permission('planned_coachings')
    can_pw = current_user.has_permission('add_workshop')
    can_view_others = _can_view_others_planned_in_scope()
    can_see_coaching_plans = can_pc or can_view_others
    can_see_workshop_plans = can_pw or can_view_others
    acc = get_accessible_project_ids()

    coaching_opts = (
        joinedload(PlannedCoaching.team_member).joinedload(TeamMember.team),
        joinedload(PlannedCoaching.project),
        joinedload(PlannedCoaching.team),
        joinedload(PlannedCoaching.coach),
    )

    items = []
    if can_see_coaching_plans:
        parts_open = []
        if can_pc:
            mine_o = PlannedCoaching.coach_id == current_user.id
            if acc is not None:
                if len(acc) == 0:
                    mine_o = and_(mine_o, false())
                else:
                    mine_o = and_(mine_o, PlannedCoaching.project_id.in_(acc))
            parts_open.append(mine_o)
        if can_view_others:
            other_o = PlannedCoaching.coach_id != current_user.id
            if acc is not None:
                if len(acc) == 0:
                    other_o = and_(other_o, false())
                else:
                    other_o = and_(other_o, PlannedCoaching.project_id.in_(acc))
            parts_open.append(other_o)
        if parts_open:
            q = (
                PlannedCoaching.query.filter(
                    PlannedCoaching.status == 'open',
                    or_(*parts_open),
                )
                .options(*coaching_opts)
                .order_by(PlannedCoaching.planned_for_date, PlannedCoaching.id)
            )
            all_open_coaching = q.all()
            items, overdue_coaching_items = _split_planned_open_by_overdue(all_open_coaching, sort_today)
            items.sort(
                key=lambda it: (
                    0 if it.planned_for_date == sort_today else 1,
                    it.planned_for_date or date.min,
                    it.id,
                )
            )
        else:
            overdue_coaching_items = []
    else:
        overdue_coaching_items = []

    workshop_items = []
    overdue_workshop_items = []
    fulfilled_workshop_plans = []
    if can_see_workshop_plans:
        parts_wo = []
        if can_pw:
            mine_w = PlannedWorkshop.coach_id == current_user.id
            if acc is not None:
                if len(acc) == 0:
                    mine_w = and_(mine_w, false())
                else:
                    mine_w = and_(
                        mine_w,
                        or_(PlannedWorkshop.project_id.in_(acc), PlannedWorkshop.project_id.is_(None)),
                    )
            parts_wo.append(mine_w)
        if can_view_others:
            other_w = PlannedWorkshop.coach_id != current_user.id
            if acc is not None:
                if len(acc) == 0:
                    other_w = and_(other_w, false())
                else:
                    other_w = and_(
                        other_w,
                        or_(PlannedWorkshop.project_id.in_(acc), PlannedWorkshop.project_id.is_(None)),
                    )
            parts_wo.append(other_w)
        if parts_wo:
            wq = (
                PlannedWorkshop.query.filter(
                    PlannedWorkshop.status == 'open',
                    or_(*parts_wo),
                )
                .options(joinedload(PlannedWorkshop.project), joinedload(PlannedWorkshop.coach))
                .order_by(PlannedWorkshop.planned_for_date, PlannedWorkshop.id)
            )
            all_open_workshops = wq.all()
            workshop_items, overdue_workshop_items = _split_planned_open_by_overdue(
                all_open_workshops, sort_today
            )
            workshop_items.sort(
                key=lambda it: (
                    0 if it.planned_for_date == sort_today else 1,
                    it.planned_for_date or date.min,
                    it.id,
                )
            )
        else:
            overdue_workshop_items = []

        parts_wd = []
        if can_pw:
            mine_d = and_(
                PlannedWorkshop.coach_id == current_user.id,
                PlannedWorkshop.fulfilled_workshop_id.isnot(None),
            )
            if acc is not None:
                if len(acc) == 0:
                    mine_d = and_(mine_d, false())
                else:
                    mine_d = and_(
                        mine_d,
                        or_(PlannedWorkshop.project_id.in_(acc), PlannedWorkshop.project_id.is_(None)),
                    )
            parts_wd.append(mine_d)
        if can_view_others:
            other_d = and_(
                PlannedWorkshop.coach_id != current_user.id,
                PlannedWorkshop.fulfilled_workshop_id.isnot(None),
            )
            if acc is not None:
                if len(acc) == 0:
                    other_d = and_(other_d, false())
                else:
                    other_d = and_(
                        other_d,
                        or_(PlannedWorkshop.project_id.in_(acc), PlannedWorkshop.project_id.is_(None)),
                    )
            parts_wd.append(other_d)
        if parts_wd:
            q_w_done = PlannedWorkshop.query.filter(or_(*parts_wd)).options(
                joinedload(PlannedWorkshop.project),
                joinedload(PlannedWorkshop.fulfilled_workshop),
                joinedload(PlannedWorkshop.coach),
            )
            fulfilled_workshop_plans = q_w_done.all()
            fulfilled_workshop_plans.sort(
                key=lambda p: (
                    p.fulfilled_workshop.workshop_date if p.fulfilled_workshop else datetime.min,
                    p.id,
                ),
                reverse=True,
            )
            fulfilled_workshop_plans = fulfilled_workshop_plans[:100]

    fulfilled_plans = []
    if can_see_coaching_plans:
        parts_done = []
        if can_pc:
            mine_done = PlannedCoaching.coach_id == current_user.id
            if acc is not None:
                if len(acc) == 0:
                    mine_done = and_(mine_done, false())
                else:
                    mine_done = and_(mine_done, PlannedCoaching.project_id.in_(acc))
            parts_done.append(and_(mine_done, PlannedCoaching.status == 'fulfilled'))
        if can_view_others:
            other_done = PlannedCoaching.coach_id != current_user.id
            if acc is not None:
                if len(acc) == 0:
                    other_done = and_(other_done, false())
                else:
                    other_done = and_(other_done, PlannedCoaching.project_id.in_(acc))
            parts_done.append(and_(other_done, PlannedCoaching.status == 'fulfilled'))
        if parts_done:
            q_done = (
                PlannedCoaching.query.filter(or_(*parts_done))
                .options(
                    *coaching_opts,
                    joinedload(PlannedCoaching.fulfilled_coaching),
                )
            )
            fulfilled_plans = q_done.all()
            fulfilled_plans.sort(
                key=lambda p: (
                    p.fulfilled_coaching.coaching_date if p.fulfilled_coaching else datetime.min,
                    p.id,
                ),
                reverse=True,
            )
            fulfilled_plans = fulfilled_plans[:100]

    def _bucket_open_rows(rows):
        out = {
            'today': [],
            'next_week': [],
            'next_month': [],
            'later': [],
        }
        for row in rows or []:
            d = row.planned_for_date or sort_today
            if d == sort_today:
                out['today'].append(row)
            elif d <= next_week_end:
                out['next_week'].append(row)
            elif d <= next_month_end:
                out['next_month'].append(row)
            else:
                out['later'].append(row)
        return out

    coaching_open_groups = _bucket_open_rows(items)
    workshop_open_groups = _bucket_open_rows(workshop_items)

    def _bucket_done_rows(rows, date_attr):
        out = {
            'today': [],
            'last_week': [],
            'last_month': [],
            'older': [],
        }
        for row in rows or []:
            done_obj = getattr(row, date_attr, None)
            d = None
            if done_obj is not None:
                dt = getattr(done_obj, 'coaching_date', None) or getattr(done_obj, 'workshop_date', None)
                if dt:
                    d = utc_naive_or_aware_to_athens_date(dt)
            if d is None:
                d = row.planned_for_date or sort_today
            if d == sort_today:
                out['today'].append(row)
            elif d >= last_week_start:
                out['last_week'].append(row)
            elif d >= last_month_start:
                out['last_month'].append(row)
            else:
                out['older'].append(row)
        return out

    coaching_done_groups = _bucket_done_rows(fulfilled_plans, 'fulfilled_coaching')
    workshop_done_groups = _bucket_done_rows(fulfilled_workshop_plans, 'fulfilled_workshop')

    active_tab = (request.args.get('tab') or 'offen').strip()
    if active_tab not in ('offen', 'ueberfaellig', 'geschlossen'):
        active_tab = 'offen'
    n_overdue = (
        (len(overdue_coaching_items) if can_see_coaching_plans else 0)
        + (len(overdue_workshop_items) if can_see_workshop_plans else 0)
    )
    can_manage_own_plans = can_pc or can_pw
    can_see_overdue_tab = can_manage_own_plans or can_view_others

    return render_template(
        'main/planned_coachings.html',
        title='Geplante Coachings / Workshops',
        items=items,
        workshop_items=workshop_items,
        overdue_coaching_items=overdue_coaching_items,
        overdue_workshop_items=overdue_workshop_items,
        fulfilled_plans=fulfilled_plans,
        fulfilled_workshop_plans=fulfilled_workshop_plans,
        can_view_others_planned=can_view_others,
        can_see_coaching_plans=can_see_coaching_plans,
        can_see_workshop_plans=can_see_workshop_plans,
        coaching_open_groups=coaching_open_groups,
        workshop_open_groups=workshop_open_groups,
        coaching_done_groups=coaching_done_groups,
        workshop_done_groups=workshop_done_groups,
        today_d=sort_today,
        active_tab=active_tab,
        n_overdue=n_overdue,
        can_manage_own_plans=can_manage_own_plans,
        can_see_overdue_tab=can_see_overdue_tab,
        can_jetzt_planen=can_pc or can_pw,
        config=current_app.config,
    )


@bp.route('/geplante-coachings/<int:planned_id>/datum', methods=['POST'])
@login_required
@permission_required('planned_coachings')
def planned_coaching_update_date(planned_id):
    pc = PlannedCoaching.query.get_or_404(planned_id)
    if not _user_may_edit_planned_coaching(pc):
        flash('Keine Berechtigung oder Eintrag nicht gefunden.', 'danger')
        return _redirect_planned_coachings_list('ueberfaellig')
    raw = (request.form.get('planned_for_date') or '').strip()
    if not raw:
        flash('Bitte ein Datum wählen.', 'warning')
        return _redirect_planned_coachings_list('ueberfaellig')
    try:
        new_date = datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        flash('Ungültiges Datum.', 'warning')
        return _redirect_planned_coachings_list('ueberfaellig')
    today = today_athens_date()
    if new_date < today:
        flash('Neues Datum darf nicht in der Vergangenheit liegen.', 'warning')
        return _redirect_planned_coachings_list('ueberfaellig')
    pc.planned_for_date = new_date
    db.session.commit()
    flash('Datum wurde aktualisiert.', 'success')
    return_tab = 'offen' if new_date >= today else 'ueberfaellig'
    return _redirect_planned_coachings_list(return_tab)


@bp.route('/geplante-coachings/<int:planned_id>/loeschen', methods=['POST'])
@login_required
@permission_required('planned_coachings')
def planned_coaching_delete(planned_id):
    pc = PlannedCoaching.query.get_or_404(planned_id)
    if not _user_may_edit_planned_coaching(pc):
        flash('Keine Berechtigung oder Eintrag nicht gefunden.', 'danger')
        return _redirect_planned_coachings_list('ueberfaellig')
    db.session.delete(pc)
    db.session.commit()
    flash('Geplantes Coaching wurde entfernt.', 'success')
    return _redirect_planned_coachings_list('ueberfaellig')


@bp.route('/geplante-coachings/workshop/<int:planned_w_id>/datum', methods=['POST'])
@login_required
@permission_required('add_workshop')
def planned_workshop_update_date(planned_w_id):
    pw = PlannedWorkshop.query.get_or_404(planned_w_id)
    if not _user_may_edit_planned_workshop(pw):
        flash('Keine Berechtigung oder Eintrag nicht gefunden.', 'danger')
        return _redirect_planned_coachings_list('ueberfaellig')
    raw = (request.form.get('planned_for_date') or '').strip()
    if not raw:
        flash('Bitte ein Datum wählen.', 'warning')
        return _redirect_planned_coachings_list('ueberfaellig')
    try:
        new_date = datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        flash('Ungültiges Datum.', 'warning')
        return _redirect_planned_coachings_list('ueberfaellig')
    today = today_athens_date()
    if new_date < today:
        flash('Neues Datum darf nicht in der Vergangenheit liegen.', 'warning')
        return _redirect_planned_coachings_list('ueberfaellig')
    pw.planned_for_date = new_date
    db.session.commit()
    flash('Datum wurde aktualisiert.', 'success')
    return_tab = 'offen' if new_date >= today else 'ueberfaellig'
    return _redirect_planned_coachings_list(return_tab)


@bp.route('/geplante-coachings/workshop/<int:planned_w_id>/loeschen', methods=['POST'])
@login_required
@permission_required('add_workshop')
def planned_workshop_delete(planned_w_id):
    pw = PlannedWorkshop.query.get_or_404(planned_w_id)
    if not _user_may_edit_planned_workshop(pw):
        flash('Keine Berechtigung oder Eintrag nicht gefunden.', 'danger')
        return _redirect_planned_coachings_list('ueberfaellig')
    db.session.delete(pw)
    db.session.commit()
    flash('Geplanter Workshop wurde entfernt.', 'success')
    return _redirect_planned_coachings_list('ueberfaellig')


@bp.route('/api/member-coaching-trend')
@login_required
@any_permission_required('view_pl_qm_dashboard', 'view_own_team')
def get_member_coaching_trend():
    team_member_id = request.args.get('team_member_id', type=int)
    count = request.args.get('count', default='10', type=str)
    if not team_member_id:
        return jsonify({'labels': [], 'scores': [], 'dates': []})

    tm_row = TeamMember.query.get(team_member_id)
    if not tm_row:
        return jsonify({'labels': [], 'scores': [], 'dates': []})
    allowed_team_ids = {t.id for t in _get_teams_for_team_view()}
    if tm_row.team_id not in allowed_team_ids:
        return jsonify({'labels': [], 'scores': [], 'dates': []})

    query = Coaching.query.filter_by(team_member_id=team_member_id).order_by(desc(Coaching.coaching_date))
    if count != 'all':
        try:
            query = query.limit(int(count))
        except (ValueError, TypeError):
            query = query.limit(10)
    coachings = query.all()
    coachings.reverse()  # oldest first for chart

    labels = [f"Coaching #{i+1}" for i in range(len(coachings))]
    scores = [(c.performance_mark or 0) * 10 for c in coachings]
    dates = [c.coaching_date.strftime('%d.%m.%Y') if c.coaching_date else '' for c in coachings]
    coaching_iso = []
    for c in coachings:
        if c.coaching_date:
            d = c.coaching_date.date() if hasattr(c.coaching_date, 'date') else c.coaching_date
            coaching_iso.append(d.isoformat() if hasattr(d, 'isoformat') else '')
        else:
            coaching_iso.append('')

    project_id = tm_row.team.project_id if tm_row.team else None
    kpi_daily = []
    if project_id and kpi_logic.kpi_features_enabled():
        kpi_daily = _member_kpi_daily_series(project_id, team_member_id, days=90)
    card_settings = _team_view_card_settings(project_id) if project_id else kpi_logic.DEFAULT_TEAM_VIEW_CARD

    return jsonify({
        'labels': labels,
        'scores': scores,
        'dates': dates,
        'coaching_dates': coaching_iso,
        'kpi_daily': kpi_daily,
        'card_settings': card_settings,
    })


# --- Project selection ---
@bp.route('/set-project/<int:project_id>')
@login_required
def set_project(project_id):
    project = Project.query.get_or_404(project_id)
    if current_user.role_name in [ROLE_ADMIN, ROLE_BETRIEBSLEITER]:
        session['active_project'] = project_id
    elif current_user.role_name == ROLE_ABTEILUNGSLEITER and project in current_user.projects:
        session['active_project'] = project_id
    else:
        allowed = get_accessible_project_ids()
        if allowed and project_id in allowed:
            session['active_project'] = project_id
        else:
            flash('Sie haben keine Berechtigung für dieses Projekt.', 'danger')
            return redirect(url_for('main.index'))
    flash(f'Projekt gewechselt zu {project.name}.', 'success')
    return redirect(request.referrer or url_for('main.index'))


# --- Assigned Coachings (Coach-Ansicht + PL/Zuweiser-Ansicht) ---
@bp.route('/assigned-coachings')
@login_required
@any_permission_required('view_assigned_coachings', 'view_pl_qm_dashboard', 'assign_coachings')
def assigned_coachings():
    _apply_query_project_to_session()
    project_id = get_visible_project_id()
    if not project_id:
        flash('Kein Projekt ausgewählt.', 'danger')
        return redirect(url_for('main.index'))

    can_assign = _user_can_assign_coachings()
    can_coach_list = current_user.has_permission('view_assigned_coachings')
    if not can_assign and not can_coach_list:
        flash('Keine Berechtigung.', 'danger')
        return redirect(url_for('main.index'))

    view_type = 'pl' if can_assign else 'coach'

    tab_active = request.args.get('status', 'current')
    if tab_active not in ('current', 'completed', 'attention'):
        tab_active = 'current'

    page = request.args.get('page', 1, type=int)
    team_filter = request.args.get('team', type=int)
    coach_filter = request.args.get('coach', type=int)
    member_filter = request.args.get('member', type=int)
    search_term = (request.args.get('search') or '').strip()
    sort_by = request.args.get('sort_by', 'deadline')
    sort_dir = request.args.get('sort_dir', 'asc')
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'

    all_teams = _teams_for_assigned_coaching_filters(project_id_single=project_id)
    visible_team_ids = [t.id for t in all_teams]
    _allowed_teams = set(visible_team_ids)
    if team_filter and team_filter not in _allowed_teams:
        team_filter = None
    if member_filter:
        _mf = TeamMember.query.get(member_filter)
        if not _mf or _mf.team_id not in _allowed_teams:
            member_filter = None

    _coach_scope = (
        AssignedCoaching.query.join(TeamMember, AssignedCoaching.team_member_id == TeamMember.id)
        .join(Team, TeamMember.team_id == Team.id)
        .filter(Team.project_id == project_id)
    )
    if view_type == 'pl':
        _coach_scope = _coach_scope.filter(AssignedCoaching.project_leader_id == current_user.id)
    else:
        _coach_scope = _coach_scope.filter(AssignedCoaching.coach_id == current_user.id)
    if visible_team_ids:
        _coach_scope = _coach_scope.filter(TeamMember.team_id.in_(visible_team_ids))
    else:
        _coach_scope = _coach_scope.filter(false())
    coach_id_list = [r[0] for r in _coach_scope.with_entities(AssignedCoaching.coach_id).distinct().all() if r[0]]
    if coach_filter and coach_filter not in coach_id_list:
        coach_filter = None
    all_coaches = (
        list(User.query.filter(User.id.in_(coach_id_list)).all())
        if coach_id_list else []
    )
    all_coaches.sort(key=lambda u: (u.coach_display_name or '').lower())

    if visible_team_ids:
        all_members = (
            TeamMember.query.join(Team, TeamMember.team_id == Team.id)
            .filter(
                TeamMember.team_id.in_(visible_team_ids),
                Team.name != ARCHIV_TEAM_NAME,
                or_(Team.active_for_coaching.is_(True), Team.visible_for_coaching_assignment.is_(True)),
            )
            .order_by(Team.name, TeamMember.name)
            .all()
        )
    else:
        all_members = []

    q = AssignedCoaching.query.options(
        joinedload(AssignedCoaching.team_member).joinedload(TeamMember.team),
        joinedload(AssignedCoaching.coach),
    ).join(TeamMember, AssignedCoaching.team_member_id == TeamMember.id).join(
        Team, TeamMember.team_id == Team.id
    ).filter(Team.project_id == project_id)

    # Enforce role-based team scope (not just dropdown scope).
    if visible_team_ids:
        q = q.filter(TeamMember.team_id.in_(visible_team_ids))
    else:
        q = q.filter(false())

    if view_type == 'pl':
        q = q.filter(AssignedCoaching.project_leader_id == current_user.id)
    else:
        q = q.filter(AssignedCoaching.coach_id == current_user.id)

    _now_cmp = datetime.utcnow()
    if tab_active == 'completed':
        q = q.filter(AssignedCoaching.status.in_(['completed', 'expired', 'rejected', 'cancelled']))
    elif tab_active == 'attention':
        # Nur überfällige angenommene / laufende Aufträge (Deadline überschritten)
        q = q.filter(
            AssignedCoaching.status.in_(['accepted', 'in_progress']),
            AssignedCoaching.deadline < _now_cmp,
        )
    else:
        # Aktuelle Aufträge: ausstehend (pending) + angenommen / in Arbeit mit Deadline noch nicht überschritten
        current_open = and_(
            AssignedCoaching.status.in_(['accepted', 'in_progress']),
            AssignedCoaching.deadline >= _now_cmp,
        )
        q = q.filter(or_(AssignedCoaching.status == 'pending', current_open))

    if team_filter:
        q = q.filter(TeamMember.team_id == team_filter)
    if coach_filter:
        q = q.filter(AssignedCoaching.coach_id == coach_filter)
    if member_filter:
        q = q.filter(AssignedCoaching.team_member_id == member_filter)
    if search_term:
        st = f'%{search_term}%'
        q = q.filter(or_(
            AssignedCoaching.coach.has(User.username.ilike(st)),
            TeamMember.name.ilike(st),
        ))

    CoachAlias = aliased(User)
    if sort_by == 'coach_name':
        q = q.join(CoachAlias, AssignedCoaching.coach_id == CoachAlias.id)
        order_expr = CoachAlias.username
    elif sort_by == 'member_name':
        order_expr = TeamMember.name
    elif sort_by == 'start':
        order_expr = AssignedCoaching.created_at
    else:
        order_expr = AssignedCoaching.deadline

    if sort_dir == 'desc':
        q = q.order_by(desc(order_expr))
    else:
        q = q.order_by(order_expr)

    assignments = q.paginate(page=page, per_page=15, error_out=False)

    kpi_period = (request.args.get('kpi_period') or 'this_month').strip()
    kpi_from = (request.args.get('kpi_from') or '').strip()
    kpi_to = (request.args.get('kpi_to') or '').strip()
    if kpi_period not in ('this_month', '30days', '90days', 'this_year', 'all', 'custom'):
        kpi_period = 'this_month'
    _, _, kpi_period_label = _kpi_period_dates(kpi_period, kpi_from, kpi_to)
    member_performance = (
        _member_performance_for_assigned_page(
            project_id, kpi_period=kpi_period, kpi_from=kpi_from, kpi_to=kpi_to,
        )
        if view_type == 'pl' else []
    )

    project_bar_extra_hidden = {'status': tab_active}
    if team_filter:
        project_bar_extra_hidden['team'] = team_filter
    if coach_filter:
        project_bar_extra_hidden['coach'] = coach_filter
    if member_filter:
        project_bar_extra_hidden['member'] = member_filter
    if search_term:
        project_bar_extra_hidden['search'] = search_term
    if sort_by not in ('deadline',):
        project_bar_extra_hidden['sort_by'] = sort_by
    if sort_dir != 'asc':
        project_bar_extra_hidden['sort_dir'] = sort_dir
    if kpi_period != 'this_month':
        project_bar_extra_hidden['kpi_period'] = kpi_period
    if kpi_period == 'custom':
        if kpi_from:
            project_bar_extra_hidden['kpi_from'] = kpi_from
        if kpi_to:
            project_bar_extra_hidden['kpi_to'] = kpi_to

    return render_template(
        'main/assigned_coachings.html',
        assignments=assignments,
        project_bar_endpoint='main.assigned_coachings',
        project_bar_extra_hidden=project_bar_extra_hidden,
        status_filter=tab_active,
        tab_active=tab_active,
        view_type=view_type,
        team_filter=team_filter,
        coach_filter=coach_filter,
        member_filter=member_filter,
        search_term=search_term,
        sort_by=sort_by,
        sort_dir=sort_dir,
        all_teams=all_teams,
        all_coaches=all_coaches,
        all_members=all_members,
        member_performance=member_performance,
        kpi_period=kpi_period,
        kpi_period_label=kpi_period_label,
        kpi_from=kpi_from,
        kpi_to=kpi_to,
        can_add_coaching=current_user.has_permission('add_coaching'),
        config=current_app.config,
    )


@bp.route('/create-assigned-coaching', methods=['GET', 'POST'])
@login_required
@permission_required('assign_coachings')
def create_assigned_coaching():
    _apply_query_project_to_session()
    project_id = get_visible_project_id()
    if not project_id:
        flash('Kein Projekt ausgewählt.', 'danger')
        return redirect(url_for('main.index'))

    selected_member_ids = _member_ids_from_assign_request()
    tm_for_coaches = selected_member_ids[0] if len(selected_member_ids) == 1 else None

    form = AssignedCoachingForm(allowed_project_ids=[project_id], team_member_id=tm_for_coaches)
    if request.method == 'GET' and len(selected_member_ids) == 1:
        form.team_member_id.data = selected_member_ids[0]

    active_counts = getattr(form, 'team_member_active_assignment_counts', {})
    selected_members = []
    if selected_member_ids:
        rows = (
            TeamMember.query.options(joinedload(TeamMember.team))
            .join(Team, TeamMember.team_id == Team.id)
            .filter(
                TeamMember.id.in_(selected_member_ids),
                Team.project_id == project_id,
                Team.name != ARCHIV_TEAM_NAME,
            )
            .all()
        )
        by_id = {m.id: m for m in rows}
        for mid in selected_member_ids:
            m = by_id.get(mid)
            if m:
                selected_members.append({
                    'id': m.id,
                    'name': m.name,
                    'team_name': m.team.name if m.team else '',
                    'active_assignment_count': active_counts.get(m.id, 0),
                })

    if form.validate_on_submit():
        submit_ids = _member_ids_from_assign_request()
        if not submit_ids:
            flash('Bitte mindestens ein Teammitglied auswählen.', 'danger')
            return redirect(url_for('main.create_assigned_coaching', project=project_id))

        coach_u = User.query.get(form.coach_id.data)
        d = form.deadline.data
        dl = datetime(d.year, d.month, d.day, 23, 59, 59)
        note_raw = request.form.get('current_note')
        try:
            cur_note = float(note_raw) if note_raw else None
        except (TypeError, ValueError):
            cur_note = None

        created = 0
        skipped = 0
        for mid in submit_ids:
            tm_as = TeamMember.query.options(joinedload(TeamMember.team)).get(mid)
            if not tm_as or not tm_as.team or tm_as.team.project_id != project_id:
                skipped += 1
                continue
            if not team_member_eligible_for_coaching_assignment(tm_as):
                skipped += 1
                continue
            if not coach_u or not user_eligible_assignable_coach(
                coach_u, project_id, mid, for_assignment=True
            ):
                skipped += 1
                continue
            perf_note = cur_note
            if len(submit_ids) > 1:
                avg = db.session.query(db.func.avg(Coaching.performance_mark)).filter(
                    Coaching.team_member_id == mid,
                    Coaching.project_id == project_id,
                ).scalar()
                perf_note = round(float(avg or 0) * 10, 1) if avg is not None else None
            ac = AssignedCoaching(
                project_leader_id=current_user.id,
                coach_id=form.coach_id.data,
                team_member_id=mid,
                deadline=dl,
                expected_coaching_count=form.expected_coaching_count.data,
                desired_performance_note=form.desired_performance_note.data,
                current_performance_note_at_assign=perf_note,
                status='pending',
            )
            _snapshot_assignment_start_kpis(ac, project_id)
            db.session.add(ac)
            created += 1
        if created:
            db.session.commit()
            if created == 1:
                flash('Coaching-Aufgabe zugewiesen.', 'success')
            else:
                flash(f'{created} Coaching-Aufgaben zugewiesen (je eine pro Teammitglied).', 'success')
            return redirect(url_for('main.assigned_coachings', project=project_id, status='current'))
        db.session.rollback()
        flash('Zuweisung fehlgeschlagen. Coach oder Teammitglied ungültig.', 'danger')
        return redirect(url_for('main.create_assigned_coaching', project=project_id))

    return render_template(
        'main/create_assigned_coaching.html',
        form=form,
        active_assignment_counts=active_counts,
        selected_members=selected_members,
        bulk_assign_mode=len(selected_members) > 0,
        config=current_app.config,
    )


@bp.route('/api/assignment-coaches')
@login_required
@permission_required('assign_coachings')
def api_assignment_coaches():
    """Coach dropdown options for the current project; refined by selected team member(s)."""
    project_id = get_visible_project_id()
    if not project_id:
        return jsonify([])
    mids = request.args.getlist('team_member_ids')
    parsed = []
    for raw in mids:
        for part in str(raw).split(','):
            part = part.strip()
            if part.isdigit():
                parsed.append(int(part))
    if not parsed:
        mid = request.args.get('team_member_id', type=int)
        if mid:
            parsed = [mid]
    valid = []
    for mid in parsed:
        m = TeamMember.query.get(mid)
        if m and m.team and m.team.project_id == project_id:
            valid.append(mid)
    if len(valid) > 1:
        coaches = users_for_assignment_coach_dropdown_multi(project_id, valid)
    elif len(valid) == 1:
        coaches = users_for_assignment_coach_dropdown(project_id, valid[0])
    else:
        coaches = users_for_assignment_coach_dropdown(project_id, None)
    return jsonify([
        {'id': u.id, 'label': f"{u.coach_display_name} ({u.role_name})"}
        for u in coaches
    ])


@bp.route('/api/member-current-score')
@login_required
@permission_required('assign_coachings')
def get_member_current_score():
    mid = request.args.get('member_id', type=int)
    if not mid:
        return jsonify({'score': 0})
    project_id = get_visible_project_id()
    m = TeamMember.query.get(mid)
    if not m or not m.team or m.team.project_id != project_id:
        return jsonify({'score': 0})
    avg = db.session.query(db.func.avg(Coaching.performance_mark)).filter(
        Coaching.team_member_id == mid,
        Coaching.project_id == project_id,
    ).scalar()
    score = round(float(avg or 0) * 10, 1) if avg is not None else 0.0
    return jsonify({'score': score})


@bp.route('/assigned-coachings/gesamtbericht')
@login_required
@permission_required('view_assigned_coaching_report')
def assigned_coachings_gesamtbericht():
    _apply_query_project_to_session()
    raw_status = (request.args.get('status') or '').strip()
    if raw_status in ('all', 'current', 'completed', 'pending'):
        tab_active = raw_status
    else:
        tab_active = 'current'
    page = request.args.get('page', 1, type=int)
    team_filter = request.args.get('team', type=int)
    coach_filter = request.args.get('coach', type=int)
    member_filter = request.args.get('member', type=int)
    search_term = (request.args.get('search') or '').strip()
    sort_by = request.args.get('sort_by', 'deadline')
    sort_dir = request.args.get('sort_dir', 'asc')
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'

    acc = get_accessible_project_ids()
    assigned_tabs_project_id = get_visible_project_id()
    if not assigned_tabs_project_id:
        if acc is None:
            _fp0 = Project.query.order_by(Project.name).first()
            assigned_tabs_project_id = _fp0.id if _fp0 else None
        elif acc:
            assigned_tabs_project_id = acc[0]
    project_filter = request.args.get('project', type=int)
    if project_filter and acc is not None and project_filter not in acc:
        project_filter = None

    project_leader_filter = request.args.get('project_leader', type=int)

    all_teams = _teams_for_assigned_coaching_filters(gesamt_acc=acc, gesamt_project_filter=project_filter)
    visible_team_ids = [t.id for t in all_teams]
    _allowed_team_set = set(visible_team_ids)
    if team_filter and team_filter not in _allowed_team_set:
        team_filter = None
    if member_filter:
        _gmem = TeamMember.query.get(member_filter)
        if not _gmem or _gmem.team_id not in _allowed_team_set:
            member_filter = None

    coach_sub = (
        db.session.query(AssignedCoaching.coach_id)
        .join(TeamMember, AssignedCoaching.team_member_id == TeamMember.id)
        .join(Team, TeamMember.team_id == Team.id)
    )
    if acc is not None:
        coach_sub = coach_sub.filter(Team.project_id.in_(acc))
    if project_filter:
        coach_sub = coach_sub.filter(Team.project_id == project_filter)
    if visible_team_ids:
        coach_sub = coach_sub.filter(TeamMember.team_id.in_(visible_team_ids))
    else:
        coach_sub = coach_sub.filter(false())
    g_coach_id_list = [r[0] for r in coach_sub.distinct().all() if r[0]]
    if coach_filter and coach_filter not in g_coach_id_list:
        coach_filter = None
    all_coaches = (
        list(User.query.filter(User.id.in_(g_coach_id_list)).all())
        if g_coach_id_list else []
    )
    all_coaches.sort(key=lambda usr: (usr.coach_display_name or '').lower())

    if visible_team_ids:
        all_members = (
            TeamMember.query.join(Team, TeamMember.team_id == Team.id)
            .filter(
                TeamMember.team_id.in_(visible_team_ids),
                Team.name != ARCHIV_TEAM_NAME,
                or_(Team.active_for_coaching.is_(True), Team.visible_for_coaching_assignment.is_(True)),
            )
            .order_by(Team.name, TeamMember.name)
            .all()
        )
    else:
        all_members = []

    gesamt_pbe = _gesamtbericht_project_bar_extra(
        tab_active,
        team_filter,
        coach_filter,
        member_filter,
        search_term,
        sort_by,
        sort_dir,
        project_leader_filter=project_leader_filter,
    )

    leaders_scope = _assigned_coachings_scope_query(project_filter_id=project_filter)
    if leaders_scope is not None:
        if visible_team_ids:
            leaders_scope = leaders_scope.filter(TeamMember.team_id.in_(visible_team_ids))
        else:
            leaders_scope = leaders_scope.filter(false())
    all_project_leaders = []
    if leaders_scope is not None:
        lid_rows = leaders_scope.with_entities(AssignedCoaching.project_leader_id).distinct().all()
        leader_ids = [r[0] for r in lid_rows if r[0]]
        if leader_ids:
            all_project_leaders = list(User.query.filter(User.id.in_(leader_ids)).all())
            all_project_leaders.sort(
                key=lambda u: (u.coach_display_name or u.username or '').lower()
            )

    snapshot = _assigned_coachings_scope_query(project_filter_id=project_filter)
    if snapshot is not None:
        if visible_team_ids:
            snapshot = snapshot.filter(TeamMember.team_id.in_(visible_team_ids))
        else:
            snapshot = snapshot.filter(false())
    if snapshot is not None and project_leader_filter:
        snapshot = snapshot.filter(AssignedCoaching.project_leader_id == project_leader_filter)
    report_count_current = 0
    report_count_pending = 0
    report_count_completed = 0
    if snapshot is not None:
        report_count_current = snapshot.filter(
            AssignedCoaching.status.in_(['pending', 'accepted', 'in_progress'])
        ).count()
        report_count_pending = snapshot.filter(AssignedCoaching.status == 'pending').count()
        report_count_completed = snapshot.filter(
            AssignedCoaching.status.in_(['completed', 'expired', 'rejected', 'cancelled'])
        ).count()

    base_q = _assigned_coachings_scope_query(project_filter_id=project_filter)
    if base_q is not None:
        if visible_team_ids:
            base_q = base_q.filter(TeamMember.team_id.in_(visible_team_ids))
        else:
            base_q = base_q.filter(false())
    if base_q is not None and project_leader_filter:
        base_q = base_q.filter(AssignedCoaching.project_leader_id == project_leader_filter)
    if base_q is None:
        empty_page = AssignedCoaching.query.filter(false()).paginate(page=page, per_page=20, error_out=False)
        if acc is None:
            filter_projects = Project.query.order_by(Project.name).all()
        else:
            filter_projects = []
        return render_template(
            'main/assigned_coachings_gesamtbericht.html',
            title='Zugewiesene Coachings – Gesamtbericht',
            assignments=empty_page,
            tab_active=tab_active,
            team_filter=team_filter,
            coach_filter=coach_filter,
            member_filter=member_filter,
            search_term=search_term,
            sort_by=sort_by,
            sort_dir=sort_dir,
            project_filter=project_filter,
            filter_projects=filter_projects,
            all_teams=all_teams,
            all_coaches=all_coaches,
            all_members=all_members,
            report_count_current=0,
            report_count_pending=0,
            report_count_completed=0,
            assigned_tabs_project_id=assigned_tabs_project_id,
            project_bar_endpoint='main.assigned_coachings_gesamtbericht',
            project_bar_extra_hidden=gesamt_pbe,
            project_leader_filter=project_leader_filter,
            all_project_leaders=all_project_leaders,
            config=current_app.config,
        )

    q = base_q.options(
        joinedload(AssignedCoaching.team_member).joinedload(TeamMember.team).joinedload(Team.project),
        joinedload(AssignedCoaching.coach),
        joinedload(AssignedCoaching.project_leader),
    )

    if tab_active == 'completed':
        q = q.filter(AssignedCoaching.status.in_(['completed', 'expired', 'rejected', 'cancelled']))
    elif tab_active == 'current':
        q = q.filter(AssignedCoaching.status.in_(['pending', 'accepted', 'in_progress']))
    elif tab_active == 'pending':
        q = q.filter(AssignedCoaching.status == 'pending')
    # tab_active == 'all': alle Status

    if team_filter:
        q = q.filter(TeamMember.team_id == team_filter)
    if coach_filter:
        q = q.filter(AssignedCoaching.coach_id == coach_filter)
    if member_filter:
        q = q.filter(AssignedCoaching.team_member_id == member_filter)
    if search_term:
        st = f'%{search_term}%'
        q = q.filter(or_(
            AssignedCoaching.coach.has(User.username.ilike(st)),
            TeamMember.name.ilike(st),
        ))

    CoachAlias = aliased(User)
    if sort_by == 'coach_name':
        q = q.join(CoachAlias, AssignedCoaching.coach_id == CoachAlias.id)
        order_expr = CoachAlias.username
    elif sort_by == 'member_name':
        order_expr = TeamMember.name
    elif sort_by == 'project_name':
        q = q.join(Project, Team.project_id == Project.id)
        order_expr = Project.name
    else:
        order_expr = AssignedCoaching.deadline

    if sort_dir == 'desc':
        q = q.order_by(desc(order_expr))
    else:
        q = q.order_by(order_expr)

    assignments = q.paginate(page=page, per_page=20, error_out=False)

    if acc is None:
        filter_projects = Project.query.order_by(Project.name).all()
    else:
        filter_projects = Project.query.filter(Project.id.in_(acc)).order_by(Project.name).all()

    return render_template(
        'main/assigned_coachings_gesamtbericht.html',
        title='Zugewiesene Coachings – Gesamtbericht',
        assignments=assignments,
        tab_active=tab_active,
        team_filter=team_filter,
        coach_filter=coach_filter,
        member_filter=member_filter,
        search_term=search_term,
        sort_by=sort_by,
        sort_dir=sort_dir,
        project_filter=project_filter,
        filter_projects=filter_projects,
        all_teams=all_teams,
        all_coaches=all_coaches,
        all_members=all_members,
        report_count_current=report_count_current,
        report_count_pending=report_count_pending,
        report_count_completed=report_count_completed,
        assigned_tabs_project_id=assigned_tabs_project_id,
        project_bar_endpoint='main.assigned_coachings_gesamtbericht',
        project_bar_extra_hidden=gesamt_pbe,
        project_leader_filter=project_leader_filter,
        all_project_leaders=all_project_leaders,
        config=current_app.config,
    )


@bp.route('/assigned-coaching-report/<int:assignment_id>')
@login_required
@any_permission_required('assign_coachings', 'view_pl_qm_dashboard', 'view_assigned_coaching_report')
def assigned_coaching_report(assignment_id):
    assignment = AssignedCoaching.query.options(
        joinedload(AssignedCoaching.team_member).joinedload(TeamMember.team),
        joinedload(AssignedCoaching.coach),
    ).get_or_404(assignment_id)

    tm = assignment.team_member
    if not tm or not tm.team:
        flash('Ungültige Zuweisung.', 'danger')
        return redirect(url_for('main.index'))
    project_id = tm.team.project_id

    acc = get_accessible_project_ids()
    if acc is not None:
        if len(acc) == 0 or project_id not in acc:
            flash('Kein Zugriff auf diese Zuweisung.', 'danger')
            return redirect(url_for('main.index'))

    is_pl_owner = assignment.project_leader_id == current_user.id
    may_pl = is_pl_owner and current_user.has_permission('assign_coachings')
    may_scope = current_user.has_permission('view_assigned_coaching_report')
    if not may_pl and not may_scope:
        flash('Keine Berechtigung für diesen Bericht.', 'danger')
        return redirect(url_for('main.index'))

    done_list = Coaching.query.options(joinedload(Coaching.coach)).filter(
        Coaching.assigned_coaching_id == assignment.id
    ).order_by(Coaching.coaching_date).all()

    coachings_done = len(done_list)
    if done_list:
        final_avg = sum(c.overall_score for c in done_list) / len(done_list)
    else:
        final_avg = 0.0

    end_nps = assignment.end_nps
    end_loes = assignment.end_loesung_quote
    end_info = assignment.end_info_quote
    end_nps_count = assignment.end_nps_count
    end_loes_count = assignment.end_loesung_count
    end_info_count = assignment.end_info_count
    if assignment.status == 'completed' and end_nps is None and end_loes is None and end_info is None:
        live = _member_kpi_snapshot(project_id, assignment.team_member_id)
        end_nps, end_loes, end_info = live['nps'], live['loes_quote'], live['info_quote']
        end_nps_count = live['nps_count']
        end_loes_count = live['loes_count']
        end_info_count = live['info_count']

    start_nps_count = assignment.start_nps_count_at_assign
    start_loes_count = assignment.start_loesung_count_at_assign
    start_info_count = assignment.start_info_count_at_assign
    if (
        start_nps_count is None
        and (assignment.start_nps_at_assign is not None
             or assignment.start_loesung_quote_at_assign is not None
             or assignment.start_info_quote_at_assign is not None)
    ):
        live_start = _member_kpi_snapshot(project_id, assignment.team_member_id)
        start_nps_count = live_start['nps_count']
        start_loes_count = live_start['loes_count']
        start_info_count = live_start['info_count']
    if assignment.status == 'completed' and end_nps_count is None and (
        end_nps is not None or end_loes is not None or end_info is not None
    ):
        live_end = _member_kpi_snapshot(project_id, assignment.team_member_id)
        end_nps_count = live_end['nps_count']
        end_loes_count = live_end['loes_count']
        end_info_count = live_end['info_count']

    report = {
        'assignment': assignment,
        'coachings': done_list,
        'coachings_expected': assignment.expected_coaching_count,
        'coachings_done': coachings_done,
        'start_note': assignment.current_performance_note_at_assign,
        'target_note': assignment.desired_performance_note,
        'final_avg_score': final_avg,
        'start_nps': assignment.start_nps_at_assign,
        'start_loesung': assignment.start_loesung_quote_at_assign,
        'start_info': assignment.start_info_quote_at_assign,
        'start_nps_count': start_nps_count if start_nps_count is not None else 0,
        'start_loes_count': start_loes_count if start_loes_count is not None else 0,
        'start_info_count': start_info_count if start_info_count is not None else 0,
        'end_nps': end_nps,
        'end_loesung': end_loes,
        'end_info': end_info,
        'end_nps_count': end_nps_count if end_nps_count is not None else 0,
        'end_loes_count': end_loes_count if end_loes_count is not None else 0,
        'end_info_count': end_info_count if end_info_count is not None else 0,
        'status': assignment.status,
    }
    return render_template(
        'main/assigned_coaching_report.html',
        report=report,
        assigned_report_project_id=project_id,
        config=current_app.config,
    )


@bp.route('/assigned-coaching-rejection/<int:assignment_id>')
@login_required
def assigned_coaching_rejection_bericht(assignment_id):
    assignment = AssignedCoaching.query.options(
        joinedload(AssignedCoaching.team_member).joinedload(TeamMember.team).joinedload(Team.project),
        joinedload(AssignedCoaching.coach),
        joinedload(AssignedCoaching.project_leader),
    ).get_or_404(assignment_id)
    if not _may_view_assigned_rejection_bericht(assignment):
        flash('Keine Berechtigung oder kein Ablehnungsgrund vorhanden.', 'danger')
        return redirect(url_for('main.index'))
    tm = assignment.team_member
    project_id = tm.team.project_id if tm and tm.team else get_visible_project_id()
    return render_template(
        'main/assigned_coaching_rejection_bericht.html',
        assignment=assignment,
        assigned_report_project_id=project_id,
        config=current_app.config,
    )


@bp.route('/cancel-assigned-coaching/<int:assignment_id>', methods=['POST'])
@login_required
@permission_required('assign_coachings')
def cancel_assigned_coaching(assignment_id):
    assignment = AssignedCoaching.query.get_or_404(assignment_id)
    tm = TeamMember.query.options(joinedload(TeamMember.team)).get(assignment.team_member_id)
    list_pid = tm.team.project_id if tm and tm.team else get_visible_project_id()
    if assignment.project_leader_id != current_user.id:
        flash('Nicht autorisiert.', 'danger')
        return redirect(url_for('main.assigned_coachings', project=list_pid))
    if assignment.status in ('pending', 'accepted', 'in_progress'):
        assignment.status = 'cancelled'
        db.session.commit()
        flash('Aufgabe storniert.', 'success')
    else:
        flash('Aufgabe kann nicht storniert werden.', 'warning')
    return redirect(url_for('main.assigned_coachings', project=list_pid))


@bp.route('/accept-assigned/<int:assignment_id>', methods=['POST'])
@login_required
@permission_required('accept_assigned_coaching')
def accept_assigned_coaching(assignment_id):
    assignment = AssignedCoaching.query.get_or_404(assignment_id)
    tm = TeamMember.query.options(joinedload(TeamMember.team)).get(assignment.team_member_id)
    list_pid = tm.team.project_id if tm and tm.team else get_visible_project_id()
    if assignment.coach_id != current_user.id:
        flash('Nicht autorisiert.', 'danger')
        return redirect(url_for('main.assigned_coachings', project=list_pid))
    if assignment.status == 'pending':
        tm_acc = TeamMember.query.get(assignment.team_member_id)
        if not team_member_eligible_for_coaching_assignment(tm_acc):
            flash('Annahme nicht möglich: Diese Zuweisung ist für das Team nicht mehr gültig (Team nicht freigegeben).', 'danger')
        else:
            assignment.status = 'accepted'
            db.session.commit()
            flash('Aufgabe angenommen.', 'success')
            return redirect(url_for('main.add_coaching', project=list_pid, assigned_id=assignment.id))
    else:
        flash('Aufgabe kann nicht angenommen werden.', 'warning')
    return redirect(url_for('main.assigned_coachings', project=list_pid))


@bp.route('/reject-assigned/<int:assignment_id>', methods=['POST'])
@login_required
@permission_required('reject_assigned_coaching')
def reject_assigned_coaching(assignment_id):
    assignment = AssignedCoaching.query.get_or_404(assignment_id)
    tm = TeamMember.query.options(joinedload(TeamMember.team)).get(assignment.team_member_id)
    list_pid = tm.team.project_id if tm and tm.team else get_visible_project_id()
    if assignment.coach_id != current_user.id:
        flash('Nicht autorisiert.', 'danger')
        return redirect(url_for('main.assigned_coachings', project=list_pid))
    if assignment.status == 'pending':
        reason = (request.form.get('rejection_reason') or '').strip()
        if len(reason) < 3:
            flash('Bitte geben Sie einen Ablehnungsgrund an (mindestens 3 Zeichen).', 'warning')
            return redirect(url_for('main.assigned_coachings', project=list_pid, status='current'))
        assignment.status = 'rejected'
        assignment.rejection_reason = reason[:2000]
        db.session.commit()
        flash('Aufgabe abgelehnt. Der Zuweiser sieht Ihren Ablehnungsgrund.', 'success')
    else:
        flash('Aufgabe kann nicht abgelehnt werden.', 'warning')
    return redirect(url_for('main.assigned_coachings', project=list_pid))


# =====================================================================
# KPIs (Demo): Informationsquote, Lösungsquote, NPS
# =====================================================================

def _kpi_dashboard_date_range(period_arg, date_from_str, date_to_str):
    """Return (start_date, end_date, period) as Python date objects."""
    return kpi_time.dashboard_date_range(period_arg, date_from_str, date_to_str)


def _kpi_dashboard_period_from_request(req):
    """Parse period picker params; no implicit default period."""
    period_arg = (req.args.get('period') or '').strip()
    date_from_str = (req.args.get('date_from') or '').strip()
    date_to_str = (req.args.get('date_to') or '').strip()
    confirmed, start_date, end_date, period_norm, meta = kpi_time.parse_dashboard_period(
        period_arg,
        date_from_str,
        date_to_str,
        period_year=req.args.get('period_year', type=int),
        period_month=req.args.get('period_month', type=int),
        period_week=req.args.get('period_week', type=int),
        period_quarter=req.args.get('period_quarter', type=int),
    )
    label = kpi_time.dashboard_period_label(
        period_norm,
        start_date,
        end_date,
        meta.get('period_year'),
        meta.get('period_month'),
        meta.get('period_week'),
        meta.get('period_quarter'),
    )
    return {
        'confirmed': confirmed,
        'start_date': start_date,
        'end_date': end_date,
        'period': period_norm,
        'label': label,
        'date_from_str': meta.get('date_from_str') or '',
        'date_to_str': meta.get('date_to_str') or '',
        'period_year': meta.get('period_year'),
        'period_month': meta.get('period_month'),
        'period_week': meta.get('period_week'),
        'period_quarter': meta.get('period_quarter'),
    }


def _kpi_data_period_keys(filters, date_column):
    """Return month/week/quarter/year keys with at least one matching row."""
    dcol = cast(date_column, Date)
    rows = (
        db.session.query(dcol)
        .filter(*filters)
        .filter(date_column.isnot(None))
        .distinct()
        .all()
    )
    months, weeks, quarters, years = set(), set(), set(), set()
    for (d,) in rows:
        if not d:
            continue
        years.add(int(d.year))
        months.add(f'{d.year}-{d.month:02d}')
        quarters.add(f'{d.year}-Q{(d.month - 1) // 3 + 1}')
        iso = d.isocalendar()
        weeks.add(f'{iso.year}-W{iso.week:02d}')
    return {
        'months': sorted(months),
        'weeks': sorted(weeks),
        'quarters': sorted(quarters),
        'years': sorted(years),
    }


def _kpi_scope():
    """Returns (accessible_project_ids_or_None, sees_all_teams, my_team_ids)."""
    accessible = get_accessible_project_ids()
    sees_all_teams = _user_sees_all_teams_coaching_dashboard()
    my_team_ids = _dashboard_my_team_ids() if not sees_all_teams else []
    return accessible, sees_all_teams, my_team_ids


def _kpi_base_filters(accessible, sees_all_teams, my_team_ids):
    """Scope filters restricting KpiSurvey to the user's visible projects/teams."""
    filters = []
    if accessible is None:
        pass  # Admin / Betriebsleiter: all projects
    elif not accessible:
        filters.append(KpiSurvey.project_id == -1)
    else:
        filters.append(KpiSurvey.project_id.in_(accessible))
    if not sees_all_teams:
        if my_team_ids:
            filters.append(KpiSurvey.team_id.in_(my_team_ids))
        else:
            filters.append(false())
    return filters


def _parse_kpi_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value.strip()[:10], '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def _kpi_period_dates(period, date_from_str=None, date_to_str=None):
    """Return (date_from, date_to, label) for KPI aggregation in assigned-coachings overview."""
    today = datetime.now(timezone.utc).date()
    if period == 'custom':
        date_from = _parse_kpi_date(date_from_str)
        date_to = _parse_kpi_date(date_to_str)
        if date_from and date_to and date_from <= date_to:
            label = f'{date_from.strftime("%d.%m.%Y")} – {date_to.strftime("%d.%m.%Y")}'
            return date_from, date_to, label
        return today.replace(day=1), today, 'Dieser Monat'
    if period == '30days':
        return today - timedelta(days=29), today, 'Letzte 30 Tage'
    if period == '90days':
        return today - timedelta(days=89), today, 'Letzte 90 Tage'
    if period == 'this_year':
        return today.replace(month=1, day=1), today, 'Dieses Jahr'
    if period == 'all':
        return None, None, 'Gesamt'
    # default: this_month
    return today.replace(day=1), today, 'Dieser Monat'


def _members_kpi_map(project_id, member_ids, kpi_period='this_month', kpi_from=None, kpi_to=None):
    date_from, date_to, _ = _kpi_period_dates(kpi_period, kpi_from, kpi_to)
    return kpi_logic.members_kpi_quotes(project_id, member_ids, date_from, date_to)


def _members_productivity_map(project_id, member_ids):
    """Aggregate productivity KPIs per team member (all intervals)."""
    if not project_id or not member_ids:
        return {}
    rows = ProductivityInterval.query.filter(
        ProductivityInterval.project_id == project_id,
        ProductivityInterval.team_member_id.in_(member_ids),
    ).all()
    by_member = {}
    for iv in rows:
        by_member.setdefault(iv.team_member_id, []).append(iv)
    out = {}
    for mid, intervals in by_member.items():
        sm = productivity_logic.aggregate_summary(intervals)
        if sm:
            out[mid] = sm
    return out


def _member_kpi_snapshot(project_id, member_id):
    """Single-member KPI dict for assignment snapshots."""
    empty = {
        'nps': None, 'loes_quote': None, 'info_quote': None,
        'nps_count': 0, 'loes_count': 0, 'info_count': 0,
    }
    if not project_id or not member_id:
        return empty
    m = _members_kpi_map(project_id, [member_id], kpi_period='all').get(member_id)
    if not m:
        return empty
    return {
        'nps': m.get('nps'),
        'loes_quote': m.get('loes_quote'),
        'info_quote': m.get('info_quote'),
        'nps_count': m.get('nps_count') or 0,
        'loes_count': m.get('loes_count') or 0,
        'info_count': m.get('info_count') or 0,
    }


def _snapshot_assignment_start_kpis(assignment, project_id):
    if not kpi_logic.kpi_features_enabled():
        return
    snap = _member_kpi_snapshot(project_id, assignment.team_member_id)
    assignment.start_nps_at_assign = snap['nps']
    assignment.start_loesung_quote_at_assign = snap['loes_quote']
    assignment.start_info_quote_at_assign = snap['info_quote']
    assignment.start_nps_count_at_assign = snap['nps_count']
    assignment.start_loesung_count_at_assign = snap['loes_count']
    assignment.start_info_count_at_assign = snap['info_count']


def _snapshot_assignment_end_kpis(assignment):
    if not kpi_logic.kpi_features_enabled():
        return
    tm = assignment.team_member
    if not tm or not tm.team:
        return
    snap = _member_kpi_snapshot(tm.team.project_id, assignment.team_member_id)
    assignment.end_nps = snap['nps']
    assignment.end_loesung_quote = snap['loes_quote']
    assignment.end_info_quote = snap['info_quote']
    assignment.end_nps_count = snap['nps_count']
    assignment.end_loesung_count = snap['loes_count']
    assignment.end_info_count = snap['info_count']


def _team_view_card_settings(project_id):
    row = TeamViewCardSettings.query.get(project_id) if project_id else None
    return kpi_logic.team_view_card_settings_dict(row)


def _member_kpi_daily_series(project_id, member_id, days=90):
    """Daily KPI aggregates for trend charts."""
    if not project_id or not member_id:
        return []
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=max(1, days) - 1)
    filters = [
        KpiSurvey.project_id == project_id,
        KpiSurvey.team_member_id == member_id,
        KpiSurvey.antwort_date >= start,
        KpiSurvey.antwort_date <= today,
    ]
    filters.extend(_kpi_source_filter(project_id, counting_only=True))
    rows = db.session.query(
        KpiSurvey.antwort_date,
        KpiSurvey.info_positive,
        KpiSurvey.loesung_positive,
        KpiSurvey.nps_value,
    ).filter(*filters).order_by(KpiSurvey.antwort_date).all()
    by_day = {}
    for d, info_p, loes_p, nps_v in rows:
        if d is None:
            continue
        b = by_day.setdefault(d, {'info': [], 'loes': [], 'nps': []})
        if info_p is not None:
            b['info'].append(1 if info_p else 0)
        if loes_p is not None:
            b['loes'].append(1 if loes_p else 0)
        if nps_v is not None:
            b['nps'].append(nps_v)
    out = []
    for d in sorted(by_day.keys()):
        b = by_day[d]
        nps_day = kpi_logic.compute_nps(b['nps'])
        out.append({
            'date': d.isoformat(),
            'label': d.strftime('%d.%m.'),
            'info_quote': (round(sum(b['info']) / len(b['info']) * 100, 2) if b['info'] else None),
            'loes_quote': (round(sum(b['loes']) / len(b['loes']) * 100, 2) if b['loes'] else None),
            'nps': nps_day['nps'],
        })
    return out


def _kpi_source_filter(project_id, counting_only=True):
    """Restrict surveys to the project's configured survey types. No config = all.

    counting_only=True  -> only types that feed the KPIs (counts=True).
    counting_only=False -> all relevant types (for the raw-data popup).
    """
    if not project_id:
        return []
    rows = db.session.query(
        ProjectKpiSource.survey_type, ProjectKpiSource.counts
    ).filter_by(project_id=project_id).all()
    if not rows:
        return []
    if counting_only:
        types = [st for st, counts in rows if counts]
        if not types:
            return [false()]  # configured, but nothing counts -> no KPI data
    else:
        types = [st for st, _ in rows]
    return [KpiSurvey.studie.in_(types)]


def _kpi_counting_types(project_id):
    """Set of counting survey types, or None if no source config (everything counts)."""
    if not project_id:
        return None
    rows = db.session.query(
        ProjectKpiSource.survey_type, ProjectKpiSource.counts
    ).filter_by(project_id=project_id).all()
    if not rows:
        return None
    return {st for st, counts in rows if counts}


def _kpi_visibility(project_id):
    """Which KPIs are visible on Coaching VS KPI (default: all)."""
    setting = ProjectKpiSetting.query.get(project_id) if project_id else None
    if setting is None:
        return {
            'info': True, 'loesung': True, 'nps': True,
            'fachkompetenz': True, 'vertrieb': True,
        }
    return {
        'info': setting.show_info,
        'loesung': setting.show_loesung,
        'nps': setting.show_nps,
        'fachkompetenz': setting.show_fachkompetenz,
        'vertrieb': setting.show_vertrieb,
    }


def _prod_base_filters(accessible, sees_all_teams, my_team_ids):
    """Scope filters for ProductivityInterval (mirrors KPI survey scope)."""
    filters = []
    if accessible is not None:
        filters.append(ProductivityInterval.project_id.in_(accessible))
    if not sees_all_teams and my_team_ids:
        filters.append(ProductivityInterval.team_id.in_(my_team_ids))
    return filters


def _prod_dashboard_visibility(project_id):
    row = ProjectProductivitySetting.query.get(project_id) if project_id else None
    return productivity_logic.dashboard_visibility_dict(row)


def _prod_impact_visibility(project_id):
    row = ProjectProductivitySetting.query.get(project_id) if project_id else None
    return productivity_logic.impact_visibility_dict(row)


def _prod_settings(project_id):
    row = ProjectProductivitySetting.query.get(project_id) if project_id else None
    return productivity_logic.settings_dict(row)


def _prod_labels(project_id):
    row = ProjectProductivitySetting.query.get(project_id) if project_id else None
    return productivity_logic.labels_dict(row)


def _kpi_category_labels():
    cats = KpiCategory.query.order_by(KpiCategory.sort_order, KpiCategory.id).all()
    if not cats:
        return {'qualitaet': 'Qualität', 'produktivitaet': 'Produktivität'}
    return {c.key: c.label for c in cats}


def _resolve_dashboard_granularities(
    granularity_arg, period_arg, start_date, end_date, data_dates, url_endpoint,
):
    """Resolve table/chart granularity and optional Tag-limit notice."""
    span = kpi_time.span_days(start_date, end_date, data_dates)
    explicit = granularity_arg if granularity_arg in ('day', 'week', 'month') else None
    if explicit:
        table_granularity = explicit
    else:
        table_granularity = kpi_time.resolve_granularity(
            granularity_arg, period_arg, start_date, end_date, data_dates,
        )

    granularity_notice = None
    toggle_granularity = explicit or table_granularity

    if explicit == 'day' and kpi_time.tag_view_exceeds_limit(start_date, end_date, data_dates):
        suggested = kpi_time.suggested_granularity_for_span(span)
        args = request.args.to_dict()

        continue_args = dict(args)
        continue_args['granularity'] = suggested
        continue_args.pop('granularity_confirm', None)
        continue_args.pop('day_page', None)

        vonbis_args = dict(args)
        vonbis_args['period'] = 'free'
        vonbis_args['granularity'] = 'day'
        vonbis_args.pop('granularity_confirm', None)
        vonbis_args.pop('day_page', None)
        ref_end = end_date or (max(data_dates) if data_dates else None)
        ref_start = start_date or (min(data_dates) if data_dates else None)
        if ref_end:
            vonbis_args['date_to'] = ref_end.isoformat()
            vonbis_args['date_from'] = (
                ref_end - timedelta(days=kpi_time.MAX_TAG_VIEW_DAYS - 1)
            ).isoformat()
        elif ref_start:
            vonbis_args['date_from'] = ref_start.isoformat()
            vonbis_args['date_to'] = (
                ref_start + timedelta(days=kpi_time.MAX_TAG_VIEW_DAYS - 1)
            ).isoformat()

        granularity_notice = {
            'max_days': kpi_time.MAX_TAG_VIEW_DAYS,
            'span_days': span,
            'suggested': suggested,
            'suggested_label': kpi_time.granularity_label(suggested),
            'continue_url': url_for(url_endpoint, **continue_args),
            'vonbis_url': url_for(url_endpoint, **vonbis_args),
        }
        table_granularity = suggested
        toggle_granularity = 'day'

    chart_granularity = kpi_time.chart_granularity_for_span(
        table_granularity, start_date, end_date, data_dates,
    )
    return table_granularity, chart_granularity, toggle_granularity, granularity_notice


def _paginate_daily_table_rows(daily, table_granularity, url_endpoint):
    """Paginate Tagesübersicht by calendar month when span exceeds one month."""
    nav = {'enabled': False}
    if table_granularity != 'day' or not daily:
        return daily, nav
    pages = kpi_time.group_daily_by_month(daily)
    if len(pages) <= 1:
        return daily, nav
    page_count = len(pages)
    page = request.args.get('day_page', type=int) or page_count
    page = max(1, min(page, page_count))
    current = pages[page - 1]
    args = request.args.to_dict()

    def _page_url(p):
        q = dict(args)
        q['day_page'] = p
        return url_for(url_endpoint, **q)

    nav = {
        'enabled': True,
        'page': page,
        'page_count': page_count,
        'month_label': current['label'],
        'prev_url': _page_url(page - 1) if page > 1 else None,
        'next_url': _page_url(page + 1) if page < page_count else None,
    }
    return current['rows'], nav


def _kpi_dashboard_visibility(project_id):
    """Which KPIs appear on /kpis (cards, graph, daily table)."""
    setting = ProjectKpiSetting.query.get(project_id) if project_id else None
    if setting is None:
        return {
            'info': True, 'loesung': True, 'nps': True,
            'fachkompetenz': True, 'vertrieb': True,
        }
    return {
        'info': setting.dashboard_show_info,
        'loesung': setting.dashboard_show_loesung,
        'nps': setting.dashboard_show_nps,
        'fachkompetenz': setting.dashboard_show_fachkompetenz,
        'vertrieb': setting.dashboard_show_vertrieb,
    }


def _active_project_id(mode, sel_project, sel_team, sel_member):
    """Resolve the single project context for the current selection (for KPI config)."""
    if mode == 'project' and sel_project:
        return sel_project
    if mode == 'team' and sel_team:
        team = Team.query.get(sel_team)
        return team.project_id if team else None
    if mode == 'agent' and sel_member:
        member = TeamMember.query.get(sel_member)
        if member and member.team_id:
            team = Team.query.get(member.team_id)
            return team.project_id if team else None
    return None


def _kpi_aggregate(rows):
    """rows: iterable of (info_positive, loesung_positive, nps_value). Returns KPI dict."""
    full = _kpi_aggregate_full(
        (info_positive, loesung_positive, nps_value, None, None)
        for info_positive, loesung_positive, nps_value in rows
    )
    return {k: full[k] for k in (
        'info_quote', 'info_total', 'info_pos',
        'loes_quote', 'loes_total', 'loes_pos',
        'nps', 'nps_total', 'nps_promoters', 'nps_neutrals', 'nps_detractors',
    )}


def _empty_kpi_cum():
    return {
        'info_pos': 0, 'info_total': 0,
        'loes_pos': 0, 'loes_total': 0,
        'nps': [],
        'fach': [],
        'vert_pos': 0, 'vert_total': 0,
    }


def _kpi_cum_add(cum, info_positive, loesung_positive, nps_value, fachkompetenz_stars, vertrieb_positive):
    if info_positive is not None:
        cum['info_total'] += 1
        if info_positive:
            cum['info_pos'] += 1
    if loesung_positive is not None:
        cum['loes_total'] += 1
        if loesung_positive:
            cum['loes_pos'] += 1
    if nps_value is not None:
        cum['nps'].append(nps_value)
    if fachkompetenz_stars is not None:
        cum['fach'].append(fachkompetenz_stars)
    if vertrieb_positive is not None:
        cum['vert_total'] += 1
        if vertrieb_positive:
            cum['vert_pos'] += 1


def _kpi_zero_period_metrics():
    """Chart/table values when a period has no Bewertungen."""
    return {
        'info_quote': 0,
        'loes_quote': 0,
        'nps': 0,
        'fachkompetenz': 0,
        'vertrieb_quote': 0,
    }


def _kpi_cum_metrics(cum):
    nps = kpi_logic.compute_nps(cum['nps'])
    fach_avg = round(sum(cum['fach']) / len(cum['fach']), 2) if cum['fach'] else None
    has_data = any((
        cum['info_total'], cum['loes_total'], nps['total'],
        cum['fach'], cum['vert_total'],
    ))
    if not has_data:
        return {
            'info_quote': None, 'loes_quote': None, 'nps': None,
            'fachkompetenz': None, 'vertrieb_quote': None,
        }
    return {
        'info_quote': kpi_logic.quote_percent(cum['info_pos'], cum['info_total']),
        'loes_quote': kpi_logic.quote_percent(cum['loes_pos'], cum['loes_total']),
        'nps': nps['nps'] if nps['total'] else None,
        'fachkompetenz': fach_avg,
        'vertrieb_quote': kpi_logic.quote_percent(cum['vert_pos'], cum['vert_total']),
    }


def _kpi_day_bucket_metrics(bucket):
    nps_day = kpi_logic.compute_nps(bucket['nps'])
    fach_avg = round(sum(bucket['fach']) / len(bucket['fach']), 2) if bucket['fach'] else None
    return {
        'info_quote': (
            kpi_logic.quote_percent(int(sum(bucket['info'])), len(bucket['info']))
            if bucket['info'] else None
        ),
        'loes_quote': (
            kpi_logic.quote_percent(int(sum(bucket['loes'])), len(bucket['loes']))
            if bucket['loes'] else None
        ),
        'nps': nps_day['nps'] if nps_day['total'] else None,
        'fachkompetenz': fach_avg,
        'vertrieb_quote': (
            kpi_logic.quote_percent(int(sum(bucket['vert'])), len(bucket['vert']))
            if bucket['vert'] else None
        ),
    }


def _kpi_aggregate_full(rows):
    """rows: iterable of (info, loes, nps, fach_stars, vertrieb_positive)."""
    cum = _empty_kpi_cum()
    for info_positive, loesung_positive, nps_value, fachkompetenz_stars, vertrieb_positive in rows:
        _kpi_cum_add(cum, info_positive, loesung_positive, nps_value, fachkompetenz_stars, vertrieb_positive)
    nps = kpi_logic.compute_nps(cum['nps'])
    fach_avg = round(sum(cum['fach']) / len(cum['fach']), 2) if cum['fach'] else None
    return {
        'info_quote': kpi_logic.quote_percent(cum['info_pos'], cum['info_total']),
        'info_total': cum['info_total'],
        'info_pos': cum['info_pos'],
        'loes_quote': kpi_logic.quote_percent(cum['loes_pos'], cum['loes_total']),
        'loes_total': cum['loes_total'],
        'loes_pos': cum['loes_pos'],
        'nps': nps['nps'] if nps['total'] else None,
        'nps_total': nps['total'],
        'nps_promoters': nps['promoters'],
        'nps_neutrals': nps['neutrals'],
        'nps_detractors': nps['detractors'],
        'fachkompetenz': fach_avg,
        'fachkompetenz_total': len(cum['fach']),
        'vertrieb_quote': kpi_logic.quote_percent(cum['vert_pos'], cum['vert_total']),
        'vertrieb_total': cum['vert_total'],
        'vertrieb_pos': cum['vert_pos'],
    }


def _kpi_dashboard_daily_series(
    rows, start_date, end_date, chart_granularity='day', table_granularity=None,
):
    """Cumulative KPI trend for chart + per-period values for the table."""
    table_granularity = table_granularity or chart_granularity
    by_day = {}
    for info_p, loes_p, nps_v, fach_s, vert_p, d in rows:
        if d is None:
            continue
        bucket = by_day.setdefault(d, {
            'count': 0, 'surveys': [], 'info': [], 'loes': [], 'nps': [], 'fach': [], 'vert': [],
        })
        bucket['count'] += 1
        bucket['surveys'].append((info_p, loes_p, nps_v, fach_s, vert_p))
        if info_p is not None:
            bucket['info'].append(1 if info_p else 0)
        if loes_p is not None:
            bucket['loes'].append(1 if loes_p else 0)
        if nps_v is not None:
            bucket['nps'].append(nps_v)
        if fach_s is not None:
            bucket['fach'].append(fach_s)
        if vert_p is not None:
            bucket['vert'].append(1 if vert_p else 0)

    if not by_day:
        return [], []

    chart_periods = kpi_time.bucket_ranges(
        chart_granularity, start_date, end_date, list(by_day.keys()),
    )
    table_periods = (
        chart_periods if table_granularity == chart_granularity
        else kpi_time.bucket_ranges(table_granularity, start_date, end_date, list(by_day.keys()))
    )
    if not chart_periods and not table_periods:
        return [], []

    def _period_rows(period):
        period_bucket = {
            'count': 0, 'surveys': [], 'info': [], 'loes': [], 'nps': [], 'fach': [], 'vert': [],
        }
        d = period['start']
        while d <= period['end']:
            day_b = by_day.get(d)
            if day_b:
                period_bucket['count'] += day_b['count']
                period_bucket['surveys'].extend(day_b['surveys'])
                period_bucket['info'].extend(day_b['info'])
                period_bucket['loes'].extend(day_b['loes'])
                period_bucket['nps'].extend(day_b['nps'])
                period_bucket['fach'].extend(day_b['fach'])
                period_bucket['vert'].extend(day_b['vert'])
            d += timedelta(days=1)
        return period_bucket

    cum = _empty_kpi_cum()
    chart_daily = []
    for period in chart_periods:
        period_bucket = _period_rows(period)
        if period_bucket['surveys']:
            for survey in period_bucket['surveys']:
                _kpi_cum_add(cum, *survey)
            metrics = _kpi_cum_metrics(cum)
        else:
            metrics = _kpi_zero_period_metrics()

        chart_daily.append({
            'date': period['key'],
            'label': period['label'],
            'count': period_bucket['count'],
            **metrics,
        })

    table_daily = []
    for period in table_periods:
        period_bucket = _period_rows(period)
        if period_bucket['count']:
            day_m = _kpi_day_bucket_metrics(period_bucket)
        else:
            day_m = _kpi_zero_period_metrics()
        table_daily.append({
            'date': period['key'],
            'label': period['label'],
            'count': period_bucket['count'],
            **day_m,
        })
    return chart_daily, table_daily


@bp.route('/kpis')
@login_required
@any_permission_required('view_kpi_qualitaet', 'view_kpi_produktivitaet', 'view_kpi_dashboard')
def kpi_dashboard_redirect():
    if not kpi_logic.kpi_features_enabled():
        flash('KPI-Funktionen sind derzeit deaktiviert.', 'info')
        return redirect(url_for('main.index'))
    if can_view_kpi_qualitaet(current_user):
        return redirect(url_for('main.kpi_dashboard_qualitaet', **request.args))
    if can_view_kpi_produktivitaet(current_user):
        return redirect(url_for('main.kpi_dashboard_produktivitaet', **request.args))
    flash('Keine Berechtigung für KPI-Dashboards.', 'danger')
    return redirect(url_for('main.index'))


@bp.route('/kpis/qualitaet')
@login_required
@any_permission_required('view_kpi_qualitaet', 'view_kpi_dashboard')
def kpi_dashboard_qualitaet():
    if not kpi_logic.kpi_features_enabled():
        flash('KPI-Funktionen sind derzeit deaktiviert.', 'info')
        return redirect(url_for('main.index'))
    accessible, sees_all_teams, my_team_ids = _kpi_scope()
    base_filters = _kpi_base_filters(accessible, sees_all_teams, my_team_ids)

    # --- Dropdown option sources (scoped) ---
    # Projects visible to the user that actually have KPI data.
    proj_q = (
        db.session.query(Project.id, Project.name)
        .join(KpiSurvey, KpiSurvey.project_id == Project.id)
        .filter(*base_filters)
        .distinct()
        .order_by(Project.name)
    )
    projects = [{'id': pid, 'name': pname} for pid, pname in proj_q.all()]

    mode = (request.args.get('mode') or 'project').strip()
    if mode not in ('project', 'team', 'agent'):
        mode = 'project'
    sel_project = request.args.get('project_id', type=int)
    sel_team = request.args.get('team_id', type=int)
    sel_member = request.args.get('member_id', type=int)

    # Teams within optional project filter.
    team_filters = list(base_filters)
    if sel_project:
        team_filters.append(KpiSurvey.project_id == sel_project)
    team_q = (
        db.session.query(Team.id, Team.name)
        .join(KpiSurvey, KpiSurvey.team_id == Team.id)
        .filter(*team_filters)
        .distinct()
        .order_by(Team.name)
    )
    teams = [{'id': tid, 'name': tname} for tid, tname in team_q.all()]

    # Agents within optional project/team filter.
    member_filters = list(base_filters)
    if sel_project:
        member_filters.append(KpiSurvey.project_id == sel_project)
    if sel_team:
        member_filters.append(KpiSurvey.team_id == sel_team)
    member_q = (
        db.session.query(TeamMember.id, TeamMember.name)
        .join(KpiSurvey, KpiSurvey.team_member_id == TeamMember.id)
        .filter(*member_filters)
        .distinct()
        .order_by(TeamMember.name)
    )
    members = [{'id': mid, 'name': mname} for mid, mname in member_q.all()]

    # --- Validate selections against the scoped option lists ---
    valid_project_ids = {p['id'] for p in projects}
    valid_team_ids = {t['id'] for t in teams}
    valid_member_ids = {m['id'] for m in members}
    if sel_project and sel_project not in valid_project_ids:
        sel_project = None
    if sel_team and sel_team not in valid_team_ids:
        sel_team = None
    if sel_member and sel_member not in valid_member_ids:
        sel_member = None

    # --- Date range (modal picker; no default until user confirms) ---
    period_ctx = _kpi_dashboard_period_from_request(request)
    period_arg = period_ctx['period']
    date_from_str = period_ctx['date_from_str']
    date_to_str = period_ctx['date_to_str']
    period_confirmed = period_ctx['confirmed']
    start_date = period_ctx['start_date']
    end_date = period_ctx['end_date']
    period_label = period_ctx['label']
    granularity_arg = (request.args.get('granularity') or '').strip()
    chart_granularity = kpi_time.resolve_granularity(
        granularity_arg, period_arg, start_date, end_date, None,
    ) if period_confirmed else 'day'

    # --- Build the active query filters from scope + mode + date ---
    active_project_id = _active_project_id(mode, sel_project, sel_team, sel_member)
    visible = _kpi_dashboard_visibility(active_project_id)
    filters = list(base_filters)
    filters.extend(_kpi_source_filter(active_project_id))
    if mode == 'agent' and sel_member:
        filters.append(KpiSurvey.team_member_id == sel_member)
    elif mode == 'team' and sel_team:
        filters.append(KpiSurvey.team_id == sel_team)
    elif mode == 'project' and sel_project:
        filters.append(KpiSurvey.project_id == sel_project)
    if start_date:
        filters.append(KpiSurvey.antwort_date >= start_date)
    if end_date:
        filters.append(KpiSurvey.antwort_date <= end_date)

    selection_made = (
        (mode == 'project' and sel_project) or
        (mode == 'team' and sel_team) or
        (mode == 'agent' and sel_member)
    )

    period_month_filters = list(base_filters)
    period_month_filters.extend(_kpi_source_filter(active_project_id))
    if mode == 'agent' and sel_member:
        period_month_filters.append(KpiSurvey.team_member_id == sel_member)
    elif mode == 'team' and sel_team:
        period_month_filters.append(KpiSurvey.team_id == sel_team)
    elif mode == 'project' and sel_project:
        period_month_filters.append(KpiSurvey.project_id == sel_project)
    period_data = (
        _kpi_data_period_keys(period_month_filters, KpiSurvey.antwort_date)
        if selection_made else {'months': [], 'weeks': [], 'quarters': [], 'years': []}
    )
    period_data_tick_label = 'Bewertungen vorhanden'

    kpi = None
    chart_daily = []
    daily = []
    table_granularity = chart_granularity
    toggle_granularity = chart_granularity
    granularity_notice = None
    targets = kpi_logic.DEFAULT_TEAM_VIEW_CARD
    scope_label = ''
    has_any_data = bool(projects)
    if selection_made:
        if mode == 'agent' and sel_member:
            scope_label = next((m['name'] for m in members if m['id'] == sel_member), 'Agent')
        elif mode == 'team' and sel_team:
            scope_label = next((t['name'] for t in teams if t['id'] == sel_team), 'Team')
        elif mode == 'project' and sel_project:
            scope_label = next((p['name'] for p in projects if p['id'] == sel_project), 'Projekt')
    if selection_made and period_confirmed:
        rows = (
            db.session.query(
                KpiSurvey.info_positive,
                KpiSurvey.loesung_positive,
                KpiSurvey.nps_value,
                KpiSurvey.fachkompetenz_stars,
                KpiSurvey.vertrieb_positive,
                KpiSurvey.antwort_date,
            ).filter(*filters).all()
        )
        kpi = _kpi_aggregate_full(r[:5] for r in rows)
        data_dates = [r[5] for r in rows if r[5] is not None]
        table_granularity, chart_granularity, toggle_granularity, granularity_notice = (
            _resolve_dashboard_granularities(
                granularity_arg, period_arg, start_date, end_date, data_dates,
                'main.kpi_dashboard_qualitaet',
            )
        )
        chart_daily, daily = _kpi_dashboard_daily_series(
            rows, start_date, end_date, chart_granularity, table_granularity,
        )
        targets = _team_view_card_settings(active_project_id)

    daily, daily_month_nav = _paginate_daily_table_rows(
        daily, table_granularity, 'main.kpi_dashboard_qualitaet',
    )

    return render_template(
        'main/kpi_dashboard.html',
        mode=mode,
        projects=projects,
        teams=teams,
        members=members,
        sel_project=sel_project,
        sel_team=sel_team,
        sel_member=sel_member,
        period=period_arg,
        date_from=date_from_str,
        date_to=date_to_str,
        period_confirmed=period_confirmed,
        period_label=period_label,
        period_year=period_ctx['period_year'],
        period_month=period_ctx['period_month'],
        period_week=period_ctx['period_week'],
        period_quarter=period_ctx['period_quarter'],
        granularity=granularity_arg,
        chart_granularity=chart_granularity,
        table_granularity=table_granularity,
        toggle_granularity=toggle_granularity,
        granularity_notice=granularity_notice,
        kpi=kpi,
        chart_daily=chart_daily,
        daily=daily,
        daily_month_nav=daily_month_nav,
        targets=targets,
        scope_label=scope_label,
        selection_made=bool(selection_made),
        has_any_data=has_any_data,
        visible=visible,
        kpi_category_labels=_kpi_category_labels(),
        active_kpi_nav='qualitaet',
        period_data=period_data,
        period_data_tick_label=period_data_tick_label,
    )


@bp.route('/kpis/produktivitaet')
@login_required
@any_permission_required('view_kpi_produktivitaet', 'view_kpi_dashboard')
def kpi_dashboard_produktivitaet():
    if not kpi_logic.kpi_features_enabled():
        flash('KPI-Funktionen sind derzeit deaktiviert.', 'info')
        return redirect(url_for('main.index'))
    accessible, sees_all_teams, my_team_ids = _kpi_scope()
    base_filters = _prod_base_filters(accessible, sees_all_teams, my_team_ids)

    proj_q = (
        db.session.query(Project.id, Project.name)
        .join(ProductivityInterval, ProductivityInterval.project_id == Project.id)
        .filter(*base_filters)
        .distinct().order_by(Project.name)
    )
    projects = [{'id': pid, 'name': pname} for pid, pname in proj_q.all()]

    mode = (request.args.get('mode') or 'project').strip()
    if mode not in ('project', 'team', 'agent'):
        mode = 'project'
    sel_project = request.args.get('project_id', type=int)
    sel_team = request.args.get('team_id', type=int)
    sel_member = request.args.get('member_id', type=int)

    team_filters = list(base_filters)
    if sel_project:
        team_filters.append(ProductivityInterval.project_id == sel_project)
    team_q = (
        db.session.query(Team.id, Team.name)
        .join(ProductivityInterval, ProductivityInterval.team_id == Team.id)
        .filter(*team_filters).distinct().order_by(Team.name)
    )
    teams = [{'id': tid, 'name': tname} for tid, tname in team_q.all()]

    member_filters = list(base_filters)
    if sel_project:
        member_filters.append(ProductivityInterval.project_id == sel_project)
    if sel_team:
        member_filters.append(ProductivityInterval.team_id == sel_team)
    member_q = (
        db.session.query(TeamMember.id, TeamMember.name)
        .join(ProductivityInterval, ProductivityInterval.team_member_id == TeamMember.id)
        .filter(*member_filters).distinct().order_by(TeamMember.name)
    )
    members = [{'id': mid, 'name': mname} for mid, mname in member_q.all()]

    valid_project_ids = {p['id'] for p in projects}
    valid_team_ids = {t['id'] for t in teams}
    valid_member_ids = {m['id'] for m in members}
    if sel_project and sel_project not in valid_project_ids:
        sel_project = None
    if sel_team and sel_team not in valid_team_ids:
        sel_team = None
    if sel_member and sel_member not in valid_member_ids:
        sel_member = None

    period_ctx = _kpi_dashboard_period_from_request(request)
    period_arg = period_ctx['period']
    date_from_str = period_ctx['date_from_str']
    date_to_str = period_ctx['date_to_str']
    period_confirmed = period_ctx['confirmed']
    start_date = period_ctx['start_date']
    end_date = period_ctx['end_date']
    period_label = period_ctx['label']
    granularity_arg = (request.args.get('granularity') or '').strip()
    chart_granularity = kpi_time.resolve_granularity(
        granularity_arg, period_arg, start_date, end_date, None,
    ) if period_confirmed else 'day'

    active_project_id = _active_project_id(mode, sel_project, sel_team, sel_member)
    visible = _prod_dashboard_visibility(active_project_id)
    targets = _prod_settings(active_project_id)
    prod_labels = _prod_labels(active_project_id)

    filters = list(base_filters)
    if mode == 'agent' and sel_member:
        filters.append(ProductivityInterval.team_member_id == sel_member)
    elif mode == 'team' and sel_team:
        filters.append(ProductivityInterval.team_id == sel_team)
    elif mode == 'project' and sel_project:
        filters.append(ProductivityInterval.project_id == sel_project)
    if start_date:
        filters.append(ProductivityInterval.slot_at >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        filters.append(ProductivityInterval.slot_at <= datetime.combine(end_date, datetime.max.time()))

    selection_made = (
        (mode == 'project' and sel_project) or
        (mode == 'team' and sel_team) or
        (mode == 'agent' and sel_member)
    )

    period_month_filters = list(base_filters)
    if mode == 'agent' and sel_member:
        period_month_filters.append(ProductivityInterval.team_member_id == sel_member)
    elif mode == 'team' and sel_team:
        period_month_filters.append(ProductivityInterval.team_id == sel_team)
    elif mode == 'project' and sel_project:
        period_month_filters.append(ProductivityInterval.project_id == sel_project)
    period_data = (
        _kpi_data_period_keys(period_month_filters, ProductivityInterval.slot_at)
        if selection_made else {'months': [], 'weeks': [], 'quarters': [], 'years': []}
    )
    period_data_tick_label = 'Rohdaten vorhanden'

    summary = None
    chart_daily = []
    daily = []
    table_granularity = chart_granularity
    toggle_granularity = chart_granularity
    granularity_notice = None
    scope_label = ''
    has_any_data = bool(projects)
    if selection_made:
        if mode == 'agent' and sel_member:
            scope_label = next((m['name'] for m in members if m['id'] == sel_member), 'Agent')
        elif mode == 'team' and sel_team:
            scope_label = next((t['name'] for t in teams if t['id'] == sel_team), 'Team')
        elif mode == 'project' and sel_project:
            scope_label = next((p['name'] for p in projects if p['id'] == sel_project), 'Projekt')
    if selection_made and period_confirmed:
        daily_buckets = productivity_logic.query_daily_buckets_sql(filters)
        summary = productivity_logic.query_interval_summary_sql(filters)
        data_dates = [b['day'] for b in daily_buckets]
        table_granularity, chart_granularity, toggle_granularity, granularity_notice = (
            _resolve_dashboard_granularities(
                granularity_arg, period_arg, start_date, end_date, data_dates,
                'main.kpi_dashboard_produktivitaet',
            )
        )
        chart_daily, daily = productivity_logic.build_dashboard_series_from_buckets(
            daily_buckets, start_date, end_date,
            chart_granularity, table_granularity, kpi_time.bucket_ranges,
        )

    prod_formula_hint = productivity_logic.prod_formula_hint(targets)
    daily, daily_month_nav = _paginate_daily_table_rows(
        daily, table_granularity, 'main.kpi_dashboard_produktivitaet',
    )

    return render_template(
        'main/kpi_productivity_dashboard.html',
        mode=mode,
        projects=projects,
        teams=teams,
        members=members,
        sel_project=sel_project,
        sel_team=sel_team,
        sel_member=sel_member,
        period=period_arg,
        date_from=date_from_str,
        date_to=date_to_str,
        period_confirmed=period_confirmed,
        period_label=period_label,
        period_year=period_ctx['period_year'],
        period_month=period_ctx['period_month'],
        period_week=period_ctx['period_week'],
        period_quarter=period_ctx['period_quarter'],
        granularity=granularity_arg,
        chart_granularity=chart_granularity,
        table_granularity=table_granularity,
        toggle_granularity=toggle_granularity,
        granularity_notice=granularity_notice,
        summary=summary,
        chart_daily=chart_daily,
        daily=daily,
        daily_month_nav=daily_month_nav,
        targets=targets,
        prod_labels=prod_labels,
        prod_formula_hint=prod_formula_hint,
        scope_label=scope_label,
        selection_made=bool(selection_made),
        has_any_data=has_any_data,
        visible=visible,
        kpi_category_labels=_kpi_category_labels(),
        active_kpi_nav='produktivitaet',
        period_data=period_data,
        period_data_tick_label=period_data_tick_label,
    )


@bp.route('/kpis/produktivitaet/day')
@login_required
@any_permission_required('view_kpi_produktivitaet', 'view_kpi_dashboard', 'view_coaching_impact')
def productivity_day_detail():
    accessible, sees_all_teams, my_team_ids = _kpi_scope()
    filters = _prod_base_filters(accessible, sees_all_teams, my_team_ids)

    day_str = (request.args.get('date') or '').strip()
    try:
        day = datetime.strptime(day_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Ungültiges Datum.'}), 400

    mode = (request.args.get('mode') or 'project').strip()
    sel_project = request.args.get('project_id', type=int)
    sel_team = request.args.get('team_id', type=int)
    sel_member = request.args.get('member_id', type=int)

    filters.append(ProductivityInterval.slot_at >= datetime.combine(day, datetime.min.time()))
    filters.append(ProductivityInterval.slot_at <= datetime.combine(day, datetime.max.time()))
    if mode == 'agent' and sel_member:
        filters.append(ProductivityInterval.team_member_id == sel_member)
    elif mode == 'team' and sel_team:
        filters.append(ProductivityInterval.team_id == sel_team)
    elif mode == 'project' and sel_project:
        filters.append(ProductivityInterval.project_id == sel_project)

    rows = (
        ProductivityInterval.query.options(
            joinedload(ProductivityInterval.team_member),
            joinedload(ProductivityInterval.team),
        )
        .filter(*filters)
        .order_by(ProductivityInterval.slot_at)
        .limit(500)
        .all()
    )
    out = []
    for r in rows:
        out.append({
            'member_id': r.team_member_id,
            'time': r.slot_at.strftime('%H:%M') if r.slot_at else '-',
            'agent': r.team_member.name if r.team_member else '-',
            'team': r.team.name if r.team else '-',
            'sign_on_pct': r.sign_on_pct,
            'prod_pct': r.prod_pct,
            'nach_per_call': r.nach_per_call,
            'idle_pct': r.idle_pct,
            'calls': r.calls,
            'works_beendet': r.works_beendet,
        })
    return jsonify({'date': day.strftime('%d.%m.%Y'), 'count': len(out), 'intervals': out})


@bp.route('/kpis/day')
@login_required
@any_permission_required('view_kpi_qualitaet', 'view_kpi_dashboard', 'view_coaching_impact')
def kpi_day_detail():
    """Raw question/answer data for a single day within the current scope (modal)."""
    accessible, sees_all_teams, my_team_ids = _kpi_scope()
    filters = _kpi_base_filters(accessible, sees_all_teams, my_team_ids)

    day_str = (request.args.get('date') or '').strip()
    try:
        day = datetime.strptime(day_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Ungültiges Datum.'}), 400

    mode = (request.args.get('mode') or 'project').strip()
    sel_project = request.args.get('project_id', type=int)
    sel_team = request.args.get('team_id', type=int)
    sel_member = request.args.get('member_id', type=int)

    active_pid = _active_project_id(mode, sel_project, sel_team, sel_member)
    counting_types = _kpi_counting_types(active_pid)
    filters.append(KpiSurvey.antwort_date == day)
    # Raw popup shows all relevant types (counting + show-only), not just counting ones.
    filters.extend(_kpi_source_filter(active_pid, counting_only=False))
    if mode == 'agent' and sel_member:
        filters.append(KpiSurvey.team_member_id == sel_member)
    elif mode == 'team' and sel_team:
        filters.append(KpiSurvey.team_id == sel_team)
    elif mode == 'project' and sel_project:
        filters.append(KpiSurvey.project_id == sel_project)

    surveys = (
        KpiSurvey.query.options(
            selectinload(KpiSurvey.answers),
            joinedload(KpiSurvey.team),
            joinedload(KpiSurvey.team_member),
        )
        .filter(*filters)
        .order_by(KpiSurvey.team_member_id, KpiSurvey.datensatz_id)
        .limit(500)
        .all()
    )

    out = []
    for s in surveys:
        counts = True if counting_types is None else (s.studie in counting_types)
        out.append({
            'member_id': s.team_member_id,
            'datensatz_id': s.datensatz_id,
            'agent': s.team_member.name if s.team_member else (
                (s.vorname + ' ' + s.nachname).strip() or s.ma_kenner or '-'
            ),
            'team': s.team.name if s.team else (s.be4 or '-'),
            'studie': s.studie or '-',
            'counts': counts,
            'nps': s.nps_value,
            'loesung_answer': s.loesung_answer,
            'answers': [
                {'frage': a.frage_text or a.frage_code, 'antwort': a.antwort}
                for a in s.answers
            ],
        })
    return jsonify({'date': day.strftime('%d.%m.%Y'), 'count': len(out), 'surveys': out})


# =====================================================================
# Coaching VS KPI: KPI timeline with coaching overlay
# =====================================================================


def _impact_coaching_filters(accessible, sees_all_teams, my_team_ids):
    """Scope filters restricting Coaching to the user's visible projects/teams."""
    filters = []
    if accessible is None:
        pass  # Admin / Betriebsleiter: all projects
    elif not accessible:
        filters.append(Coaching.project_id == -1)
    else:
        filters.append(Coaching.project_id.in_(accessible))
    if not sees_all_teams:
        if my_team_ids:
            filters.append(Coaching.team_id.in_(my_team_ids))
        else:
            filters.append(false())
    return filters


def _impact_avg(values):
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _coaching_performance_mark_valid(perf):
    """True when performance_mark is a usable 0–10 score."""
    if perf is None:
        return False
    try:
        mark = int(perf)
    except (TypeError, ValueError):
        return False
    return 0 <= mark <= 10


def _coaching_performance_pct(perf):
    """Convert performance_mark (0–10) to percentage, or None if invalid."""
    if not _coaching_performance_mark_valid(perf):
        return None
    return int(perf) * 10


def _impact_event_quote(values):
    if not values:
        return None
    return sum(values) / len(values) * 100.0


def _impact_before_after(events, surveys_by_member, window):
    """Compare KPI in [D-window, D-1] vs [D+1, D+window] per coaching event."""
    acc = {
        'info': {'before': [], 'after': [], 'pairs': 0},
        'loes': {'before': [], 'after': [], 'pairs': 0},
        'nps': {'before': [], 'after': [], 'pairs': 0},
        'fach': {'before': [], 'after': [], 'pairs': 0},
        'vert': {'before': [], 'after': [], 'pairs': 0},
    }
    for member_id, d in events:
        rows = surveys_by_member.get(member_id)
        if not rows:
            continue
        before_lo, before_hi = d - timedelta(days=window), d - timedelta(days=1)
        after_lo, after_hi = d + timedelta(days=1), d + timedelta(days=window)
        b_info, a_info = [], []
        b_loes, a_loes = [], []
        b_nps, a_nps = [], []
        b_fach, a_fach = [], []
        b_vert, a_vert = [], []
        for sd, info_p, loes_p, nps_v, fach_s, vert_p in rows:
            if sd is None:
                continue
            if before_lo <= sd <= before_hi:
                if info_p is not None:
                    b_info.append(1 if info_p else 0)
                if loes_p is not None:
                    b_loes.append(1 if loes_p else 0)
                if nps_v is not None:
                    b_nps.append(nps_v)
                if fach_s is not None:
                    b_fach.append(fach_s)
                if vert_p is not None:
                    b_vert.append(1 if vert_p else 0)
            elif after_lo <= sd <= after_hi:
                if info_p is not None:
                    a_info.append(1 if info_p else 0)
                if loes_p is not None:
                    a_loes.append(1 if loes_p else 0)
                if nps_v is not None:
                    a_nps.append(nps_v)
                if fach_s is not None:
                    a_fach.append(fach_s)
                if vert_p is not None:
                    a_vert.append(1 if vert_p else 0)
        if b_info and a_info:
            acc['info']['before'].append(_impact_event_quote(b_info))
            acc['info']['after'].append(_impact_event_quote(a_info))
            acc['info']['pairs'] += 1
        if b_loes and a_loes:
            acc['loes']['before'].append(_impact_event_quote(b_loes))
            acc['loes']['after'].append(_impact_event_quote(a_loes))
            acc['loes']['pairs'] += 1
        if b_nps and a_nps:
            acc['nps']['before'].append(kpi_logic.compute_nps(b_nps)['nps'])
            acc['nps']['after'].append(kpi_logic.compute_nps(a_nps)['nps'])
            acc['nps']['pairs'] += 1
        if b_fach and a_fach:
            acc['fach']['before'].append(_impact_avg(b_fach))
            acc['fach']['after'].append(_impact_avg(a_fach))
            acc['fach']['pairs'] += 1
        if b_vert and a_vert:
            acc['vert']['before'].append(_impact_event_quote(b_vert))
            acc['vert']['after'].append(_impact_event_quote(a_vert))
            acc['vert']['pairs'] += 1

    out = {}
    for key, bucket in acc.items():
        n = bucket['pairs']
        if n:
            before = round(sum(bucket['before']) / n, 2)
            after = round(sum(bucket['after']) / n, 2)
            out[key] = {'before': before, 'after': after, 'delta': round(after - before, 2), 'pairs': n}
        else:
            out[key] = {'before': None, 'after': None, 'delta': None, 'pairs': 0}
    return out


def _impact_before_after_prod(events, intervals_by_member, window):
    """Before/after productivity metrics around coaching events."""
    acc = {
        'sign_on': {'before': [], 'after': [], 'pairs': 0},
        'prod': {'before': [], 'after': [], 'pairs': 0},
        'nach': {'before': [], 'after': [], 'pairs': 0},
        'idle': {'before': [], 'after': [], 'pairs': 0},
    }
    for member_id, d in events:
        rows = intervals_by_member.get(member_id)
        if not rows:
            continue
        before_lo, before_hi = d - timedelta(days=window), d - timedelta(days=1)
        after_lo, after_hi = d + timedelta(days=1), d + timedelta(days=window)
        b_sign, a_sign = [], []
        b_prod, a_prod = [], []
        b_nach, a_nach = [], []
        b_idle, a_idle = [], []
        for sd, sign_p, prod_p, nach_p, idle_p in rows:
            if sd is None:
                continue
            day = sd.date() if isinstance(sd, datetime) else sd
            if before_lo <= day <= before_hi:
                if sign_p is not None:
                    b_sign.append(sign_p)
                if prod_p is not None:
                    b_prod.append(prod_p)
                if nach_p is not None:
                    b_nach.append(nach_p)
                if idle_p is not None:
                    b_idle.append(idle_p)
            elif after_lo <= day <= after_hi:
                if sign_p is not None:
                    a_sign.append(sign_p)
                if prod_p is not None:
                    a_prod.append(prod_p)
                if nach_p is not None:
                    a_nach.append(nach_p)
                if idle_p is not None:
                    a_idle.append(idle_p)
        if b_sign and a_sign:
            acc['sign_on']['before'].append(_impact_avg(b_sign))
            acc['sign_on']['after'].append(_impact_avg(a_sign))
            acc['sign_on']['pairs'] += 1
        if b_prod and a_prod:
            acc['prod']['before'].append(_impact_avg(b_prod))
            acc['prod']['after'].append(_impact_avg(a_prod))
            acc['prod']['pairs'] += 1
        if b_nach and a_nach:
            acc['nach']['before'].append(_impact_avg(b_nach))
            acc['nach']['after'].append(_impact_avg(a_nach))
            acc['nach']['pairs'] += 1
        if b_idle and a_idle:
            acc['idle']['before'].append(_impact_avg(b_idle))
            acc['idle']['after'].append(_impact_avg(a_idle))
            acc['idle']['pairs'] += 1

    lower_is_better = {'nach', 'idle'}
    out = {}
    for key, bucket in acc.items():
        n = bucket['pairs']
        if n:
            before = round(sum(bucket['before']) / n, 2)
            after = round(sum(bucket['after']) / n, 2)
            out[key] = {
                'before': before,
                'after': after,
                'delta': round(after - before, 2),
                'pairs': n,
                'lower_is_better': key in lower_is_better,
            }
        else:
            out[key] = {
                'before': None,
                'after': None,
                'delta': None,
                'pairs': 0,
                'lower_is_better': key in lower_is_better,
            }
    return out


def _coaching_impact_overlay(kpi_by_day, coaching_by_day, prod_by_day, start_date, end_date, chart_granularity):
    data_dates = sorted(set(kpi_by_day.keys()) | set(coaching_by_day.keys()) | set(prod_by_day.keys()))
    periods = kpi_time.bucket_ranges(chart_granularity, start_date, end_date, data_dates)
    overlay = []
    for period in periods:
        kb = {'info': [], 'loes': [], 'nps': [], 'fach': [], 'vert': []}
        cb = {'count': 0, 'perf': [], 'time': 0}
        period_prod_buckets = []
        d = period['start']
        while d <= period['end']:
            day_kb = kpi_by_day.get(d)
            if day_kb:
                kb['info'].extend(day_kb['info'])
                kb['loes'].extend(day_kb['loes'])
                kb['nps'].extend(day_kb['nps'])
                kb['fach'].extend(day_kb['fach'])
                kb['vert'].extend(day_kb['vert'])
            day_cb = coaching_by_day.get(d)
            if day_cb:
                cb['count'] += day_cb['count']
                cb['perf'].extend(day_cb['perf'])
                cb['time'] += day_cb['time']
            day_prod = prod_by_day.get(d)
            if day_prod:
                period_prod_buckets.append(day_prod)
            d += timedelta(days=1)

        nps_day = kpi_logic.compute_nps(kb['nps'])
        prod_totals = productivity_logic._sum_buckets(period_prod_buckets)
        prod_sm = (
            {'intervals': prod_totals['count'], **productivity_logic._row_metrics_from_totals(prod_totals)}
            if prod_totals['count'] else None
        )
        overlay.append({
            'date': period['key'],
            'label': period['label'],
            'info_quote': (round(sum(kb['info']) / len(kb['info']) * 100, 2) if kb['info'] else None),
            'loes_quote': (round(sum(kb['loes']) / len(kb['loes']) * 100, 2) if kb['loes'] else None),
            'nps': nps_day['nps'] if nps_day['total'] else None,
            'fachkompetenz': (_impact_avg(kb['fach']) if kb['fach'] else None),
            'vertrieb_quote': (round(sum(kb['vert']) / len(kb['vert']) * 100, 2) if kb['vert'] else None),
            'sign_on_pct': prod_sm['sign_on_pct'] if prod_sm else None,
            'prod_pct': prod_sm['prod_pct'] if prod_sm else None,
            'nach_per_call': prod_sm['nach_per_call'] if prod_sm else None,
            'idle_pct': prod_sm['idle_pct'] if prod_sm else None,
            'works': prod_sm['works'] if prod_sm else None,
            'coachings': cb['count'],
            'avg_perf': (round(sum(cb['perf']) / len(cb['perf']), 1) if cb['perf'] else None),
            'coaching_time': cb['time'] or 0,
        })
    return overlay


def _coaching_impact_coaches_in_scope(coaching_filters):
    """Distinct coaches who performed coachings matching scope filters (excludes coach filter)."""
    coach_ids_q = (
        db.session.query(Coaching.coach_id)
        .filter(*coaching_filters)
        .filter(Coaching.coach_id.isnot(None))
        .distinct()
    )
    users = (
        User.query.options(selectinload(User.team_members))
        .filter(User.id.in_(coach_ids_q))
        .all()
    )
    coaches = [{'id': u.id, 'name': u.coach_display_name or u.username or '—'} for u in users]
    coaches.sort(key=lambda c: (c['name'] or '').lower())
    return coaches


def _coaching_impact_scope_filters(mode, sel_project, sel_team, sel_member, kpi_base, coaching_base,
                                   prod_base, projects, teams, members, sel_coach=None):
    """Build scoped KPI/coaching/productivity filters; return None if selection incomplete."""
    selection_made = (
        (mode == 'project' and sel_project) or
        (mode == 'team' and sel_team) or
        (mode == 'agent' and sel_member)
    )
    if not selection_made:
        return None
    active_project_id = _active_project_id(mode, sel_project, sel_team, sel_member)
    kpi_filters = list(kpi_base)
    kpi_filters.extend(_kpi_source_filter(active_project_id))
    coaching_filters = list(coaching_base)
    prod_filters = list(prod_base)
    scope_label = ''
    if mode == 'agent' and sel_member:
        kpi_filters.append(KpiSurvey.team_member_id == sel_member)
        coaching_filters.append(Coaching.team_member_id == sel_member)
        prod_filters.append(ProductivityInterval.team_member_id == sel_member)
        scope_label = next((m['name'] for m in members if m['id'] == sel_member), 'Agent')
    elif mode == 'team' and sel_team:
        kpi_filters.append(KpiSurvey.team_id == sel_team)
        coaching_filters.append(Coaching.team_id == sel_team)
        prod_filters.append(ProductivityInterval.team_id == sel_team)
        scope_label = next((t['name'] for t in teams if t['id'] == sel_team), 'Team')
    elif mode == 'project' and sel_project:
        kpi_filters.append(KpiSurvey.project_id == sel_project)
        coaching_filters.append(Coaching.project_id == sel_project)
        prod_filters.append(ProductivityInterval.project_id == sel_project)
        scope_label = next((p['name'] for p in projects if p['id'] == sel_project), 'Projekt')
    if sel_coach:
        coaching_filters.append(Coaching.coach_id == sel_coach)
    return {
        'kpi_filters': kpi_filters,
        'coaching_filters': coaching_filters,
        'prod_filters': prod_filters,
        'scope_label': scope_label,
        'active_project_id': active_project_id,
    }


def _coaching_impact_range_confirmed(period_arg, date_from_str, date_to_str):
    """True only after the user applied Zeitraum via the wizard (vonbis + valid dates)."""
    if (period_arg or '').strip() != 'vonbis':
        return False, None, None
    start = _parse_kpi_date(date_from_str)
    end = _parse_kpi_date(date_to_str)
    if start and end and start <= end:
        return True, start, end
    return False, None, None


def _parse_coaching_impact_coach_arg(raw):
    """Return (coach_chosen, sel_coach_id or None). 'all' = all coaches, no filter."""
    raw = (raw or '').strip()
    if not raw:
        return False, None
    if raw.lower() == 'all':
        return True, None
    try:
        return True, int(raw)
    except (TypeError, ValueError):
        return False, None


@bp.route('/coaching-impact/activity-map')
@login_required
@permission_required('view_coaching_impact')
def coaching_impact_activity_map():
    """Daily coaching + survey counts for the range wizard timeline."""
    if not kpi_logic.kpi_features_enabled():
        return jsonify({'error': 'KPI deaktiviert.'}), 403
    accessible, sees_all_teams, my_team_ids = _kpi_scope()
    kpi_base = _kpi_base_filters(accessible, sees_all_teams, my_team_ids)
    coaching_base = _impact_coaching_filters(accessible, sees_all_teams, my_team_ids)
    prod_base = _prod_base_filters(accessible, sees_all_teams, my_team_ids)

    mode = (request.args.get('mode') or 'project').strip()
    sel_project = request.args.get('project_id', type=int)
    sel_team = request.args.get('team_id', type=int)
    sel_member = request.args.get('member_id', type=int)
    coach_chosen, sel_coach = _parse_coaching_impact_coach_arg(request.args.get('coach_id'))

    scope_base = _coaching_impact_scope_filters(
        mode, sel_project, sel_team, sel_member, kpi_base, coaching_base, prod_base,
        [], [], [], sel_coach=None,
    )
    if scope_base is None:
        return jsonify({'error': 'Bitte zuerst Projekt, Team oder Agent wählen.'}), 400
    if not coach_chosen:
        return jsonify({'error': 'Bitte zuerst Coach wählen (Alle oder einzeln).'}), 400
    valid_coach_ids = {c['id'] for c in _coaching_impact_coaches_in_scope(scope_base['coaching_filters'])}
    if sel_coach and sel_coach not in valid_coach_ids:
        sel_coach = None
    scope = _coaching_impact_scope_filters(
        mode, sel_project, sel_team, sel_member, kpi_base, coaching_base, prod_base,
        [], [], [], sel_coach=sel_coach,
    )

    show_surveys = can_view_kpi_qualitaet(current_user)
    show_prod_perm = can_view_kpi_produktivitaet(current_user)

    window_q = request.args.get('window', type=int)
    default_window = window_q if window_q and 1 <= window_q <= 90 else kpi_logic.coaching_impact_window_days()

    payload = ci_wizard.build_activity_map_payload(
        scope['coaching_filters'],
        scope['kpi_filters'],
        scope['prod_filters'],
        default_window,
        show_surveys,
        show_prod_perm,
    )
    show_productivity = show_prod_perm and any(
        d.get('productivity', 0) > 0 for d in payload.get('days', [])
    )
    payload['show_productivity'] = show_productivity
    if not payload['days'] or payload.get('actionable_coaching_count', 0) == 0:
        msg = (
            'Keine Coachings mit auswertbaren Vorher/Nachher-Daten '
            '(Bewertungen oder Prod.-Rohdaten vor und nach dem Coaching).'
        )
        if payload.get('coaching_events'):
            msg = (
                'Coachings vorhanden, aber keines mit Bewertungen oder Prod.-Rohdaten '
                'sowohl vor als auch nach dem Termin im Wirkungsfenster.'
            )
        return jsonify({
            **payload,
            'error': None,
            'empty_message': msg,
        })
    return jsonify(payload)


@bp.route('/coaching-impact')
@login_required
@permission_required('view_coaching_impact')
def coaching_impact():
    if not kpi_logic.kpi_features_enabled():
        flash('KPI-Funktionen sind derzeit deaktiviert.', 'info')
        return redirect(url_for('main.index'))
    accessible, sees_all_teams, my_team_ids = _kpi_scope()
    kpi_base = _kpi_base_filters(accessible, sees_all_teams, my_team_ids)
    prod_base = _prod_base_filters(accessible, sees_all_teams, my_team_ids)
    coaching_base = _impact_coaching_filters(accessible, sees_all_teams, my_team_ids)

    qual_pids = {
        r[0] for r in db.session.query(KpiSurvey.project_id).filter(
            KpiSurvey.project_id.isnot(None), *kpi_base,
        ).distinct().all()
    }
    prod_pids = {
        r[0] for r in db.session.query(ProductivityInterval.project_id).filter(
            ProductivityInterval.project_id.isnot(None), *prod_base,
        ).distinct().all()
    }
    all_pids = qual_pids | prod_pids
    if all_pids:
        proj_rows = (
            db.session.query(Project.id, Project.name)
            .filter(Project.id.in_(all_pids)).order_by(Project.name).all()
        )
        projects = [{'id': pid, 'name': pname} for pid, pname in proj_rows]
    else:
        projects = []

    mode = (request.args.get('mode') or 'project').strip()
    if mode not in ('project', 'team', 'agent'):
        mode = 'project'
    sel_project = request.args.get('project_id', type=int)
    sel_team = request.args.get('team_id', type=int)
    sel_member = request.args.get('member_id', type=int)
    coach_chosen, sel_coach = _parse_coaching_impact_coach_arg(request.args.get('coach_id'))

    team_filters = list(kpi_base)
    if sel_project:
        team_filters.append(KpiSurvey.project_id == sel_project)
    team_q = (
        db.session.query(Team.id, Team.name)
        .join(KpiSurvey, KpiSurvey.team_id == Team.id)
        .filter(*team_filters).distinct().order_by(Team.name)
    )
    prod_team_filters = list(prod_base)
    if sel_project:
        prod_team_filters.append(ProductivityInterval.project_id == sel_project)
    prod_team_q = (
        db.session.query(Team.id, Team.name)
        .join(ProductivityInterval, ProductivityInterval.team_id == Team.id)
        .filter(*prod_team_filters).distinct().order_by(Team.name)
    )
    team_map = {tid: tname for tid, tname in team_q.all()}
    for tid, tname in prod_team_q.all():
        team_map.setdefault(tid, tname)
    teams = [{'id': tid, 'name': tname} for tid, tname in sorted(team_map.items(), key=lambda x: x[1])]

    member_filters = list(kpi_base)
    if sel_project:
        member_filters.append(KpiSurvey.project_id == sel_project)
    if sel_team:
        member_filters.append(KpiSurvey.team_id == sel_team)
    member_q = (
        db.session.query(TeamMember.id, TeamMember.name)
        .join(KpiSurvey, KpiSurvey.team_member_id == TeamMember.id)
        .filter(*member_filters).distinct().order_by(TeamMember.name)
    )
    prod_member_filters = list(prod_base)
    if sel_project:
        prod_member_filters.append(ProductivityInterval.project_id == sel_project)
    if sel_team:
        prod_member_filters.append(ProductivityInterval.team_id == sel_team)
    prod_member_q = (
        db.session.query(TeamMember.id, TeamMember.name)
        .join(ProductivityInterval, ProductivityInterval.team_member_id == TeamMember.id)
        .filter(*prod_member_filters).distinct().order_by(TeamMember.name)
    )
    member_map = {mid: mname for mid, mname in member_q.all()}
    for mid, mname in prod_member_q.all():
        member_map.setdefault(mid, mname)
    members = [{'id': mid, 'name': mname} for mid, mname in sorted(member_map.items(), key=lambda x: x[1])]

    valid_project_ids = {p['id'] for p in projects}
    valid_team_ids = {t['id'] for t in teams}
    valid_member_ids = {m['id'] for m in members}
    if sel_project and sel_project not in valid_project_ids:
        sel_project = None
    if sel_team and sel_team not in valid_team_ids:
        sel_team = None
    if sel_member and sel_member not in valid_member_ids:
        sel_member = None

    # --- Date range + impact window (wizard required; no preset period) ---
    period_arg = (request.args.get('period') or '').strip()
    date_from_str = (request.args.get('date_from') or '').strip()
    date_to_str = (request.args.get('date_to') or '').strip()
    granularity_arg = (request.args.get('granularity') or '').strip()
    range_confirmed, start_date, end_date = _coaching_impact_range_confirmed(
        period_arg, date_from_str, date_to_str,
    )
    window_q = request.args.get('window', type=int)
    admin_window = kpi_logic.coaching_impact_window_days()
    if range_confirmed:
        if window_q is not None and 1 <= window_q <= 90:
            impact_window_days = window_q
        else:
            impact_window_days = admin_window
        chart_granularity = kpi_time.resolve_granularity(
            granularity_arg, period_arg, start_date, end_date, None,
        )
    else:
        impact_window_days = admin_window
        chart_granularity = 'day'
    table_granularity = chart_granularity
    toggle_granularity = chart_granularity
    granularity_notice = None

    selection_made = (
        (mode == 'project' and sel_project) or
        (mode == 'team' and sel_team) or
        (mode == 'agent' and sel_member)
    )

    overlay = []
    before_after = None
    before_after_prod = None
    summary = None
    kpi = None
    scope_label = ''
    has_any_data = bool(projects)
    visible = {
        'info': True, 'loesung': True, 'nps': True,
        'fachkompetenz': True, 'vertrieb': True,
    }
    visible_prod = productivity_logic.impact_visibility_dict(None)
    prod_labels = productivity_logic.labels_dict(None)
    coaches = []

    if selection_made:
        # KPI active filters (scope + mode + date)
        active_project_id = _active_project_id(mode, sel_project, sel_team, sel_member)
        visible = _kpi_visibility(active_project_id)
        visible_prod = _prod_impact_visibility(active_project_id)
        prod_labels = _prod_labels(active_project_id)
        kpi_filters = list(kpi_base)
        kpi_filters.extend(_kpi_source_filter(active_project_id))
        coaching_filters = list(coaching_base)
        if mode == 'agent' and sel_member:
            kpi_filters.append(KpiSurvey.team_member_id == sel_member)
            coaching_filters.append(Coaching.team_member_id == sel_member)
            scope_label = next((m['name'] for m in members if m['id'] == sel_member), 'Agent')
        elif mode == 'team' and sel_team:
            kpi_filters.append(KpiSurvey.team_id == sel_team)
            coaching_filters.append(Coaching.team_id == sel_team)
            scope_label = next((t['name'] for t in teams if t['id'] == sel_team), 'Team')
        elif mode == 'project' and sel_project:
            kpi_filters.append(KpiSurvey.project_id == sel_project)
            coaching_filters.append(Coaching.project_id == sel_project)
            scope_label = next((p['name'] for p in projects if p['id'] == sel_project), 'Projekt')

        coaches = _coaching_impact_coaches_in_scope(coaching_filters)
        valid_coach_ids = {c['id'] for c in coaches}
        if sel_coach and sel_coach not in valid_coach_ids:
            sel_coach = None
        if sel_coach:
            coaching_filters.append(Coaching.coach_id == sel_coach)
            coach_name = next((c['name'] for c in coaches if c['id'] == sel_coach), None)
            if coach_name:
                scope_label = scope_label + ' · ' + coach_name

    if selection_made and coach_chosen and range_confirmed:
        kpi_range_filters = list(kpi_filters)
        if start_date:
            kpi_range_filters.append(KpiSurvey.antwort_date >= start_date)
        if end_date:
            kpi_range_filters.append(KpiSurvey.antwort_date <= end_date)

        coaching_range_filters = list(coaching_filters)
        if start_date:
            coaching_range_filters.append(cast(Coaching.coaching_date, Date) >= start_date)
        if end_date:
            coaching_range_filters.append(cast(Coaching.coaching_date, Date) <= end_date)

        # KPI rows in range -> daily series + overall cards
        kpi_rows = (
            db.session.query(
                KpiSurvey.info_positive,
                KpiSurvey.loesung_positive,
                KpiSurvey.nps_value,
                KpiSurvey.fachkompetenz_stars,
                KpiSurvey.vertrieb_positive,
                KpiSurvey.antwort_date,
            ).filter(*kpi_range_filters).all()
        )
        kpi = _kpi_aggregate_full(r[:5] for r in kpi_rows)

        kpi_by_day = {}
        for info_p, loes_p, nps_v, fach_s, vert_p, d in kpi_rows:
            if d is None:
                continue
            bucket = kpi_by_day.setdefault(d, {'info': [], 'loes': [], 'nps': [], 'fach': [], 'vert': []})
            if info_p is not None:
                bucket['info'].append(1 if info_p else 0)
            if loes_p is not None:
                bucket['loes'].append(1 if loes_p else 0)
            if nps_v is not None:
                bucket['nps'].append(nps_v)
            if fach_s is not None:
                bucket['fach'].append(fach_s)
            if vert_p is not None:
                bucket['vert'].append(1 if vert_p else 0)

        # Coaching rows in range -> events + daily coaching activity
        coaching_rows = (
            db.session.query(
                Coaching.team_member_id,
                cast(Coaching.coaching_date, Date),
                Coaching.performance_mark,
                Coaching.time_spent,
            ).filter(*coaching_range_filters).all()
        )
        events = [(r[0], r[1]) for r in coaching_rows if r[1] is not None]
        show_surveys_ci = can_view_kpi_qualitaet(current_user)
        show_prod_ci = can_view_kpi_produktivitaet(current_user)
        prod_scope_filters = list(prod_base)
        if mode == 'agent' and sel_member:
            prod_scope_filters.append(ProductivityInterval.team_member_id == sel_member)
        elif mode == 'team' and sel_team:
            prod_scope_filters.append(ProductivityInterval.team_id == sel_team)
        elif mode == 'project' and sel_project:
            prod_scope_filters.append(ProductivityInterval.project_id == sel_project)
        survey_dates_ci = (
            ci_wizard.load_survey_dates_by_member(kpi_filters) if show_surveys_ci else {}
        )
        prod_dates_ci = (
            ci_wizard.load_prod_dates_by_member(prod_scope_filters) if show_prod_ci else {}
        )
        events = ci_wizard.filter_actionable_events(
            events,
            impact_window_days,
            survey_dates_ci,
            prod_dates_ci,
            show_surveys_ci,
            show_prod_ci,
        )
        actionable_set = set(events)
        coaching_by_day = {}
        total_time = 0
        perf_values = []
        for member_id, d, perf, time_spent in coaching_rows:
            if d is None or (member_id, d) not in actionable_set:
                continue
            if time_spent:
                total_time += time_spent
            perf_pct = _coaching_performance_pct(perf)
            if perf_pct is not None:
                perf_values.append(perf_pct)
            cb = coaching_by_day.setdefault(d, {'count': 0, 'perf': [], 'time': 0})
            cb['count'] += 1
            if time_spent:
                cb['time'] += time_spent
            if perf_pct is not None:
                cb['perf'].append(perf_pct)

        prod_filters = list(prod_scope_filters)
        if start_date:
            prod_filters.append(ProductivityInterval.slot_at >= datetime.combine(start_date, datetime.min.time()))
        if end_date:
            prod_filters.append(ProductivityInterval.slot_at <= datetime.combine(end_date, datetime.max.time()))
        prod_by_day = {
            b['day']: b for b in productivity_logic.query_daily_buckets_sql(prod_filters)
        }

        data_dates = sorted(set(kpi_by_day.keys()) | set(coaching_by_day.keys()) | set(prod_by_day.keys()))
        table_granularity, chart_granularity, toggle_granularity, granularity_notice = (
            _resolve_dashboard_granularities(
                granularity_arg, period_arg, start_date, end_date, data_dates,
                'main.coaching_impact',
            )
        )
        overlay = _coaching_impact_overlay(
            kpi_by_day, coaching_by_day, prod_by_day, start_date, end_date, chart_granularity,
        )

        window = impact_window_days
        surveys_by_member = {}
        if events:
            ev_dates = [d for _, d in events]
            ext_start = min(ev_dates) - timedelta(days=window)
            ext_end = max(ev_dates) + timedelta(days=window)
            member_ids = {m for m, _ in events}
            surv_filters = list(kpi_filters)
            surv_filters.append(KpiSurvey.team_member_id.in_(member_ids))
            surv_filters.append(KpiSurvey.antwort_date >= ext_start)
            surv_filters.append(KpiSurvey.antwort_date <= ext_end)
            surv_rows = (
                db.session.query(
                    KpiSurvey.team_member_id,
                    KpiSurvey.antwort_date,
                    KpiSurvey.info_positive,
                    KpiSurvey.loesung_positive,
                    KpiSurvey.nps_value,
                    KpiSurvey.fachkompetenz_stars,
                    KpiSurvey.vertrieb_positive,
                ).filter(*surv_filters).all()
            )
            for member_id, sd, info_p, loes_p, nps_v, fach_s, vert_p in surv_rows:
                surveys_by_member.setdefault(member_id, []).append(
                    (sd, info_p, loes_p, nps_v, fach_s, vert_p)
                )
        before_after = _impact_before_after(events, surveys_by_member, window)

        intervals_by_member = {}
        if events:
            ev_dates = [d for _, d in events]
            ext_start = min(ev_dates) - timedelta(days=window)
            ext_end = max(ev_dates) + timedelta(days=window)
            member_ids = {m for m, _ in events}
            iv_filters = list(prod_base)
            iv_filters.append(ProductivityInterval.team_member_id.in_(member_ids))
            iv_filters.append(ProductivityInterval.slot_at >= datetime.combine(ext_start, datetime.min.time()))
            iv_filters.append(ProductivityInterval.slot_at <= datetime.combine(ext_end, datetime.max.time()))
            if active_project_id:
                iv_filters.append(ProductivityInterval.project_id == active_project_id)
            iv_rows = (
                db.session.query(
                    ProductivityInterval.team_member_id,
                    ProductivityInterval.slot_at,
                    ProductivityInterval.sign_on_pct,
                    ProductivityInterval.prod_pct,
                    ProductivityInterval.nach_per_call,
                    ProductivityInterval.idle_pct,
                ).filter(*iv_filters).all()
            )
            for member_id, slot_at, sign_p, prod_p, nach_p, idle_p in iv_rows:
                intervals_by_member.setdefault(member_id, []).append(
                    (slot_at, sign_p, prod_p, nach_p, idle_p)
                )
        before_after_prod = _impact_before_after_prod(events, intervals_by_member, window)

        summary = {
            'coachings': len(events),
            'total_time': total_time,
            'total_time_h': total_time // 60,
            'total_time_m': total_time % 60,
            'avg_perf': (round(sum(perf_values) / len(perf_values), 1) if perf_values else None),
        }

    range_summary = None
    if range_confirmed and start_date and end_date:
        range_summary = (
            f'{start_date.strftime("%d.%m.%Y")} – {end_date.strftime("%d.%m.%Y")}'
            f' · Wirkungsfenster {impact_window_days} Tage'
        )

    return render_template(
        'main/coaching_impact.html',
        visible=visible,
        visible_prod=visible_prod,
        prod_labels=prod_labels,
        mode=mode,
        projects=projects,
        teams=teams,
        members=members,
        sel_project=sel_project,
        sel_team=sel_team,
        sel_member=sel_member,
        sel_coach=sel_coach,
        coach_chosen=coach_chosen,
        coaches=coaches,
        period=period_arg,
        date_from=date_from_str if range_confirmed else '',
        date_to=date_to_str if range_confirmed else '',
        granularity=granularity_arg,
        chart_granularity=chart_granularity,
        table_granularity=table_granularity,
        toggle_granularity=toggle_granularity,
        granularity_notice=granularity_notice,
        overlay=overlay,
        before_after=before_after,
        before_after_prod=before_after_prod,
        impact_window_days=impact_window_days,
        range_summary=range_summary,
        range_confirmed=range_confirmed,
        summary=summary,
        kpi=kpi,
        scope_label=scope_label,
        selection_made=bool(selection_made),
        has_any_data=has_any_data,
        has_productivity_data=bool(prod_pids),
        kpi_category_labels=_kpi_category_labels(),
        active_kpi_nav='coaching_impact',
    )


def _serialize_coaching_impact_day_item(c):
    """JSON payload for one coaching in the Coaching VS KPI day modal."""
    lf = c.leitfaden_erfuellung_stats
    return {
        'id': c.id,
        'agent': c.team_member.name if c.team_member else '-',
        'coach': c.coach.coach_display_name if c.coach else '-',
        'subject': c.coaching_subject or '-',
        'style': c.coaching_style or '-',
        'tcap_id': c.tcap_id or '',
        'performance': _coaching_performance_pct(c.performance_mark),
        'performance_mark': c.performance_mark,
        'time_spent': c.time_spent or 0,
        'coach_notes': (c.coach_notes or '').strip(),
        'leitfaden_stats': (
            {'percent': lf[0], 'positive': lf[1], 'total': lf[2]} if lf else None
        ),
        'leitfaden_fields': [
            {'name': name, 'value': value} for name, value in c.leitfaden_fields_list
        ],
    }


@bp.route('/coaching-impact/day')
@login_required
@permission_required('view_coaching_impact')
def coaching_impact_day():
    """Coachings on a single day within the current scope (modal for the coaching badge)."""
    if not kpi_logic.kpi_features_enabled():
        return jsonify({'coachings': []})
    accessible, sees_all_teams, my_team_ids = _kpi_scope()
    filters = _impact_coaching_filters(accessible, sees_all_teams, my_team_ids)

    day_str = (request.args.get('date') or '').strip()
    try:
        day = datetime.strptime(day_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Ungültiges Datum.'}), 400

    mode = (request.args.get('mode') or 'project').strip()
    sel_project = request.args.get('project_id', type=int)
    sel_team = request.args.get('team_id', type=int)
    sel_member = request.args.get('member_id', type=int)
    coach_chosen, sel_coach = _parse_coaching_impact_coach_arg(request.args.get('coach_id'))

    filters.append(cast(Coaching.coaching_date, Date) == day)
    if mode == 'agent' and sel_member:
        filters.append(Coaching.team_member_id == sel_member)
    elif mode == 'team' and sel_team:
        filters.append(Coaching.team_id == sel_team)
    elif mode == 'project' and sel_project:
        filters.append(Coaching.project_id == sel_project)
    if sel_coach:
        filters.append(Coaching.coach_id == sel_coach)

    coachings = (
        Coaching.query.options(
            joinedload(Coaching.team_member),
            selectinload(Coaching.coach).selectinload(User.team_members),
            selectinload(Coaching.leitfaden_responses).joinedload(CoachingLeitfadenResponse.item),
        )
        .filter(*filters)
        .order_by(Coaching.team_member_id)
        .limit(500)
        .all()
    )

    out = [_serialize_coaching_impact_day_item(c) for c in coachings]
    return jsonify({'date': day.strftime('%d.%m.%Y'), 'count': len(out), 'coachings': out})
