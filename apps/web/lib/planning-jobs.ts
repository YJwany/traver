"use client";

/**
 * Client for long-running planning jobs and their progress stream.
 *
 * Two things here are not the obvious choice, and both are deliberate.
 *
 * **The stream is read with `fetch`, not `EventSource`.** `EventSource` cannot send an
 * `Authorization` header, so using it would mean putting the API key in the query string —
 * where it lands in server logs, proxy logs and browser history. Traver's key is the whole
 * of a user's access, so that trade is not available. `fetch` plus a `ReadableStream` reader
 * gives the same SSE frames with the header intact.
 *
 * **The stream is a convenience, not the mechanism.** A job is durable state on the server;
 * the stream only saves polling. If it fails — a proxy that buffers, a browser that drops
 * the connection — the caller falls back to polling and still gets correct answers. A
 * progress display that breaks when streaming breaks would be worse than no streaming.
 */

import { getApiKey } from "./auth";
import { ApiError } from "./api-client";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

/**
 * Whether a job creates a trip or re-plans one.
 *
 * The server reports this rather than leaving the client to infer it from `target_trip_id`
 * being set: the two mean different things in the UI (a new trip appears, versus the trip
 * already on screen being replaced), and inferring intent from a field that means something
 * else produces a screen that is right by accident.
 */
export type JobKind = "plan" | "refresh";

export type JobStep = {
  name: string;
  detail: string;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
};

export type PlanningJob = {
  id: string;
  status: JobStatus;
  kind: JobKind;
  profile: Record<string, unknown>;
  budget_strategy: string;
  steps: JobStep[];
  current_step: string | null;
  trip_id: string | null;
  /**
   * For a refresh, the trip being re-planned — known before the work starts.
   *
   * Distinct from `trip_id`, which is only the result. A client that just queued a refresh
   * needs the id it asked about, because the trip on screen is still the one being replaced.
   */
  target_trip_id: string | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  elapsed_ms: number | null;
  cancel_requested: boolean;
  is_cancellable: boolean;
};

/**
 * What each pipeline step is called in the UI.
 *
 * The names are the server's real stage boundaries, not a script: if planning is reordered
 * or a stage is removed, this list stops matching and the fallback below shows the raw name
 * rather than nothing. A progress display that silently omits an unknown step would hide
 * exactly the change someone needs to know about.
 *
 * That fallback is a safety net, not a licence to forget: an entry missing here renders as
 * `resolve_places` in the middle of a Chinese progress line, and the user cannot tell that
 * from a deliberate technical label. `test_web_api_contract.py` now reads the names the
 * server actually emits out of its own source and fails when this table does not cover them,
 * which is how `resolve_places` was caught.
 */
export const STEP_LABELS: Record<string, string> = {
  resolve_sources: "装配可用数据源",
  reload_trip: "读取已保存的行程与偏好",
  collect_places: "采集酒店、餐厅、景点、天气",
  room_offers: "查询房型与报价",
  enrich_attractions: "补充景点简介",
  resolve_places: "定位笔记里提到的地点",
  // Announced even when no source serves it, so the step list keeps the same shape on every
  // run. The label therefore has to be readable in the "skipped, no source" case too: 「查询
  // 近期评价」 says what would happen, and the step's own detail says 无来源，跳过.
  search_reviews: "查询近期评价",
  calculate_routes: "计算路线耗时",
  build_itinerary: "生成每日行程与预算",
  collect_health: "检查数据源状态",
  save_trip: "保存行程",
};

export function stepLabel(name: string): string {
  return STEP_LABELS[name] ?? name;
}

export function isTerminal(status: JobStatus): boolean {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}

/** A duration a human can read, from a value that may not exist yet. */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} 毫秒`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} 秒`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} 分 ${Math.round(seconds - minutes * 60)} 秒`;
}

/**
 * A one-line description of where a job is.
 *
 * Derived from the steps rather than from the status alone, because "running" tells a
 * waiting user nothing about whether progress is being made.
 *
 * Non-terminal states deliberately never use the word 已完成. "已完成 0 步" is
 * grammatically fine and reads as *done* to anyone skimming, which is the one thing a
 * progress line must never say while work is in flight.
 */
export function progressSummary(job: PlanningJob): string {
  const done = job.steps.filter((step) => step.finished_at).length;
  if (job.status === "queued") return "排队中";
  if (job.status === "succeeded") return "已完成";
  if (job.status === "cancelled") return "已取消";
  if (job.status === "failed") return "失败";
  if (job.current_step) {
    return `进行中 · ${stepLabel(job.current_step)}（${done}/${job.steps.length}）`;
  }
  if (job.steps.length) {
    return `进行中 · 已走完 ${done}/${job.steps.length} 步`;
  }
  return "进行中 · 正在准备";
}

async function jobRequest<T>(
  path: string,
  options: { method?: "GET" | "POST"; params?: Record<string, string> } = {},
): Promise<T> {
  const url = new URL(path, API_URL);
  for (const [key, value] of Object.entries(options.params ?? {})) {
    url.searchParams.set(key, value);
  }
  const headers: Record<string, string> = { Accept: "application/json" };
  const key = getApiKey();
  if (key) headers.Authorization = `Bearer ${key}`;

  let response: Response;
  try {
    response = await fetch(url.toString(), {
      method: options.method ?? "GET",
      headers,
      cache: "no-store",
    });
  } catch (reason) {
    throw new ApiError(0, reason instanceof Error ? reason.message : "无法连接 API");
  }
  const text = await response.text();
  let payload: unknown;
  try {
    payload = text ? JSON.parse(text) : undefined;
  } catch {
    payload = text;
  }
  if (!response.ok) {
    const detail =
      payload && typeof payload === "object" && "detail" in payload
        ? String((payload as { detail: unknown }).detail)
        : `请求失败（HTTP ${response.status}）`;
    throw new ApiError(response.status, detail);
  }
  return payload as T;
}

export async function submitPlanningJob(
  profile: Record<string, unknown>,
  options: { strategy?: string; queueTrip?: boolean } = {},
): Promise<PlanningJob> {
  const url = new URL("/api/v1/planning/jobs", API_URL);
  url.searchParams.set("strategy", options.strategy ?? "balanced");
  // Explicit, because "create a trip" and "just show me the plan" are different promises
  // and the default must not silently be the one that writes to the database.
  url.searchParams.set("queue_trip", options.queueTrip === false ? "false" : "true");

  const headers: Record<string, string> = {
    Accept: "application/json",
    "Content-Type": "application/json",
  };
  const key = getApiKey();
  if (key) headers.Authorization = `Bearer ${key}`;

  let response: Response;
  try {
    response = await fetch(url.toString(), {
      method: "POST",
      headers,
      body: JSON.stringify(profile),
      cache: "no-store",
    });
  } catch (reason) {
    throw new ApiError(0, reason instanceof Error ? reason.message : "无法连接 API");
  }
  const text = await response.text();
  let payload: unknown;
  try {
    payload = text ? JSON.parse(text) : undefined;
  } catch {
    payload = text;
  }
  if (!response.ok) {
    const detail =
      payload && typeof payload === "object" && "detail" in payload
        ? typeof (payload as { detail: unknown }).detail === "string"
          ? String((payload as { detail: unknown }).detail)
          : JSON.stringify((payload as { detail: unknown }).detail)
        : `请求失败（HTTP ${response.status}）`;
    throw new ApiError(response.status, detail);
  }
  return payload as PlanningJob;
}

export function getPlanningJob(jobId: string): Promise<PlanningJob> {
  return jobRequest<PlanningJob>(`/api/v1/planning/jobs/${jobId}`);
}

/**
 * Queue a re-plan of a stored trip, returning the job immediately.
 *
 * A refresh re-runs every provider, so it costs what a plan costs. Without this the trip
 * page could only offer a blocking request that freezes the browser for tens of seconds,
 * which is exactly the problem the job API exists to solve.
 *
 * A trip that does not exist — or belongs to someone else — is rejected here with a 404
 * rather than becoming a job that dies later, so the caller gets the answer immediately.
 */
export function submitRefreshJob(tripId: string): Promise<PlanningJob> {
  return jobRequest<PlanningJob>(`/api/v1/trips/${tripId}/refresh/jobs`, { method: "POST" });
}

export function cancelPlanningJob(jobId: string): Promise<PlanningJob> {
  return jobRequest<PlanningJob>(`/api/v1/planning/jobs/${jobId}/cancel`, { method: "POST" });
}

/**
 * Read the job's progress stream until it ends.
 *
 * Resolves when the server closes the stream, which it does at a terminal state. Each
 * update is a whole snapshot, so a caller never has to merge deltas — a late subscriber
 * and an early one end up with the same state.
 *
 * Throws on a transport failure so the caller can fall back to polling; it deliberately
 * does not retry internally, because a caller that cannot see a failure cannot decide
 * whether to keep waiting.
 */
export async function streamPlanningJob(
  jobId: string,
  onUpdate: (job: PlanningJob) => void,
  signal?: AbortSignal,
): Promise<void> {
  const headers: Record<string, string> = { Accept: "text/event-stream" };
  // The reason this is not `EventSource`: the key must not go in the URL.
  const key = getApiKey();
  if (key) headers.Authorization = `Bearer ${key}`;

  const response = await fetch(`${API_URL}/api/v1/planning/jobs/${jobId}/events`, {
    headers,
    signal,
    cache: "no-store",
  });
  if (!response.ok) {
    throw new ApiError(response.status, `进度流不可用（HTTP ${response.status}）`);
  }
  if (!response.body) {
    throw new ApiError(0, "浏览器不支持流式读取");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE frames are separated by a blank line. Frames that are still arriving are kept
      // in the buffer rather than parsed as truncated JSON.
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf("\n\n");
        for (const line of frame.split("\n")) {
          if (!line.startsWith("data: ")) continue; // ": keep-alive" comments land here
          try {
            onUpdate(JSON.parse(line.slice(6)) as PlanningJob);
          } catch {
            // A malformed frame is skipped rather than thrown: one bad event must not
            // discard the progress updates that follow it.
          }
        }
      }
    }
  } finally {
    reader.cancel().catch(() => undefined);
  }
}
