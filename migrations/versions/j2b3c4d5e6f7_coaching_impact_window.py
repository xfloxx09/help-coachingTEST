"""Coaching VS KPI Wirkungsfenster on platform settings.

Revision ID: j2b3c4d5e6f7
Revises: i1a2b3c4d5e6
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa


revision = 'j2b3c4d5e6f7'
down_revision = 'i1a2b3c4d5e6'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if 'platform_settings' not in insp.get_table_names():
        return
    cols = {c['name'] for c in insp.get_columns('platform_settings')}
    if 'coaching_impact_window_days' not in cols:
        op.add_column(
            'platform_settings',
            sa.Column('coaching_impact_window_days', sa.Integer(), nullable=False, server_default='14'),
        )


def downgrade():
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if 'platform_settings' not in insp.get_table_names():
        return
    cols = {c['name'] for c in insp.get_columns('platform_settings')}
    if 'coaching_impact_window_days' in cols:
        op.drop_column('platform_settings', 'coaching_impact_window_days')
