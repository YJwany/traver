import { describe, expect, it } from "vitest";

import {
  adviceAge,
  confidenceLabel,
  factKindMeta,
  formatDate,
  FACT_KINDS,
} from "./ingestions";

/**
 * The wording that carries the ingestion feature's honesty to the screen.
 *
 * Two rules are load-bearing and are tested here rather than trusted:
 *
 *   1. **Confidence is about the reading, never the claim.** "识别明确" says we are sure
 *      the sentence says this. It must never read as "this is true" — the note is
 *      somebody's account of a trip, and the extractor has no way to verify it.
 *   2. **The publication date and the fetch date are two facts.** A page that states no
 *      date leaves the advice's age genuinely unknown, and that has to be visible. The
 *      failure mode is a fresh fetch reading as fresh advice.
 */

describe("factKindMeta", () => {
  it("groups the kinds by what they mean for planning", () => {
    // A closed day removes an option; a price is context. Showing them identically
    // would make the reader do that triage themselves.
    expect(factKindMeta("opening_hours").tone).toBe("caution");
    expect(factKindMeta("warning").tone).toBe("caution");
    expect(factKindMeta("price").tone).toBe("fact");
    expect(factKindMeta("duration").tone).toBe("fact");
    expect(factKindMeta("place_mention").tone).toBe("mention");
  });

  it("falls back to the raw value rather than inventing a label", () => {
    const meta = factKindMeta("something_new");
    expect(meta.label).toBe("something_new");
    expect(meta.tone).toBe("mention");
  });

  it("has a label for every kind the server can send", () => {
    for (const kind of Object.keys(FACT_KINDS)) {
      expect(FACT_KINDS[kind].label).not.toBe("");
    }
  });
});

describe("confidenceLabel", () => {
  it("describes the confidence in the reading, not in the claim", () => {
    // "识别明确" is about pattern matching. A label like "可信" would assert the truth of
    // a stranger's claim, which the extractor cannot know.
    expect(confidenceLabel("high")).toBe("识别明确");
    expect(confidenceLabel("medium")).toBe("识别一般");
    expect(confidenceLabel("low")).toBe("仅为线索");
  });

  it("never uses a word that asserts the claim is true", () => {
    for (const level of ["high", "medium", "low"]) {
      const label = confidenceLabel(level);
      for (const forbidden of ["真实", "准确", "可信", "已验证", "确认"]) {
        expect(label).not.toContain(forbidden);
      }
    }
  });

  it("passes an unknown level through rather than guessing", () => {
    expect(confidenceLabel("experimental")).toBe("experimental");
  });
});

describe("adviceAge", () => {
  it("states the publication date when the page gave one", () => {
    expect(adviceAge("2024-05-01T08:00:00", "2026-09-01T00:00:00")).toBe(
      "原文发布于 2024-05-01",
    );
  });

  it("says the publication date is unknown rather than showing a blank", () => {
    // The important case. A blank would read as "no data", which is easy to ignore; the
    // sentence makes clear that the advice's age is genuinely unknown.
    const text = adviceAge(null, "2026-09-01T10:30:00");
    expect(text).toContain("未标注发布时间");
    expect(text).toContain("2026-09-01");
  });

  it("keeps the two times distinguishable", () => {
    // With a date, the fetch time is not what is being reported — and without one, the
    // fetch time must not be mistaken for the advice's age.
    const dated = adviceAge("2019-04-01T10:00:00", "2026-09-01T10:00:00");
    const undated = adviceAge(null, "2026-09-01T10:00:00");

    expect(dated).not.toBe(undated);
    expect(dated).not.toContain("抓取于");
    expect(undated).toContain("抓取于");
  });

  it("does not present an old note as current merely because it was just fetched", () => {
    const text = adviceAge("2019-04-01T10:00:00", new Date().toISOString());
    expect(text).toContain("2019-04-01");
    expect(text).not.toContain("刚刚");
  });
});

describe("formatDate", () => {
  it("shows the date without implying a precision the value does not have", () => {
    expect(formatDate("2024-05-01T08:33:12Z")).toBe("2024-05-01");
  });
});
