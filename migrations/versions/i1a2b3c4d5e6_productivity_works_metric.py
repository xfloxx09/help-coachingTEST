"""Productivity Works metric settings.

Revision ID: i1a2b3c4d5e6
Revises: h0a1b2c3d4e5
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa


revision = 'i1a2b3c4d5e6'
down_revision = 'h0a1b2c3d4e5'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if 'project_productivity_settings' not in insp.get_table_names():
        return
    existing = {c['name'] for c in insp.get_columns('project_productivity_settings')}
    if 'works_col' not in existing:
        op.add_column(
            'project_productivity_settings',
            sa.Column('works_col', sa.String(80), nullable=False, server_default='Works_Beendet'),
        )
    if 'dashboard_show_works' not in existing:
        op.add_column(
            'project_productivity_settings',
            sa.Column('dashboard_show_works', sa.Boolean(), nullable=False, server_default=sa.true()),
        )
    if 'impact_show_works' not in existing:
        op.add_column(
            'project_productivity_settings',
            sa.Column('impact_show_works', sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if 'label_works' not in existing:
        op.add_column(
            'project_productivity_settings',
            sa.Column('label_works', sa.String(80), nullable=False, server_default='Works'),
        )


def downgrade():
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if 'project_productivity_settings' not in insp.get_table_names():
        return
    existing = {c['name'] for c in insp.get_columns('project_productivity_settings')}
    for col in ('label_works', 'impact_show_works', 'dashboard_show_works', 'works_col'):
        if col in existing:
            op.drop_column('project_productivity_settings', col)
