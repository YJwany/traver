import { describe, expect, it } from "vitest";

import {
  BASIS_LIVE_COMPUTED,
  BASIS_PROVIDER_REPORTED,
  BASIS_SOURCE_SILENT,
  BASIS_UNKNOWN,
  dataAgeIsKnown,
  freshnessClass,
  freshnessLabel,
  timeAgo,
} from "./freshness";

/**
 * Tests for the wording that describes how old a datum is.
 *
 * This is not cosmetic. The server sends two different times — when we fetched, and
 * when the data was produced — and `is_stale` only speaks to whether our copy is past
 * its validity window. A source that publishes no timestamps has `is_stale: false`
 * while its data may be years old, so a two-way "fresh / stale" label is a claim we
 * cannot support in that case.
 *
 * Each basis therefore gets its own wording, and the tests below pin the cases where a
 * careless implementation would overstate.
 */

function minutesAgo(minutes: number): string {
  return new Date(Date.now() - minutes * 60000).toISOString();
}

function reference(basis: string, observedAt: string, fetchedAt = observedAt) {
  return {
    source: "test-source",
    observed_at: observedAt,
    fetched_at: fetchedAt,
    observed_at_basis: basis,
    is_stale: false,
  };
}

describe("timeAgo", () => {
  it("describes recent times in minutes and older ones in hours or days", () => {
    expect(timeAgo(minutesAgo(0))).toBe("刚刚");
    expect(timeAgo(minutesAgo(5))).toBe("5 分钟前");
    expect(timeAgo(minutesAgo(120))).toBe("2 小时前");
    expect(timeAgo(minutesAgo(60 * 24 * 3))).toBe("3 天前");
  });

  it("does not report a negative age for a timestamp slightly in the future", () => {
    // Clock skew between this browser and the server is normal, and "-1 分钟前" would
    // be nonsense rather than merely imprecise.
    expect(timeAgo(new Date(Date.now() + 60000).toISOString())).toBe("刚刚");
  });

  it("never claims a specific age it cannot compute", () => {
    expect(timeAgo(undefined)).toBe("未知");
    expect(timeAgo("not a date")).toBe("未知");
  });
});

describe("freshnessLabel", () => {
  it("says 实时计算 only when the datum was produced by this request", () => {
    const label = freshnessLabel(reference(BASIS_LIVE_COMPUTED, minutesAgo(1)));
    expect(label).toContain("实时计算");
  });

  it("states the real age when the source reported an observation time", () => {
    const label = freshnessLabel(reference(BASIS_PROVIDER_REPORTED, minutesAgo(90)));
    // The data's own age, not the fetch age — this is the number that decides whether
    // the fact is worth anything.
    expect(label).toContain("数据 1 小时前");
  });

  it("admits that a silent source's data age is unknown", () => {
    const label = freshnessLabel(
      reference(BASIS_SOURCE_SILENT, minutesAgo(0), minutesAgo(0)),
    );
    expect(label).toContain("数据时间未知");
    expect(label).toContain("抓取于");
  });

  it("does not call a silent source 实时 merely because our copy is not stale", () => {
    // The bug this module was extracted to fix. `is_stale: false` here means only that
    // we fetched recently; the data could be years old.
    const silent = reference(BASIS_SOURCE_SILENT, minutesAgo(0));
    expect(silent.is_stale).toBe(false);
    expect(freshnessLabel(silent)).not.toBe("实时");
    expect(freshnessLabel(silent)).not.toContain("实时计算");
  });

  it("treats an unclassified basis as unknown rather than as fresh", () => {
    // A provider that forgets to classify must fail towards caution; the default must
    // not be the flattering answer.
    const label = freshnessLabel(reference(BASIS_UNKNOWN, minutesAgo(0)));
    expect(label).toContain("数据时间未知");
  });

  it("reports our own expired copy as expired, whatever the basis", () => {
    const stale = { ...reference(BASIS_PROVIDER_REPORTED, minutesAgo(1)), is_stale: true };
    expect(freshnessLabel(stale)).toBe("已过期");
  });

  it("labels an estimate as an estimate rather than as sourced", () => {
    expect(freshnessLabel(undefined, true)).toBe("估算");
    expect(freshnessLabel(reference(BASIS_PROVIDER_REPORTED, minutesAgo(1)), true)).toBe("估算");
  });

  it("distinguishes a fresh fetch of old data from fresh data", () => {
    // Both were fetched a moment ago; only one is worth acting on.
    const justFetchedOldData = reference(BASIS_PROVIDER_REPORTED, minutesAgo(60 * 24 * 30), minutesAgo(0));
    const justComputed = reference(BASIS_LIVE_COMPUTED, minutesAgo(0));

    expect(freshnessLabel(justFetchedOldData)).toContain("数据 30 天前");
    expect(freshnessLabel(justComputed)).toContain("实时计算");
    expect(freshnessLabel(justFetchedOldData)).not.toBe(freshnessLabel(justComputed));
  });

  it("states the fetch time for every basis, because that is the one time always known", () => {
    // The requirement, in the user's words: 「什么时候拉取到的数据就标注为什么时候的」.
    //
    // This is the test the two branches above were missing. They asserted the data-age
    // half of the label and never the fetch half, so 实时计算 and 数据 30 天前 shipped
    // without saying when we looked — in exactly the two cases that occur most often.
    // A label that answers "how old is the data" but not "when did you ask" leaves a plan
    // opened a week later reading as though it were current.
    for (const basis of [
      BASIS_LIVE_COMPUTED,
      BASIS_PROVIDER_REPORTED,
      BASIS_SOURCE_SILENT,
      BASIS_UNKNOWN,
    ]) {
      const label = freshnessLabel(reference(basis, minutesAgo(5), minutesAgo(5)));
      expect(label).toContain("抓取于");
      // And it has to be a *time*, not the word alone: the fetch is 5 minutes old here.
      expect(label).toContain("5 分钟前");
    }
  });

  it("keeps a live-computed label from reading as 实时 when the plan is old", () => {
    // The specific misreading. "实时计算" is present tense about a past event: the route
    // was computed live, but that was three days ago. Only the fetch time says so.
    const computedThreeDaysAgo = reference(
      BASIS_LIVE_COMPUTED,
      minutesAgo(60 * 24 * 3),
      minutesAgo(60 * 24 * 3),
    );
    const label = freshnessLabel(computedThreeDaysAgo);

    expect(label).toContain("3 天前");
    // Bare 实时计算 is what it used to be, and on its own it reads as "just now".
    expect(label).not.toBe("实时计算");
  });

  it("shows both times when the source reported one", () => {
    // The panel tells the reader 「抓取时间与数据自身时间分别标注」, so both must be there
    // rather than one standing in for the other.
    const label = freshnessLabel(reference(BASIS_PROVIDER_REPORTED, minutesAgo(90), minutesAgo(2)));

    expect(label).toContain("数据 1 小时前");
    expect(label).toContain("抓取于 2 分钟前");
  });
});

describe("freshnessClass", () => {
  it("marks only a verifiable problem as stale", () => {
    expect(freshnessClass({ ...reference(BASIS_PROVIDER_REPORTED, minutesAgo(1)), is_stale: true })).toBe(
      "status-chip status-stale",
    );
  });

  it("does not use the reassuring colour for an unknown age", () => {
    // Green reads as reassurance. For a silent source we have nothing to reassure with,
    // so it must be muted rather than green.
    expect(freshnessClass(reference(BASIS_SOURCE_SILENT, minutesAgo(0)))).toBe(
      "status-chip status-estimated",
    );
    expect(freshnessClass(reference(BASIS_UNKNOWN, minutesAgo(0)))).not.toContain("status-fresh");
  });

  it("uses the fresh colour when currency is actually supported", () => {
    expect(freshnessClass(reference(BASIS_LIVE_COMPUTED, minutesAgo(1)))).toBe(
      "status-chip status-fresh",
    );
    expect(freshnessClass(reference(BASIS_PROVIDER_REPORTED, minutesAgo(1)))).toBe(
      "status-chip status-fresh",
    );
  });
});

describe("dataAgeIsKnown", () => {
  it("is true only for a provider-reported observation time", () => {
    expect(dataAgeIsKnown(reference(BASIS_PROVIDER_REPORTED, minutesAgo(1)))).toBe(true);
    for (const basis of [BASIS_LIVE_COMPUTED, BASIS_SOURCE_SILENT, BASIS_UNKNOWN]) {
      expect(dataAgeIsKnown(reference(basis, minutesAgo(1)))).toBe(false);
    }
    expect(dataAgeIsKnown(undefined)).toBe(false);
  });
});
