"use client";

/**
 * Dated opinions about the places in the plan.
 *
 * The panel exists because the requirement for this tool was a rule about *time*: 「只能用近期的
 * 评价……不能是那种几年前那种」. A bare `rating` float cannot satisfy that rule — 「4.8 分」 from 2019
 * and 「4.8 分」 from last month are the same number and different facts — so a review carries
 * `published_at` and the panel's job is to put that date in front of the reader rather than
 * quietly ranking by it.
 *
 * Three decisions worth stating, because each one had a wrong version first:
 *
 *   - **Nothing is rendered when there are no reviews.** Absent is the normal state here, not an
 *     empty one: no source this deployment can reach publishes guest scores, which is also why
 *     every hotel and restaurant card shows no rating. An empty box would read as a broken
 *     feature; the settings page is where 「没有来源」 belongs, and it says so by name.
 *   - **No recency threshold is computed here.** The horizon lives on the server
 *     (`MAX_AGE_SECONDS["review"]`) and its verdict arrives in `stale_facts`, which the banner
 *     above this panel already renders. The first draft of this file re-derived "older than 18
 *     months" as a literal 548 days — the same rule written twice, free to drift, and already
 *     wrong by eight days.
 *   - **An undated review is reported as undated.** Never as recent, and never by substituting
 *     our fetch time: that substitution is exactly the mistake the whole 时效性 rule is about.
 */

import type { Review } from "../../lib/reviews";

function scoreText(review: Review): string {
  if (review.rating === null || review.rating === undefined) return "未给出分数";
  // A source's own scale is shown when it is not out of five, so a 9/10 is not silently
  // rewritten as a 4.5 and a reader is not misled about what the number meant.
  if (review.rating_scale_max && review.rating_scale_max !== 5) {
    return `${review.rating} / ${review.rating_scale_max}`;
  }
  return `${review.rating} 分`;
}

function dateText(review: Review): string {
  if (!review.published_at) {
    return "这条评价没有标注发布时间，因此无法判断是否仍然适用";
  }
  const day = String(review.published_at).slice(0, 10);
  if (review.age_days === null || review.age_days === undefined) return `写于 ${day}`;
  return `写于 ${day}（${review.age_days} 天前）`;
}

export function ReviewsPanel({ reviews }: { reviews: Review[] | undefined | null }) {
  // Absent and empty are both "nothing to show", and both must render nothing rather than an
  // empty panel. See the module docstring.
  if (!Array.isArray(reviews) || reviews.length === 0) return null;

  const undated = reviews.filter(
    (review) => review.age_days === null || review.age_days === undefined,
  ).length;

  return (
    <section className="panel" style={{ marginTop: 18 }}>
      <div className="panel-heading">
        <h2>评价 / reviews</h2>
        <span className="muted">
          {reviews.length} 条
          {undated ? ` · ${undated} 条未标注发布时间` : ""}
        </span>
      </div>
      <div className="panel-body">
        <div className="entity-meta" style={{ marginBottom: 10 }}>
          评价是别人写的意见，不是实测事实，因此只在这里列出，不会覆盖地点自身的字段。
          日期是重点：攻略面向未来的行程，一条几年前的评分说的可能是改造前、换老板前的那个地方。
        </div>        {reviews.map((review, index) => (
          <div className="entity-card" key={`${review.subject_id}:${index}`}>
            <div className="entity-title">
              <span>{scoreText(review)}</span>
              <span className="muted">{review.source}</span>
            </div>
            {review.text ? <div className="entity-meta">{review.text}</div> : null}
            <div className="entity-meta">{dateText(review)}</div>
          </div>
        ))}
      </div>
    </section>
  );
}
