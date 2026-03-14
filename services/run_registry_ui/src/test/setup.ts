import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  window.location.hash = "#/";
  delete (window as Window & { __RUN_REGISTRY_POLL_INTERVAL_MS__?: number }).__RUN_REGISTRY_POLL_INTERVAL_MS__;
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

beforeEach(() => {
  window.location.hash = "#/";
});
