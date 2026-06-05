"""Add Fachkompetenz and Vertriebliche Ansprache KPI fields.

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa


revision = 'c5d6e7f8a9b0'
down_revision = 'b4c5d6e7f8a9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('kpi_surveys', schema=None) as batch_op:
        batch_op.add_column(sa.Column('fachkompetenz_stars', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('vertrieb_positive', sa.Boolean(), nullable=True))

    with op.batch_alter_table('project_kpi_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('show_fachkompetenz', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column('show_vertrieb', sa.Boolean(), nullable=False, server_default=sa.true()))

    with op.batch_alter_table('team_view_card_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('show_fachkompetenz', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column('show_vertrieb', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column('target_fachkompetenz', sa.Float(), nullable=False, server_default='4'))
        batch_op.add_column(sa.Column('target_vertrieb', sa.Float(), nullable=False, server_default='80'))
        batch_op.add_column(sa.Column('warn_fachkompetenz', sa.Float(), nullable=False, server_default='3'))
        batch_op.add_column(sa.Column('warn_vertrieb', sa.Float(), nullable=False, server_default='60'))


def downgrade():
    with op.batch_alter_table('team_view_card_settings', schema=None) as batch_op:
        batch_op.drop_column('warn_vertrieb')
        batch_op.drop_column('warn_fachkompetenz')
        batch_op.drop_column('target_vertrieb')
        batch_op.drop_column('target_fachkompetenz')
        batch_op.drop_column('show_vertrieb')
        batch_op.drop_column('show_fachkompetenz')

    with op.batch_alter_table('project_kpi_settings', schema=None) as batch_op:
        batch_op.drop_column('show_vertrieb')
        batch_op.drop_column('show_fachkompetenz')

    with op.batch_alter_table('kpi_surveys', schema=None) as batch_op:
        batch_op.drop_column('vertrieb_positive')
        batch_op.drop_column('fachkompetenz_stars')
