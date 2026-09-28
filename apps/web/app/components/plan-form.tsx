"use client";

/**
 * Structured trip planning with visible progress.
 *
 * This screen exists for two reasons that turn out to be the same reason.
 *
 * The first is honesty about time. A real plan takes tens of seconds — one upstream query
 * was measured at 36.5 s — and a form that simply freezes for that long is
 * indistinguishable from one that has crashed. The job API turns the wait into a list of
 * stages with real elapsed times.
 *
 * The second is 渐进增强. Until now the only way to create a trip in the browser was to
 * describe it in natural language, which needs a model. A traveller who had configured
 * nothing could not get a plan from the UI at all, even though the API produces one with
 * zero credentials. This form asks for the fields directly and needs no model — so it is
 * the path that always works, and the AI path becomes the enhancement.
 *
 * On the progress itself: **it shows what the server says, never a simulation.** The steps
 * come from the pipeline's real stage boundaries. There is no animated bar, because a bar
 * that advances while nothing happens teaches the user to distrust the one signal that
 * could have told them something was wrong. If a step is slow, that is visible as a slow
 * step; if no step is running, nothing moves.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../../lib/api-client";
import {
  cancelPlanningJob,
  formatDuration,
  getPlanningJob,
  isTerminal,
  progressSummary,
  stepLabel,
  streamPlanningJob,
  submitPlanningJob,
  type PlanningJob,
} from "../../lib/planning-jobs";

export type PlanFormValues = {
  destination: string;
  start_date: string;
  end_date: string;
  travelers: number;
  total_budget: number | null;
  pace: string;
  interests: string;
};

const EMPTY: PlanFormValues = {
  destination: "",
  start_date: "",
  end_date: "",
  travelers: 2,
  total_budget: null,
  pace: "balanced",
  interests: "",
};

const PACES = [
  { value: "relaxed", label: "轻松", hint: "每天 2 个点左右" },
  { value: "balanced", label: "适中", hint: "每天 3 个点左右" },
  { value: "packed", label: "紧凑", hint: "尽量多走" },
];

function StepList({ job }: { job: PlanningJob }) {
  if (!job.steps.length) {
    return (
      <div className="muted" style={{ fontSize: 13 }}>
        正在装配数据源…
      </div>
    );
  }
  return (
    <ol className="step-list">
      {job.steps.map((step, index) => {
        const running = !step.finished_at && isTerminal(job.status) === false;
        return (
          <li
            // Steps repeat their names across runs, so the index is part of the key.
            key={`${step.name}:${index}`}
            className={`step-item ${step.finished_at ? "step-done" : "step-active"}`}
          >
            <span className="step-mark" aria-hidden="true">
              {step.finished_at ? "✓" : running ? "◐" : "·"}
            </span>
            <span className="step-name">{stepLabel(step.name)}</span>
            {step.detail && <span className="step-detail">{step.detail}</span>}
            {/* The real measured duration, or nothing. A placeholder would imply a
                measurement that has not happened. */}
            <span className="step-time">
              {step.finished_at ? formatDuration(step.duration_ms) : "进行中"}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

export function PlanForm({ apiUrl }: { apiUrl: string }) {
  const [values, setValues] = useState<PlanFormValues>(EMPTY);
  const [job, setJob] = useState<PlanningJob | null>(null);
  const [error, setError] = useState("");
  const [needsKey, setNeedsKey] = useState(false);
  const [busy, setBusy] = useState(false);
  const streamAbort = useRef<AbortController | null>(null);

  useEffect(
    () => () => {
      streamAbort.current?.abort();
    },
    [],
  );

  const update = useCallback(
    <K extends keyof PlanFormValues>(key: K, value: PlanFormValues[K]) =>
      setValues((current) => ({ ...current, [key]: value })),
    [],
  );

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!values.destination.trim() || !values.start_date || !values.end_date) return;
    setBusy(true);
    setError("");
    setNeedsKey(false);
    setJob(null);

    const profile: Record<string, unknown> = {
      destination: values.destination.trim(),
      start_date: values.start_date,
      end_date: values.end_date,
      travelers: values.travelers,
      pace: values.pace,
    };
    if (values.total_budget !== null && values.total_budget > 0) {
      profile.total_budget = values.total_budget;
    }
    const interests = values.interests
      .split(/[,，、\s]+/)
      .map((item) => item.trim())
      .filter(Boolean);
    if (interests.length) profile.interests = interests;

    try {
      const created = await submitPlanningJob(profile, { queueTrip: true });
      setJob(created);
      await follow(created.id);
    } catch (reason) {
      setNeedsKey(reason instanceof ApiError && (reason.isUnauthorized || reason.isForbidden));
      setError(describe(reason, apiUrl));
      setBusy(false);
    }
  }

  /** Stream to the end, falling back to polling if streaming is unavailable. */
  async function follow(jobId: string) {
    const controller = new AbortController();
    streamAbort.current = controller;
    try {
      await streamPlanningJob(
        jobId,
        (update) => {
          setJob(update);
          if (isTerminal(update.status)) setBusy(false);
        },
        controller.signal,
      );
      // The stream closes at a terminal state, but a proxy may close it early — so the
      // final state is always read explicitly rather than assumed from the stream.
      const settled = await getPlanningJob(jobId);
      setJob(settled);
      if (isTerminal(settled.status)) setBusy(false);
      else await poll(jobId);
    } catch (reason) {
      if (controller.signal.aborted) return; // the user navigated away
      // Streaming is a convenience. Losing it must not lose the job.
      setError(`进度流不可用，改为轮询：${describe(reason, apiUrl)}`);
      await poll(jobId);
    }
  }

  async function poll(jobId: string) {
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      if (streamAbort.current?.signal.aborted) return;
      try {
        const current = await getPlanningJob(jobId);
        setJob(current);
        if (isTerminal(current.status)) {
          setBusy(false);
          return;
        }
      } catch (reason) {
        setError(describe(reason, apiUrl));
        setBusy(false);
        return;
      }
    }
  }

  async function cancel() {
    if (!job) return;
    try {
      setJob(await cancelPlanningJob(job.id));
    } catch (reason) {
      setError(describe(reason, apiUrl));
    } finally {
      streamAbort.current?.abort();
      setBusy(false);
    }
  }

  const succeeded = job?.status === "succeeded" && job.trip_id;

  return (
    <>
      <section className="form-card card">
        <form onSubmit={submit}>
          <div className="unlock-grid">
            <label className="credential-row">
              <span>目的地</span>
              <input
                className="textarea"
                style={{ minHeight: 0, padding: "9px 11px" }}
                value={values.destination}
                onChange={(event) => update("destination", event.target.value)}
                placeholder="成都"
                required
              />
            </label>
            <label className="credential-row">
              <span>人数</span>
              <input
                className="textarea"
                style={{ minHeight: 0, padding: "9px 11px" }}
                type="number"
                min={1}
                max={20}
                value={values.travelers}
                onChange={(event) => update("travelers", Number(event.target.value) || 1)}
              />
            </label>
            <label className="credential-row">
              <span>出发日期</span>
              <input
                className="textarea"
                style={{ minHeight: 0, padding: "9px 11px" }}
                type="date"
                value={values.start_date}
                onChange={(event) => update("start_date", event.target.value)}
                required
              />
            </label>
            <label className="credential-row">
              <span>返回日期</span>
              <input
                className="textarea"
                style={{ minHeight: 0, padding: "9px 11px" }}
                type="date"
                value={values.end_date}
                onChange={(event) => update("end_date", event.target.value)}
                required
              />
            </label>
            <label className="credential-row">
              <span>总预算（可留空）</span>
              <input
                className="textarea"
                style={{ minHeight: 0, padding: "9px 11px" }}
                type="number"
                min={0}
                value={values.total_budget ?? ""}
                onChange={(event) =>
                  update("total_budget", event.target.value ? Number(event.target.value) : null)
                }
                placeholder="5000"
              />
            </label>
            <label className="credential-row">
              <span>偏好（逗号分隔，可留空）</span>
              <input
                className="textarea"
                style={{ minHeight: 0, padding: "9px 11px" }}
                value={values.interests}
                onChange={(event) => update("interests", event.target.value)}
                placeholder="美食, 博物馆"
              />
            </label>
          </div>

          <div style={{ marginTop: 14 }}>
            <div className="credential-row">
              <span>节奏</span>
              <div className="example-row">
                {PACES.map((pace) => (
                  <button
                    key={pace.value}
                    type="button"
                    className={`example ${values.pace === pace.value ? "example-active" : ""}`}
                    onClick={() => update("pace", pace.value)}
                    aria-pressed={values.pace === pace.value}
                  >
                    {pace.label} · {pace.hint}
                  </button>
                ))}
              </div>
            </div>
          </div>

          {error && (
            <div className="error" style={{ margin: "14px 0" }} role="status">
              {error}
            </div>
          )}

          <div className="credential-help" style={{ marginTop: 16 }}>
            <button
              className="button"
              type="submit"
              disabled={busy || !values.destination.trim()}
            >
              {busy ? "正在规划…" : "开始规划 →"}
            </button>
            {busy && job ? (
              <button className="button secondary" type="button" onClick={cancel}>
                取消
              </button>
            ) : null}
          </div>
        </form>
      </section>

      {job && (
        <section className="card catalog-panel" style={{ marginTop: 18 }}>
          <div className="api-key-head">
            <div>
              <div className="eyebrow">PLANNING / PROGRESS</div>
              <h2 style={{ margin: "6px 0 4px" }}>{progressSummary(job)}</h2>
            </div>
            <div className="provider-badges">
              <span className={`status-chip status-${job.status}`}>
                用时 {formatDuration(job.elapsed_ms)}
              </span>
            </div>
          </div>

          <StepList job={job} />

          {job.status === "failed" && (
            <div className="error" style={{ marginTop: 12 }} role="alert">
              {/* The server's own message, not a paraphrase: it is the only thing that
                  makes the failure actionable. */}
              规划失败：{job.error}
            </div>
          )}

          {job.status === "cancelled" && (
            <div className="notice" style={{ marginTop: 12 }} role="status">
              已取消。这次规划没有产生行程。
            </div>
          )}

          {succeeded && (
            <div className="notice" style={{ marginTop: 12 }} role="status">
              已完成。{" "}
              <a href={`/trips/${job.trip_id}`} style={{ color: "inherit", textDecoration: "underline" }}>
                查看行程 →
              </a>
            </div>
          )}

          <p className="muted" style={{ margin: "14px 0 0", fontSize: 12, lineHeight: 1.6 }}>
            每一步的耗时都是真实测量的：没有进度动画，因为动画会在什么都没发生时继续前进。
            某一步慢，就在这里显示为慢。
          </p>
        </section>
      )}
    </>
  );
}

function describe(reason: unknown, apiUrl: string): string {
  if (reason instanceof ApiError) {
    if (reason.status === 0) return `无法连接 API（${apiUrl}）：${reason.detail}`;
    if (reason.isUnauthorized) return "需要先在设置页填写接口密钥。";
    if (reason.isForbidden) return "当前密钥缺少 trip:write 权限。";
    return reason.detail;
  }
  return reason instanceof Error ? reason.message : "规划请求失败";
}
