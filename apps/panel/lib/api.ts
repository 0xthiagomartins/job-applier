import { ExecutionSummary, PanelState } from "@/lib/types";

export function apiUrl(path: string): string {
  return path;
}

async function parseResponse<T>(response: Response): Promise<T> {
  const payload = (await response.json()) as T & { detail?: string; message?: string };

  if (!response.ok) {
    const detail =
      typeof payload.detail === "string"
        ? payload.detail
        : typeof payload.message === "string"
          ? payload.message
          : "Unexpected request error.";
    throw new Error(detail);
  }

  return payload;
}

export async function fetchPanelState(): Promise<PanelState> {
  const response = await fetch(apiUrl("/api/panel/state"), {
    cache: "no-store",
  });
  return parseResponse<PanelState>(response);
}

export async function saveProfile(formData: FormData): Promise<{ message: string }> {
  const response = await fetch(apiUrl("/api/panel/profile"), {
    method: "POST",
    body: formData,
  });
  return parseResponse<{ message: string }>(response);
}

export async function savePreferences(formData: FormData): Promise<{ message: string }> {
  const response = await fetch(apiUrl("/api/panel/preferences"), {
    method: "PUT",
    body: formData,
  });
  return parseResponse<{ message: string }>(response);
}

export async function saveSchedule(formData: FormData): Promise<{ message: string }> {
  const response = await fetch(apiUrl("/api/panel/schedule"), {
    method: "PUT",
    body: formData,
  });
  return parseResponse<{ message: string }>(response);
}

export async function saveAiSettings(formData: FormData): Promise<{ message: string }> {
  const response = await fetch(apiUrl("/api/panel/ai"), {
    method: "PUT",
    body: formData,
  });
  return parseResponse<{ message: string }>(response);
}

export async function fetchExecutions(limit = 5): Promise<ExecutionSummary[]> {
  const response = await fetch(apiUrl(`/api/agent/executions?limit=${limit}`), {
    cache: "no-store",
  });
  const payload = await parseResponse<{ executions: ExecutionSummary[] }>(response);
  return payload.executions;
}

export async function runAgentNow(): Promise<ExecutionSummary> {
  const response = await fetch(apiUrl("/api/agent/run"), {
    method: "POST",
  });
  const payload = await parseResponse<{ execution: ExecutionSummary }>(response);
  return payload.execution;
}
