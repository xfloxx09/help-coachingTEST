"""Platform-wide KPI features toggle.

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa


revision = 'e7f8a9b0c1d2'
down_revision = 'd6e7f8a9b0c1'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if 'platform_settings' in insp.get_table_names():
        return
    op.create_table(
        'platform_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kpi_features_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint('id'),
    )
    op.execute(
        "INSERT INTO platform_settings (id, kpi_features_enabled) VALUES (1, TRUE)"
    )


def downgrade():
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if 'platform_settings' in insp.get_table_names():
        op.drop_table('platform_settings')
