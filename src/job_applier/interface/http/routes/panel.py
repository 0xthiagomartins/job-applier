"""API routes for the MVP configuration UI."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import AnyUrl, SecretStr

from job_applier.application.panel import (
    AIFormInput,
    PreferencesFormInput,
    ProfileFormInput,
    parse_csv_lines,
    parse_int_mapping_lines,
    parse_text_mapping_lines,
)
from job_applier.domain.enums import SeniorityLevel, WorkplaceType
from job_applier.infrastructure.local_panel_store import LocalPanelSettingsStore
from job_applier.interface.http.dependencies import get_panel_settings_store

api_router = APIRouter(prefix="/api/panel", tags=["panel-api"])


@api_router.get("/profile")
async def get_profile(
    store: Annotated[LocalPanelSettingsStore, Depends(get_panel_settings_store)],
) -> JSONResponse:
    """Return the persisted profile section."""

    document = store.load()
    return JSONResponse(
        content={
            "profile": document.profile.model_dump(mode="json"),
            "cv_uploaded": bool(document.profile.cv_path),
        },
    )


@api_router.get("/state")
async def get_panel_state(
    store: Annotated[LocalPanelSettingsStore, Depends(get_panel_settings_store)],
) -> JSONResponse:
    """Return the safe combined state used by the Next.js panel."""

    document = store.load()
    return JSONResponse(
        content={
            "profile": document.profile.model_dump(mode="json"),
            "preferences": document.preferences.model_dump(mode="json"),
            "ai": {
                "model": document.ai.model,
                "has_api_key": document.ai.api_key is not None,
                "masked_api_key": document.ai.masked_key(),
            },
            "options": {
                "workplace_types": [option.value for option in WorkplaceType],
                "seniority_levels": [option.value for option in SeniorityLevel],
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
    linkedin_url: Annotated[AnyUrl, Form()],
    github_url: Annotated[AnyUrl | None, Form()] = None,
    portfolio_url: Annotated[AnyUrl | None, Form()] = None,
    years_experience_by_stack: Annotated[str, Form()] = "",
    work_authorized: Annotated[bool, Form()] = False,
    needs_sponsorship: Annotated[bool, Form()] = False,
    salary_expectation: Annotated[int | None, Form()] = None,
    availability: Annotated[str, Form()] = "",
    default_responses: Annotated[str, Form()] = "",
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
            default_responses=parse_text_mapping_lines(default_responses),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    document = store.save_profile(profile_input, cv_upload=cv_file)
    return JSONResponse(
        content={
            "message": "Profile saved successfully.",
            "profile": document.profile.model_dump(mode="json"),
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
    positive_keywords: Annotated[str, Form()] = "",
    negative_keywords: Annotated[str, Form()] = "",
    auto_connect_with_recruiter: Annotated[bool, Form()] = False,
) -> JSONResponse:
    """Persist search filters and preferences from the panel."""

    preferences_input = PreferencesFormInput(
        keywords=parse_csv_lines(keywords),
        location=location,
        posted_within_hours=posted_within_hours,
        workplace_types=tuple(workplace_types or ()),
        seniority=tuple(seniority or ()),
        easy_apply_only=easy_apply_only,
        positive_keywords=parse_csv_lines(positive_keywords),
        negative_keywords=parse_csv_lines(negative_keywords),
        auto_connect_with_recruiter=auto_connect_with_recruiter,
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
