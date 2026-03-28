"""API routes for successful application history."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from job_applier.application.history import SubmissionHistoryFilters
from job_applier.application.repositories import SubmissionHistoryRepository
from job_applier.application.schemas import (
    ApplicationHistoryDetailEnvelope,
    ApplicationHistoryDetailRead,
    ApplicationHistoryPageRead,
)
from job_applier.interface.http.dependencies import get_submission_history_repository

api_router = APIRouter(prefix="/api/applications", tags=["applications-api"])


@api_router.get("", response_model=ApplicationHistoryPageRead)
async def list_applications(
    history_repository: Annotated[
        SubmissionHistoryRepository,
        Depends(get_submission_history_repository),
    ],
    company: Annotated[str | None, Query()] = None,
    title: Annotated[str | None, Query()] = None,
    submitted_from: Annotated[date | None, Query()] = None,
    submitted_to: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ApplicationHistoryPageRead:
    """Return paginated successful applications filtered by business criteria."""

    page = history_repository.query(
        SubmissionHistoryFilters(
            company_name=company.strip() if company else None,
            title=title.strip() if title else None,
            submitted_from=_date_start(submitted_from),
            submitted_to=_date_end(submitted_to),
            limit=limit,
            offset=offset,
        ),
    )
    return ApplicationHistoryPageRead.from_page(page)


@api_router.get("/{submission_id}", response_model=ApplicationHistoryDetailEnvelope)
async def get_application_detail(
    submission_id: UUID,
    history_repository: Annotated[
        SubmissionHistoryRepository,
        Depends(get_submission_history_repository),
    ],
) -> ApplicationHistoryDetailEnvelope:
    """Return the full audit view for one successful application."""

    entry = history_repository.get(submission_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Application not found.") from None
    return ApplicationHistoryDetailEnvelope(
        application=ApplicationHistoryDetailRead.from_entry(entry),
    )


def _date_start(value: date | None) -> datetime | None:
    if value is None:
        return None
    return datetime.combine(value, time.min, tzinfo=UTC)


def _date_end(value: date | None) -> datetime | None:
    if value is None:
        return None
    return datetime.combine(value, time.max, tzinfo=UTC)
