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
