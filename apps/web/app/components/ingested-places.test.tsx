import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { IngestedPlacesPanel } from "./ingested-places";
import type { ResolvedPlace } from "../../lib/ingested-places";

/**
 * Tests for the panel, and specifically for what it refuses to render.
 *
 * The wording itself is tested directly in `lib/ingested-places.test.ts`; these tests are
 * about the three rendering decisions that a reader would be misled by:
 *
 *   * an **absent** list must render nothing. An older snapshot/api does not report
 *     resolutions, and rendering "0 个地名" would claim the lookups ran and found nothing —
 *     the manufactured-verification failure in its latest appearance.
 *   * `located` must be shown as located *and* as not scheduled, because the whole risk of
 *     this feature is a reader believing a mention is a plan.
 *   * `elsewhere` must be shown with its distance rather than dropped, because dropping it
 *     reads as a failed lookup.
 */

function place(overrides: Partial<ResolvedPlace> = {}): ResolvedPlace {
  return {
    name: "灵隐寺",
    status: "located",
    coordinates: { latitude: 30.2408, longitude: 120.101 },
    display_name: "灵隐寺",
    match_confidence: 0.85,
    straight_line_from_centre_km: 6.4,
    reason: "按距离目的地最近的结果选取（同名地点可能有多个）",
    source_reference: { source: "amap" },
    ...overrides,
  };
}

describe("IngestedPlacesPanel", () => {
  it("renders nothing when the field is absent", () => {
    // The guard that matters most: absent is not empty.
    const { container } = render(<IngestedPlacesPanel places={undefined} />);
    expect(container.textContent).toBe("");
  });

  it("renders nothing for a non-array payload", () => {
    const malformed = { "0": place() } as unknown as ResolvedPlace[];
    const { container } = render(<IngestedPlacesPanel places={malformed} />);
    expect(container.textContent).toBe("");
  });

  it("renders nothing for an empty list", () => {
    // Present and empty means no unmatched mentions, and there is nothing to say about that.
    const { container } = render(<IngestedPlacesPanel places={[]} />);
    expect(container.textContent).toBe("");
  });

  it("shows a located place as located, and says it was not scheduled", () => {
    render(<IngestedPlacesPanel places={[place()]} />);

    expect(screen.getByText("灵隐寺")).toBeTruthy();
    expect(screen.getByText("located")).toBeTruthy();
    expect(screen.getByText(/没有被排进行程/)).toBeTruthy();
    expect(screen.getByText(/坐标 30\.2408, 120\.1010/)).toBeTruthy();
  });

  it("shows 在别处 with the distance and without a coordinate", () => {
    render(
      <IngestedPlacesPanel
        places={[
          place({
            name: "西湖",
            status: "elsewhere",
            coordinates: null,
            display_name: "惠州西湖",
            straight_line_from_centre_km: 979.1,
          }),
        ]}
      />,
    );

    expect(screen.getByText("elsewhere")).toBeTruthy();
    expect(screen.getByText(/约 979 公里/)).toBeTruthy();
    expect(screen.queryByText(/坐标/)).toBeNull();
  });

  it("keeps 'we looked and found nothing' apart from 'nobody looked'", () => {
    render(
      <IngestedPlacesPanel
        places={[
          place({ name: "查不到的地方", status: "not_found", coordinates: null }),
          place({ name: "没查过的", status: "not_attempted", coordinates: null }),
        ]}
      />,
    );

    expect(screen.getByText(/已查询地名定位来源/)).toBeTruthy();
    expect(screen.getByText(/没有可用的地名定位来源/)).toBeTruthy();
    // The claim that must never be made about an unasked lookup.
    expect(screen.queryByText(/没有找到/)).toBeNull();
  });

  it("counts each outcome in the heading rather than only the total", () => {
    render(
      <IngestedPlacesPanel
        places={[
          place(),
          place({ name: "西湖", status: "elsewhere", coordinates: null }),
          place({ name: "查不到", status: "not_found", coordinates: null }),
        ]}
      />,
    );

    const heading = screen.getByText(/3 个地名/);
    expect(heading.textContent).toContain("已定位 1");
    expect(heading.textContent).toContain("在别处 1");
    expect(heading.textContent).toContain("未定位 1");
  });

  it("shows the source's own reason, so a reader can disagree with the match", () => {
    render(<IngestedPlacesPanel places={[place()]} />);
    expect(screen.getByText(/判定依据：/)).toBeTruthy();
    expect(screen.getByText(/同名地点可能有多个/)).toBeTruthy();
  });

  it("states that a located place has no measured fields and no travel time", () => {
    render(<IngestedPlacesPanel places={[place()]} />);

    // The caveat a reader needs before they treat a coordinate as a plan.
    expect(screen.getByText(/没有评分、票价或营业时间/)).toBeTruthy();
    // Both places the phrase 直线距离 appears: on the coordinate line, and in the caveat that
    // says it is not a travel time.
    expect(screen.getAllByText(/直线距离/).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/不能用来推算通行时间/)).toBeTruthy();
    // A naive reader would take 6.4 公里 as a drive; the panel says what it is instead.
    expect(screen.getByText(/只能说明「是不是同一个地方」/)).toBeTruthy();
  });

  it("labels a located place as the traveller's material, not as a found attraction", () => {
    render(<IngestedPlacesPanel places={[place()]} />);
    expect(screen.getByText(/笔记里提到的/)).toBeTruthy();
    expect(screen.getByText(/不是查到的景点/)).toBeTruthy();
  });

  it("does not render an unknown status as a plausible one", () => {
    render(
      <IngestedPlacesPanel
        places={[place({ status: "something_new" as ResolvedPlace["status"] })]}
      />,
    );
    // Shown, and shown as unknown rather than silently reusing another status's wording.
    expect(screen.getByText(/未知的定位结果/)).toBeTruthy();
  });

  it("renders one card per place, in the server's order", () => {
    render(
      <IngestedPlacesPanel
        places={[
          place({ name: "第一个" }),
          place({ name: "第二个" }),
          place({ name: "第三个" }),
        ]}
      />,
    );

    const names = screen.getAllByText(/第.个/).map((node) => node.textContent);
    expect(names).toEqual(["第一个", "第二个", "第三个"]);
  });

  it("survives a click without throwing, since it has no interactive parts", () => {
    // A trivial guard, but this panel sits in a client component and a future edit adding a
    // control must not make the rest of the page fail silently.
    const { container } = render(<IngestedPlacesPanel places={[place()]} />);
    const card = container.querySelector(".entity-card");
    expect(card).not.toBeNull();
    fireEvent.click(card as Element);
    expect(screen.getByText("located")).toBeTruthy();
  });
});
