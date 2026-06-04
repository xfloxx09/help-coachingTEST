"""Add KPI (Demo) tables: kpi_import_batches, kpi_surveys, kpi_answers

Revision ID: d1e2f3a4b5c6
Revises: e1f2a3b4c5d6
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa


revision = 'd1e2f3a4b5c6'
down_revision = 'e1f2a3b4c5d6'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    tables = set(sa.inspect(conn).get_table_names())

    if 'kpi_import_batches' not in tables:
        op.create_table(
            'kpi_import_batches',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('filename', sa.String(length=255), nullable=True),
            sa.Column('imported_by_id', sa.Integer(), nullable=True),
            sa.Column('imported_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.Column('date_from', sa.Date(), nullable=True),
            sa.Column('date_to', sa.Date(), nullable=True),
            sa.Column('surveys_total', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('surveys_matched_team', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('surveys_matched_member', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('surveys_unassigned', sa.Integer(), nullable=False, server_default='0'),
            sa.ForeignKeyConstraint(['imported_by_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )

    if 'kpi_surveys' not in tables:
        op.create_table(
            'kpi_surveys',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('datensatz_id', sa.String(length=64), nullable=False),
            sa.Column('interviewnummer', sa.String(length=64), nullable=True),
            sa.Column('antwort_date', sa.Date(), nullable=True),
            sa.Column('kontakt_date', sa.Date(), nullable=True),
            sa.Column('be4', sa.String(length=100), nullable=True),
            sa.Column('ma_kenner', sa.String(length=50), nullable=True),
            sa.Column('ospname', sa.String(length=100), nullable=True),
            sa.Column('kampagne', sa.String(length=150), nullable=True),
            sa.Column('studie', sa.String(length=150), nullable=True),
            sa.Column('queue', sa.String(length=150), nullable=True),
            sa.Column('vorname', sa.String(length=100), nullable=True),
            sa.Column('nachname', sa.String(length=100), nullable=True),
            sa.Column('team_id', sa.Integer(), nullable=True),
            sa.Column('project_id', sa.Integer(), nullable=True),
            sa.Column('team_member_id', sa.Integer(), nullable=True),
            sa.Column('nps_value', sa.Integer(), nullable=True),
            sa.Column('loesung_answer', sa.String(length=255), nullable=True),
            sa.Column('info_positive', sa.Boolean(), nullable=True),
            sa.Column('loesung_positive', sa.Boolean(), nullable=True),
            sa.Column('batch_id', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.ForeignKeyConstraint(['team_id'], ['teams.id']),
            sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
            sa.ForeignKeyConstraint(['team_member_id'], ['team_members.id']),
            sa.ForeignKeyConstraint(['batch_id'], ['kpi_import_batches.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_kpi_surveys_datensatz_id', 'kpi_surveys', ['datensatz_id'])
        op.create_index('ix_kpi_surveys_antwort_date', 'kpi_surveys', ['antwort_date'])
        op.create_index('ix_kpi_surveys_team_id', 'kpi_surveys', ['team_id'])
        op.create_index('ix_kpi_surveys_project_id', 'kpi_surveys', ['project_id'])
        op.create_index('ix_kpi_surveys_team_member_id', 'kpi_surveys', ['team_member_id'])

    if 'kpi_answers' not in tables:
        op.create_table(
            'kpi_answers',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('survey_id', sa.Integer(), nullable=False),
            sa.Column('frage_code', sa.String(length=40), nullable=True),
            sa.Column('frage_text', sa.Text(), nullable=True),
            sa.Column('antwort', sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(['survey_id'], ['kpi_surveys.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_kpi_answers_survey_id', 'kpi_answers', ['survey_id'])


def downgrade():
    conn = op.get_bind()
    tables = set(sa.inspect(conn).get_table_names())
    if 'kpi_answers' in tables:
        op.drop_table('kpi_answers')
    if 'kpi_surveys' in tables:
        op.drop_table('kpi_surveys')
    if 'kpi_import_batches' in tables:
        op.drop_table('kpi_import_batches')
