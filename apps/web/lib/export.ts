"use client";

/**
 * Downloading an exported plan.
 *
 * A plain `<a href>` cannot be used. The API is authenticated with a bearer key held
 * in this app, and an anchor sends no `Authorization` header — so the download would
 * 401 for exactly the users who have configured a key. The file is therefore fetched
 * with the normal API client and handed to the browser as a blob.
 *
 * The naming rules are separate pure functions because they are the part that can be
 * wrong without anything visibly failing. The server sends both `filename` (ASCII) and
 * `filename*` (RFC 5987, percent-encoded UTF-8) precisely because a Chinese destination
 * cannot go in a plain header value. Parsing the wrong one yields a file called
 * `trip-2026-10-03.md` when the plan is for 杭州 — not an error, just quietly less
 * useful, which is the kind of defect nothing complains about.
 */

import { ApiError } from "./api-client";
import { getApiKey } from "./auth";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export type ExportFormat = "md" | "ics";

/** The API paths, spelled once so the button labels and the requests cannot drift apart. */
export const EXPORT_PATHS: Record<ExportFormat, string> = {
  md: "export.md",
  ics: "export.ics",
};

/**
 * Read the filename out of a `Content-Disposition` header.
 *
 * Prefers `filename*`, which is the only one that can carry a non-ASCII name, and
 * falls back to `filename` when it is absent or the percent-decoding fails. Returns
 * `null` rather than guessing when neither is present, so the caller decides; a
 * download must never fail because a name could not be read.
 */
export function parseContentDisposition(header: string | null, fallback: string): string {
  if (!header) return fallback;

  const extended = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (extended) {
    try {
      const decoded = decodeURIComponent(extended[1].trim());
      // A path separator in a downloaded name is either a mistake or an attempt to
      // place the file somewhere unexpected; the browser would strip it anyway, so
      // taking the last segment keeps the visible name and drops nothing real.
      const name = decoded.split(/[\\/]/).pop() ?? "";
      if (name) return name;
    } catch {
      // Malformed percent-encoding: fall through to the ASCII parameter rather than
      // failing the download over a cosmetic detail.
    }
  }

  const plain = /filename="?([^";]+)"?/i.exec(header);
  if (plain) {
    const name = plain[1].trim();
    if (name) return name;
  }

  return fallback;
}

/**
 * A filename safe to hand to `URL.createObjectURL`, for the rare header-less case.
 *
 * Strips the characters a filesystem reserves rather than keeping only letters and
 * digits: the latter would delete the Chinese in 杭州, which is the whole name. The
 * targets are the path separators and reserved punctuation, so everything meaningful
 * survives untouched.
 */
export function fallbackFilename(destination: string, format: ExportFormat): string {
  const slug = destination.replace(/[\\/:*?"<>|\u0000-\u001f\s]+/g, "-").replace(/^-+|-+$/g, "");
  return `${slug || "trip"}.${format}`;
}

/**
 * Fetch an export and save it.
 *
 * The error path matters: a 404 here means the plan is not this owner's (or no longer
 * exists), which the caller shows rather than silently doing nothing. Anything else is
 * rethrown for the caller's message helper.
 */
export async function downloadExport(
  tripId: string,
  format: ExportFormat,
  destination: string,
): Promise<string> {
  const path = `/api/v1/trips/${tripId}/${EXPORT_PATHS[format]}`;
  const response = await fetchWithAuth(path);
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new ApiError(response.status, detail.slice(0, 200) || `导出失败（HTTP ${response.status}）`);
  }

  const filename = parseContentDisposition(
    response.headers.get("Content-Disposition"),
    fallbackFilename(destination, format),
  );
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    // Revoked on the next tick: revoking synchronously can cancel the download in
    // some browsers before it has read the blob.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return filename;
}

/**
 * A `fetch` that carries the stored API key.
 *
 * `apiRequest` is not used because it parses the body as JSON and this response is a
 * document. The key is read from the same module `apiRequest` uses, so the two cannot
 * drift into using different credentials.
 */
async function fetchWithAuth(path: string): Promise<Response> {
  const headers: Record<string, string> = {};
  const key = getApiKey();
  if (key) headers.Authorization = `Bearer ${key}`;
  try {
    return await fetch(new URL(path, API_URL).toString(), { headers, cache: "no-store" });
  } catch (reason) {
    const detail = reason instanceof Error ? reason.message : "无法连接 API";
    throw new ApiError(0, detail);
  }
}
