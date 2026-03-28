export type ProfileSection = {
  name: string;
  email: string | null;
  phone: string;
  city: string;
  linkedin_url: string | null;
  github_url: string | null;
  portfolio_url: string | null;
  years_experience_by_stack: Record<string, number>;
  work_authorized: boolean;
  needs_sponsorship: boolean;
  salary_expectation: number | null;
  availability: string;
  default_responses: Record<string, string>;
  cv_path: string | null;
  cv_filename: string | null;
};

export type PreferencesSection = {
  keywords: string[];
  location: string;
  posted_within_hours: number;
  workplace_types: string[];
  seniority: string[];
  easy_apply_only: boolean;
  minimum_score_threshold: number;
  positive_keywords: string[];
  negative_keywords: string[];
  auto_connect_with_recruiter: boolean;
};

export type ScheduleSection = {
  frequency: string;
  run_at: string;
  timezone: string;
};

export type AISection = {
  model: string;
  has_api_key: boolean;
  masked_api_key: string | null;
};

export type ExecutionSummary = {
  execution_id: string;
  origin: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  snapshot_id: string | null;
  jobs_seen: number;
  jobs_selected: number;
  successful_submissions: number;
  error_count: number;
  last_error: string | null;
};

export type ApplicationHistoryListItem = {
  id: string;
  submitted_at: string;
  company_name: string;
  job_title: string;
  job_url: string;
  location: string | null;
  external_job_id: string | null;
  cv_version: string | null;
  execution_origin: string;
  notes: string | null;
};

export type ApplicationAnswer = {
  id: string;
  submission_id: string;
  step_index: number;
  question_raw: string;
  question_type: string;
  normalized_key: string;
  answer_raw: string;
  answer_source: string;
  ambiguity_flag: boolean;
  fill_strategy: string;
};

export type ArtifactSnapshot = {
  id: string;
  submission_id: string;
  artifact_type: string;
  path: string;
  sha256: string;
  created_at: string;
};

export type ExecutionEvent = {
  id: string;
  execution_id: string;
  submission_id: string | null;
  event_type: string;
  timestamp: string;
  payload_json: string;
  payload: Record<string, unknown>;
};

export type RecruiterInteraction = {
  id: string;
  submission_id: string;
  recruiter_name: string;
  recruiter_profile_url: string | null;
  action: string;
  message_sent: string | null;
  status: string;
  sent_at: string | null;
};

export type ProfileSnapshot = {
  id: string;
  created_at: string;
  data_json: string;
  data: Record<string, unknown>;
};

export type ApplicationHistoryDetail = {
  submission: {
    id: string;
    job_posting_id: string;
    status: string;
    started_at: string;
    submitted_at: string | null;
    cv_version: string | null;
    cover_letter_version: string | null;
    profile_snapshot_id: string | null;
    ruleset_version: string | null;
    ai_model_used: string | null;
    execution_origin: string;
    notes: string | null;
  };
  job_posting: {
    id: string;
    platform: string;
    external_job_id: string | null;
    url: string;
    title: string;
    company_name: string;
    location: string | null;
    workplace_type: string | null;
    seniority: string | null;
    easy_apply: boolean;
    description_raw: string;
    description_hash: string;
    captured_at: string;
  };
  answers: ApplicationAnswer[];
  profile_snapshot: ProfileSnapshot | null;
  recruiter_interactions: RecruiterInteraction[];
  execution_events: ExecutionEvent[];
  artifacts: ArtifactSnapshot[];
};

export type ApplicationHistoryPage = {
  items: ApplicationHistoryListItem[];
  total: number;
  limit: number;
  offset: number;
};

export type PanelState = {
  profile: ProfileSection;
  preferences: PreferencesSection;
  schedule: ScheduleSection;
  ai: AISection;
  options: {
    schedule_frequencies: string[];
    workplace_types: string[];
    seniority_levels: string[];
  };
};
