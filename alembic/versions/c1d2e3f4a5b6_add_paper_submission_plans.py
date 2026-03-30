"""add_paper_submission_plans

Revision ID: c1d2e3f4a5b6
Revises: b2c3d4e5f6a7
Create Date: 2026-03-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'paper_submission_plans',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('paper_id', sa.Integer(), nullable=False),
        sa.Column('conference_edition_id', sa.Integer(), nullable=True),
        sa.Column('journal_id', sa.Integer(), nullable=True),
        sa.Column('journal_special_issue_id', sa.Integer(), nullable=True),
        sa.Column('notes', sa.String(512), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['paper_id'], ['paper_projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['conference_edition_id'], ['conference_editions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['journal_id'], ['journals.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['journal_special_issue_id'], ['journal_special_issues.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_paper_submission_plans_paper_id', 'paper_submission_plans', ['paper_id'])
    op.create_index('ix_paper_submission_plans_conference_edition_id', 'paper_submission_plans', ['conference_edition_id'])
    op.create_index('ix_paper_submission_plans_journal_id', 'paper_submission_plans', ['journal_id'])


def downgrade() -> None:
    op.drop_index('ix_paper_submission_plans_journal_id', 'paper_submission_plans')
    op.drop_index('ix_paper_submission_plans_conference_edition_id', 'paper_submission_plans')
    op.drop_index('ix_paper_submission_plans_paper_id', 'paper_submission_plans')
    op.drop_table('paper_submission_plans')
