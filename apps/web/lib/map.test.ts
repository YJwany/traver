import { describe, expect, it } from "vitest";

import { boundsOf, INSET_PERCENT, placePins, type GeoPoint } from "./map";

/**
 * 成都, the city the panel used to be pinned to.
 *
 * Kept as a named constant because the defect this file exists for is that these two numbers
 * were written into the bounding-box calculation as fallbacks — so the box was always the union
 * of the trip and 成都, and only a 成都 trip looked right.
 */
const CHENGDU: GeoPoint = { latitude: 30.66, longitude: 104.06 };

/** 杭州, roughly 16 degrees of longitude away from 成都. */
const HANGZHOU: GeoPoint = { latitude: 30.25, longitude: 120.15 };

function spread(points: GeoPoint[]) {
  const placed = placePins(points);
  const lefts = placed.map((pin) => pin!.left);
  const tops = placed.map((pin) => pin!.top);
  return {
    leftSpan: Math.max(...lefts) - Math.min(...lefts),
    topSpan: Math.max(...tops) - Math.min(...tops),
    lefts,
    tops,
  };
}

describe("boundsOf", () => {
  it("uses only the points it was given, wherever they are", () => {
    const bounds = boundsOf([HANGZHOU, { latitude: 30.28, longitude: 120.19 }]);
    expect(bounds).not.toBeNull();
    // The failure mode: these came out as 30.25–30.66 / 104.06–120.19, i.e. 成都 and 杭州 together.
    expect(bounds!.minLatitude).toBeCloseTo(30.25, 6);
    expect(bounds!.maxLatitude).toBeCloseTo(30.28, 6);
    expect(bounds!.minLongitude).toBeCloseTo(120.15, 6);
    expect(bounds!.maxLongitude).toBeCloseTo(120.19, 6);
  });

  it("answers null rather than a default box when there is nothing to place", () => {
    // A default box is a claim about where the trip is. 成都 was that claim.
    expect(boundsOf([])).toBeNull();
    expect(boundsOf([null, undefined])).toBeNull();
    expect(boundsOf([{ latitude: NaN, longitude: 120 }])).toBeNull();
  });

  it("rejects Null Island, which is a real coordinate and not a place", () => {
    // (0, 0) is what an unset coordinate field serialises to. Left in, it drags the box to the
    // Gulf of Guinea and every real pin collapses into one corner.
    const bounds = boundsOf([{ latitude: 0, longitude: 0 }, HANGZHOU]);
    expect(bounds).not.toBeNull();
    expect(bounds!.minLatitude).toBeCloseTo(30.25, 6);
    expect(bounds!.minLongitude).toBeCloseTo(120.15, 6);
    // And on its own it leaves nothing to draw.
    expect(boundsOf([{ latitude: 0, longitude: 0 }])).toBeNull();
  });

  it("rejects latitudes and longitudes that are not on Earth", () => {
    expect(boundsOf([{ latitude: 91, longitude: 120 }])).toBeNull();
    expect(boundsOf([{ latitude: 30, longitude: 181 }])).toBeNull();
    expect(boundsOf([{ latitude: -90.5, longitude: 120 }])).toBeNull();
  });
});

describe("placePins", () => {
  it("spreads a non-成都 trip across the panel instead of into one corner", () => {
    // The regression this file exists for. With 成都 in the box, 杭州's spans were
    // 0.03° of latitude over a 0.41° box and 0.04° of longitude over a 16.13° box.
    const { leftSpan, topSpan, lefts, tops } = spread([
      HANGZHOU,
      { latitude: 30.28, longitude: 120.19 },
      { latitude: 30.21, longitude: 120.11 },
    ]);
    // The full inset range is available to the extremes of the data.
    expect(leftSpan).toBeCloseTo(100 - 2 * INSET_PERCENT, 4);
    expect(topSpan).toBeCloseTo(100 - 2 * INSET_PERCENT, 4);
    // Nothing is jammed against the right edge, which is where every pin used to land.
    expect(Math.max(...lefts)).toBeLessThanOrEqual(100 - INSET_PERCENT + 1e-9);
    expect(Math.min(...lefts)).toBeGreaterThanOrEqual(INSET_PERCENT - 1e-9);
    expect(Math.max(...tops)).toBeLessThanOrEqual(100 - INSET_PERCENT + 1e-9);
  });

  it("draws 成都 the same way as anywhere else, rather than specially well", () => {
    const chengduSpan = spread([
      CHENGDU,
      { latitude: 30.70, longitude: 104.10 },
      { latitude: 30.62, longitude: 104.02 },
    ]);
    const hangzhouSpan = spread([
      HANGZHOU,
      { latitude: 30.29, longitude: 120.19 },
      { latitude: 30.21, longitude: 120.11 },
    ]);
    // Both trips use the same box, because the box now depends only on the trip.
    expect(chengduSpan.leftSpan).toBeCloseTo(hangzhouSpan.leftSpan, 6);
    expect(chengduSpan.topSpan).toBeCloseTo(hangzhouSpan.topSpan, 6);
  });

  it("puts north up and east right", () => {
    const [west, east] = placePins([
      { latitude: 30.25, longitude: 120.10 },
      { latitude: 30.25, longitude: 120.20 },
    ]);
    expect(east!.left).toBeGreaterThan(west!.left);
    // Same latitude, so the same row.
    expect(east!.top).toBeCloseTo(west!.top, 6);

    const [south, north] = placePins([
      { latitude: 30.20, longitude: 120.15 },
      { latitude: 30.30, longitude: 120.15 },
    ]);
    expect(north!.top).toBeLessThan(south!.top);
  });

  it("centres a single place rather than stretching a zero span", () => {
    const [only] = placePins([HANGZHOU]);
    expect(only).not.toBeNull();
    expect(only!.left).toBeCloseTo(50, 6);
    expect(only!.top).toBeCloseTo(50, 6);
  });

  it("centres several identical coordinates rather than magnifying noise", () => {
    const placed = placePins([HANGZHOU, { ...HANGZHOU }, { ...HANGZHOU }]);
    for (const pin of placed) {
      expect(pin!.left).toBeCloseTo(50, 6);
      expect(pin!.top).toBeCloseTo(50, 6);
    }
  });

  it("keeps unplaceable points in place as null, so pins stay aligned with their entities", () => {
    // Dropping them would renumber the remaining pins and label the wrong dot.
    const placed = placePins([HANGZHOU, null, { latitude: 30.28, longitude: 120.19 }, undefined]);
    expect(placed).toHaveLength(4);
    expect(placed[0]).not.toBeNull();
    expect(placed[1]).toBeNull();
    expect(placed[2]).not.toBeNull();
    expect(placed[3]).toBeNull();
  });

  it("returns an entry per input even when nothing at all can be placed", () => {
    const placed = placePins([null, { latitude: 999, longitude: 999 }]);
    expect(placed).toEqual([null, null]);
  });

  it("never places a pin outside the panel", () => {
    const placed = placePins([
      { latitude: -33.87, longitude: 151.21 }, // Sydney
      { latitude: 64.15, longitude: -21.94 }, // Reykjavik
      { latitude: 30.25, longitude: 120.15 },
      { latitude: -54.8, longitude: -68.3 }, // Ushuaia
    ]);
    for (const pin of placed) {
      expect(pin).not.toBeNull();
      expect(pin!.left).toBeGreaterThanOrEqual(INSET_PERCENT);
      expect(pin!.left).toBeLessThanOrEqual(100 - INSET_PERCENT);
      expect(pin!.top).toBeGreaterThanOrEqual(INSET_PERCENT);
      expect(pin!.top).toBeLessThanOrEqual(100 - INSET_PERCENT);
    }
  });
});
