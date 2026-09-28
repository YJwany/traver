"use client";

/**
 * API key storage for the browser.
 *
 * There is deliberately no `NEXT_PUBLIC_API_KEY`. Every `NEXT_PUBLIC_*` value is
 * inlined into the JavaScript bundle at build time, so a key placed there is
 * readable by anyone who loads the page — it would look like security while
 * providing none. The key is instead entered by the operator and held in
 * `localStorage`, which at least confines it to that one browser profile.
 *
 * This is honest about its limits: `localStorage` is readable by any script on
 * the origin, so a single XSS defeats it. That is an acceptable trade for a
 * self-hosted planning tool talking to your own API, and it is the reason the
 * key is not treated as a high-value credential. A shared or public deployment
 * should serve the API behind a real session (httpOnly cookie), not this.
 */

const STORAGE_KEY = "traver.apiKey";

/** Fired on the window when the stored key changes, so hooks can re-read it. */
export const API_KEY_CHANGE_EVENT = "traver:api-key-change";

function notify(): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(API_KEY_CHANGE_EVENT));
  }
}

export function getApiKey(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    // Private browsing modes can throw on access; treat as "no key".
    return "";
  }
}

export function setApiKey(key: string): void {
  if (typeof window === "undefined") return;
  const trimmed = key.trim();
  try {
    if (trimmed) window.localStorage.setItem(STORAGE_KEY, trimmed);
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* storage unavailable; the caller surfaces the failure on the next request */
  }
  notify();
}

export function clearApiKey(): void {
  setApiKey("");
}

/**
 * Whether a string is shaped like a Traver key.
 *
 * Checked before storing so an obvious paste error is caught locally instead of
 * producing a confusing 401 later. The prefix is the only part verifiable
 * client-side; the API remains the authority.
 */
export function looksLikeApiKey(value: string): boolean {
  return /^trv_[A-Za-z0-9_-]{16,}$/.test(value.trim());
}
