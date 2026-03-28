"use client";

import { FormEvent, useEffect, useState } from "react";

import { FeedbackBanner } from "@/components/feedback-banner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  fetchApplicationDetail,
  fetchApplications,
} from "@/lib/api";
import {
  ApplicationHistoryDetail,
  ApplicationHistoryPage,
} from "@/lib/types";

const PAGE_SIZE = 10;

type HistoryFilters = {
  company: string;
  title: string;
  submitted_from: string;
  submitted_to: string;
};

const EMPTY_FILTERS: HistoryFilters = {
  company: "",
  title: "",
  submitted_from: "",
  submitted_to: "",
};

export function ApplicationHistory(): React.JSX.Element {
  const [draftFilters, setDraftFilters] = useState<HistoryFilters>(EMPTY_FILTERS);
  const [appliedFilters, setAppliedFilters] = useState<HistoryFilters>(EMPTY_FILTERS);
  const [historyPage, setHistoryPage] = useState<ApplicationHistoryPage | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedApplication, setSelectedApplication] =
    useState<ApplicationHistoryDetail | null>(null);
  const [page, setPage] = useState(0);
  const [listLoading, setListLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function loadHistory(): Promise<void> {
      setListLoading(true);
      setErrorMessage(null);

      try {
        const pageResponse = await fetchApplications({
          ...appliedFilters,
          limit: PAGE_SIZE,
          offset: page * PAGE_SIZE,
        });
        if (cancelled) {
          return;
        }

        setHistoryPage(pageResponse);
        if (pageResponse.items.length === 0) {
          setSelectedId(null);
          setSelectedApplication(null);
          return;
        }

        const selectedStillVisible = pageResponse.items.some((item) => item.id === selectedId);
        if (!selectedStillVisible) {
          setSelectedId(pageResponse.items[0].id);
        }
      } catch (error) {
        if (cancelled) {
          return;
        }
        setHistoryPage(null);
        setSelectedId(null);
        setSelectedApplication(null);
        setErrorMessage(
          error instanceof Error ? error.message : "Could not load the application history.",
        );
      } finally {
        if (!cancelled) {
          setListLoading(false);
        }
      }
    }

    void loadHistory();

    return () => {
      cancelled = true;
    };
  }, [appliedFilters, page]);

  useEffect(() => {
    if (!selectedId) {
      return;
    }

    const applicationId = selectedId;
    let cancelled = false;

    async function loadDetail(): Promise<void> {
      setDetailLoading(true);
      setSelectedApplication(null);
      setErrorMessage(null);

      try {
        const detail = await fetchApplicationDetail(applicationId);
        if (!cancelled) {
          setSelectedApplication(detail);
        }
      } catch (error) {
        if (!cancelled) {
          setSelectedApplication(null);
          setErrorMessage(
            error instanceof Error ? error.message : "Could not load the application detail.",
          );
        }
      } finally {
        if (!cancelled) {
          setDetailLoading(false);
        }
      }
    }

    void loadDetail();

    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const totalItems = historyPage?.total ?? 0;
  const currentFrom = totalItems === 0 ? 0 : page * PAGE_SIZE + 1;
  const currentTo = historyPage ? page * PAGE_SIZE + historyPage.items.length : 0;

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    setPage(0);
    setSelectedId(null);
    setAppliedFilters({ ...draftFilters });
  }

  function handleClear(): void {
    setDraftFilters(EMPTY_FILTERS);
    setAppliedFilters(EMPTY_FILTERS);
    setPage(0);
    setSelectedId(null);
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[0.95fr_1.05fr]">
      <div className="space-y-6">
        <Card className="bg-white/80">
          <CardHeader>
            <CardTitle>Filters</CardTitle>
            <CardDescription>
              Search the successful applications by company, job title and submitted date.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form className="grid gap-4 md:grid-cols-2" onSubmit={handleSubmit}>
              <div className="space-y-2">
                <Label htmlFor="company">Company</Label>
                <Input
                  id="company"
                  value={draftFilters.company}
                  onChange={(event) =>
                    setDraftFilters((current) => ({ ...current, company: event.target.value }))
                  }
                  placeholder="Acme"
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="title">Job title</Label>
                <Input
                  id="title"
                  value={draftFilters.title}
                  onChange={(event) =>
                    setDraftFilters((current) => ({ ...current, title: event.target.value }))
                  }
                  placeholder="Automation Engineer"
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="submitted_from">Submitted from</Label>
                <Input
                  id="submitted_from"
                  type="date"
                  value={draftFilters.submitted_from}
                  onChange={(event) =>
                    setDraftFilters((current) => ({
                      ...current,
                      submitted_from: event.target.value,
                    }))
                  }
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="submitted_to">Submitted to</Label>
                <Input
                  id="submitted_to"
                  type="date"
                  value={draftFilters.submitted_to}
                  onChange={(event) =>
                    setDraftFilters((current) => ({
                      ...current,
                      submitted_to: event.target.value,
                    }))
                  }
                />
              </div>
              <div className="flex flex-wrap gap-3 md:col-span-2">
                <Button type="submit">Apply filters</Button>
                <Button onClick={handleClear} type="button" variant="secondary">
                  Clear
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>

        {errorMessage ? <FeedbackBanner kind="error" message={errorMessage} /> : null}

        <Card className="bg-white/80">
          <CardHeader>
            <CardTitle>Applications</CardTitle>
            <CardDescription>
              {listLoading
                ? "Loading successful applications..."
                : `Showing ${currentFrom}-${currentTo} of ${totalItems} successful applications.`}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {historyPage?.items.length ? (
              historyPage.items.map((item) => {
                const isSelected = item.id === selectedId;

                return (
                  <button
                    key={item.id}
                    className={`w-full rounded-2xl border p-4 text-left transition ${
                      isSelected
                        ? "border-[#d4b298] bg-[#fff7ef]"
                        : "border-border bg-white hover:border-[#d4b298]"
                    }`}
                    onClick={() => setSelectedId(item.id)}
                    type="button"
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <p className="font-semibold text-foreground">{item.job_title}</p>
                        <p className="text-sm text-muted-foreground">{item.company_name}</p>
                      </div>
                      <Badge>{formatDate(item.submitted_at)}</Badge>
                    </div>
                    <div className="mt-3 flex flex-wrap gap-2 text-xs text-muted-foreground">
                      {item.location ? <span>{item.location}</span> : null}
                      {item.cv_version ? <span>CV: {item.cv_version}</span> : null}
                      {item.external_job_id ? <span>ID: {item.external_job_id}</span> : null}
                    </div>
                  </button>
                );
              })
            ) : (
              <p className="text-sm text-muted-foreground">
                {listLoading
                  ? "Waiting for the history query..."
                  : "No successful applications matched the current filters."}
              </p>
            )}

            <div className="flex items-center justify-between gap-3 pt-2">
              <Button
                disabled={page === 0 || listLoading}
                onClick={() => setPage((current) => Math.max(0, current - 1))}
                type="button"
                variant="secondary"
              >
                Previous
              </Button>
              <p className="text-sm text-muted-foreground">Page {page + 1}</p>
              <Button
                disabled={listLoading || !historyPage || currentTo >= historyPage.total}
                onClick={() => setPage((current) => current + 1)}
                type="button"
                variant="secondary"
              >
                Next
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>

      <Card className="bg-white/80">
        <CardHeader>
          <CardTitle>Application Detail</CardTitle>
          <CardDescription>
            {selectedApplication
              ? "Review exactly what was sent for this successful application."
              : "Select an application on the left to inspect the submitted context."}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {detailLoading ? (
            <p className="text-sm text-muted-foreground">Loading application detail...</p>
          ) : null}

          {!detailLoading && selectedApplication ? (
            <>
              <section className="space-y-3">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h3 className="text-xl font-semibold text-foreground">
                      {selectedApplication.job_posting.title}
                    </h3>
                    <p className="text-sm text-muted-foreground">
                      {selectedApplication.job_posting.company_name}
                    </p>
                  </div>
                  <Badge>
                    {formatDate(selectedApplication.submission.submitted_at)}
                  </Badge>
                </div>
                <div className="grid gap-3 text-sm text-muted-foreground md:grid-cols-2">
                  <div>CV used: {selectedApplication.submission.cv_version || "Not informed"}</div>
                  <div>Origin: {selectedApplication.submission.execution_origin}</div>
                  <div>
                    Ruleset: {selectedApplication.submission.ruleset_version || "Not informed"}
                  </div>
                  <div>
                    AI model: {selectedApplication.submission.ai_model_used || "Not informed"}
                  </div>
                </div>
                {selectedApplication.submission.notes ? (
                  <p className="rounded-2xl bg-secondary/60 px-4 py-3 text-sm text-foreground">
                    {selectedApplication.submission.notes}
                  </p>
                ) : null}
              </section>

              <section className="space-y-3">
                <h3 className="text-lg font-semibold text-foreground">Answers sent</h3>
                <div className="space-y-3">
                  {selectedApplication.answers.map((answer) => (
                    <div
                      key={answer.id}
                      className="rounded-2xl border border-border bg-[#fffaf5] p-4"
                    >
                      <p className="font-medium text-foreground">{answer.question_raw}</p>
                      <p className="mt-2 text-sm text-muted-foreground">{answer.answer_raw}</p>
                      <div className="mt-3 flex flex-wrap gap-2 text-xs text-muted-foreground">
                        <span>Source: {answer.answer_source}</span>
                        <span>Strategy: {answer.fill_strategy}</span>
                        {answer.ambiguity_flag ? <span>Ambiguous</span> : null}
                      </div>
                    </div>
                  ))}
                </div>
              </section>

              <section className="space-y-3">
                <h3 className="text-lg font-semibold text-foreground">Profile snapshot</h3>
                {selectedApplication.profile_snapshot ? (
                  <pre className="overflow-x-auto rounded-2xl bg-slate-950 p-4 text-xs text-slate-100">
                    {JSON.stringify(selectedApplication.profile_snapshot.data, null, 2)}
                  </pre>
                ) : (
                  <p className="text-sm text-muted-foreground">No profile snapshot stored.</p>
                )}
              </section>

              <section className="grid gap-6 md:grid-cols-2">
                <div className="space-y-3">
                  <h3 className="text-lg font-semibold text-foreground">Artifacts</h3>
                  {selectedApplication.artifacts.length ? (
                    selectedApplication.artifacts.map((artifact) => (
                      <div
                        key={artifact.id}
                        className="rounded-2xl border border-border bg-white p-4 text-sm"
                      >
                        <p className="font-medium text-foreground">{artifact.artifact_type}</p>
                        <p className="mt-1 break-all text-muted-foreground">{artifact.path}</p>
                      </div>
                    ))
                  ) : (
                    <p className="text-sm text-muted-foreground">No artifacts stored.</p>
                  )}
                </div>

                <div className="space-y-3">
                  <h3 className="text-lg font-semibold text-foreground">Execution events</h3>
                  {selectedApplication.execution_events.length ? (
                    selectedApplication.execution_events.map((event) => (
                      <div
                        key={event.id}
                        className="rounded-2xl border border-border bg-white p-4 text-sm"
                      >
                        <p className="font-medium text-foreground">{event.event_type}</p>
                        <p className="mt-1 text-muted-foreground">{formatDate(event.timestamp)}</p>
                        <pre className="mt-3 overflow-x-auto rounded-xl bg-slate-950 p-3 text-xs text-slate-100">
                          {JSON.stringify(event.payload, null, 2)}
                        </pre>
                      </div>
                    ))
                  ) : (
                    <p className="text-sm text-muted-foreground">No execution events stored.</p>
                  )}
                </div>
              </section>
            </>
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}

function formatDate(value: string | null): string {
  if (!value) {
    return "Not informed";
  }

  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}
