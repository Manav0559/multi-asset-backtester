"use client";

import { clearTokens, getAccess, getRefresh, setTokens } from "./auth";

// All calls go through Next's /api rewrite to the FastAPI backend.
const BASE = "/api";

async function refreshAccess(): Promise<boolean> {
  const refresh = getRefresh();
  if (!refresh) return false;
  const res = await fetch(`${BASE}/auth/refresh`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refresh }),
  });
  if (!res.ok) return false;
  const data = await res.json();
  setTokens(data.access_token, data.refresh_token);
  return true;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function api<T = any>(
  path: string,
  opts: RequestInit & { auth?: boolean } = {}
): Promise<T> {
  const { auth = true, ...init } = opts;
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  if (auth && getAccess()) headers.set("Authorization", `Bearer ${getAccess()}`);

  let res = await fetch(`${BASE}${path}`, { ...init, headers });

  // Transparent refresh-and-retry on a single 401.
  if (res.status === 401 && auth && (await refreshAccess())) {
    headers.set("Authorization", `Bearer ${getAccess()}`);
    res = await fetch(`${BASE}${path}`, { ...init, headers });
  }

  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {}
    if (res.status === 401) clearTokens();
    throw new ApiError(res.status, typeof detail === "string" ? detail : "Request failed");
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

// SWR fetcher bound to the authed client.
export const fetcher = (path: string) => api(path);
