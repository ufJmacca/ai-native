import type { RegistrySession } from "./types";

const SESSION_KEY = "run-registry-ui.session";

export function loadStoredSession(): RegistrySession | null {
  if (typeof window === "undefined") {
    return null;
  }

  const raw = window.sessionStorage.getItem(SESSION_KEY);
  if (!raw) {
    return null;
  }

  try {
    const parsed = JSON.parse(raw) as Partial<RegistrySession>;
    if (!parsed.apiBaseUrl || !parsed.token) {
      return null;
    }

    return {
      apiBaseUrl: parsed.apiBaseUrl,
      token: parsed.token,
    };
  } catch {
    return null;
  }
}

export function storeSession(session: RegistrySession): void {
  if (typeof window === "undefined") {
    return;
  }

  window.sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
}

export function clearStoredSession(): void {
  if (typeof window === "undefined") {
    return;
  }

  window.sessionStorage.removeItem(SESSION_KEY);
}
