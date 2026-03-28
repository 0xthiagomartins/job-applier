"""LinkedIn automation adapters."""

from job_applier.infrastructure.linkedin.auth import (
    LinkedInAuthError,
    LinkedInCredentials,
    LinkedInSessionManager,
)
from job_applier.infrastructure.linkedin.search import (
    LINKEDIN_JOBS_URL,
    LinkedInCollectedJob,
    LinkedInJobFetcher,
    LinkedInJobParser,
    LinkedInSearchCriteria,
    LinkedInSearchError,
    PlaywrightLinkedInJobsClient,
    build_paginated_search_url,
    build_search_criteria,
    infer_seniority,
    infer_workplace_type,
)

__all__ = [
    "LINKEDIN_JOBS_URL",
    "LinkedInAuthError",
    "LinkedInCollectedJob",
    "LinkedInCredentials",
    "LinkedInJobFetcher",
    "LinkedInJobParser",
    "LinkedInSearchCriteria",
    "LinkedInSearchError",
    "LinkedInSessionManager",
    "PlaywrightLinkedInJobsClient",
    "build_paginated_search_url",
    "build_search_criteria",
    "infer_seniority",
    "infer_workplace_type",
]
