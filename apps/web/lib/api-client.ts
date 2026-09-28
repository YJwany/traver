"use client";

/**
 * Typed fetch wrapper for the Traver API.
 *
 * Two jobs beyond `fetch`:
 *   1. attach the stored API key, so no call site has to remember to;
 *   2. classify failures into something a UI can act on.
 *
 * The classification matters because these states need *different* screens. A
 * 401 means "no key, or a key the server does not recognise" and is fixed by the
 * user entering one. A 403 means the key is valid but lacks a scope, which
 * entering the same key again cannot fix. Collapsing both into "request failed"
 * would send the user to the wrong remedy — so the status is preserved rather
 * than flattened into a bare Error.
 */

import { getApiKey } from "./auth";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

/** A non-2xx response, carrying enough detail for the UI to choose a remedy. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }

  /** The key is missing, malformed, revoked or unknown. */
  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  /** The key is valid but not permitted to do this. */
  get isForbidden(): boolean {
    return this.status === 403;
  }

  /** The caller is being asked to slow down. */
  get isRateLimited(): boolean {
    return this.status === 429;
  }

  /** Someone else changed the trip first. */
  get isConflict(): boolean {
    return this.status === 409;
  }
}

type RequestOptions = {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  /** Extra query parameters; `undefined`/`null` values are omitted. */
  params?: Record<string, string | number | undefined | null>;
  signal?: AbortSignal;
};

function buildUrl(path: string, params?: RequestOptions["params"]): string {
  const url = new URL(path, API_URL);
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

/**
 * Extract a readable message from an error body.
 *
 * Three shapes have to be handled:
 *   * `{detail: "..."}` — a normal `HTTPException`;
 *   * `{detail: [{loc, msg}, ...]}` — request validation, where interpolating the
 *     array yields "[object Object]";
 *   * a non-JSON body — an upstream proxy's HTML error page, which must NOT be
 *     rendered into the UI. A short plain-text body ("Service Unavailable") is
 *     still useful, so that case is kept and markup is rejected.
 */
function readDetail(payload: unknown, fallback: string): string {
  if (typeof payload === "string") {
    const text = payload.trim();
    if (text && text.length <= 200 && !text.includes("<")) return text;
    return fallback;
  }
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail: unknown }).detail;
    if (typeof detail === "string" && detail) return detail;
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) => (item && typeof item === "object" && "msg" in item ? String(item.msg) : null))
        .filter((item): item is string => Boolean(item));
      if (messages.length > 0) return messages.join("；");
    }
  }
  return fallback;
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, params, signal } = options;
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const key = getApiKey();
  if (key) headers.Authorization = `Bearer ${key}`;

  let response: Response;
  try {
    response = await fetch(buildUrl(path, params), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
      cache: "no-store",
    });
  } catch (reason) {
    // A thrown fetch means the request never reached the API: DNS, refused
    // connection, CORS preflight rejection or navigation. Reported with status 0
    // so callers can tell "the API is unreachable" from "the API said no".
    const detail = reason instanceof Error ? reason.message : "无法连接 API";
    throw new ApiError(0, detail);
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let payload: unknown = undefined;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }

  if (!response.ok) {
    throw new ApiError(response.status, readDetail(payload, `请求失败（HTTP ${response.status}）`));
  }

  return payload as T;
}
