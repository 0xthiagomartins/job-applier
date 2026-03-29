"use client";

import Link from "next/link";
import { useEffect, useState, useTransition } from "react";

import { FeedbackBanner } from "@/components/feedback-banner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { fetchExecutions, fetchPanelState, runAgentNow } from "@/lib/api";
import { ExecutionSummary, PanelState } from "@/lib/types";

type FeedbackState = { kind: "success" | "error"; message: string } | null;

export function OperationalDashboard(): React.JSX.Element {
  const [state, setState] = useState<PanelState | null>(null);
  const [executions, setExecutions] = useState<ExecutionSummary[]>([]);
  const [feedback, setFeedback] = useState<FeedbackState>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isRunning, startRunning] = useTransition();

  useEffect(() => {
    void loadDashboard();
  }, []);

  async function loadDashboard(): Promise<void> {
    setIsLoading(true);
    try {
      const [panelState, executionList] = await Promise.all([fetchPanelState(), fetchExecutions(5)]);
      setState(panelState);
      setExecutions(executionList);
    } catch (error) {
      setFeedback({
        kind: "error",
        message: error instanceof Error ? error.message : "Could not load the dashboard.",
      });
    } finally {
      setIsLoading(false);
    }
  }

  function handleRunNow(): void {
    if (typeof window !== "undefined") {
      const confirmed = window.confirm(
        "Executar o agente agora? Isso vai iniciar o fluxo completo de busca e aplicação.",
      );
      if (!confirmed) {
        return;
      }
    }

    startRunning(() => {
      void runAgentNow()
        .then(async (execution) => {
          setExecutions((current) =>
            [execution, ...current.filter((item) => item.execution_id !== execution.execution_id)].slice(
              0,
              5,
            ),
          );
          const refreshedState = await fetchPanelState();
          setState(refreshedState);
          setFeedback({
            kind: "success",
            message: `Execucao finalizada com status ${execution.status}. Submissoes: ${execution.successful_submissions}.`,
          });
        })
        .catch((error: Error) =>
          setFeedback({
            kind: "error",
            message: error.message,
          }),
        );
    });
  }

  if (isLoading || !state) {
    return <p className="text-muted-foreground">Loading operational dashboard...</p>;
  }

  const lastExecution = executions[0] ?? null;
  const nextExecutionAt = state.computed.next_execution_at
    ? new Date(state.computed.next_execution_at)
    : null;

  return (
    <div className="space-y-6">
      {feedback ? <FeedbackBanner kind={feedback.kind} message={feedback.message} /> : null}

      <div className="grid gap-5 xl:grid-cols-[1.4fr_0.9fr]">
        <Card className="bg-white/75">
          <CardHeader className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <CardTitle>Agent status</CardTitle>
                <CardDescription>
                  Use this dashboard for the operational pulse of the agent and quick access to the
                  configuration flow.
                </CardDescription>
              </div>
              <StatusBadge execution={lastExecution} />
            </div>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="grid gap-4 md:grid-cols-2">
              <MetricCard
                description="Most recent recorded run."
                label="Last execution"
                value={lastExecution ? formatDateTime(lastExecution.started_at) : "Not executed yet"}
              />
              <MetricCard
                description={`${state.schedule.frequency} at ${state.schedule.run_at} (${state.schedule.timezone})`}
                label="Next scheduled run"
                value={nextExecutionAt ? formatDateTime(nextExecutionAt.toISOString()) : "Unavailable"}
              />
              <MetricCard
                description="Vacancies discovered in the last execution."
                label="Jobs found"
                value={String(lastExecution?.jobs_seen ?? 0)}
              />
              <MetricCard
                description="Vacancies that passed qualification."
                label="Qualified"
                value={String(lastExecution?.jobs_selected ?? 0)}
              />
              <MetricCard
                description="Successful applications completed."
                label="Applications sent"
                value={String(lastExecution?.successful_submissions ?? 0)}
              />
              <MetricCard
                description="Last execution error count."
                label="Errors"
                value={String(lastExecution?.error_count ?? 0)}
              />
            </div>

            {lastExecution?.last_error ? (
              <div className="rounded-2xl bg-rose-100 px-4 py-3 text-sm text-rose-900">
                {lastExecution.last_error}
              </div>
            ) : null}

            <div className="flex flex-wrap gap-3">
              <Button disabled={isRunning} type="button" onClick={handleRunNow}>
                {isRunning ? "Running agent..." : "Run now"}
              </Button>
              <Button asChild type="button" variant="secondary">
                <Link href="/schedule">Adjust schedule</Link>
              </Button>
              <Button asChild type="button" variant="secondary">
                <Link href="/history">Open history</Link>
              </Button>
            </div>
          </CardContent>
        </Card>

        <Card className="bg-white/75">
          <CardHeader>
            <CardTitle>Quick links</CardTitle>
            <CardDescription>Jump straight to the most common operational screens.</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-3">
            <QuickLink href="/profile" title="Candidate profile" description={state.profile.name || "Complete the core profile and CV."} />
            <QuickLink
              href="/preferences"
              title="Search filters"
              description={state.preferences.keywords.join(", ") || "Define keywords and scoring filters."}
            />
            <QuickLink
              href="/schedule"
              title="Agent schedule"
              description={`${state.schedule.run_at} • ${state.schedule.timezone}`}
            />
            <QuickLink href="/history" title="Application history" description="Review successful submissions and artifacts." />
            <QuickLink href="/ai" title="AI settings" description={state.ai.model || "Configure the active model and key."} />
          </CardContent>
        </Card>
      </div>

      <Card className="bg-white/75">
        <CardHeader>
          <CardTitle>Recent executions</CardTitle>
          <CardDescription>Latest runs recorded by the agent.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {executions.length === 0 ? (
            <p className="text-sm text-muted-foreground">No executions recorded yet.</p>
          ) : (
            executions.map((execution) => (
              <div key={execution.execution_id} className="rounded-2xl bg-white/85 px-4 py-3">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <StatusBadge execution={execution} />
                    <span className="text-sm text-muted-foreground">
                      {formatDateTime(execution.started_at)}
                    </span>
                  </div>
                  <span className="text-xs uppercase tracking-[0.12em] text-muted-foreground">
                    {execution.origin}
                  </span>
                </div>
                <p className="mt-2 text-sm text-foreground">
                  Found {execution.jobs_seen} | qualified {execution.jobs_selected} | applied{" "}
                  {execution.successful_submissions}
                </p>
              </div>
            ))
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function MetricCard({
  label,
  value,
  description,
}: {
  label: string;
  value: string;
  description: string;
}): React.JSX.Element {
  return (
    <div className="rounded-2xl bg-[#fffaf5] px-4 py-4">
      <p className="text-xs font-semibold uppercase tracking-[0.12em] text-[#7a3b1e]">{label}</p>
      <p className="mt-2 text-2xl font-semibold text-foreground">{value}</p>
      <p className="mt-2 text-sm text-muted-foreground">{description}</p>
    </div>
  );
}

function StatusBadge({
  execution,
}: {
  execution: ExecutionSummary | null;
}): React.JSX.Element {
  if (!execution) {
    return <Badge className="bg-slate-200 text-slate-800">Not run yet</Badge>;
  }

  if (execution.status === "completed" && execution.error_count === 0) {
    return <Badge className="bg-emerald-100 text-emerald-900">Healthy</Badge>;
  }
  if (execution.status === "completed") {
    return <Badge className="bg-amber-100 text-amber-900">Completed with warnings</Badge>;
  }
  if (execution.status === "failed") {
    return <Badge className="bg-rose-100 text-rose-900">Failed</Badge>;
  }
  return <Badge className="bg-slate-200 text-slate-800">{execution.status}</Badge>;
}

function QuickLink({
  href,
  title,
  description,
}: {
  href: string;
  title: string;
  description: string;
}): React.JSX.Element {
  return (
    <Link
      className="rounded-2xl border border-border bg-white/85 px-4 py-4 transition hover:-translate-y-0.5 hover:border-[#d4b298]"
      href={href}
    >
      <p className="font-semibold text-foreground">{title}</p>
      <p className="mt-1 text-sm text-muted-foreground">{description}</p>
    </Link>
  );
}

function formatDateTime(value: string): string {
  return new Date(value).toLocaleString();
}
