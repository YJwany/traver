import { describe, expect, it } from "vitest";

import {
  locatedPlaces,
  resolutionCounts,
  resolvedPlaceWording,
  unmatchedMentionWording,
  type ResolvedPlace,
} from "./ingested-places";

/**
 * Tests for what the plan *says* about a place named in the traveller's own note.
 *
 * The server's job is to decide where a name is. This is the other half: the sentence a
 * reader sees, and the sentences that must not be merged. `not_found` and `not_attempted`
 * produce the same absence of a coordinate and mean opposite things — one is a fact about the
 * place, the other a fact about our configuration — and rendering them the same way is how
 * "we have no source for this" becomes "this place does not exist".
 *
 * The third case pinned here is the absent list. An older snapshot/api sends no
 * `ingested_places` at all, and deriving a claim from that absence manufactures a
 * verification that never ran.
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

describe("resolvedPlaceWording", () => {
  it("says a located place is located, and that it was not scheduled", () => {
    const wording = resolvedPlaceWording(place());
    expect(wording.tone).toBe("ok");
    expect(wording.text).toContain("已定位");
    expect(wording.text).toContain("6.4 公里");
    // The half that stops a reader believing a mention is a plan.
    expect(wording.text).toContain("没有被排进行程");
  });

  it("says 在别处 with the distance, and does not call it a failure", () => {
    const wording = resolvedPlaceWording(
      place({
        status: "elsewhere",
        coordinates: null,
        straight_line_from_centre_km: 979.1,
        display_name: "惠州西湖",
      }),
    );
    expect(wording.tone).toBe("warn");
    expect(wording.text).toContain("约 979 公里");
    expect(wording.text).toContain("惠州西湖");
    // Neither "找不到" nor "已定位": the place was found and is not here.
    expect(wording.text).not.toContain("没有找到");
    expect(wording.text).not.toContain("已定位");
  });

  it("distinguishes 'we looked and found nothing' from 'nobody looked'", () => {
    const looked = resolvedPlaceWording(
      place({ status: "not_found", coordinates: null, straight_line_from_centre_km: null }),
    );
    const unasked = resolvedPlaceWording(
      place({ status: "not_attempted", coordinates: null, straight_line_from_centre_km: null }),
    );

    expect(looked.text).toContain("已查询");
    expect(unasked.text).toContain("没有可用的地名定位来源");
    expect(unasked.text).toContain("没有查询过");
    // The claim that must never appear for an unasked lookup.
    expect(unasked.text).not.toContain("没有找到");
    expect(unasked.text).not.toContain("没有返回");
    expect(looked.text).not.toBe(unasked.text);
  });

  it("does not print a confidence the source never stated", () => {
    // `match_confidence === null` means the source said nothing, so the sentence says nothing.
    const wording = resolvedPlaceWording(place({ match_confidence: null }));
    expect(wording.text).not.toContain("0.");
    expect(wording.text).not.toContain("置信");
  });

  it("names the matched place when the source calls it something else", () => {
    const wording = resolvedPlaceWording(place({ display_name: "灵隐寺(售票处)" }));
    expect(wording.text).toContain("灵隐寺(售票处)");
  });

  it("omits the parenthetical when the display name is the queried name", () => {
    const wording = resolvedPlaceWording(place({ name: "灵隐寺", display_name: "灵隐寺" }));
    expect(wording.text).not.toContain("（灵隐寺）");
  });

  it("reports an unknown status as unknown rather than guessing at it", () => {
    // A server that adds a fifth status must not be silently mis-described here.
    const wording = resolvedPlaceWording(
      place({ status: "something_new" as ResolvedPlace["status"] }),
    );
    expect(wording.tone).toBe("unknown");
    expect(wording.text).toContain("something_new");
  });

  it("does not render a broken distance as a number", () => {
    const wording = resolvedPlaceWording(
      place({ straight_line_from_centre_km: Number.NaN }),
    );
    expect(wording.text).not.toContain("NaN");
    expect(wording.text).toContain("已定位");
  });
});

describe("unmatchedMentionWording", () => {
  it("uses the resolution when the mention was resolved", () => {
    const wording = unmatchedMentionWording("灵隐寺", [place()]);
    expect(wording.text).toContain("已定位");
  });

  it("finds the right entry among several", () => {
    const wording = unmatchedMentionWording("西湖", [
      place(),
      place({ name: "西湖", status: "elsewhere", straight_line_from_centre_km: 979.1 }),
    ]);
    expect(wording.text).toContain("约 979 公里");
  });

  it("says no lookup ran when the list is present but has no entry", () => {
    // Present-and-empty while an unmatched mention exists means lookups did not happen.
    const wording = unmatchedMentionWording("灵隐寺", []);
    expect(wording.text).toContain("没有对它做地名定位");
    // Still not a claim about the place.
    expect(wording.text).not.toContain("没有这个地点");
  });

  it("claims nothing when the field is absent entirely", () => {
    // An older api does not report resolutions. `undefined` is not "nothing was found".
    for (const missing of [undefined, null]) {
      const wording = unmatchedMentionWording("灵隐寺", missing);
      expect(wording.tone).toBe("unknown");
      expect(wording.text).toContain("没有地名定位信息");
      // The manufactured-verification guard: no claim about the place, and no claim that a
      // lookup was tried.
      expect(wording.text).not.toContain("没有找到");
      expect(wording.text).not.toContain("没有这个地点");
      expect(wording.text).not.toContain("没有查询过");
    }
  });

  it("does not mistake a non-array for an empty list", () => {
    // A malformed payload must not read as "we looked and found nothing".
    const malformed = { "0": place() } as unknown as ResolvedPlace[];
    const wording = unmatchedMentionWording("灵隐寺", malformed);
    expect(wording.text).toContain("没有地名定位信息");
  });
});

describe("locatedPlaces and resolutionCounts", () => {
  it("keeps only the places that actually have a coordinate", () => {
    const places = [
      place(),
      place({ name: "西湖", status: "elsewhere", coordinates: null }),
      place({ name: "查不到", status: "not_found", coordinates: null }),
      // A `located` entry with no coordinates would be a contradiction; it is not rendered
      // as a point on a map.
      place({ name: "矛盾", status: "located", coordinates: null }),
    ];
    expect(locatedPlaces(places).map((item) => item.name)).toEqual(["灵隐寺"]);
  });

  it("gives no counts at all for an absent list", () => {
    // `null` rather than zeros: zero would read as "we checked everything and located none".
    expect(resolutionCounts(undefined)).toBeNull();
    expect(resolutionCounts(null)).toBeNull();
    expect(resolutionCounts([])).toEqual({
      total: 0,
      located: 0,
      elsewhere: 0,
      unresolved: 0,
    });
  });

  it("counts each outcome separately", () => {
    const counts = resolutionCounts([
      place(),
      place({ name: "a", status: "elsewhere" }),
      place({ name: "b", status: "not_found" }),
      place({ name: "c", status: "not_attempted" }),
    ]);
    expect(counts).toEqual({ total: 4, located: 1, elsewhere: 1, unresolved: 2 });
  });
});
