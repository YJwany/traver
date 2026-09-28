import { describe, expect, it, vi } from "vitest";

import { describeJobError, useJobProgress } from "./use-job-progress";
import { ApiError } from "./api-client";
import { getPlanningJob, streamPlanningJob } from "./planning-jobs";
import { act, renderHook, waitFor } from "@testing-library/react";

/**
 * Following a job, which is where the honesty of the progress display is decided.
 *
 * The two failures these guard against are the ones that look like success:
 *
 *   * treating a closed stream as proof the work finished. A stream also closes when a
 *     proxy buffers or a phone changes network, and a job reported as done while it is
 *     still running shows the traveller a plan that is about to change underneath them.
 *   * losing the job when the stream is unavailable. Streaming is a convenience; if the
 *     fallback is missing, a buffering proxy becomes a failed plan.
 */

vi.mock("./planning-jobs", async () => {
  const actual = await vi.importActual<typeof import("./planning-jobs")>("./planning-jobs");
  return {
    ...actual,
    getPlanningJob: vi.fn(),
    streamPlanningJob: vi.fn(),
    cancelPlanningJob: vi.fn(),
    submitRefreshJob: vi.fn(),
  };
});

const API = "http://api.test";

function aJob(overrides: Record<string, unknown> = {}) {
  return {
    id: "job-1",
    status: "running" as const,
    kind: "refresh" as const,
    profile: {},
    budget_strategy: "balanced",
    steps: [],
    current_step: null,
    trip_id: null,
    target_trip_id: "trip-1",
    error: null,
    created_at: "2026-09-28T10:00:00Z",
    started_at: "2026-09-28T10:00:01Z",
    finished_at: null,
    elapsed_ms: 1000,
    cancel_requested: false,
    is_cancellable: true,
    ...overrides,
  } as never;
}

describe("describeJobError", () => {
  it("distinguishes a missing key from a key without permission", () => {
    // Different fixes: one is a credential the user has not added, the other is a
    // credential that exists but cannot write. Collapsing them sends half the users to
    // the wrong screen.
    expect(describeJobError(new ApiError(401, "nope"), API)).toContain("填写接口密钥");
    expect(describeJobError(new ApiError(403, "nope"), API)).toContain("trip:write");
  });

  it("names the API it could not reach", () => {
    // The URL is the fix for a connection failure, so it has to be in the message.
    expect(describeJobError(new ApiError(0, "ECONNREFUSED"), API)).toContain(API);
  });

  it("prefers the server's own words", () => {
    expect(describeJobError(new ApiError(500, "overpass timed out"), API)).toBe(
      "overpass timed out",
    );
  });
});

describe("useJobProgress", () => {
  it("does not treat a closed stream as a finished job", async () => {
    // The stream ends without ever reaching a terminal state — which is what a buffering
    // proxy does. The authoritative read must decide, not the stream closing.
    vi.mocked(streamPlanningJob).mockResolvedValue(undefined);
    vi.mocked(getPlanningJob).mockResolvedValue(aJob({ status: "running" }));

    const { result } = renderHook(() => useJobProgress(API));
    // The job stays busy rather than being reported as done.
    await act(async () => {
      void result.current.follow("job-1");
    });

    expect(vi.mocked(getPlanningJob)).toHaveBeenCalledWith("job-1");
    expect(result.current.job?.status).toBe("running");
  });

  it("falls back to polling when streaming is unavailable, and says so", async () => {
    vi.mocked(streamPlanningJob).mockRejectedValue(new ApiError(0, "stream broke"));
    vi.mocked(getPlanningJob).mockResolvedValue(aJob({ status: "succeeded", trip_id: "trip-1" }));

    const { result } = renderHook(() => useJobProgress(API));
    await act(async () => {
      await result.current.follow("job-1");
    });

    // The job survives the transport failure: that is the whole point of the fallback.
    await waitFor(() => expect(result.current.job?.status).toBe("succeeded"));
    // And the user is told which mechanism is in use rather than left guessing.
    expect(result.current.notice).toContain("轮询");
    expect(result.current.error).toBe("");
  });

  it("reads the final state explicitly after the stream closes", async () => {
    // The stream's last frame can be one step behind the server. Without the extra read,
    // a succeeded job can be displayed as still running.
    vi.mocked(streamPlanningJob).mockImplementation(async (_id, onUpdate) => {
      onUpdate(aJob({ status: "running" }));
    });
    vi.mocked(getPlanningJob).mockResolvedValue(aJob({ status: "succeeded", trip_id: "trip-1" }));

    const { result } = renderHook(() => useJobProgress(API));
    await act(async () => {
      await result.current.follow("job-1");
    });

    expect(result.current.job?.status).toBe("succeeded");
    expect(result.current.busy).toBe(false);
  });

  it("surfaces the transport failure when polling also fails", async () => {
    // Both mechanisms gone is a real failure, and it must not be silent.
    vi.mocked(streamPlanningJob).mockRejectedValue(new ApiError(0, "stream broke"));
    vi.mocked(getPlanningJob).mockRejectedValue(new ApiError(500, "api on fire"));

    const { result } = renderHook(() => useJobProgress(API));
    await act(async () => {
      await result.current.follow("job-1");
    });

    await waitFor(() => expect(result.current.error).toContain("api on fire"));
    expect(result.current.busy).toBe(false);
  });
});
