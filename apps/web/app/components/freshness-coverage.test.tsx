import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AgeUnknownPanel, freshnessReferenceCount } from "./freshness-coverage";

/**
 * Tests for the plan-level freshness statement.
 *
 * The defect this panel exists to fix is an argument from silence. `stale_facts` empty was
 * rendered as no banner at all, and no banner reads as "everything checked, everything
 * current". On the free stack that is wrong by a wide margin: only the POI source publishes
 * an observation time, so a normal plan has several sources whose data age is unknowable
 * while the warning panel stays hidden.
 *
 * The three states are checked separately, plus the backward-compatibility case, because
 * that last one is where a careless implementation invents a claim: an API that predates
 * the field sends nothing, and "nothing" must not be read as "nothing to report".
 */

const reference = { source: "wikipedia", source_id: "wikipedia:西湖" };

function preview(overrides: Record<string, unknown> = {}) {
  return {
    source_references: [reference],
    stale_facts: [],
    age_unknown_facts: [],
    ...overrides,
  };
}

function unknownFact(source: string, dataType: string): Record<string, unknown> {
  return {
    source,
    data_type: dataType,
    source_id: `${source}:x`,
    reason: "age_unknown",
    observed_at_basis: "source_silent",
    fetched_at: "2026-09-28T10:00:00Z",
    evaluated_at: "2026-09-28T10:00:05Z",
  };
}

describe("freshnessReferenceCount", () => {
  it("counts the plan's citations", () => {
    expect(freshnessReferenceCount(preview())).toBe(1);
    expect(freshnessReferenceCount({ source_references: [] })).toBe(0);
    expect(freshnessReferenceCount(undefined)).toBe(0);
  });
});

describe("AgeUnknownPanel", () => {
  it("says nothing when the plan cites nothing", () => {
    // "0 of 0 verified" is an empty boast, not a finding.
    const { container } = render(<AgeUnknownPanel preview={preview({ source_references: [] })} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("states verification positively when every source reported its own age", () => {
    // The one case where silence would understate: this state is checkable, so it is said.
    render(<AgeUnknownPanel preview={preview()} />);

    expect(screen.getByText("全部可验证")).toBeInTheDocument();
    expect(screen.getByText(/1 条来源引用都给出了数据自身的时间/)).toBeInTheDocument();
  });

  it("counts the sources whose data age cannot be established", () => {
    render(
      <AgeUnknownPanel
        preview={preview({
          age_unknown_facts: [unknownFact("open-meteo", "weather"), unknownFact("wikipedia", "attraction")],
        })}
      />,
    );

    expect(screen.getByText("2 项无法确认")).toBeInTheDocument();
    // Grouped by source, because the source is what does or does not publish a time.
    expect(screen.getByText("open-meteo")).toBeInTheDocument();
    expect(screen.getByText("wikipedia")).toBeInTheDocument();
    expect(screen.getByText(/weather/)).toBeInTheDocument();
  });

  it("does not let an empty staleness list read as verification", () => {
    // The core of the defect. No stale facts, but sources that cannot be checked — the panel
    // must be present and must say which of the two things that means.
    render(
      <AgeUnknownPanel
        preview={preview({ stale_facts: [], age_unknown_facts: [unknownFact("wikipedia", "attraction")] })}
      />,
    );

    expect(screen.getByText("1 项无法确认")).toBeInTheDocument();
    expect(screen.queryByText("全部可验证")).toBeNull();
    expect(
      screen.getByText(/这不等于全部数据都被核实过，只等于没有任何一条能被证明过期/),
    ).toBeInTheDocument();
  });

  it("explains that a fetch time is not evidence about the data", () => {
    render(
      <AgeUnknownPanel
        preview={preview({ age_unknown_facts: [unknownFact("open-meteo", "weather")] })}
      />,
    );

    expect(screen.getByText(/只知道「什么时候抓的」，不知道数据本身有多新/)).toBeInTheDocument();
    expect(screen.getByText(/抓取时间是准确的，但它不能证明数据是新的/)).toBeInTheDocument();
  });

  it("stays out of the way when staleness is the only finding", () => {
    // The amber panel already names these; repeating the count here would be noise, and
    // noise is how a reader learns to skip both panels.
    const { container } = render(
      <AgeUnknownPanel preview={preview({ stale_facts: [{ source: "open-meteo", data_type: "weather" }] })} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("does not invent verification when the API predates the field", () => {
    // Absent is not empty. An older API sends no `age_unknown_facts`, and reading that as
    // "no unverifiable facts" would print 全部可验证 for a plan nothing ever checked —
    // a claim manufactured from a missing key.
    const { age_unknown_facts: _dropped, ...withoutField } = preview();
    const { container } = render(<AgeUnknownPanel preview={withoutField} />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText("全部可验证")).toBeNull();
  });

  it("still reports unverifiable facts under an older API that has them but no stale list", () => {
    // The other direction of the same compatibility question: the field we branch on is
    // present, so the panel works even if another field is missing.
    render(
      <AgeUnknownPanel
        preview={{ source_references: [reference], age_unknown_facts: [unknownFact("wikipedia", "attraction")] }}
      />,
    );
    expect(screen.getByText("1 项无法确认")).toBeInTheDocument();
  });

  it("ignores a non-array value rather than throwing", () => {
    // A malformed payload must not blank the whole trip page.
    const { container } = render(
      <AgeUnknownPanel preview={preview({ age_unknown_facts: null })} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("groups multiple references from one source into a single line", () => {
    // Two weather references are one fact about the source, not two problems.
    render(
      <AgeUnknownPanel
        preview={preview({
          age_unknown_facts: [unknownFact("open-meteo", "weather"), unknownFact("open-meteo", "weather")],
        })}
      />,
    );

    expect(screen.getByText("2 项无法确认")).toBeInTheDocument();
    expect(screen.getByText(/2 条 · weather/)).toBeInTheDocument();
    expect(screen.getAllByText("open-meteo")).toHaveLength(1);
  });
});
