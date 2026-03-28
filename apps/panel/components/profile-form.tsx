"use client";

import { useEffect, useState, useTransition } from "react";

import { FeedbackBanner } from "@/components/feedback-banner";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { fetchPanelState, saveProfile } from "@/lib/api";

type FeedbackState = { kind: "success" | "error"; message: string } | null;

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
  });
  const [cvFile, setCvFile] = useState<File | null>(null);
  const [currentCvName, setCurrentCvName] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<FeedbackState>(null);
  const [isPending, startTransition] = useTransition();

  useEffect(() => {
    void fetchPanelState()
      .then((state) => {
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
        });
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

    startTransition(() => {
      void saveProfile(payload)
        .then((result) => {
          setFeedback({ kind: "success", message: result.message });
          if (cvFile) {
            setCurrentCvName(cvFile.name);
            setCvFile(null);
          }
        })
        .catch((error: Error) => {
          setFeedback({ kind: "error", message: error.message });
        });
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
            required
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
