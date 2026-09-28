"use client";

/**
 * The one place a job's progress is rendered.
 *
 * Shared by the new-trip form and the trip page's re-plan button so a traveller learns the
 * display once. The alternative — a second, slightly different progress panel for refreshes
 * — would also mean two places to keep the no-phantom-progress rule honest.
 *
 * The rule that matters: **the display shows what the server measured and nothing else.**
 * There is no animated bar, because a bar that advances while nothing happens teaches the
 * reader to ignore the one signal that could reveal a stall. An unfinished step says
 * 进行中; a finished one shows its real duration; a step with no measurement shows nothing
 * rather than a zero that would look like one.
 */

import { formatDuration, isTerminal, progressSummary, stepLabel, type PlanningJob } from "../../lib/planning-jobs";

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
            <span className="step-time">
              {step.finished_at ? formatDuration(step.duration_ms) : "进行中"}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

export type JobProgressPanelProps = {
  job: PlanningJob;
  /** Shown instead of the generic heading, e.g. 正在重新规划. */
  title?: string;
  onCancel?: () => void;
  /** Rendered on success; the caller decides what the success means. */
  successContent?: React.ReactNode;
  /** Extra transport-level message, such as a fallback to polling. */
  notice?: string;
};

export function JobProgressPanel({
  job,
  title,
  onCancel,
  successContent,
  notice,
}: JobProgressPanelProps) {
  const running = !isTerminal(job.status);
  return (
    <section className="card catalog-panel" style={{ marginTop: 18 }}>
      <div className="api-key-head">
        <div>
          <div className="eyebrow">PLANNING / PROGRESS</div>
          <h2 style={{ margin: "6px 0 4px" }}>{title ?? progressSummary(job)}</h2>
          {title && <div className="muted">{progressSummary(job)}</div>}
        </div>
        <div className="provider-badges">
          <span className={`status-chip status-${job.status}`}>
            用时 {formatDuration(job.elapsed_ms)}
          </span>
        </div>
      </div>

      <StepList job={job} />

      {notice && (
        <div className="notice" style={{ marginTop: 12 }} role="status">
          {notice}
        </div>
      )}

      {job.status === "failed" && (
        <div className="error" style={{ marginTop: 12 }} role="alert">
          {/* The server's own message, not a paraphrase: it is the only thing that makes
              the failure actionable. */}
          {job.kind === "refresh" ? "重新规划失败" : "规划失败"}：{job.error}
        </div>
      )}

      {job.status === "cancelled" && (
        <div className="notice" style={{ marginTop: 12 }} role="status">
          已取消。这次没有产生新的行程。
        </div>
      )}

      {job.status === "succeeded" && successContent}

      <div className="credential-help" style={{ marginTop: 14 }}>
        {running && job.is_cancellable && onCancel ? (
          <button className="button secondary" type="button" onClick={onCancel}>
            取消
          </button>
        ) : null}
      </div>

      <p className="muted" style={{ margin: "14px 0 0", fontSize: 12, lineHeight: 1.6 }}>
        每一步的耗时都是真实测量的：没有进度动画，因为动画会在什么都没发生时继续前进。
        某一步慢，就在这里显示为慢。
      </p>
    </section>
  );
}
