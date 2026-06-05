"""Add KPI configuration tables: project_kpi_sources, project_kpi_settings, kpi_question_mappings

Revision ID: f2a3b4c5d6e7
Revises: d1e2f3a4b5c6
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa


revision = 'f2a3b4c5d6e7'
down_revision = 'd1e2f3a4b5c6'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    tables = set(sa.inspect(conn).get_table_names())

    if 'project_kpi_sources' not in tables:
        op.create_table(
            'project_kpi_sources',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('survey_type', sa.String(length=150), nullable=False),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('project_id', 'survey_type', name='uq_project_kpi_source'),
        )
        op.create_index('ix_project_kpi_sources_project_id', 'project_kpi_sources', ['project_id'])

    if 'project_kpi_settings' not in tables:
        op.create_table(
            'project_kpi_settings',
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('show_info', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('show_loesung', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('show_nps', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
            sa.PrimaryKeyConstraint('project_id'),
        )

    if 'kpi_question_mappings' not in tables:
        op.create_table(
            'kpi_question_mappings',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('project_id', sa.Integer(), nullable=False),
            sa.Column('survey_type', sa.String(length=150), nullable=False),
            sa.Column('kpi_kind', sa.String(length=20), nullable=False),
            sa.Column('frage_code', sa.String(length=40), nullable=False),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('project_id', 'survey_type', 'kpi_kind', name='uq_kpi_question_mapping'),
        )
        op.create_index('ix_kpi_question_mappings_project_id', 'kpi_question_mappings', ['project_id'])


def downgrade():
    conn = op.get_bind()
    tables = set(sa.inspect(conn).get_table_names())
    if 'kpi_question_mappings' in tables:
        op.drop_table('kpi_question_mappings')
    if 'project_kpi_settings' in tables:
        op.drop_table('project_kpi_settings')
    if 'project_kpi_sources' in tables:
        op.drop_table('project_kpi_sources')
