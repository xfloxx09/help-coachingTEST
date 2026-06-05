"""Per-project KPI dashboard (/kpis) visibility toggles.

Revision ID: f8a9b0c1d3e4
Revises: e7f8a9b0c1d2
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa


revision = 'f8a9b0c1d3e4'
down_revision = 'e7f8a9b0c1d2'
branch_labels = None
depends_on = None

_DASH_COLS = (
    'dashboard_show_info', 'dashboard_show_loesung', 'dashboard_show_nps',
    'dashboard_show_fachkompetenz', 'dashboard_show_vertrieb',
)


def upgrade():
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if 'project_kpi_settings' not in insp.get_table_names():
        return
    cols = {c['name'] for c in insp.get_columns('project_kpi_settings')}
    for col in _DASH_COLS:
        if col not in cols:
            op.add_column('project_kpi_settings', sa.Column(col, sa.Boolean(), nullable=False, server_default=sa.true()))
    conn.execute(sa.text(
        'UPDATE project_kpi_settings SET '
        'dashboard_show_info = show_info, '
        'dashboard_show_loesung = show_loesung, '
        'dashboard_show_nps = show_nps, '
        'dashboard_show_fachkompetenz = show_fachkompetenz, '
        'dashboard_show_vertrieb = show_vertrieb'
    ))


def downgrade():
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if 'project_kpi_settings' not in insp.get_table_names():
        return
    cols = {c['name'] for c in insp.get_columns('project_kpi_settings')}
    for col in _DASH_COLS:
        if col in cols:
            op.drop_column('project_kpi_settings', col)
