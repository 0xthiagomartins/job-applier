"use client";

import { useEffect, useState, useTransition } from "react";

import { FeedbackBanner } from "@/components/feedback-banner";
import { CapabilityOverride, CapabilityProfileItem, PanelState } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { fetchPanelState, saveProfile } from "@/lib/api";

type FeedbackState = { kind: "success" | "error"; message: string } | null;

function defaultOverride(item: CapabilityProfileItem): CapabilityOverride {
  return {
    min_years: item.min_years,
    max_years: item.max_years,
    recommended_years: item.recommended_years,
    enabled: true,
  };
}

function normalizeOverrideValue(
  draft: CapabilityOverride,
  item: CapabilityProfileItem,
): CapabilityOverride {
  const minYears = Math.max(0, draft.min_years);
  const maxYears = Math.max(minYears, draft.max_years);
  const recommendedYears = draft.recommended_years === null
    ? maxYears
    : Math.min(maxYears, Math.max(minYears, draft.recommended_years));
  const fallback = defaultOverride(item);
  return {
    min_years: Number.isFinite(minYears) ? minYears : fallback.min_years,
    max_years: Number.isFinite(maxYears) ? maxYears : fallback.max_years,
    recommended_years: Number.isFinite(recommendedYears)
      ? recommendedYears
      : fallback.recommended_years,
    enabled: draft.enabled,
  };
}

function overridesEqual(left: CapabilityOverride, right: CapabilityOverride): boolean {
  return (
    left.min_years === right.min_years &&
    left.max_years === right.max_years &&
    left.recommended_years === right.recommended_years &&
    left.enabled === right.enabled
  );
}

export function ProfileForm(): React.JSX.Element {
  const [form, setForm] = useState({
    name: "",
    email: "",
    phone: "",
    city: "",
    linkedin_url: "",
    github_url: "",
    portfolio_url: "",
    years_experience_by_stack: "",
    work_authorized: false,
    needs_sponsorship: false,
    salary_expectation: "",
    availability: "",
    default_responses: "",
    resume_mode: "static",
    preferred_language: "en",
    resume_css: "",
  });
  const [panelState, setPanelState] = useState<PanelState | null>(null);
  const [capabilityOverrides, setCapabilityOverrides] = useState<
    Record<string, CapabilityOverride>
  >({});
  const [cvFile, setCvFile] = useState<File | null>(null);
  const [currentCvName, setCurrentCvName] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<FeedbackState>(null);
  const [isPending, startTransition] = useTransition();

  useEffect(() => {
    void fetchPanelState()
      .then((state) => {
        setPanelState(state);
        setForm({
          name: state.profile.name ?? "",
          email: state.profile.email ?? "",
          phone: state.profile.phone ?? "",
          city: state.profile.city ?? "",
          linkedin_url: state.profile.linkedin_url ?? "",
          github_url: state.profile.github_url ?? "",
          portfolio_url: state.profile.portfolio_url ?? "",
          years_experience_by_stack: Object.entries(state.profile.years_experience_by_stack)
            .map(([key, value]) => `${key}=${value}`)
            .join("\n"),
          work_authorized: state.profile.work_authorized,
          needs_sponsorship: state.profile.needs_sponsorship,
          salary_expectation: state.profile.salary_expectation?.toString() ?? "",
          availability: state.profile.availability ?? "",
          default_responses: Object.entries(state.profile.default_responses)
            .map(([key, value]) => `${key}=${value}`)
            .join("\n"),
          resume_mode: state.profile.resume_mode ?? "static",
          preferred_language: state.profile.preferred_language ?? "en",
          resume_css: state.profile.resume_css ?? "",
        });
        setCapabilityOverrides(state.profile.capability_overrides ?? {});
        setCurrentCvName(state.profile.cv_filename);
      })
      .catch((error: Error) => {
        setFeedback({ kind: "error", message: error.message });
      });
  }, []);

  function updateField(name: string, value: string | boolean): void {
    setForm((current) => ({ ...current, [name]: value }));
  }

  function handleSubmit(event: React.FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    setFeedback(null);

    const payload = new FormData();
    Object.entries(form).forEach(([key, value]) => {
      if (typeof value === "boolean") {
        if (value) {
          payload.append(key, "true");
        }
        return;
      }
      payload.append(key, value);
    });
    if (cvFile) {
      payload.append("cv_file", cvFile);
    }
    payload.append("capability_overrides", JSON.stringify(capabilityOverrides));

    startTransition(() => {
      void saveProfile(payload)
        .then((result) => {
          setFeedback({ kind: "success", message: result.message });
          if (cvFile) {
            setCurrentCvName(cvFile.name);
            setCvFile(null);
          }
          return fetchPanelState();
        })
        .then((state) => {
          if (!state) {
            return;
          }
          setPanelState(state);
          setCapabilityOverrides(state.profile.capability_overrides ?? {});
        })
        .catch((error: Error) => {
          setFeedback({ kind: "error", message: error.message });
        });
    });
  }

  const capabilityItems = panelState?.computed.capability_profile?.capabilities ?? [];

  function displayedOverride(item: CapabilityProfileItem): CapabilityOverride {
    const existing = capabilityOverrides[item.capability];
    return normalizeOverrideValue(existing ?? defaultOverride(item), item);
  }

  function updateCapabilityOverride(
    item: CapabilityProfileItem,
    patch: Partial<CapabilityOverride>,
  ): void {
    setCapabilityOverrides((current) => {
      const next = normalizeOverrideValue(
        {
          ...displayedOverride(item),
          ...patch,
        },
        item,
      );
      const fallback = defaultOverride(item);
      if (overridesEqual(next, fallback)) {
        const { [item.capability]: _removed, ...rest } = current;
        return rest;
      }
      return { ...current, [item.capability]: next };
    });
  }

  function resetCapabilityOverride(item: CapabilityProfileItem): void {
    setCapabilityOverrides((current) => {
      const { [item.capability]: _removed, ...rest } = current;
      return rest;
    });
  }

  return (
    <form className="space-y-6" onSubmit={handleSubmit}>
      <div className="grid gap-5 md:grid-cols-2">
        <Field label="Name">
          <Input required value={form.name} onChange={(event) => updateField("name", event.target.value)} />
        </Field>
        <Field label="E-mail">
          <Input
            required
            type="email"
            value={form.email}
            onChange={(event) => updateField("email", event.target.value)}
          />
        </Field>
        <Field label="Phone">
          <Input required value={form.phone} onChange={(event) => updateField("phone", event.target.value)} />
        </Field>
        <Field label="City">
          <Input required value={form.city} onChange={(event) => updateField("city", event.target.value)} />
        </Field>
        <Field label="LinkedIn URL">
          <Input
            type="url"
            value={form.linkedin_url}
            onChange={(event) => updateField("linkedin_url", event.target.value)}
          />
        </Field>
        <Field label="GitHub URL">
          <Input
            type="url"
            value={form.github_url}
            onChange={(event) => updateField("github_url", event.target.value)}
          />
        </Field>
        <Field className="md:col-span-2" label="Portfolio URL">
          <Input
            type="url"
            value={form.portfolio_url}
            onChange={(event) => updateField("portfolio_url", event.target.value)}
          />
        </Field>
        <Field className="md:col-span-2" label="Experience by stack">
          <Textarea
            placeholder={"python=8\nfastapi=4"}
            value={form.years_experience_by_stack}
            onChange={(event) => updateField("years_experience_by_stack", event.target.value)}
          />
        </Field>
        <Field label="Salary expectation">
          <Input
            type="number"
            value={form.salary_expectation}
            onChange={(event) => updateField("salary_expectation", event.target.value)}
          />
        </Field>
        <Field label="Availability / notice period">
          <Input
            required
            value={form.availability}
            onChange={(event) => updateField("availability", event.target.value)}
          />
        </Field>
        <Field className="md:col-span-2" label="Reusable default responses">
          <Textarea
            placeholder={"work_authorization=Yes\nvisa_sponsorship=No"}
            value={form.default_responses}
            onChange={(event) => updateField("default_responses", event.target.value)}
          />
        </Field>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <ToggleCard
          checked={form.work_authorized}
          description="Use this as the default answer for work authorization questions."
          label="Work authorized"
          onCheckedChange={(checked) => updateField("work_authorized", checked)}
        />
        <ToggleCard
          checked={form.needs_sponsorship}
          description="Enable this if the candidate needs visa sponsorship."
          label="Needs sponsorship"
          onCheckedChange={(checked) => updateField("needs_sponsorship", checked)}
        />
      </div>

      <Card className="bg-white/70">
        <CardContent className="space-y-3 pt-6">
          <Field label="CV upload">
            <Input
              accept=".pdf,.doc,.docx"
              type="file"
              onChange={(event) => setCvFile(event.target.files?.[0] ?? null)}
            />
          </Field>
          {currentCvName ? <p className="text-sm text-muted-foreground">Current file: {currentCvName}</p> : null}
          <Field label="Resume mode">
            <select
              className="flex h-11 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
              value={form.resume_mode}
              onChange={(event) => updateField("resume_mode", event.target.value)}
            >
              <option value="static">Static: upload the base CV exactly as provided</option>
              <option value="dynamic">Dynamic: tailor a CV variant per matched vacancy</option>
            </select>
          </Field>
          <Field label="Default content language">
            <select
              className="flex h-11 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
              value={form.preferred_language}
              onChange={(event) => updateField("preferred_language", event.target.value)}
            >
              {(panelState?.options.supported_languages ?? ["en", "pt"]).map((language) => (
                <option key={language} value={language}>
                  {language === "pt" ? "Portuguese" : "English"}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Resume CSS override">
            <Textarea
              placeholder="#resume-preview h2 { color: #4f8d34; }"
              rows={10}
              value={form.resume_css}
              onChange={(event) => updateField("resume_css", event.target.value)}
            />
          </Field>
        </CardContent>
      </Card>

      <Card className="bg-white/70">
        <CardContent className="space-y-4 pt-6">
          <div className="space-y-1">
            <Label>Capability profile</Label>
            <p className="text-sm text-muted-foreground">
              These ranges are inferred from the base CV and used for screening answers like
              years of experience. Exact values from “Experience by stack” still win when both
              exist.
            </p>
            <p className="text-sm text-muted-foreground">
              Dynamic resumes default to this language when the job language is weak or ambiguous.
              Static resumes always use the uploaded file unchanged.
            </p>
          </div>
          {panelState?.computed.capability_profile ? (
            <div className="space-y-3">
              <div className="rounded-xl bg-emerald-50 px-4 py-3 text-sm text-emerald-900">
                Total career span inferred from the CV:{" "}
                <strong>{panelState.computed.capability_profile.total_career_years} years</strong>
              </div>
              <div className="space-y-3">
                {capabilityItems.map((item) => {
                  const draft = displayedOverride(item);
                  const isReviewed = Boolean(capabilityOverrides[item.capability]);
                  return (
                    <div
                      key={item.capability}
                      className="space-y-3 rounded-2xl border border-emerald-100 bg-white/90 p-4"
                    >
                      <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
                        <div>
                          <p className="font-medium capitalize">{item.capability}</p>
                          <p className="text-sm text-muted-foreground">
                            Source: {item.source.replaceAll("_", " ")} · Confidence:{" "}
                            {Math.round(item.confidence * 100)}%
                          </p>
                        </div>
                        <div className="text-sm text-muted-foreground">
                          {isReviewed ? "Reviewed override active" : "Using inferred default"}
                        </div>
                      </div>
                      <div className="grid gap-3 md:grid-cols-4">
                        <Field label="Min years">
                          <Input
                            min={0}
                            type="number"
                            value={draft.min_years}
                            onChange={(event) =>
                              updateCapabilityOverride(item, {
                                min_years: Number(event.target.value || 0),
                              })
                            }
                          />
                        </Field>
                        <Field label="Max years">
                          <Input
                            min={0}
                            type="number"
                            value={draft.max_years}
                            onChange={(event) =>
                              updateCapabilityOverride(item, {
                                max_years: Number(event.target.value || 0),
                              })
                            }
                          />
                        </Field>
                        <Field label="Recommended years">
                          <Input
                            min={0}
                            type="number"
                            value={draft.recommended_years ?? ""}
                            onChange={(event) =>
                              updateCapabilityOverride(item, {
                                recommended_years: Number(event.target.value || 0),
                              })
                            }
                          />
                        </Field>
                        <ToggleCard
                          checked={draft.enabled}
                          description="Disable this capability if the inferred range feels misleading."
                          label="Use for screening"
                          onCheckedChange={(checked) =>
                            updateCapabilityOverride(item, { enabled: checked })
                          }
                        />
                      </div>
                      <div className="flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
                        <span>
                          Inferred default: {item.min_years}-{item.max_years} years, recommended{" "}
                          {item.recommended_years}
                        </span>
                        {isReviewed ? (
                          <button
                            className="font-medium text-emerald-700 underline-offset-4 hover:underline"
                            type="button"
                            onClick={() => resetCapabilityOverride(item)}
                          >
                            Revert to inferred default
                          </button>
                        ) : null}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">
              Upload a base CV and save the profile once to generate the inferred capability
              profile.
            </p>
          )}
        </CardContent>
      </Card>

      <div className="space-y-4">
        {feedback ? <FeedbackBanner kind={feedback.kind} message={feedback.message} /> : null}
        <Button disabled={isPending} type="submit">
          {isPending ? "Saving profile..." : "Save profile"}
        </Button>
      </div>
    </form>
  );
}

function Field({
  children,
  className,
  label,
}: {
  children: React.ReactNode;
  className?: string;
  label: string;
}): React.JSX.Element {
  return (
    <div className={className}>
      <div className="space-y-2">
        <Label>{label}</Label>
        {children}
      </div>
    </div>
  );
}

function ToggleCard({
  checked,
  description,
  label,
  onCheckedChange,
}: {
  checked: boolean;
  description: string;
  label: string;
  onCheckedChange: (checked: boolean) => void;
}): React.JSX.Element {
  return (
    <Card className="bg-white/70">
      <CardContent className="flex items-start justify-between gap-4 pt-6">
        <div className="space-y-2">
          <Label>{label}</Label>
          <p className="text-sm leading-6 text-muted-foreground">{description}</p>
        </div>
        <Checkbox checked={checked} onCheckedChange={(value) => onCheckedChange(value === true)} />
      </CardContent>
    </Card>
  );
}
