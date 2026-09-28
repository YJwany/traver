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

import { useCallback, useState } from "react";

import { ApiError } from "../../lib/api-client";
import { describeJobError, useJobProgress } from "../../lib/use-job-progress";
import { submitPlanningJob } from "../../lib/planning-jobs";
import { JobProgressPanel } from "./job-progress";

export type PlanFormValues = {
  destination: string;
  start_date: string;
  end_date: string;
  travelers: number;
  total_budget: number | null;
  pace: string;
  travel_mode: string;
  interests: string;
};

const EMPTY: PlanFormValues = {
  destination: "",
  start_date: "",
  end_date: "",
  travelers: 2,
  total_budget: null,
  pace: "balanced",
  travel_mode: "",
  interests: "",
};

/**
 * The three paces the API accepts: relaxed / balanced / full.
 *
 * These `value` strings are the wire format, not a display vocabulary. The third one
 * previously read "packed", which is not a member of the API's `Pace` enum, so choosing
 * 紧凑 returned HTTP 422 and no trip at all. A label is free to differ from the value;
 * the value is not free to differ from the API. `tests/unit/test_web_api_contract.py`
 * compares this list against the enum, because nothing else can see both sides.
 */
export const PACES = [
  { value: "relaxed", label: "轻松", hint: "每天 2 个点左右" },
  { value: "balanced", label: "适中", hint: "每天 3 个点左右" },
  { value: "full", label: "紧凑", hint: "尽量多走" },
];

/** Human wording for a stored pace. All three, so 紧凑 is not shown as 平衡. */
export const PACE_LABELS: Record<string, string> = {
  relaxed: "轻松节奏",
  balanced: "适中节奏",
  full: "紧凑节奏",
};

/**
 * How to move between places, or "let the pace decide".
 *
 * The empty value is not a fourth mode: it means "derive it from the pace", which is what
 * every trip did before this could be chosen. The submit handler omits the field entirely
 * in that case rather than sending null, so the API's own default decides.
 *
 * 公交 / 地铁 is listed first among the real modes because it is the one that needs a 高德
 * key — the same key whose absence the capability matrix already reports. Selecting it
 * without one produces estimated travel times rather than a refused plan, and those
 * estimates are labelled as estimates.
 */
export const TRAVEL_MODES = [
  { value: "", label: "跟着节奏走", hint: "由节奏决定步行或驾车" },
  { value: "transit", label: "公交 / 地铁", hint: "需要高德 Key，否则只能估算" },
  { value: "walk", label: "步行", hint: "按真实步行路网计算" },
  { value: "drive", label: "驾车", hint: "按真实道路计算" },
  { value: "taxi", label: "打车", hint: "按驾车路线加打车估价" },
];

/** Human wording for a stored travel mode, for the trip header. */
export const TRAVEL_MODE_LABELS: Record<string, string> = {
  transit: "公交 / 地铁",
  walk: "步行",
  drive: "驾车",
  taxi: "打车",
};

export function PlanForm({ apiUrl }: { apiUrl: string }) {
  const [values, setValues] = useState<PlanFormValues>(EMPTY);
  const [needsKey, setNeedsKey] = useState(false);
  const [submitError, setSubmitError] = useState("");
  // The following, polling-fallback and cancel logic is shared with the trip page's
  // re-plan button, so both screens agree on what a closed stream means.
  const { job, busy, error, notice, follow, cancel, reset } = useJobProgress(apiUrl);

  const update = useCallback(
    <K extends keyof PlanFormValues>(key: K, value: PlanFormValues[K]) =>
      setValues((current) => ({ ...current, [key]: value })),
    [],
  );

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!values.destination.trim() || !values.start_date || !values.end_date) return;
    setSubmitError("");
    setNeedsKey(false);
    reset();

    const profile: Record<string, unknown> = {
      destination: values.destination.trim(),
      start_date: values.start_date,
      end_date: values.end_date,
      travelers: values.travelers,
      pace: values.pace,
    };
    // Omitted rather than sent as null when unset: the field means "derive it from the
    // pace", and the API's own default is what should decide that, not this form.
    if (values.travel_mode) {
      profile.travel_mode = values.travel_mode;
    }
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
      await follow(created.id);
    } catch (reason) {
      setNeedsKey(reason instanceof ApiError && (reason.isUnauthorized || reason.isForbidden));
      setSubmitError(describeJobError(reason, apiUrl));
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
            <div className="credential-row" style={{ marginTop: 10 }}>
              <span>城际交通</span>
              <div className="example-row">
                {TRAVEL_MODES.map((mode) => (
                  <button
                    key={mode.value || "auto"}
                    type="button"
                    className={`example ${values.travel_mode === mode.value ? "example-active" : ""}`}
                    onClick={() => update("travel_mode", mode.value)}
                    aria-pressed={values.travel_mode === mode.value}
                  >
                    {mode.label} · {mode.hint}
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
          {submitError && (
            <div className="error" style={{ margin: "14px 0" }} role="status">
              {submitError}
            </div>
          )}
          {needsKey && (
            <div className="muted" style={{ marginTop: 8 }}>
              密钥在<a href="/settings">设置</a>里填写。
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
        <JobProgressPanel
          job={job}
          onCancel={cancel}
          notice={notice}
          successContent={
            succeeded ? (
              <div className="notice" style={{ marginTop: 12 }} role="status">
                已完成。{" "}
                <a
                  href={`/trips/${job.trip_id}`}
                  style={{ color: "inherit", textDecoration: "underline" }}
                >
                  查看行程 →
                </a>
              </div>
            ) : null
          }
        />
      )}
    </>
  );
}
