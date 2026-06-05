"""KPI snapshots on assigned coachings + team view card settings

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa


revision = 'b4c5d6e7f8a9'
down_revision = 'a3b4c5d6e7f8'
branch_labels = None
depends_on = None

_AC_COLS = (
    'start_nps_at_assign', 'start_loesung_quote_at_assign', 'start_info_quote_at_assign',
    'end_nps', 'end_loesung_quote', 'end_info_quote',
)


def upgrade():
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if 'assigned_coachings' in tables:
        cols = {c['name'] for c in insp.get_columns('assigned_coachings')}
        for col in _AC_COLS:
            if col not in cols:
                op.add_column('assigned_coachings', sa.Column(col, sa.Float(), nullable=True))

    if 'team_view_card_settings' not in tables:
        op.create_table(
            'team_view_card_settings',
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('show_nps', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('show_loesung', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('show_info', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('show_performance', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('target_nps', sa.Float(), nullable=False, server_default='50'),
            sa.Column('target_loesung', sa.Float(), nullable=False, server_default='80'),
            sa.Column('target_info', sa.Float(), nullable=False, server_default='80'),
            sa.Column('target_performance', sa.Float(), nullable=False, server_default='80'),
            sa.Column('warn_nps', sa.Float(), nullable=False, server_default='0'),
            sa.Column('warn_loesung', sa.Float(), nullable=False, server_default='60'),
            sa.Column('warn_info', sa.Float(), nullable=False, server_default='60'),
            sa.Column('warn_performance', sa.Float(), nullable=False, server_default='50'),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
            sa.PrimaryKeyConstraint('project_id'),
        )


def downgrade():
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())
    if 'team_view_card_settings' in tables:
        op.drop_table('team_view_card_settings')
    if 'assigned_coachings' in tables:
        cols = {c['name'] for c in insp.get_columns('assigned_coachings')}
        for col in _AC_COLS:
            if col in cols:
                op.drop_column('assigned_coachings', col)
