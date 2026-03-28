import { PanelState } from "@/lib/types";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? "http://127.0.0.1:8000";

export function apiUrl(path: string): string {
  return `${API_BASE_URL}${path}`;
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

export async function saveAiSettings(formData: FormData): Promise<{ message: string }> {
  const response = await fetch(apiUrl("/api/panel/ai"), {
    method: "PUT",
    body: formData,
  });
  return parseResponse<{ message: string }>(response);
}
