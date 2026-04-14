"""Initial schema: platforms, cv_profiles, jobs, match_results, applications, daily_reports

Revision ID: 001
Revises:
Create Date: 2026-04-05
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platforms",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False, unique=True),
        sa.Column("base_url", sa.String(255), nullable=False),
        sa.Column("auth_method", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("daily_limit", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table(
        "cv_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False, unique=True),
        sa.Column("json_path", sa.String(255), nullable=False),
        sa.Column("pdf_path", sa.String(255), nullable=False),
        sa.Column("structured_data", sa.JSON()),
        sa.Column("target_keywords", sa.JSON()),
        sa.Column("filters", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("platform_id", sa.Integer(), sa.ForeignKey("platforms.id"), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("company", sa.String(255)),
        sa.Column("location", sa.String(255)),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("salary_range", sa.String(100)),
        sa.Column("modality", sa.String(20)),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("scraped_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime()),
        sa.UniqueConstraint("platform_id", "external_id", name="uq_platform_external_id"),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_company_title", "jobs", ["company", "title"])

    op.create_table(
        "match_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("cv_profile_id", sa.Integer(), sa.ForeignKey("cv_profiles.id"), nullable=False),
        sa.Column("score", sa.Integer()),
        sa.Column("match_reason", sa.Text()),
        sa.Column("auto_apply", sa.Boolean(), server_default="false"),
        sa.Column("missing_skills", sa.JSON()),
        sa.Column("risk_flags", sa.JSON()),
        sa.Column("llm_response_raw", sa.JSON()),
        sa.Column("evaluated_at", sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table(
        "applications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("cv_profile_id", sa.Integer(), sa.ForeignKey("cv_profiles.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="QUEUED"),
        sa.Column("priority_score", sa.Integer()),
        sa.Column("last_successful_step", sa.JSON()),
        sa.Column("orphan_questions", sa.JSON()),
        sa.Column("error_log", sa.Text()),
        sa.Column("screenshot_path", sa.String(255)),
        sa.Column("retry_count", sa.Integer(), server_default="0"),
        sa.Column("applied_at", sa.DateTime()),
    )

    op.create_table(
        "daily_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("report_date", sa.Date(), nullable=False, unique=True),
        sa.Column("jobs_scraped", sa.Integer(), server_default="0"),
        sa.Column("jobs_matched", sa.Integer(), server_default="0"),
        sa.Column("jobs_applied", sa.Integer(), server_default="0"),
        sa.Column("jobs_failed", sa.Integer(), server_default="0"),
        sa.Column("platform_breakdown", sa.JSON()),
        sa.Column("api_cost_usd", sa.Numeric(10, 6)),
        sa.Column("sent_at", sa.DateTime()),
    )


def downgrade() -> None:
    op.drop_table("daily_reports")
    op.drop_table("applications")
    op.drop_table("match_results")
    op.drop_index("ix_jobs_company_title", table_name="jobs")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("cv_profiles")
    op.drop_table("platforms")
