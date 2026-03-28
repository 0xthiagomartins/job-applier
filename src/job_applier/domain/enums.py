"""Domain enums shared across Job Applier entities."""

from enum import StrEnum


class Platform(StrEnum):
    LINKEDIN = "linkedin"


class WorkplaceType(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"


class SeniorityLevel(StrEnum):
    INTERN = "intern"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    STAFF = "staff"
    PRINCIPAL = "principal"
    MANAGER = "manager"
    DIRECTOR = "director"


class SubmissionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUBMITTED = "submitted"
    FAILED = "failed"
    SKIPPED = "skipped"


class QuestionType(StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    CITY = "city"
    LINKEDIN_URL = "linkedin_url"
    GITHUB_URL = "github_url"
    PORTFOLIO_URL = "portfolio_url"
    WORK_AUTHORIZATION = "work_authorization"
    VISA_SPONSORSHIP = "visa_sponsorship"
    YEARS_EXPERIENCE = "years_experience"
    SALARY_EXPECTATION = "salary_expectation"
    START_DATE = "start_date"
    RESUME_UPLOAD = "resume_upload"
    COVER_LETTER = "cover_letter"
    YES_NO_GENERIC = "yes_no_generic"
    FREE_TEXT_GENERIC = "free_text_generic"
    UNKNOWN = "unknown"


class AnswerSource(StrEnum):
    RULE = "rule"
    PROFILE_SNAPSHOT = "profile_snapshot"
    DEFAULT_RESPONSE = "default_response"
    BEST_EFFORT_AUTOFILL = "best_effort_autofill"


class FillStrategy(StrEnum):
    DETERMINISTIC = "deterministic"
    BEST_EFFORT = "best_effort"


class RecruiterAction(StrEnum):
    CONNECT = "connect"


class RecruiterInteractionStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    SKIPPED = "skipped"
    FAILED = "failed"


class ExecutionEventType(StrEnum):
    EXECUTION_STARTED = "execution_started"
    STEP_REACHED = "step_reached"
    AUTOFILL_APPLIED = "autofill_applied"
    SUBMISSION_COMPLETED = "submission_completed"
    EXECUTION_FAILED = "execution_failed"


class ArtifactType(StrEnum):
    SCREENSHOT = "screenshot"
    HTML_DUMP = "html_dump"
    PLAYWRIGHT_TRACE = "playwright_trace"
    CV_METADATA = "cv_metadata"


class ExecutionOrigin(StrEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"
