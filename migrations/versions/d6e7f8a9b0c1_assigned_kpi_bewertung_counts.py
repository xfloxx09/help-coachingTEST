"""Assigned coaching KPI Bewertung counts at snapshot time.

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa


revision = 'd6e7f8a9b0c1'
down_revision = 'c5d6e7f8a9b0'
branch_labels = None
depends_on = None

_AC_COUNT_COLS = (
    'start_nps_count_at_assign', 'start_loesung_count_at_assign', 'start_info_count_at_assign',
    'end_nps_count', 'end_loesung_count', 'end_info_count',
)


def upgrade():
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if 'assigned_coachings' not in insp.get_table_names():
        return
    cols = {c['name'] for c in insp.get_columns('assigned_coachings')}
    for col in _AC_COUNT_COLS:
        if col not in cols:
            op.add_column('assigned_coachings', sa.Column(col, sa.Integer(), nullable=True))


def downgrade():
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if 'assigned_coachings' not in insp.get_table_names():
        return
    cols = {c['name'] for c in insp.get_columns('assigned_coachings')}
    for col in _AC_COUNT_COLS:
        if col in cols:
            op.drop_column('assigned_coachings', col)
