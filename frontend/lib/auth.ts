"use client";

// Minimal token store. Access + refresh tokens live in localStorage; the
// API client attaches the access token and transparently refreshes on 401.
const ACCESS = "bt_access";
const REFRESH = "bt_refresh";

export function setTokens(access: string, refresh: string) {
  localStorage.setItem(ACCESS, access);
  localStorage.setItem(REFRESH, refresh);
}

export function getAccess(): string | null {
  return typeof window === "undefined" ? null : localStorage.getItem(ACCESS);
}

export function getRefresh(): string | null {
  return typeof window === "undefined" ? null : localStorage.getItem(REFRESH);
}

export function clearTokens() {
  localStorage.removeItem(ACCESS);
  localStorage.removeItem(REFRESH);
}

export function isAuthed(): boolean {
  return !!getAccess();
}

// Decode the `sub` (user id) from the access-token JWT payload. Best-effort:
// used only for UI niceties (e.g. excluding yourself from the typing hint),
// never for authorization — the server is the source of truth.
export function currentUserId(): string | null {
  const t = getAccess();
  if (!t) return null;
  try {
    const b64 = t.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const pad = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
    return JSON.parse(atob(pad)).sub ?? null;
  } catch {
    return null;
  }
}
