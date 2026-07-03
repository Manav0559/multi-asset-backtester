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
