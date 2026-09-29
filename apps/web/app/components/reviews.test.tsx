/**
 * The reviews panel, and the two ways it could mislead.
 *
 * The requirement behind this feature was a rule about time — 「只能用近期的评价……不能是那种几年前
 * 那种」 — so the tests are about dates rather than about layout:
 *
 *   1. An undated review must be *reported as undated*. The tempting rendering is to show it
 *      without a date, which reads as current; the other tempting one is to fall back to our
 *      fetch time, which asserts something nobody said. Both are failures of the same rule.
 *   2. The panel must not re-derive a recency threshold. The horizon lives on the server and its
 *      verdict arrives in `stale_facts`; a client-side copy is the same rule written twice. The
 *      first draft of this component had a hand-written 548 days that did not even match the
 *      server's 540, so the guard is a grep-shaped assertion on the source rather than a render.
 */

import { render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { ReviewsPanel } from "./reviews";
import type { Review } from "../../lib/reviews";

function review(overrides: Partial<Review> = {}): Review {
  return {
    subject_id: "11111111-1111-1111-1111-111111111111",
    rating: 4.5,
    rating_scale_max: 5,
    text: "环境不错，出餐快",
    published_at: "2026-09-01T10:00:00+00:00",
    age_days: 28,
    source: "review-source",
    ...overrides,
  };
}

describe("ReviewsPanel", () => {
  it("renders nothing when there are no reviews", () => {
    // Absent reviews are the normal state — no reachable source publishes guest scores — so an
    // empty panel would read as a broken feature rather than as an unconnected one.
    const { container: empty } = render(<ReviewsPanel reviews={[]} />);
    expect(empty).toBeEmptyDOMElement();

    const { container: missing } = render(<ReviewsPanel reviews={undefined} />);
    expect(missing).toBeEmptyDOMElement();

    const { container: nullish } = render(<ReviewsPanel reviews={null} />);
    expect(nullish).toBeEmptyDOMElement();
  });

  it("names the author's own date rather than only a relative age", () => {
    render(<ReviewsPanel reviews={[review({ published_at: "2026-09-01T10:00:00+00:00" })]} />);

    // The absolute date, so the sentence stays true when the page is read later; the relative
    // age is kept as well because it is what a reader judges quickly.
    expect(screen.getByText(/2026-09-01/)).toBeInTheDocument();
    expect(screen.getByText(/28 天前/)).toBeInTheDocument();
  });

  it("reports an undated review as undated instead of showing it dateless", () => {
    render(<ReviewsPanel reviews={[review({ published_at: null, age_days: null })]} />);

    // Not silently dateless: a review with no date shown reads as a current one.
    expect(screen.getByText(/没有标注发布时间/)).toBeInTheDocument();
    // And the count in the heading says so too, so it is visible without reading every row.
    expect(screen.getByText(/1 条未标注发布时间/)).toBeInTheDocument();
    // Anything resembling a substituted date is the failure this guards.
    expect(screen.queryByText(/写于/)).not.toBeInTheDocument();
    expect(screen.queryByText(/1970|刚刚|今天/)).not.toBeInTheDocument();
  });

  it("counts dated and undated reviews separately", () => {
    render(
      <ReviewsPanel
        reviews={[
          review(),
          review({ subject_id: "b", published_at: null, age_days: null }),
          review({ subject_id: "c", published_at: null, age_days: null }),
        ]}
      />,
    );

    expect(screen.getByText(/3 条/)).toBeInTheDocument();
    expect(screen.getByText(/2 条未标注发布时间/)).toBeInTheDocument();
  });

  it("shows the source's own scale rather than normalising it", () => {
    // A 9/10 rendered as 「4.5 分」 would be a rewrite of the source's claim into a scale it never
    // used, and would make two incomparable numbers look comparable.
    render(<ReviewsPanel reviews={[review({ rating: 9, rating_scale_max: 10 })]} />);

    expect(screen.getByText("9 / 10")).toBeInTheDocument();
    expect(screen.queryByText(/4\.5/)).not.toBeInTheDocument();
  });

  it("says when no score was given rather than printing a blank or a zero", () => {
    render(<ReviewsPanel reviews={[review({ rating: null, text: "只说体验，没打分" })]} />);

    expect(screen.getByText("未给出分数")).toBeInTheDocument();
    // Still listed: an unrated review is evidence of a visit, which is what makes it worth
    // showing at all.
    expect(screen.getByText("只说体验，没打分")).toBeInTheDocument();
  });

  it("names the source of each score", () => {
    render(<ReviewsPanel reviews={[review({ source: "大众点评" })]} />);

    expect(screen.getByText("大众点评")).toBeInTheDocument();
  });

  it("does not re-derive the server's recency horizon", () => {
    // The rule lives in `MAX_AGE_SECONDS["review"]` on the server (18 * 30 days = 540), and the
    // verdict it produces arrives in `stale_facts`. A second copy here would be free to drift —
    // and the first draft of this file had 548, which is already a different rule.
    //
    // Comments are stripped before the check, and that is not a technicality: this file's own
    // docstring quotes the wrong number it is warning about, so a naive scan fails on the
    // explanation of the bug rather than on the bug. A guard that cannot tell code from prose
    // would force the prose to stop naming the defect, which is the opposite of what the
    // comments here are for.
    const code = readFileSync(join(__dirname, "reviews.tsx"), "utf8")
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/\/\/.*$/gm, "");

    for (const stray of ["548", "540", "18 * 30", "518400"]) {
      expect(code).not.toContain(stray);
    }
    // And no local comparison against an age, which is how such a threshold would be applied.
    expect(code).not.toMatch(/age_days\s*[<>]/);
  });
});
