"""Add counts flag to project_kpi_sources (counting vs. show-only survey types)

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa


revision = 'a3b4c5d6e7f8'
down_revision = 'f2a3b4c5d6e7'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if 'project_kpi_sources' in insp.get_table_names():
        cols = [c['name'] for c in insp.get_columns('project_kpi_sources')]
        if 'counts' not in cols:
            op.add_column(
                'project_kpi_sources',
                sa.Column('counts', sa.Boolean(), nullable=False, server_default=sa.true()),
            )


def downgrade():
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if 'project_kpi_sources' in insp.get_table_names():
        cols = [c['name'] for c in insp.get_columns('project_kpi_sources')]
        if 'counts' in cols:
            op.drop_column('project_kpi_sources', 'counts')
