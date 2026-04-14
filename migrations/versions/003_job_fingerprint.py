"""Add fingerprint column to jobs for cross-platform deduplication

Revision ID: 003
Revises: 002
Create Date: 2026-04-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("fingerprint", sa.String(32), nullable=True),
    )
    # Partial unique index: only enforce uniqueness where fingerprint is not null
    op.create_index(
        "ix_jobs_fingerprint",
        "jobs",
        ["fingerprint"],
        unique=True,
        postgresql_where=sa.text("fingerprint IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_fingerprint", table_name="jobs")
    op.drop_column("jobs", "fingerprint")
