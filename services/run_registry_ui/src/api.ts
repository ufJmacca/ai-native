import type { RegistrySession, RunDetail, RunSummary } from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function normalizeBaseUrl(apiBaseUrl: string): string {
  return apiBaseUrl.trim().replace(/\/+$/, "");
}

async function buildErrorMessage(response: Response): Promise<string> {
  const prefix = `${response.status} ${response.statusText}`.trim();
  const contentType = response.headers.get("content-type") ?? "";

  if (contentType.includes("application/json")) {
    const payload = (await response.json()) as { detail?: string };
    if (payload.detail) {
      return `${prefix}: ${payload.detail}`;
    }
  } else {
    const text = (await response.text()).trim();
    if (text) {
      return `${prefix}: ${text}`;
    }
  }

  return prefix || "Run registry request failed";
}

async function fetchJson<T>(session: RegistrySession, path: string): Promise<T> {
  const response = await fetch(`${normalizeBaseUrl(session.apiBaseUrl)}${path}`, {
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${session.token}`,
    },
  });

  if (!response.ok) {
    throw new ApiError(await buildErrorMessage(response), response.status);
  }

  return (await response.json()) as T;
}

export function fetchRuns(session: RegistrySession): Promise<RunSummary[]> {
  return fetchJson<RunSummary[]>(session, "/v1/runs?limit=250");
}

export function fetchRunDetail(session: RegistrySession, runId: string): Promise<RunDetail> {
  return fetchJson<RunDetail>(session, `/v1/runs/${encodeURIComponent(runId)}`);
}
