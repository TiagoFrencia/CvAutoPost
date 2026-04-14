from enum import Enum


class AuthMethod(str, Enum):
    API = "api"
    COOKIES = "cookies"
    NONE = "none"


class Modality(str, Enum):
    REMOTO = "remoto"
    PRESENCIAL = "presencial"
    HIBRIDO = "hibrido"


class JobStatus(str, Enum):
    PENDING = "PENDING"
    SCORED = "SCORED"
    SKIPPED = "SKIPPED"
    AUTO_APPLY = "AUTO_APPLY"
    REVIEW_SCORE = "REVIEW_SCORE"
    APPLIED = "APPLIED"
    FAILED = "FAILED"
    DEAD = "DEAD"


class ApplicationStatus(str, Enum):
    QUEUED = "QUEUED"
    APPLIED = "APPLIED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"       # external apply or otherwise not applicable
    CONFIRMED = "CONFIRMED"
    REVIEW_FORM = "REVIEW_FORM"
    INTERVIEW = "INTERVIEW"   # company replied with interview invitation
    REJECTED = "REJECTED"     # company sent rejection


class CVProfileName(str, Enum):
    REMOTO = "remoto"
    LOCAL = "local"
