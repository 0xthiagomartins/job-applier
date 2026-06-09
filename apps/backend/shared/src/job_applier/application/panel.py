"""Panel-facing schemas and helpers."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    field_validator,
)

from job_applier.domain.enums import (
    ResumeMode,
    ScheduleFrequency,
    SeniorityLevel,
    SupportedLanguage,
    WorkplaceType,
)

MODEL_OPTIONS = ("o3-mini", "gpt-4.1-mini", "gpt-4o-mini")
SCHEDULE_FREQUENCY_OPTIONS = (ScheduleFrequency.DAILY,)
SUPPORTED_LANGUAGE_OPTIONS = (SupportedLanguage.ENGLISH, SupportedLanguage.PORTUGUESE)
TIMEZONE_OPTIONS = (
    "UTC",
    "America/Sao_Paulo",
    "America/New_York",
    "America/Chicago",
    "America/Los_Angeles",
    "Europe/London",
)

PRIVATE_METADATA_AI_USAGE_WARNING = (
    "Sensitive metadata may be used in future Easy Apply runs and may be sent to OpenAI "
    "when needed to answer unresolved application fields. Review carefully before saving."
)

PRIVATE_METADATA_DISPLAY_LABELS: dict[str, str] = {
    "cpf": "CPF",
    "rg": "RG",
    "father_name": "Nome do pai",
    "mother_name": "Nome da mãe",
    "birth_date": "Data de nascimento",
    "current_employer": "Empresa atual",
    "current_salary": "Salário atual/último",
    "current_benefits": "Benefícios atuais/últimos",
}

_PRIVATE_METADATA_KEY_ALIASES: dict[str, str] = {
    "cpf": "cpf",
    "cadastro_de_pessoas_fisicas": "cpf",
    "tax_id": "cpf",
    "taxid": "cpf",
    "documento_cpf": "cpf",
    "rg": "rg",
    "registro_geral": "rg",
    "identity_document": "rg",
    "documento_de_identidade": "rg",
    "pai": "father_name",
    "nome_do_pai": "father_name",
    "father": "father_name",
    "father_name": "father_name",
    "mae": "mother_name",
    "mãe": "mother_name",
    "nome_da_mae": "mother_name",
    "nome_da_mãe": "mother_name",
    "mother": "mother_name",
    "mother_name": "mother_name",
    "data_de_nascimento": "birth_date",
    "nascimento": "birth_date",
    "birth_date": "birth_date",
    "date_of_birth": "birth_date",
    "dob": "birth_date",
    "current_employer": "current_employer",
    "empresa_atual": "current_employer",
    "empregador_atual": "current_employer",
    "current_salary": "current_salary",
    "salario_atual": "current_salary",
    "salário_atual": "current_salary",
    "ultimo_salario": "current_salary",
    "último_salário": "current_salary",
    "current_benefits": "current_benefits",
    "beneficios_atuais": "current_benefits",
    "benefícios_atuais": "current_benefits",
    "ultimos_beneficios": "current_benefits",
    "últimos_benefícios": "current_benefits",
}

_MISSING_PRIVATE_METADATA_NOTE_PATTERN = re.compile(r"normalized_key=([a-z0-9_]+)")


class PanelModel(BaseModel):
    """Base model used for persisted panel sections."""

    model_config = ConfigDict(frozen=True)


class CapabilityRangeInput(PanelModel):
    """User-reviewed capability range persisted by the panel."""

    min_years: int = Field(ge=0, default=0)
    max_years: int = Field(ge=0, default=0)
    recommended_years: int | None = Field(default=None, ge=0)
    enabled: bool = True


def parse_csv_lines(raw: str) -> tuple[str, ...]:
    """Parse comma-separated or multi-line text into normalized tuples."""

    values: list[str] = []
    for chunk in raw.replace("\n", ",").split(","):
        candidate = chunk.strip()
        if candidate:
            values.append(candidate)
    return tuple(values)


def parse_mapping_lines(
    raw: str,
    *,
    value_type: type[int] | type[str],
) -> dict[str, int] | dict[str, str]:
    """Parse `key=value` or `key:value` lines into dictionaries."""

    parsed: dict[str, int] | dict[str, str] = {}
    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        separator = "=" if "=" in candidate else ":"
        if separator not in candidate:
            msg = "Expected one key/value pair per line using `=` or `:`"
            raise ValueError(msg)
        key, value = (item.strip() for item in candidate.split(separator, maxsplit=1))
        if not key or not value:
            msg = "Each key/value line must have both key and value"
            raise ValueError(msg)
        parsed[key] = value_type(value) if value_type is int else value  # type: ignore[assignment]
    return parsed


def parse_int_mapping_lines(raw: str) -> dict[str, int]:
    """Parse `key=value` lines whose values must be integers."""

    return cast(dict[str, int], parse_mapping_lines(raw, value_type=int))


def parse_text_mapping_lines(raw: str) -> dict[str, str]:
    """Parse `key=value` lines whose values remain as text."""

    return cast(dict[str, str], parse_mapping_lines(raw, value_type=str))


def normalize_private_metadata_key(raw_key: str) -> str:
    """Normalize one user-provided metadata key into a stable canonical identifier."""

    normalized = unicodedata.normalize("NFKD", raw_key).encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized.lower()).strip("_")
    if not normalized:
        msg = "Metadata keys must contain at least one alphanumeric character."
        raise ValueError(msg)
    return _PRIVATE_METADATA_KEY_ALIASES.get(normalized, normalized)


def parse_private_metadata_lines(raw: str) -> dict[str, str]:
    """Parse raw multiline sensitive metadata into normalized key/value pairs."""

    parsed: dict[str, str] = {}
    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        separator = "=" if "=" in candidate else ":"
        if separator not in candidate:
            msg = "Expected one metadata entry per line using `=` or `:`"
            raise ValueError(msg)
        key, value = (item.strip() for item in candidate.split(separator, maxsplit=1))
        if not key or not value:
            msg = "Each metadata line must include both key and value"
            raise ValueError(msg)
        parsed[normalize_private_metadata_key(key)] = value
    return parsed


def build_private_metadata_state_summary(
    *,
    raw_text: str,
    consent_to_ai_usage: bool,
) -> dict[str, object]:
    """Build a safe summary of the persisted private metadata section."""

    parse_error: str | None = None
    try:
        entries = parse_private_metadata_lines(raw_text)
    except ValueError as exc:
        entries = {}
        parse_error = str(exc)
    stored_keys = sorted(entries)
    stored_labels = [
        PRIVATE_METADATA_DISPLAY_LABELS.get(
            key,
            key.replace("_", " ").title(),
        )
        for key in stored_keys
    ]
    return {
        "consent_to_ai_usage": consent_to_ai_usage,
        "has_entries": bool(entries),
        "entry_count": len(entries),
        "stored_keys": stored_keys,
        "stored_labels": stored_labels,
        "ai_usage_warning": PRIVATE_METADATA_AI_USAGE_WARNING,
        "parse_error": parse_error,
    }


def build_missing_private_metadata_feedback(
    notes: Iterable[str | None],
    *,
    raw_text: str = "",
    consent_to_ai_usage: bool = False,
) -> dict[str, object]:
    """Aggregate recent skip notes into a user-facing missing-metadata summary."""

    current_state = build_private_metadata_state_summary(
        raw_text=raw_text,
        consent_to_ai_usage=consent_to_ai_usage,
    )
    stored_keys = set(cast(list[str], current_state["stored_keys"]))

    counts: Counter[str] = Counter()
    total_skipped = 0
    for note in notes:
        if (
            not note
            or "Required LinkedIn Easy Apply field could not be resolved safely" not in note
        ):
            continue
        match = _MISSING_PRIVATE_METADATA_NOTE_PATTERN.search(note)
        if match is None:
            continue
        normalized_key = normalize_private_metadata_key(match.group(1))
        counts[normalized_key] += 1
        total_skipped += 1

    if total_skipped == 0:
        return {
            "has_missing_fields": False,
            "skipped_submission_count": 0,
            "missing_fields": [],
            "configured_missing_fields": [],
            "missing_unconfigured_fields": [],
            "missing_field_count": 0,
            "configured_missing_field_count": 0,
            "missing_unconfigured_field_count": 0,
            "consent_required_for_ai_usage": False,
            "suggested_raw_text_template": "",
            "next_action": None,
            "message": None,
        }

    missing_fields = [
        {
            "key": key,
            "label": PRIVATE_METADATA_DISPLAY_LABELS.get(key, key.replace("_", " ").title()),
            "occurrences": occurrences,
            "is_configured": key in stored_keys,
        }
        for key, occurrences in counts.most_common()
    ]
    configured_missing_fields = [item for item in missing_fields if bool(item["is_configured"])]
    missing_unconfigured_fields = [
        item for item in missing_fields if not bool(item["is_configured"])
    ]
    suggested_raw_text_template = "\n".join(
        f"{item['label']}: " for item in missing_unconfigured_fields
    )
    consent_required_for_ai_usage = bool(configured_missing_fields) and not consent_to_ai_usage
    example_labels = ", ".join(str(item["label"]) for item in missing_fields[:4])
    if missing_unconfigured_fields and consent_required_for_ai_usage:
        next_action = (
            "Adicione os campos faltantes no private metadata e habilite o consentimento para "
            "uso com OpenAI se quiser que eu tente essas informacoes em futuras buscas."
        )
    elif missing_unconfigured_fields:
        next_action = (
            "Adicione os campos faltantes no private metadata se quiser que eu tente essas "
            "informacoes em futuras buscas."
        )
    elif consent_required_for_ai_usage:
        next_action = (
            "Os campos faltantes ja parecem cadastrados no private metadata, mas o "
            "consentimento para uso com OpenAI esta desativado."
        )
    else:
        next_action = (
            "Os campos faltantes ja estao cadastrados. Se o problema persistir, revise os "
            "valores salvos no private metadata."
        )
    message = (
        "Nao consegui aplicar em "
        f"{total_skipped} vaga(s) porque faltaram dados factuais que nao posso inferir com "
        f"seguranca. Exemplos: {example_labels}. {next_action} Cuidado: esses dados podem ser "
        "enviados para a OpenAI quando forem necessarios para responder formularios."
    )
    return {
        "has_missing_fields": True,
        "skipped_submission_count": total_skipped,
        "missing_fields": missing_fields,
        "configured_missing_fields": configured_missing_fields,
        "missing_unconfigured_fields": missing_unconfigured_fields,
        "missing_field_count": len(missing_fields),
        "configured_missing_field_count": len(configured_missing_fields),
        "missing_unconfigured_field_count": len(missing_unconfigured_fields),
        "consent_required_for_ai_usage": consent_required_for_ai_usage,
        "suggested_raw_text_template": suggested_raw_text_template,
        "next_action": next_action,
        "message": message,
    }


def parse_capability_override_json(raw: str) -> dict[str, CapabilityRangeInput]:
    """Parse a JSON object of reviewed capability overrides."""

    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = "Capability profile overrides must be valid JSON."
        raise ValueError(msg) from exc
    if not isinstance(payload, dict):
        msg = "Capability profile overrides must be a JSON object."
        raise ValueError(msg)

    parsed: dict[str, CapabilityRangeInput] = {}
    for capability, override_value in payload.items():
        if not isinstance(capability, str) or not capability.strip():
            msg = "Capability override keys must be non-empty strings."
            raise ValueError(msg)
        if not isinstance(override_value, dict):
            msg = "Each capability override must be an object."
            raise ValueError(msg)
        parsed[capability.strip()] = CapabilityRangeInput.model_validate(override_value)
    return parsed


def mapping_to_multiline(value: dict[str, Any]) -> str:
    """Render mapping values into multiline strings for the templates."""

    return "\n".join(f"{key}={item}" for key, item in value.items())


def tuple_to_csv(value: tuple[str, ...]) -> str:
    """Render tuple values as comma-separated strings for templates."""

    return ", ".join(value)


class StoredProfileSection(PanelModel):
    """Persisted user profile section."""

    name: str = ""
    email: EmailStr | None = None
    phone: str = ""
    city: str = ""
    linkedin_url: AnyUrl | None = None
    github_url: AnyUrl | None = None
    portfolio_url: AnyUrl | None = None
    years_experience_by_stack: dict[str, int] = Field(default_factory=dict)
    capability_overrides: dict[str, CapabilityRangeInput] = Field(default_factory=dict)
    work_authorized: bool = False
    needs_sponsorship: bool = False
    salary_expectation: int | None = None
    availability: str = ""
    default_responses: dict[str, str] = Field(default_factory=dict)
    cv_path: str | None = None
    cv_filename: str | None = None
    resume_mode: ResumeMode = ResumeMode.STATIC
    preferred_language: SupportedLanguage = SupportedLanguage.ENGLISH
    resume_css: str | None = None


class ProfileFormInput(BaseModel):
    """Validated profile payload coming from the panel."""

    name: str = Field(min_length=1)
    email: EmailStr
    phone: str = Field(min_length=1)
    city: str = Field(min_length=1)
    linkedin_url: AnyUrl | None = None
    github_url: AnyUrl | None = None
    portfolio_url: AnyUrl | None = None
    years_experience_by_stack: dict[str, int] = Field(default_factory=dict)
    capability_overrides: dict[str, CapabilityRangeInput] = Field(default_factory=dict)
    work_authorized: bool
    needs_sponsorship: bool = False
    salary_expectation: int | None = None
    availability: str = Field(min_length=1)
    default_responses: dict[str, str] = Field(default_factory=dict)
    resume_mode: ResumeMode = ResumeMode.STATIC
    preferred_language: SupportedLanguage = SupportedLanguage.ENGLISH
    resume_css: str | None = None


class StoredPreferencesSection(PanelModel):
    """Persisted search and preference section."""

    keywords: tuple[str, ...] = ()
    location: str = ""
    posted_within_hours: int = 24
    workplace_types: tuple[WorkplaceType, ...] = ()
    seniority: tuple[SeniorityLevel, ...] = ()
    easy_apply_only: bool = True
    minimum_score_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    positive_keywords: tuple[str, ...] = ()
    negative_keywords: tuple[str, ...] = ()
    auto_connect_with_recruiter: bool = True
    auto_send_job_email: bool = False


class PreferencesFormInput(BaseModel):
    """Validated preferences payload coming from the panel."""

    keywords: tuple[str, ...]
    location: str = Field(min_length=1)
    posted_within_hours: int = Field(ge=1, le=168)
    workplace_types: tuple[WorkplaceType, ...] = ()
    seniority: tuple[SeniorityLevel, ...] = ()
    easy_apply_only: bool = True
    minimum_score_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    positive_keywords: tuple[str, ...] = ()
    negative_keywords: tuple[str, ...] = ()
    auto_connect_with_recruiter: bool = True
    auto_send_job_email: bool = False


class StoredAISection(PanelModel):
    """Persisted AI section."""

    api_key: SecretStr | None = None
    model: str = "o3-mini"

    def masked_key(self) -> str | None:
        """Return a masked representation of the stored key."""

        if self.api_key is None:
            return None
        secret = self.api_key.get_secret_value()
        suffix = secret[-4:] if len(secret) >= 4 else secret
        return f"Configured (ends with {suffix})"


class AIFormInput(BaseModel):
    """Validated AI payload coming from the panel."""

    api_key: SecretStr | None = None
    model: str = "o3-mini"

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        """Keep the dropdown limited to supported options for the MVP panel."""

        if value not in MODEL_OPTIONS:
            msg = "Unsupported model option"
            raise ValueError(msg)
        return value

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        """Validate the basic format of OpenAI API keys when present."""

        if value is None:
            return value
        secret = value.get_secret_value().strip()
        if not secret:
            return None
        if not re.fullmatch(r"sk-[A-Za-z0-9._-]+", secret):
            msg = "API key must look like an OpenAI secret key"
            raise ValueError(msg)
        return SecretStr(secret)


class StoredPrivateMetadataSection(PanelModel):
    """Persisted private metadata separate from the CV and its canonical snapshot."""

    raw_text: str = ""
    consent_to_ai_usage: bool = False


class PrivateMetadataFormInput(BaseModel):
    """Validated sensitive metadata payload coming from the panel."""

    raw_text: str = ""
    consent_to_ai_usage: bool = False


class StoredScheduleSection(PanelModel):
    """Persisted schedule section."""

    frequency: ScheduleFrequency = ScheduleFrequency.DAILY
    run_at: str = "23:00"
    timezone: str = "UTC"


class ScheduleFormInput(BaseModel):
    """Validated schedule payload coming from the panel."""

    frequency: ScheduleFrequency = ScheduleFrequency.DAILY
    run_at: str = "23:00"
    timezone: str = "UTC"

    @field_validator("run_at")
    @classmethod
    def validate_run_at(cls, value: str) -> str:
        """Ensure the configured time uses `HH:MM` 24-hour format."""

        try:
            hour_text, minute_text = value.split(":", maxsplit=1)
            hour = int(hour_text)
            minute = int(minute_text)
        except ValueError as exc:
            msg = "Time must use HH:MM format"
            raise ValueError(msg) from exc

        if hour not in range(24) or minute not in range(60):
            msg = "Time must be a valid 24-hour value"
            raise ValueError(msg)
        return f"{hour:02d}:{minute:02d}"

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """Ensure the configured timezone exists."""

        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            msg = "Timezone must be a valid IANA timezone"
            raise ValueError(msg) from exc
        return value


class PanelSettingsDocument(PanelModel):
    """Full persisted panel document."""

    profile: StoredProfileSection = Field(default_factory=StoredProfileSection)
    preferences: StoredPreferencesSection = Field(default_factory=StoredPreferencesSection)
    ai: StoredAISection = Field(default_factory=StoredAISection)
    private_metadata: StoredPrivateMetadataSection = Field(
        default_factory=StoredPrivateMetadataSection
    )
    schedule: StoredScheduleSection = Field(default_factory=StoredScheduleSection)


class ResumeSourceSnapshotUpdateInput(BaseModel):
    """Validated payload used to persist a reviewed canonical resume snapshot."""

    snapshot: dict[str, Any]
    source_resume_language: SupportedLanguage | None = None


class PanelOverview(BaseModel):
    """Simple derived overview for the panel landing page."""

    profile_ready: bool
    preferences_ready: bool
    ai_ready: bool
    schedule_ready: bool

    @classmethod
    def from_document(cls, document: PanelSettingsDocument) -> PanelOverview:
        """Compute readiness indicators for the home page."""

        return cls(
            profile_ready=bool(document.profile.name and document.profile.email),
            preferences_ready=bool(document.preferences.keywords and document.preferences.location),
            ai_ready=document.ai.api_key is not None,
            schedule_ready=bool(document.schedule.run_at and document.schedule.timezone),
        )


def ensure_runtime_dir(path: Path) -> Path:
    """Create a runtime directory when needed."""

    path.mkdir(parents=True, exist_ok=True)
    return path


def calculate_next_execution_at(
    schedule: StoredScheduleSection,
    *,
    now_utc: datetime | None = None,
) -> datetime:
    """Return the next UTC timestamp for the configured daily schedule."""

    current_utc = now_utc or datetime.now(UTC)
    timezone = ZoneInfo(schedule.timezone)
    current_local = current_utc.astimezone(timezone)
    hour_text, minute_text = schedule.run_at.split(":", maxsplit=1)
    next_local = current_local.replace(
        hour=int(hour_text),
        minute=int(minute_text),
        second=0,
        microsecond=0,
    )
    if next_local <= current_local:
        next_local += timedelta(days=1)
    return next_local.astimezone(UTC)
