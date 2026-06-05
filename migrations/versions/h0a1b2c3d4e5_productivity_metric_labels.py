"""Productivity metric display labels per project.

Revision ID: h0a1b2c3d4e5
Revises: g9a0b1c2d3e4
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa


revision = 'h0a1b2c3d4e5'
down_revision = 'g9a0b1c2d3e4'
branch_labels = None
depends_on = None

LABEL_COLS = (
    ('label_sign_on', 'Sign-On'),
    ('label_prod', 'Produktivität'),
    ('label_nach', 'Nacharbeit'),
    ('label_idle', 'Idle'),
    ('label_calls', 'Calls'),
)


def upgrade():
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if 'project_productivity_settings' not in insp.get_table_names():
        return
    existing = {c['name'] for c in insp.get_columns('project_productivity_settings')}
    for col, default in LABEL_COLS:
        if col not in existing:
            op.add_column(
                'project_productivity_settings',
                sa.Column(col, sa.String(80), nullable=False, server_default=default),
            )


def downgrade():
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if 'project_productivity_settings' not in insp.get_table_names():
        return
    existing = {c['name'] for c in insp.get_columns('project_productivity_settings')}
    for col, _ in reversed(LABEL_COLS):
        if col in existing:
            op.drop_column('project_productivity_settings', col)
