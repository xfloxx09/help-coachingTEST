"""Helpers to link TeamMember rows with User accounts (Option A – no schema merge)."""
import re

from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from app import db
from app.models import TeamMember, User, Role
from app.utils import ROLE_MITARBEITER, get_or_create_archiv_team


def _norm(s):
    return (s or '').strip()


def _norm_lower(s):
    return _norm(s).lower()


def _guess_username_from_name(full_name, fallback=''):
    parts = _norm(full_name).split(None, 1)
    if len(parts) >= 2:
        base = f"{parts[0][:4]}{parts[1]}".lower()
    elif parts:
        base = parts[0].lower()
    else:
        base = fallback.lower()
    base = re.sub(r'[^a-z0-9.]', '', base)
    return base or 'mitglied'


def _unique_username(base):
    username = base[:64]
    if not User.query.filter_by(username=username).first():
        return username
    n = 2
    while n < 1000:
        candidate = f"{base[:58]}{n}"
        if not User.query.filter_by(username=candidate).first():
            return candidate
        n += 1
    return f"{base[:50]}{id(base)}"


def member_link_status(member):
    """Return dict describing login link state for a TeamMember."""
    if member.user_id and member.user:
        u = member.user
        return {
            'state': 'linked',
            'user_id': u.id,
            'username': u.username,
            'email': u.email or '',
            'role_name': u.role_name or '–',
        }
    return {'state': 'import_only', 'user_id': None}


def score_user_for_member(user, member):
    """Score how likely this User belongs to member (higher = better)."""
    score = 0
    reasons = []
    m_name = _norm_lower(member.name)
    m_ma = _norm(member.ma_kennung)
    m_dag = _norm(member.dag_id)
    m_pylon = _norm(member.pylon)

    if user.team_members:
        for tm in user.team_members:
            if tm.id == member.id:
                return 100, ['bereits dieses Mitglied']
            if m_name and _norm_lower(tm.name) == m_name:
                score += 45
                reasons.append('gleicher Name (anderes Mitglied des Benutzers)')
            if m_ma and _norm(tm.ma_kennung) == m_ma:
                score += 40
                reasons.append('gleiche MA-Kennung')
            if m_dag and _norm(tm.dag_id) == m_dag:
                score += 35
                reasons.append('gleiche DAG-ID')
            if m_pylon and _norm(tm.pylon) == m_pylon:
                score += 30
                reasons.append('gleiche Pylon-Nr')

    uname = _norm_lower(user.username)
    if m_name:
        guess = _guess_username_from_name(member.name)
        if uname == guess or uname.startswith(guess[:6]):
            score += 25
            reasons.append('ähnlicher Benutzername')
        name_compact = m_name.replace(' ', '')
        if name_compact and name_compact in uname.replace('.', ''):
            score += 15
            reasons.append('Name im Benutzernamen')

    if user.email and m_name:
        local = user.email.split('@')[0].lower()
        if local in m_name.replace(' ', '.') or m_name.split()[0].lower() in local:
            score += 20
            reasons.append('E-Mail passt zum Namen')

    # Dedupe reasons preserving order
    seen = set()
    uniq_reasons = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            uniq_reasons.append(r)
    return min(score, 99), uniq_reasons


def find_user_candidates_for_member(member, limit=8):
    """Users that might be the login for this TeamMember (not yet linked to it)."""
    if member.user_id:
        return []

    m_name = _norm_lower(member.name)
    m_ma = _norm(member.ma_kennung)
    m_dag = _norm(member.dag_id)
    m_pylon = _norm(member.pylon)

    filters = []
    if m_ma:
        filters.append(TeamMember.ma_kennung == m_ma)
    if m_dag:
        filters.append(TeamMember.dag_id == m_dag)
    if m_pylon:
        filters.append(TeamMember.pylon == m_pylon)
    if m_name:
        filters.append(TeamMember.name.ilike(member.name.strip()))

    user_ids = set()
    if filters:
        rows = (
            TeamMember.query.filter(or_(*filters))
            .filter(TeamMember.id != member.id)
            .filter(TeamMember.user_id.isnot(None))
            .with_entities(TeamMember.user_id)
            .distinct()
            .all()
        )
        user_ids.update(r[0] for r in rows if r[0])

    guess = _guess_username_from_name(member.name, m_pylon)
    for u in User.query.filter(User.username.ilike(f'{guess}%')).limit(5).all():
        user_ids.add(u.id)

    if m_name:
        first = member.name.split()[0]
        for u in User.query.filter(User.username.ilike(f'{first.lower()}%')).limit(5).all():
            user_ids.add(u.id)

    candidates = []
    if user_ids:
        users = (
            User.query.options(joinedload(User.role), joinedload(User.team_members))
            .filter(User.id.in_(user_ids))
            .all()
        )
        for u in users:
            if any(tm.id == member.id for tm in u.team_members):
                continue
            sc, reasons = score_user_for_member(u, member)
            if sc >= 15:
                candidates.append({
                    'user_id': u.id,
                    'username': u.username,
                    'email': u.email or '',
                    'role_name': u.role_name or '–',
                    'score': sc,
                    'reasons': reasons,
                })
    candidates.sort(key=lambda c: (-c['score'], c['username']))
    return candidates[:limit]


def find_unlinked_members_for_user(user, limit=8):
    """TeamMember rows without login that might belong to this user."""
    linked_ids = {m.id for m in user.team_members}
    m0 = user.team_members[0] if user.team_members else None
    filters = []
    if m0:
        if m0.ma_kennung:
            filters.append(TeamMember.ma_kennung == m0.ma_kennung)
        if m0.dag_id:
            filters.append(TeamMember.dag_id == m0.dag_id)
        if m0.pylon:
            filters.append(TeamMember.pylon == m0.pylon)
        if m0.name:
            filters.append(TeamMember.name.ilike(m0.name.strip()))
    elif user.username:
        filters.append(TeamMember.name.ilike(f'%{user.username}%'))

    q = TeamMember.query.options(joinedload(TeamMember.team)).filter(
        TeamMember.user_id.is_(None),
    )
    if filters:
        q = q.filter(or_(*filters))
    else:
        return []

    out = []
    for m in q.limit(40).all():
        if m.id in linked_ids:
            continue
        sc, reasons = score_user_for_member(user, m)
        if sc >= 20:
            out.append({
                'member_id': m.id,
                'name': m.name,
                'team_name': m.team.name if m.team else '–',
                'ma_kennung': m.ma_kennung or '',
                'dag_id': m.dag_id or '',
                'score': sc,
                'reasons': reasons,
            })
    out.sort(key=lambda c: (-c['score'], c['name']))
    return out[:limit]


def link_member_to_user(member, user, replace_existing=True):
    """Attach member to user. Returns True on success."""
    if not member or not user:
        return False
    if member.user_id == user.id:
        return True
    if member.user_id and member.user_id != user.id and not replace_existing:
        return False
    member.user_id = user.id
    db.session.add(member)
    return True


def unlink_member_user(member):
    if member.user_id:
        member.user_id = None
        db.session.add(member)
        return True
    return False


def try_auto_link_member(member, min_score=50):
    """Link member to best user candidate if score is high enough. Returns user_id or None."""
    if member.user_id:
        return member.user_id
    candidates = find_user_candidates_for_member(member, limit=3)
    if not candidates:
        return None
    best = candidates[0]
    if best['score'] < min_score:
        return None
    if len(candidates) > 1 and candidates[1]['score'] >= min_score:
        # Ambiguous – do not auto-link
        return None
    user = User.query.get(best['user_id'])
    if not user:
        return None
    link_member_to_user(member, user)
    return user.id


def create_user_for_member(member, role_name=ROLE_MITARBEITER, password='Start123'):
    """Create a login User and link to member. Returns (user, created_bool)."""
    if member.user_id:
        return User.query.get(member.user_id), False

    team = member.team
    project_id = None
    if team:
        project_id = team.project_id
    if member.original_project_id:
        project_id = member.original_project_id
    if not project_id:
        archiv = get_or_create_archiv_team()
        if team and team.id != archiv.id:
            project_id = team.project_id

    role = Role.query.filter_by(name=role_name).first()
    if not role:
        role = Role.query.first()
    if not project_id:
        from app.models import Project
        p = Project.query.order_by(Project.id).first()
        project_id = p.id if p else None
    if not project_id or not role:
        raise ValueError('Kein Projekt oder keine Rolle für neues Benutzerkonto gefunden.')

    username = _unique_username(_guess_username_from_name(member.name, member.pylon or 'agent'))
    user = User(
        username=username,
        email=None,
        role_id=role.id,
        project_id=project_id,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.flush()
    link_member_to_user(member, user)
    return user, True


def auto_link_all_unlinked(min_score=50):
    """Try to link all members without user_id. Returns count linked."""
    members = TeamMember.query.filter(TeamMember.user_id.is_(None)).all()
    linked = 0
    for m in members:
        if try_auto_link_member(m, min_score=min_score):
            linked += 1
    return linked
