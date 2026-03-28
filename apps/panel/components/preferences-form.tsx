"use client";

import { useEffect, useState, useTransition } from "react";

import { FeedbackBanner } from "@/components/feedback-banner";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { fetchPanelState, savePreferences } from "@/lib/api";
import { PanelState } from "@/lib/types";

type FeedbackState = { kind: "success" | "error"; message: string } | null;

export function PreferencesForm(): React.JSX.Element {
  const [state, setState] = useState<PanelState | null>(null);
  const [feedback, setFeedback] = useState<FeedbackState>(null);
  const [isPending, startTransition] = useTransition();

  useEffect(() => {
    void fetchPanelState()
      .then(setState)
      .catch((error: Error) => setFeedback({ kind: "error", message: error.message }));
  }, []);

  function updateField<K extends keyof PanelState["preferences"]>(
    field: K,
    value: PanelState["preferences"][K],
  ): void {
    setState((current) =>
      current
        ? {
            ...current,
            preferences: {
              ...current.preferences,
              [field]: value,
            },
          }
        : current,
    );
  }

  function toggleFromArray(field: "workplace_types" | "seniority", value: string): void {
    if (!state) {
      return;
    }
    const currentValues = state.preferences[field];
    const nextValues = currentValues.includes(value)
      ? currentValues.filter((item) => item !== value)
      : [...currentValues, value];
    updateField(field, nextValues);
  }

  function handleSubmit(event: React.FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (!state) {
      return;
    }

    const payload = new FormData();
    payload.append("keywords", state.preferences.keywords.join(", "));
    payload.append("location", state.preferences.location);
    payload.append("posted_within_hours", String(state.preferences.posted_within_hours));
    state.preferences.workplace_types.forEach((value) => payload.append("workplace_types", value));
    state.preferences.seniority.forEach((value) => payload.append("seniority", value));
    if (state.preferences.easy_apply_only) {
      payload.append("easy_apply_only", "true");
    }
    payload.append("positive_keywords", state.preferences.positive_keywords.join(", "));
    payload.append("negative_keywords", state.preferences.negative_keywords.join(", "));
    if (state.preferences.auto_connect_with_recruiter) {
      payload.append("auto_connect_with_recruiter", "true");
    }

    startTransition(() => {
      void savePreferences(payload)
        .then((result) => setFeedback({ kind: "success", message: result.message }))
        .catch((error: Error) => setFeedback({ kind: "error", message: error.message }));
    });
  }

  if (!state) {
    return <p className="text-muted-foreground">Loading preferences...</p>;
  }

  return (
    <form className="space-y-6" onSubmit={handleSubmit}>
      <div className="grid gap-5 md:grid-cols-2">
        <Field className="md:col-span-2" label="Keywords">
          <Textarea
            placeholder="python, automation, easy apply"
            value={state.preferences.keywords.join(", ")}
            onChange={(event) =>
              updateField(
                "keywords",
                event.target.value
                  .split(",")
                  .map((item) => item.trim())
                  .filter(Boolean),
              )
            }
          />
        </Field>

        <Field label="Location">
          <Input
            value={state.preferences.location}
            onChange={(event) => updateField("location", event.target.value)}
          />
        </Field>

        <Field label="Published within (hours)">
          <Input
            min={1}
            max={168}
            type="number"
            value={state.preferences.posted_within_hours}
            onChange={(event) => updateField("posted_within_hours", Number(event.target.value))}
          />
        </Field>

        <Field className="md:col-span-2" label="Workplace types">
          <div className="grid gap-3 sm:grid-cols-3">
            {state.options.workplace_types.map((option) => (
              <SelectionChip
                key={option}
                checked={state.preferences.workplace_types.includes(option)}
                label={option.replace("_", " ")}
                onCheckedChange={() => toggleFromArray("workplace_types", option)}
              />
            ))}
          </div>
        </Field>

        <Field className="md:col-span-2" label="Seniority">
          <div className="grid gap-3 sm:grid-cols-3">
            {state.options.seniority_levels.map((option) => (
              <SelectionChip
                key={option}
                checked={state.preferences.seniority.includes(option)}
                label={option}
                onCheckedChange={() => toggleFromArray("seniority", option)}
              />
            ))}
          </div>
        </Field>

        <Field className="md:col-span-2" label="Positive keywords">
          <Textarea
            placeholder="fastapi, python, automation"
            value={state.preferences.positive_keywords.join(", ")}
            onChange={(event) =>
              updateField(
                "positive_keywords",
                event.target.value
                  .split(",")
                  .map((item) => item.trim())
                  .filter(Boolean),
              )
            }
          />
        </Field>

        <Field className="md:col-span-2" label="Negative keywords / blacklist">
          <Textarea
            placeholder="internship, unpaid"
            value={state.preferences.negative_keywords.join(", ")}
            onChange={(event) =>
              updateField(
                "negative_keywords",
                event.target.value
                  .split(",")
                  .map((item) => item.trim())
                  .filter(Boolean),
              )
            }
          />
        </Field>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <Card className="bg-white/70">
          <CardContent className="flex items-start justify-between gap-4 pt-6">
            <div className="space-y-2">
              <Label>Easy Apply only</Label>
              <p className="text-sm leading-6 text-muted-foreground">
                Restrict the search to LinkedIn vacancies with Easy Apply enabled.
              </p>
            </div>
            <Switch
              checked={state.preferences.easy_apply_only}
              onCheckedChange={(checked) => updateField("easy_apply_only", checked)}
            />
          </CardContent>
        </Card>

        <Card className="bg-white/70">
          <CardContent className="flex items-start justify-between gap-4 pt-6">
            <div className="space-y-2">
              <Label>Auto-connect with recruiter</Label>
              <p className="text-sm leading-6 text-muted-foreground">
                Default stays off for the MVP. Enable only if you want the agent to attempt a
                recruiter connection after supported applications.
              </p>
            </div>
            <Switch
              checked={state.preferences.auto_connect_with_recruiter}
              onCheckedChange={(checked) => updateField("auto_connect_with_recruiter", checked)}
            />
          </CardContent>
        </Card>
      </div>

      <div className="space-y-4">
        {feedback ? <FeedbackBanner kind={feedback.kind} message={feedback.message} /> : null}
        <Button disabled={isPending} type="submit">
          {isPending ? "Saving preferences..." : "Save preferences"}
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

function SelectionChip({
  checked,
  label,
  onCheckedChange,
}: {
  checked: boolean;
  label: string;
  onCheckedChange: () => void;
}): React.JSX.Element {
  return (
    <Card className="bg-white/70">
      <CardContent className="flex items-center gap-3 pt-6">
        <Checkbox checked={checked} onCheckedChange={onCheckedChange} />
        <span className="text-sm font-medium capitalize">{label}</span>
      </CardContent>
    </Card>
  );
}
