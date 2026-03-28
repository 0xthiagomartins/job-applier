"use client";

import { useEffect, useState, useTransition } from "react";

import { FeedbackBanner } from "@/components/feedback-banner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { fetchExecutions, fetchPanelState, runAgentNow, saveSchedule } from "@/lib/api";
import { ExecutionSummary, PanelState } from "@/lib/types";

type FeedbackState = { kind: "success" | "error"; message: string } | null;

export function ScheduleForm(): React.JSX.Element {
  const [state, setState] = useState<PanelState | null>(null);
  const [executions, setExecutions] = useState<ExecutionSummary[]>([]);
  const [feedback, setFeedback] = useState<FeedbackState>(null);
  const [isSaving, startSaving] = useTransition();
  const [isRunning, startRunning] = useTransition();

  useEffect(() => {
    void Promise.all([fetchPanelState(), fetchExecutions()])
      .then(([panelState, executionList]) => {
        setState(panelState);
        setExecutions(executionList);
      })
      .catch((error: Error) => setFeedback({ kind: "error", message: error.message }));
  }, []);

  function updateField<K extends keyof PanelState["schedule"]>(
    field: K,
    value: PanelState["schedule"][K],
  ): void {
    setState((current) =>
      current
        ? {
            ...current,
            schedule: {
              ...current.schedule,
              [field]: value,
            },
          }
        : current,
    );
  }

  function handleSubmit(event: React.FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (!state) {
      return;
    }

    const payload = new FormData();
    payload.append("frequency", state.schedule.frequency);
    payload.append("run_at", state.schedule.run_at);
    payload.append("timezone", state.schedule.timezone);

    startSaving(() => {
      void saveSchedule(payload)
        .then((result) => setFeedback({ kind: "success", message: result.message }))
        .catch((error: Error) => setFeedback({ kind: "error", message: error.message }));
    });
  }

  function handleRunNow(): void {
    startRunning(() => {
      void runAgentNow()
        .then((execution) => {
          setExecutions((current) => [execution, ...current.filter((item) => item.execution_id !== execution.execution_id)].slice(0, 5));
          setFeedback({ kind: "success", message: "Manual execution completed." });
        })
        .catch((error: Error) => setFeedback({ kind: "error", message: error.message }));
    });
  }

  if (!state) {
    return <p className="text-muted-foreground">Loading schedule...</p>;
  }

  return (
    <div className="space-y-6">
      <form className="space-y-6" onSubmit={handleSubmit}>
        <div className="grid gap-5 md:grid-cols-3">
          <Field label="Frequency">
            <Select
              value={state.schedule.frequency}
              onValueChange={(value) => updateField("frequency", value)}
            >
              <SelectTrigger>
                <SelectValue placeholder="Select a frequency" />
              </SelectTrigger>
              <SelectContent>
                {state.options.schedule_frequencies.map((option) => (
                  <SelectItem key={option} value={option}>
                    {option === "daily" ? "1x per day" : option}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>

          <Field label="Run at">
            <Input
              type="time"
              value={state.schedule.run_at}
              onChange={(event) => updateField("run_at", event.target.value)}
            />
          </Field>

          <Field label="Timezone">
            <Input
              value={state.schedule.timezone}
              onChange={(event) => updateField("timezone", event.target.value)}
            />
          </Field>
        </div>

        <div className="space-y-4">
          {feedback ? <FeedbackBanner kind={feedback.kind} message={feedback.message} /> : null}
          <div className="flex flex-wrap gap-3">
            <Button disabled={isSaving} type="submit">
              {isSaving ? "Saving schedule..." : "Save schedule"}
            </Button>
            <Button disabled={isRunning} type="button" variant="secondary" onClick={handleRunNow}>
              {isRunning ? "Running agent..." : "Run now"}
            </Button>
          </div>
        </div>
      </form>

      <Card className="bg-white/70">
        <CardHeader>
          <CardTitle>Recent executions</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {executions.length === 0 ? (
            <p className="text-sm text-muted-foreground">No executions recorded yet.</p>
          ) : (
            executions.map((execution) => (
              <div key={execution.execution_id} className="rounded-2xl bg-white/80 px-4 py-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-sm font-semibold uppercase tracking-[0.12em] text-[#7a3b1e]">
                    {execution.status}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {new Date(execution.started_at).toLocaleString()}
                  </span>
                </div>
                <p className="mt-2 text-sm text-foreground">
                  Jobs seen: {execution.jobs_seen} | selected: {execution.jobs_selected} | submitted:{" "}
                  {execution.successful_submissions}
                </p>
                {execution.last_error ? (
                  <p className="mt-1 text-sm text-[#9a3412]">{execution.last_error}</p>
                ) : null}
              </div>
            ))
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function Field({
  children,
  label,
}: {
  children: React.ReactNode;
  label: string;
}): React.JSX.Element {
  return (
    <div className="space-y-2">
      <Label>{label}</Label>
      {children}
    </div>
  );
}
