import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import App from "./App";

function jsonResponse(payload: unknown, init: ResponseInit = {}): Promise<Response> {
  return Promise.resolve(
    new Response(JSON.stringify(payload), {
      headers: {
        "Content-Type": "application/json",
      },
      status: 200,
      ...init,
    }),
  );
}

const runSummary = {
  run_id: "20260315T000000000000Z-agent-registry",
  workflow: "ai-native",
  feature_slug: "agent-registry",
  spec_path: "/workspace/specs/agent-registry.md",
  workspace_root: "/workspace/app",
  status: "in_progress",
  current_stage: "verify",
  scheduler_status: "running",
  active_slice: "AR-10",
  created_at: "2026-03-15T00:00:00+00:00",
  updated_at: "2026-03-15T00:10:00+00:00",
  last_heartbeat_at: "2026-03-15T00:09:55+00:00",
  expires_at: "2026-04-14T00:00:00+00:00",
  liveness: "active",
} as const;

const runDetail = {
  ...runSummary,
  run_dir: "/workspace/.ai-native/runs/20260315T000000000000Z-agent-registry",
  metadata: {
    heartbeat: {
      agent: "ainative",
      updated_at: "2026-03-15T00:09:55+00:00",
    },
  },
  run_projection: {
    schema_version: 1,
    completed_steps: ["intake", "recon", "plan"],
    in_progress_steps: ["verify"],
    blocked_steps: [
      {
        step: "pr",
        reason: "Waiting for verify",
      },
    ],
    next_executable_steps: ["commit"],
  },
  stage_status: {
    plan: {
      stage: "plan",
      status: "completed",
      artifacts: ["/workspace/.ai-native/runs/run-1/plan/plan.md"],
      notes: [],
    },
    verify: {
      stage: "verify",
      status: "completed",
      artifacts: ["/workspace/.ai-native/runs/run-1/verify/AR-10.md"],
      notes: ["Verification green"],
    },
  },
  slice_states: {
    "AR-10": {
      slice_id: "AR-10",
      branch_name: "codex/agent-registry",
      worktree_path: "/workspace/.ai-native/worktrees/run-1/AR-10",
      status: "running",
      current_stage: "verify",
      block_reason: null,
      commit_sha: "abc1234",
      pr_url: "https://github.com/example/repo/pull/42",
      attempt_counts: {
        verify: 1,
      },
      started_at: "2026-03-15T00:03:00+00:00",
      updated_at: "2026-03-15T00:09:55+00:00",
    },
  },
} as const;

describe("Run registry UI", () => {
  it("opens the console from the token gate and saves the session", async () => {
    const fetchMock = vi.fn().mockImplementation(() => jsonResponse([runSummary]));
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    await userEvent.clear(screen.getByLabelText("API base URL"));
    await userEvent.type(screen.getByLabelText("API base URL"), "http://registry.internal:8080");
    await userEvent.type(screen.getByLabelText("Bearer token"), "secret-token");
    await userEvent.click(screen.getByRole("button", { name: "Open console" }));

    expect(await screen.findByRole("heading", { name: "AI-native runs" })).toBeInTheDocument();
    expect(screen.getByText("agent-registry")).toBeInTheDocument();

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "http://registry.internal:8080/v1/runs?limit=250",
        expect.objectContaining({
          headers: expect.objectContaining({
            Authorization: "Bearer secret-token",
          }),
        }),
      ),
    );

    expect(window.sessionStorage.getItem("run-registry-ui.session")).toContain("registry.internal:8080");
  });

  it("restores a saved session and shows an API error state", async () => {
    window.sessionStorage.setItem(
      "run-registry-ui.session",
      JSON.stringify({
        apiBaseUrl: "http://localhost:8080",
        token: "saved-token",
      }),
    );

    const fetchMock = vi
      .fn()
      .mockImplementation(() =>
        jsonResponse(
          {
            detail: "Registry unavailable",
          },
          {
            status: 503,
            statusText: "Service Unavailable",
          },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Unable to load registry data" })).toBeInTheDocument();
    expect(screen.getByText(/503 Service Unavailable: Registry unavailable/)).toBeInTheDocument();
  });

  it("pauses polling while the page is hidden and resumes when visible again", async () => {
    window.sessionStorage.setItem(
      "run-registry-ui.session",
      JSON.stringify({
        apiBaseUrl: "http://localhost:8080",
        token: "saved-token",
      }),
    );
    (window as Window & { __RUN_REGISTRY_POLL_INTERVAL_MS__?: number }).__RUN_REGISTRY_POLL_INTERVAL_MS__ = 30;

    let hidden = false;
    Object.defineProperty(document, "hidden", {
      configurable: true,
      get: () => hidden,
    });

    const fetchMock = vi.fn().mockImplementation(() => jsonResponse([runSummary]));
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    hidden = true;
    document.dispatchEvent(new Event("visibilitychange"));
    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 80));
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);

    hidden = false;
    document.dispatchEvent(new Event("visibilitychange"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("renders overview, timeline, slices, and raw tabs on the run detail route", async () => {
    window.sessionStorage.setItem(
      "run-registry-ui.session",
      JSON.stringify({
        apiBaseUrl: "http://localhost:8080",
        token: "saved-token",
      }),
    );
    window.location.hash = `#/runs/${runSummary.run_id}`;

    const fetchMock = vi.fn().mockImplementation(() => jsonResponse(runDetail));
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(await screen.findByText("Waiting for verify")).toBeInTheDocument();
    expect(screen.getAllByText("/workspace/app")).toHaveLength(2);
    expect(screen.getByText(runSummary.run_id)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Timeline" }));
    expect(await screen.findByText("Verification green")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Slices" }));
    expect(await screen.findByText("codex/agent-registry")).toBeInTheDocument();
    expect(screen.getByText("https://github.com/example/repo/pull/42")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Raw" }));
    expect(await screen.findByText(/"slice_id": "AR-10"/)).toBeInTheDocument();
  });

  it("shows the empty state when no runs have been published", async () => {
    window.sessionStorage.setItem(
      "run-registry-ui.session",
      JSON.stringify({
        apiBaseUrl: "http://localhost:8080",
        token: "saved-token",
      }),
    );

    const fetchMock = vi.fn().mockImplementation(() => jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "No runs published yet" })).toBeInTheDocument();
  });
});
