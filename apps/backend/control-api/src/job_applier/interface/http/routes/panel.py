"""API routes for the MVP configuration UI."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import AnyUrl, SecretStr

from job_applier.application.agent_execution import (
    PanelSettingsConfigurationError,
    build_user_agent_settings,
)
from job_applier.application.panel import (
    PRIVATE_METADATA_AI_USAGE_WARNING,
    SCHEDULE_FREQUENCY_OPTIONS,
    SUPPORTED_LANGUAGE_OPTIONS,
    TIMEZONE_OPTIONS,
    AIFormInput,
    EmploymentContextSummary,
    PreferencesFormInput,
    PrivateMetadataFormInput,
    ProfileFormInput,
    ResumeSourceSnapshotUpdateInput,
    ScheduleFormInput,
    StoredProfileSection,
    build_current_employer_feedback,
    build_employment_context_summary,
    build_missing_private_metadata_feedback,
    build_private_metadata_state_summary,
    calculate_next_execution_at,
    parse_capability_override_json,
    parse_csv_lines,
    parse_int_mapping_lines,
    parse_private_metadata_lines,
    parse_text_mapping_lines,
)
from job_applier.application.repositories import SubmissionRepository
from job_applier.domain.enums import (
    EmploymentStatus,
    ResumeMode,
    ScheduleFrequency,
    SeniorityLevel,
    SupportedLanguage,
    WorkplaceType,
)
from job_applier.infrastructure.candidate_capabilities import (
    build_candidate_capability_profile,
    capability_profile_to_payload,
)
from job_applier.infrastructure.local_panel_store import LocalPanelSettingsStore
from job_applier.infrastructure.resume_dynamic import (
    OhMyCvDynamicResumeBuilder,
    ResolvedResumeSourceSnapshot,
)
from job_applier.interface.http.dependencies import (
    get_panel_settings_store,
    get_resume_source_snapshot_repository,
    get_submission_repository,
)
from job_applier.settings import get_runtime_settings

api_router = APIRouter(prefix="/api/panel", tags=["panel-api"])


def _private_metadata_response(raw_text: str, consent_to_ai_usage: bool) -> dict[str, object]:
    parse_error: str | None = None
    try:
        entries = parse_private_metadata_lines(raw_text)
    except ValueError as exc:
        entries = {}
        parse_error = str(exc)
    return {
        "raw_text": raw_text,
        "consent_to_ai_usage": consent_to_ai_usage,
        "has_entries": bool(entries),
        "stored_keys": sorted(entries),
        "ai_usage_warning": PRIVATE_METADATA_AI_USAGE_WARNING,
        "parse_error": parse_error,
    }


def _private_metadata_state_summary(raw_text: str, consent_to_ai_usage: bool) -> dict[str, object]:
    return build_private_metadata_state_summary(
        raw_text=raw_text,
        consent_to_ai_usage=consent_to_ai_usage,
    )


def _employment_context_summary(profile: StoredProfileSection) -> dict[str, object]:
    return build_employment_context_summary(
        current_employer=profile.current_employer,
        employment_status=profile.employment_status,
        cv_path=profile.cv_path,
    ).model_dump(mode="json")


def _resume_source_snapshot_response(
    snapshot: ResolvedResumeSourceSnapshot | None,
) -> dict[str, object]:
    if snapshot is None:
        return {
            "snapshot_available": False,
            "snapshot": None,
        }
    return {
        "snapshot_available": True,
        "owner_key": snapshot.owner_key,
        "cv_sha256": snapshot.cv_sha256,
        "source_cv_path": str(snapshot.source_cv_path),
        "source_cv_filename": snapshot.source_cv_filename,
        "source_resume_language": snapshot.source_resume_language.value,
        "snapshot_schema_version": snapshot.snapshot_schema_version,
        "snapshot_origin": snapshot.snapshot_origin,
        "user_edited": snapshot.user_edited,
        "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
        "updated_at": snapshot.updated_at.isoformat() if snapshot.updated_at else None,
        "snapshot": {
            "header_role": snapshot.snapshot.header_role,
            "summary": snapshot.snapshot.summary,
            "experience_entries": [
                {
                    "title": entry.title,
                    "company_name": entry.company_name,
                    "date_range": entry.date_range,
                    "bullets": list(entry.bullets),
                }
                for entry in snapshot.snapshot.experience_entries
            ],
            "certifications": [
                {"name": entry.name, "issuer": entry.issuer}
                for entry in snapshot.snapshot.certifications
            ],
            "education_entries": [
                {
                    "institution": entry.institution,
                    "degree": entry.degree,
                    "location": entry.location,
                    "date_range": entry.date_range,
                }
                for entry in snapshot.snapshot.education_entries
            ],
            "skill_lines": list(snapshot.snapshot.skill_lines),
            "additional_sections": [
                {"title": title, "lines": list(lines)}
                for title, lines in snapshot.snapshot.additional_sections
            ],
            "word_count": snapshot.snapshot.word_count,
            "phone": snapshot.snapshot.phone,
            "email": snapshot.snapshot.email,
            "city": snapshot.snapshot.city,
            "portfolio_hint": snapshot.snapshot.portfolio_hint,
        },
    }


def _build_resume_snapshot_service() -> OhMyCvDynamicResumeBuilder:
    return OhMyCvDynamicResumeBuilder(
        get_runtime_settings(),
        resume_source_snapshot_repository=get_resume_source_snapshot_repository(),
    )


@api_router.get("/profile")
async def get_profile(
    store: Annotated[LocalPanelSettingsStore, Depends(get_panel_settings_store)],
) -> JSONResponse:
    """Return the persisted profile section."""

    document = store.load()
    return JSONResponse(
        content={
            "profile": document.profile.model_dump(mode="json"),
            "employment_context": _employment_context_summary(document.profile),
            "cv_uploaded": bool(document.profile.cv_path),
        },
    )


@api_router.get("/private-metadata")
async def get_private_metadata(
    store: Annotated[LocalPanelSettingsStore, Depends(get_panel_settings_store)],
) -> JSONResponse:
    """Return the persisted private metadata section."""

    document = store.load()
    return JSONResponse(
        content={
            "private_metadata": _private_metadata_response(
                document.private_metadata.raw_text,
                document.private_metadata.consent_to_ai_usage,
            )
        }
    )


@api_router.put("/private-metadata")
async def save_private_metadata(
    payload: PrivateMetadataFormInput,
    store: Annotated[LocalPanelSettingsStore, Depends(get_panel_settings_store)],
) -> JSONResponse:
    """Persist user-provided private metadata used by future Easy Apply runs."""

    try:
        parse_private_metadata_lines(payload.raw_text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    document = store.save_private_metadata(payload)
    return JSONResponse(
        content={
            "message": "Private metadata saved successfully.",
            "private_metadata": _private_metadata_response(
                document.private_metadata.raw_text,
                document.private_metadata.consent_to_ai_usage,
            ),
        }
    )


@api_router.get("/resume-source-snapshot")
async def get_resume_source_snapshot(
    store: Annotated[LocalPanelSettingsStore, Depends(get_panel_settings_store)],
) -> JSONResponse:
    """Return the current persisted canonical snapshot for the uploaded base CV."""

    document = store.load()
    try:
        settings = build_user_agent_settings(document)
    except PanelSettingsConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    snapshot = _build_resume_snapshot_service().get_or_create_source_snapshot(settings=settings)
    return JSONResponse(content=_resume_source_snapshot_response(snapshot))


@api_router.post("/resume-source-snapshot/refresh")
async def refresh_resume_source_snapshot(
    store: Annotated[LocalPanelSettingsStore, Depends(get_panel_settings_store)],
) -> JSONResponse:
    """Force a rebuild of the persisted canonical snapshot from the current base CV."""

    document = store.load()
    try:
        settings = build_user_agent_settings(document)
    except PanelSettingsConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    snapshot = _build_resume_snapshot_service().refresh_source_snapshot(settings=settings)
    return JSONResponse(content=_resume_source_snapshot_response(snapshot))


@api_router.put("/resume-source-snapshot")
async def save_resume_source_snapshot(
    payload: ResumeSourceSnapshotUpdateInput,
    store: Annotated[LocalPanelSettingsStore, Depends(get_panel_settings_store)],
) -> JSONResponse:
    """Persist a reviewed canonical snapshot override for the current base CV."""

    document = store.load()
    try:
        settings = build_user_agent_settings(document)
    except PanelSettingsConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        snapshot = _build_resume_snapshot_service().update_source_snapshot(
            settings=settings,
            snapshot_payload=payload.snapshot,
            source_resume_language=payload.source_resume_language,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(content=_resume_source_snapshot_response(snapshot))


@api_router.get("/state")
async def get_panel_state(
    store: Annotated[LocalPanelSettingsStore, Depends(get_panel_settings_store)],
    submission_repository: Annotated[SubmissionRepository, Depends(get_submission_repository)],
) -> JSONResponse:
    """Return the safe combined state used by the Next.js panel."""

    document = store.load()
    skipped_notes = [
        submission.notes
        for submission in submission_repository.list(limit=200)
        if submission.status.value == "skipped"
    ]
    private_metadata_summary = _private_metadata_state_summary(
        document.private_metadata.raw_text,
        document.private_metadata.consent_to_ai_usage,
    )
    missing_private_metadata_feedback = build_missing_private_metadata_feedback(
        skipped_notes,
        raw_text=document.private_metadata.raw_text,
        consent_to_ai_usage=document.private_metadata.consent_to_ai_usage,
    )
    current_employment_context = EmploymentContextSummary.model_validate(
        _employment_context_summary(document.profile)
    )
    current_employer_feedback = build_current_employer_feedback(
        skipped_notes,
        employment_context=current_employment_context,
    )
    capability_profile_payload: dict[str, object] | None = None
    try:
        settings = build_user_agent_settings(document)
    except PanelSettingsConfigurationError:
        capability_profile_payload = None
    else:
        capability_profile_payload = capability_profile_to_payload(
            build_candidate_capability_profile(settings)
        )
    return JSONResponse(
        content={
            "profile": document.profile.model_dump(mode="json"),
            "preferences": document.preferences.model_dump(mode="json"),
            "schedule": document.schedule.model_dump(mode="json"),
            "computed": {
                "next_execution_at": calculate_next_execution_at(document.schedule).isoformat(),
                "capability_profile": capability_profile_payload,
                "employment_context": current_employment_context.model_dump(mode="json"),
            },
            "private_metadata": private_metadata_summary,
            "feedback": {
                "missing_private_metadata": missing_private_metadata_feedback,
                "current_employer": current_employer_feedback,
            },
            "ai": {
                "model": document.ai.model,
                "has_api_key": document.ai.api_key is not None,
                "masked_api_key": document.ai.masked_key(),
            },
            "options": {
                "schedule_frequencies": [option.value for option in SCHEDULE_FREQUENCY_OPTIONS],
                "timezones": list(TIMEZONE_OPTIONS),
                "workplace_types": [option.value for option in WorkplaceType],
                "seniority_levels": [option.value for option in SeniorityLevel],
                "supported_languages": [option.value for option in SUPPORTED_LANGUAGE_OPTIONS],
            },
        },
    )


@api_router.post("/profile")
async def save_profile(
    store: Annotated[LocalPanelSettingsStore, Depends(get_panel_settings_store)],
    name: Annotated[str, Form()],
    email: Annotated[str, Form()],
    phone: Annotated[str, Form()],
    city: Annotated[str, Form()],
    linkedin_url: Annotated[AnyUrl | None, Form()] = None,
    github_url: Annotated[AnyUrl | None, Form()] = None,
    portfolio_url: Annotated[AnyUrl | None, Form()] = None,
    years_experience_by_stack: Annotated[str, Form()] = "",
    work_authorized: Annotated[bool, Form()] = False,
    needs_sponsorship: Annotated[bool, Form()] = False,
    salary_expectation: Annotated[int | None, Form()] = None,
    availability: Annotated[str, Form()] = "",
    employment_status: Annotated[EmploymentStatus, Form()] = EmploymentStatus.UNKNOWN,
    current_employer: Annotated[str, Form()] = "",
    default_responses: Annotated[str, Form()] = "",
    capability_overrides: Annotated[str, Form()] = "",
    resume_mode: Annotated[ResumeMode, Form()] = ResumeMode.STATIC,
    preferred_language: Annotated[SupportedLanguage, Form()] = SupportedLanguage.ENGLISH,
    resume_css: Annotated[str, Form()] = "",
    cv_file: Annotated[UploadFile | None, File()] = None,
) -> JSONResponse:
    """Persist the profile section from a multipart form."""

    try:
        profile_input = ProfileFormInput(
            name=name,
            email=email,
            phone=phone,
            city=city,
            linkedin_url=linkedin_url,
            github_url=github_url,
            portfolio_url=portfolio_url,
            years_experience_by_stack=parse_int_mapping_lines(years_experience_by_stack),
            work_authorized=work_authorized,
            needs_sponsorship=needs_sponsorship,
            salary_expectation=salary_expectation,
            availability=availability,
            employment_status=employment_status,
            current_employer=current_employer.strip() or None,
            default_responses=parse_text_mapping_lines(default_responses),
            capability_overrides=parse_capability_override_json(capability_overrides),
            resume_mode=resume_mode,
            preferred_language=preferred_language,
            resume_css=resume_css.strip() or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    document = store.save_profile(profile_input, cv_upload=cv_file)
    return JSONResponse(
        content={
            "message": "Profile saved successfully.",
            "profile": document.profile.model_dump(mode="json"),
            "employment_context": _employment_context_summary(document.profile),
            "cv_uploaded": bool(document.profile.cv_path),
        },
    )


@api_router.get("/preferences")
async def get_preferences(
    store: Annotated[LocalPanelSettingsStore, Depends(get_panel_settings_store)],
) -> JSONResponse:
    """Return the persisted preferences section."""

    document = store.load()
    return JSONResponse(content={"preferences": document.preferences.model_dump(mode="json")})


@api_router.put("/preferences")
async def save_preferences(
    store: Annotated[LocalPanelSettingsStore, Depends(get_panel_settings_store)],
    keywords: Annotated[str, Form()],
    location: Annotated[str, Form()],
    posted_within_hours: Annotated[int, Form()],
    workplace_types: Annotated[list[WorkplaceType] | None, Form()] = None,
    seniority: Annotated[list[SeniorityLevel] | None, Form()] = None,
    easy_apply_only: Annotated[bool, Form()] = False,
    minimum_score_threshold: Annotated[float, Form()] = 0.55,
    positive_keywords: Annotated[str, Form()] = "",
    negative_keywords: Annotated[str, Form()] = "",
    auto_connect_with_recruiter: Annotated[bool, Form()] = False,
    auto_send_job_email: Annotated[bool, Form()] = False,
) -> JSONResponse:
    """Persist search filters and preferences from the panel."""

    preferences_input = PreferencesFormInput(
        keywords=parse_csv_lines(keywords),
        location=location,
        posted_within_hours=posted_within_hours,
        workplace_types=tuple(workplace_types or ()),
        seniority=tuple(seniority or ()),
        easy_apply_only=easy_apply_only,
        minimum_score_threshold=minimum_score_threshold,
        positive_keywords=parse_csv_lines(positive_keywords),
        negative_keywords=parse_csv_lines(negative_keywords),
        auto_connect_with_recruiter=auto_connect_with_recruiter,
        auto_send_job_email=auto_send_job_email,
    )
    document = store.save_preferences(preferences_input)
    return JSONResponse(
        content={
            "message": "Preferences saved successfully.",
            "preferences": document.preferences.model_dump(mode="json"),
        },
    )


@api_router.get("/ai")
async def get_ai(
    store: Annotated[LocalPanelSettingsStore, Depends(get_panel_settings_store)],
) -> JSONResponse:
    """Return the safe AI section payload."""

    document = store.load()
    return JSONResponse(
        content={
            "ai": {
                "model": document.ai.model,
                "has_api_key": document.ai.api_key is not None,
                "masked_api_key": document.ai.masked_key(),
            },
        },
    )


@api_router.get("/schedule")
async def get_schedule(
    store: Annotated[LocalPanelSettingsStore, Depends(get_panel_settings_store)],
) -> JSONResponse:
    """Return the persisted schedule section."""

    document = store.load()
    return JSONResponse(
        content={
            "schedule": document.schedule.model_dump(mode="json"),
            "computed": {
                "next_execution_at": calculate_next_execution_at(document.schedule).isoformat(),
            },
        },
    )


@api_router.put("/schedule")
async def save_schedule(
    store: Annotated[LocalPanelSettingsStore, Depends(get_panel_settings_store)],
    frequency: Annotated[ScheduleFrequency, Form()] = ScheduleFrequency.DAILY,
    run_at: Annotated[str, Form()] = "23:00",
    timezone: Annotated[str, Form()] = "UTC",
) -> JSONResponse:
    """Persist scheduler configuration from the panel."""

    schedule_input = ScheduleFormInput(
        frequency=frequency,
        run_at=run_at,
        timezone=timezone,
    )
    document = store.save_schedule(schedule_input)
    return JSONResponse(
        content={
            "message": "Schedule saved successfully.",
            "schedule": document.schedule.model_dump(mode="json"),
            "computed": {
                "next_execution_at": calculate_next_execution_at(document.schedule).isoformat(),
            },
        },
    )


@api_router.put("/ai")
async def save_ai(
    store: Annotated[LocalPanelSettingsStore, Depends(get_panel_settings_store)],
    api_key: Annotated[str, Form()] = "",
    model: Annotated[str, Form()] = "o3-mini",
) -> JSONResponse:
    """Persist AI configuration from the panel."""

    ai_input = AIFormInput(
        api_key=SecretStr(api_key) if api_key else None,
        model=model,
    )
    document = store.save_ai(ai_input)
    return JSONResponse(
        content={
            "message": "AI settings saved successfully.",
            "ai": {
                "model": document.ai.model,
                "has_api_key": document.ai.api_key is not None,
                "masked_api_key": document.ai.masked_key(),
            },
        },
    )
