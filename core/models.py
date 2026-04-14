from sqlalchemy import (
    Column, Integer, String, Boolean, Text, DateTime, Date,
    ForeignKey, Numeric, JSON, UniqueConstraint, Index,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.database import Base


class Platform(Base):
    __tablename__ = "platforms"

    id = Column(Integer, primary_key=True)
    name = Column(String(50), unique=True, nullable=False)
    base_url = Column(String(255), nullable=False)
    auth_method = Column(String(20), nullable=False)  # AuthMethod enum values
    is_active = Column(Boolean, default=True, nullable=False)
    daily_limit = Column(Integer, default=10, nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    jobs = relationship("Job", back_populates="platform")


class CVProfile(Base):
    __tablename__ = "cv_profiles"

    id = Column(Integer, primary_key=True)
    name = Column(String(50), unique=True, nullable=False)  # 'remoto' | 'local'
    json_path = Column(String(255), nullable=False)
    pdf_path = Column(String(255), nullable=False)
    structured_data = Column(JSON)
    target_keywords = Column(JSON)
    filters = Column(JSON)
    created_at = Column(DateTime, server_default=func.now())

    match_results = relationship("MatchResult", back_populates="cv_profile")
    applications = relationship("Application", back_populates="cv_profile")


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True)
    platform_id = Column(Integer, ForeignKey("platforms.id"), nullable=False)
    external_id = Column(String(255), nullable=False)
    title = Column(String(500), nullable=False)
    company = Column(String(255))
    location = Column(String(255))
    url = Column(Text, nullable=False)
    description = Column(Text)
    salary_range = Column(String(100))
    modality = Column(String(20))  # Modality enum values
    status = Column(String(20), default="PENDING", nullable=False)
    scraped_at = Column(DateTime, server_default=func.now())
    expires_at = Column(DateTime)

    platform = relationship("Platform", back_populates="jobs")
    match_results = relationship("MatchResult", back_populates="job")
    applications = relationship("Application", back_populates="job")

    __table_args__ = (
        UniqueConstraint("platform_id", "external_id", name="uq_platform_external_id"),
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_company_title", "company", "title"),
    )


class MatchResult(Base):
    __tablename__ = "match_results"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    cv_profile_id = Column(Integer, ForeignKey("cv_profiles.id"), nullable=False)
    score = Column(Integer)
    match_reason = Column(Text)
    auto_apply = Column(Boolean, default=False)
    missing_skills = Column(JSON)
    risk_flags = Column(JSON)
    llm_response_raw = Column(JSON)
    evaluated_at = Column(DateTime, server_default=func.now())

    job = relationship("Job", back_populates="match_results")
    cv_profile = relationship("CVProfile", back_populates="match_results")


class Application(Base):
    __tablename__ = "applications"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    cv_profile_id = Column(Integer, ForeignKey("cv_profiles.id"), nullable=False)
    status = Column(String(20), default="QUEUED", nullable=False)
    priority_score = Column(Integer)
    last_successful_step = Column(JSON)   # checkpoint for idempotent retries
    orphan_questions = Column(JSON)       # form fields the LLM couldn't answer
    error_log = Column(Text)
    screenshot_path = Column(String(255))
    retry_count = Column(Integer, default=0)
    applied_at = Column(DateTime)
    email_category = Column(String(20))   # set when an email reply is linked: INTERVIEW/OFFER/REJECTION/RECEIVED

    job = relationship("Job", back_populates="applications")
    cv_profile = relationship("CVProfile", back_populates="applications")


class DailyReport(Base):
    __tablename__ = "daily_reports"

    id = Column(Integer, primary_key=True)
    report_date = Column(Date, nullable=False, unique=True)
    jobs_scraped = Column(Integer, default=0)
    jobs_matched = Column(Integer, default=0)
    jobs_applied = Column(Integer, default=0)
    jobs_failed = Column(Integer, default=0)
    platform_breakdown = Column(JSON)
    api_cost_usd = Column(Numeric(10, 6))
    sent_at = Column(DateTime)
