"""KPI categories and productivity tables.

Revision ID: g9a0b1c2d3e4
Revises: f8a9b0c1d3e4
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa


revision = 'g9a0b1c2d3e4'
down_revision = 'f8a9b0c1d3e4'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if 'kpi_categories' not in tables:
        op.create_table(
            'kpi_categories',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('key', sa.String(40), nullable=False, unique=True),
            sa.Column('label', sa.String(100), nullable=False),
            sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('is_system', sa.Boolean(), nullable=False, server_default=sa.true()),
        )
        conn.execute(sa.text(
            "INSERT INTO kpi_categories (key, label, sort_order, is_system) VALUES "
            "('qualitaet', 'Qualität', 1, true), "
            "('produktivitaet', 'Produktivität', 2, true)"
        ))

    if 'productivity_import_batches' not in tables:
        op.create_table(
            'productivity_import_batches',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('filename', sa.String(255)),
            sa.Column('imported_by_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
            sa.Column('imported_at', sa.DateTime(), nullable=False),
            sa.Column('date_from', sa.Date(), nullable=True),
            sa.Column('date_to', sa.Date(), nullable=True),
            sa.Column('rows_total', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('intervals_stored', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('matched_member', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('unmatched_member', sa.Integer(), nullable=False, server_default='0'),
        )

    if 'productivity_intervals' not in tables:
        op.create_table(
            'productivity_intervals',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('batch_id', sa.Integer(), sa.ForeignKey('productivity_import_batches.id'), nullable=True),
            sa.Column('team_member_id', sa.Integer(), sa.ForeignKey('team_members.id'), nullable=True),
            sa.Column('team_id', sa.Integer(), sa.ForeignKey('teams.id'), nullable=True),
            sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id'), nullable=True),
            sa.Column('slot_at', sa.DateTime(), nullable=False),
            sa.Column('interval_sec', sa.Integer(), nullable=False, server_default='1800'),
            sa.Column('sign_on_sec', sa.Float(), nullable=False, server_default='0'),
            sa.Column('prod_sec', sa.Float(), nullable=False, server_default='0'),
            sa.Column('nach_sec', sa.Float(), nullable=False, server_default='0'),
            sa.Column('idle_sec', sa.Float(), nullable=False, server_default='0'),
            sa.Column('pause_sec', sa.Float(), nullable=False, server_default='0'),
            sa.Column('calls', sa.Float(), nullable=False, server_default='0'),
            sa.Column('works_beendet', sa.Float(), nullable=False, server_default='0'),
            sa.Column('sign_on_pct', sa.Float(), nullable=True),
            sa.Column('prod_pct', sa.Float(), nullable=True),
            sa.Column('nach_pct', sa.Float(), nullable=True),
            sa.Column('idle_pct', sa.Float(), nullable=True),
            sa.Column('nach_per_call', sa.Float(), nullable=True),
            sa.Column('kpi_denom', sa.Float(), nullable=True),
            sa.Column('raw_json', sa.Text(), nullable=True),
        )
        op.create_index('ix_productivity_intervals_batch_id', 'productivity_intervals', ['batch_id'])
        op.create_index('ix_productivity_intervals_team_member_id', 'productivity_intervals', ['team_member_id'])
        op.create_index('ix_productivity_intervals_team_id', 'productivity_intervals', ['team_id'])
        op.create_index('ix_productivity_intervals_project_id', 'productivity_intervals', ['project_id'])
        op.create_index('ix_productivity_intervals_slot_at', 'productivity_intervals', ['slot_at'])

    if 'project_productivity_settings' not in tables:
        op.create_table(
            'project_productivity_settings',
            sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id'), primary_key=True),
            sa.Column('interval_sec', sa.Integer(), nullable=False, server_default='1800'),
            sa.Column('pause_col', sa.String(80), nullable=False, server_default='IDLE_RC12_Bearbeitung'),
            sa.Column('calls_col', sa.String(80), nullable=False, server_default='Mex1'),
            sa.Column('sign_on_cols', sa.Text(), nullable=True),
            sa.Column('prod_cols', sa.Text(), nullable=True),
            sa.Column('nach_cols', sa.Text(), nullable=True),
            sa.Column('idle_cols', sa.Text(), nullable=True),
            sa.Column('excluded_cols', sa.Text(), nullable=True),
            sa.Column('dashboard_show_sign_on', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('dashboard_show_prod', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('dashboard_show_nach', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('dashboard_show_idle', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('dashboard_show_calls', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('impact_show_sign_on', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('impact_show_prod', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('impact_show_nach', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('impact_show_idle', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('impact_show_calls', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('target_sign_on', sa.Float(), nullable=False, server_default='95'),
            sa.Column('target_prod', sa.Float(), nullable=False, server_default='85'),
            sa.Column('target_nach_per_call', sa.Float(), nullable=False, server_default='30'),
            sa.Column('target_idle_max', sa.Float(), nullable=False, server_default='10'),
        )


def downgrade():
    op.drop_table('project_productivity_settings')
    op.drop_index('ix_productivity_intervals_slot_at', 'productivity_intervals')
    op.drop_index('ix_productivity_intervals_project_id', 'productivity_intervals')
    op.drop_index('ix_productivity_intervals_team_id', 'productivity_intervals')
    op.drop_index('ix_productivity_intervals_team_member_id', 'productivity_intervals')
    op.drop_index('ix_productivity_intervals_batch_id', 'productivity_intervals')
    op.drop_table('productivity_intervals')
    op.drop_table('productivity_import_batches')
    op.drop_table('kpi_categories')
