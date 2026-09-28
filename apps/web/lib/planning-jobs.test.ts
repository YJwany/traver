import { describe, expect, it, vi } from "vitest";

import {
  formatDuration,
  isTerminal,
  progressSummary,
  stepLabel,
  submitRefreshJob,
  STEP_LABELS,
  type JobStatus,
  type PlanningJob,
} from "./planning-jobs";

/**
 * The wording that keeps the progress display honest.
 *
 * The failure this guards against is a progress indicator that looks informative but is
 * not: a simulation that advances on its own, a step name that silently renders blank, a
 * duration that is invented rather than measured. Each of those teaches the user to ignore
 * the display, which costs exactly the information it existed to provide.
 */

function job(overrides: Partial<PlanningJob> = {}): PlanningJob {
  return {
    id: "job-1",
    status: "running",
    kind: "plan",
    profile: {},
    budget_strategy: "balanced",
    steps: [],
    current_step: null,
    trip_id: null,
    target_trip_id: null,
    error: null,
    created_at: "2026-09-28T10:00:00Z",
    started_at: "2026-09-28T10:00:01Z",
    finished_at: null,
    elapsed_ms: 1000,
    cancel_requested: false,
    is_cancellable: true,
    ...overrides,
  };
}

describe("stepLabel", () => {
  it("names every step the server currently emits", () => {
    // If a stage is renamed server-side this list stops matching, and the test below
    // catches the fallback rather than this one — which is the intended behaviour.
    expect(stepLabel("collect_places")).toBe("采集酒店、餐厅、景点、天气");
    expect(stepLabel("calculate_routes")).toBe("计算路线耗时");
    expect(stepLabel("save_trip")).toBe("保存行程");
  });

  it("shows the raw name for a step it does not know", () => {
    // A blank label would hide a pipeline change that someone needs to know about, so an
    // unknown step is visible even if it is not pretty.
    expect(stepLabel("brand_new_stage")).toBe("brand_new_stage");
  });

  it("has a non-empty label for every mapped step", () => {
    for (const [name, label] of Object.entries(STEP_LABELS)) {
      expect(label, name).not.toBe("");
    }
  });
});

describe("isTerminal", () => {
  it("treats exactly the three end states as terminal", () => {
    // `cancelled` is terminal even though it is neither success nor failure: the job will
    // not progress further, so polling must stop.
    expect(isTerminal("succeeded")).toBe(true);
    expect(isTerminal("failed")).toBe(true);
    expect(isTerminal("cancelled")).toBe(true);
    expect(isTerminal("running")).toBe(false);
    expect(isTerminal("queued")).toBe(false);
  });

  it("covers every status the type allows", () => {
    const statuses: JobStatus[] = ["queued", "running", "succeeded", "failed", "cancelled"];
    expect(statuses.filter(isTerminal)).toHaveLength(3);
  });
});

describe("formatDuration", () => {
  it("uses units a reader can act on", () => {
    // The point of this feature is that 36 seconds feels long, so the number must be
    // legible as seconds rather than as 36000 milliseconds.
    expect(formatDuration(500)).toBe("500 毫秒");
    expect(formatDuration(1500)).toBe("1.5 秒");
    expect(formatDuration(36500)).toBe("36.5 秒");
    expect(formatDuration(90000)).toBe("1 分 30 秒");
  });

  it("says nothing rather than zero when there is no measurement", () => {
    // An unfinished step has no duration, and reporting 0 ms would look like a measurement
    // that said "instant" — which is a claim, not an absence.
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(undefined)).toBe("—");
  });

  it("does not round a sub-second duration up to a second", () => {
    // Otherwise every trivial step reports "1 秒" and real slowness stops standing out.
    expect(formatDuration(1)).toBe("1 毫秒");
    expect(formatDuration(999)).toBe("999 毫秒");
  });
});

describe("progressSummary", () => {
  it("names the stage that is running, not just the state", () => {
    // "running" alone tells a waiting user nothing about whether anything is happening.
    const summary = progressSummary(
      job({
        steps: [
          {
            name: "collect_places",
            detail: "",
            started_at: "2026-09-28T10:00:02Z",
            finished_at: null,
            duration_ms: null,
          },
        ],
        current_step: "collect_places",
      }),
    );

    expect(summary).toContain("采集酒店、餐厅、景点、天气");
    expect(summary).toContain("0/1");
  });

  it("counts finished steps so progress is visible between stages", () => {
    const summary = progressSummary(
      job({
        steps: [
          {
            name: "collect_places",
            detail: "",
            started_at: "2026-09-28T10:00:02Z",
            finished_at: "2026-09-28T10:00:30Z",
            duration_ms: 28000,
          },
          {
            name: "calculate_routes",
            detail: "",
            started_at: "2026-09-28T10:00:30Z",
            finished_at: "2026-09-28T10:00:40Z",
            duration_ms: 10000,
          },
        ],
        current_step: null,
      }),
    );

    expect(summary).toBe("进行中 · 已走完 2/2 步");
  });

  it("describes a job that has not produced a step yet", () => {
    // The gap between submitting and the first stage opening is real and can be visible,
    // so it gets a description rather than "已完成 0 步".
    expect(progressSummary(job({ steps: [] }))).toBe("进行中 · 正在准备");
  });

  it("describes each terminal state distinctly", () => {
    expect(progressSummary(job({ status: "queued" }))).toBe("排队中");
    expect(progressSummary(job({ status: "succeeded" }))).toBe("已完成");
    expect(progressSummary(job({ status: "failed" }))).toBe("失败");
    expect(progressSummary(job({ status: "cancelled" }))).toBe("已取消");
  });

  it("never uses the word for done while the work is still in flight", () => {
    // The specific misreading this prevents: "已完成 0 步" is grammatical and reads as
    // *finished* to anyone skimming, which would have the user leave the page.
    for (const status of ["queued", "running"] as JobStatus[]) {
      for (const steps of [[], job().steps]) {
        expect(progressSummary(job({ status, steps }))).not.toContain("已完成");
      }
    }
  });
});

describe("re-planning an existing trip", () => {
  it("posts to the trip's own refresh endpoint, not the planning collection", async () => {
    // The distinction is not cosmetic: posting to /planning/jobs would create a *second*
    // trip instead of re-planning this one, which is data loss dressed up as a refresh.
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 202,
      text: async () =>
        JSON.stringify({
          id: "job-9",
          status: "queued",
          kind: "refresh",
          profile: {},
          budget_strategy: "balanced",
          steps: [],
          current_step: null,
          trip_id: null,
          target_trip_id: "trip-9",
          error: null,
          created_at: "2026-09-28T10:00:00Z",
          started_at: null,
          finished_at: null,
          elapsed_ms: null,
          cancel_requested: false,
          is_cancellable: true,
        }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const queued = await submitRefreshJob("trip-9");

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/v1/trips/trip-9/refresh/jobs");
    expect(String(url)).not.toContain("/api/v1/planning/jobs");
    expect(init.method).toBe("POST");
    // The trip being re-planned is known before the work starts, unlike trip_id.
    expect(queued.target_trip_id).toBe("trip-9");
    expect(queued.trip_id).toBeNull();
    expect(queued.kind).toBe("refresh");

    vi.unstubAllGlobals();
  });

  it("names the extra step a refresh emits", () => {
    // The server publishes reload_trip for a refresh. An unmapped step would render as the
    // raw identifier in the one panel a waiting user is reading.
    expect(stepLabel("reload_trip")).toBe("读取已保存的行程与偏好");
    expect(stepLabel("reload_trip")).not.toContain("_");
  });
});
