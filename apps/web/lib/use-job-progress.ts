"use client";

/**
 * Following a job to its end, as one reusable piece.
 *
 * This was lifted out of the new-trip form when the trip page needed the same thing for
 * re-planning. The duplication it removes is not just code: the fallback rules below are
 * the part that has to be right, and two copies would eventually disagree about whether a
 * closed stream means "finished" or "a proxy gave up".
 *
 * Three commitments, all inherited from the API's design:
 *
 * **Terminal state is read explicitly, never inferred from the stream.** A stream closes at
 * a terminal state, but also when a proxy buffers or a network drops. So the stream ending
 * triggers one authoritative `GET`; only that decides whether work is over.
 *
 * **Losing the stream is not losing the job.** Polling takes over, and the caller is told
 * which mechanism is in use rather than being left to guess why updates slowed down.
 *
 * **A cancelled or failed job is reported by the server's own message.** Nothing is
 * paraphrased here, because the server's error is the only thing that makes a failure
 * fixable.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "./api-client";
import {
  cancelPlanningJob,
  getPlanningJob,
  isTerminal,
  streamPlanningJob,
  type PlanningJob,
} from "./planning-jobs";

export type JobProgress = {
  job: PlanningJob | null;
  /** True while the job is being started or followed. */
  busy: boolean;
  error: string;
  /** Set when streaming failed and polling took over, so the UI can say so. */
  notice: string;
  /** Start following an already-created job. */
  follow: (jobId: string) => Promise<void>;
  cancel: () => Promise<void>;
  reset: () => void;
};

/**
 * A message for a failure, preferring the server's own words.
 *
 * `needsKey` is not inferred here: a 401 and a 403 need different fixes, and the caller
 * knows which screens it has to offer.
 */
export function describeJobError(reason: unknown, apiUrl: string): string {
  if (reason instanceof ApiError) {
    if (reason.status === 0) return `无法连接 API（${apiUrl}）：${reason.detail}`;
    if (reason.isUnauthorized) return "需要先在设置页填写接口密钥。";
    if (reason.isForbidden) return "当前密钥缺少 trip:write 权限。";
    return reason.detail;
  }
  return reason instanceof Error ? reason.message : "请求失败";
}

export function useJobProgress(apiUrl: string): JobProgress {
  const [job, setJob] = useState<PlanningJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const streamAbort = useRef<AbortController | null>(null);
  // The job id for `cancel` comes from a ref, not from a state updater: an updater must be
  // pure, and React is allowed to call it twice (it does in StrictMode), which would send
  // two cancels for one click.
  const jobRef = useRef<PlanningJob | null>(null);

  const track = useCallback((next: PlanningJob | null) => {
    jobRef.current = next;
    setJob(next);
  }, []);

  useEffect(
    () => () => {
      streamAbort.current?.abort();
    },
    [],
  );

  const poll = useCallback(
    async (jobId: string) => {
      for (;;) {
        await new Promise((resolve) => setTimeout(resolve, 1500));
        if (streamAbort.current?.signal.aborted) return;
        try {
          const current = await getPlanningJob(jobId);
          track(current);
          if (isTerminal(current.status)) {
            setBusy(false);
            return;
          }
        } catch (reason) {
          setError(describeJobError(reason, apiUrl));
          setBusy(false);
          return;
        }
      }
    },
    [apiUrl, track],
  );

  const follow = useCallback(
    async (jobId: string) => {
      setBusy(true);
      setError("");
      setNotice("");
      const controller = new AbortController();
      streamAbort.current = controller;
      try {
        await streamPlanningJob(
          jobId,
          (update) => {
            track(update);
            if (isTerminal(update.status)) setBusy(false);
          },
          controller.signal,
        );
        // Authoritative read: the stream closing is not evidence that the work is done.
        const settled = await getPlanningJob(jobId);
        track(settled);
        if (isTerminal(settled.status)) {
          setBusy(false);
        } else {
          await poll(jobId);
        }
      } catch (reason) {
        if (controller.signal.aborted) {
          setBusy(false);
          return; // the user navigated away
        }
        setNotice(`进度流不可用，改为轮询：${describeJobError(reason, apiUrl)}`);
        await poll(jobId);
      }
    },
    [apiUrl, poll, track],
  );

  const cancel = useCallback(async () => {
    const current = jobRef.current;
    if (!current) return;
    try {
      track(await cancelPlanningJob(current.id));
    } catch (reason) {
      setError(describeJobError(reason, apiUrl));
    } finally {
      streamAbort.current?.abort();
      setBusy(false);
    }
  }, [apiUrl, track]);

  const reset = useCallback(() => {
    streamAbort.current?.abort();
    track(null);
    setError("");
    setNotice("");
    setBusy(false);
  }, [track]);

  return { job, busy, error, notice, follow, cancel, reset };
}
