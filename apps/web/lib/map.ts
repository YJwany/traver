/**
 * Where a plan's places sit in the panel that sketches them.
 *
 * This exists as its own module for the same reason `freshness.ts` does: the numbers *are* the
 * feature, and they were wrong in a way nobody could see.
 *
 * The map panel computes each pin's position from the bounding box of the trip's own
 * coordinates. That box was previously seeded with two literals:
 *
 *     Math.min(...lats, 30.6), Math.max(...lats, 30.6)
 *     Math.min(...lons, 104.0), Math.max(...lons, 104.0)
 *
 * `30.6, 104.0` is 成都. So the box was never the trip's bounding box — it was the union of the
 * trip and one fixed city. For a 成都 trip that is invisible, which is presumably why it
 * survived: every fixture and every manual check used 成都.
 *
 * For anywhere else it is the opposite of a map. Take 杭州 (lat ≈ 30.25, lon ≈ 120.15) against
 * that 成都 box: the latitude span becomes 30.25–30.6 instead of centring on the pins, and the
 * longitude span starts at 104.0, which is 16 degrees west of everything being drawn. Every pin
 * lands at x ≈ 88%, y ≈ 78–82% — a smear in the bottom-right corner, with the whole panel empty.
 * The more destinations a deployment serves, the more trips this misdraws.
 *
 * The same defect class as a hard-coded 成都 centre in the scorer, `city == destination` equality,
 * and `(0.0, 0.0)` used as "no origin": a constant that is really a property of one place.
 *
 * A note on what this panel is, because it bears on what the numbers may claim: it is a
 * **schematic**. There is no basemap, no tile server, and no projection — it arranges the trip's
 * own points inside a box so a reader can see which places are near each other. Distances and
 * bearings on screen are not to scale and must not be read as geography. The server side already
 * keeps that distinction (`PlaceResolution` carries a straight-line distance labelled as such and
 * never derives travel time from it); this panel keeps it by drawing nothing that looks like a
 * coastline or a road.
 */

export interface GeoPoint {
  latitude: number;
  longitude: number;
}

export interface PinPlacement {
  /** Percentage from the left edge, within `[INSET_PERCENT, 100 - INSET_PERCENT]`. */
  left: number;
  /** Percentage from the top edge. North is up, so a larger latitude gives a smaller value. */
  top: number;
}

/** How far from each edge pins are kept, in percent, so none is clipped by the panel border. */
export const INSET_PERCENT = 12;

/** A span narrower than this is treated as no spread at all. */
const NEGLIGIBLE_SPAN_DEGREES = 1e-6;

function isUsable(point: GeoPoint | null | undefined): point is GeoPoint {
  return (
    !!point &&
    Number.isFinite(point.latitude) &&
    Number.isFinite(point.longitude) &&
    // A latitude outside ±90 or a longitude outside ±180 is not a place. Providers do send
    // these — `(0.0, 0.0)` in particular, which is a real coordinate in the Atlantic and would
    // otherwise drag the bounding box to the Gulf of Guinea and squash everything else.
    Math.abs(point.latitude) <= 90 &&
    Math.abs(point.longitude) <= 180 &&
    !(point.latitude === 0 && point.longitude === 0)
  );
}

/**
 * The box the trip's own points occupy, or `null` when there is nothing to draw.
 *
 * `null` rather than a default box, because a default box is a claim about where the trip is.
 * The caller renders an empty state instead — which is also the honest answer when every
 * coordinate turned out to be unusable.
 */
export function boundsOf(points: readonly (GeoPoint | null | undefined)[]): {
  minLatitude: number;
  maxLatitude: number;
  minLongitude: number;
  maxLongitude: number;
} | null {
  const usable = points.filter(isUsable);
  if (!usable.length) return null;
  const latitudes = usable.map((point) => point.latitude);
  const longitudes = usable.map((point) => point.longitude);
  return {
    minLatitude: Math.min(...latitudes),
    maxLatitude: Math.max(...latitudes),
    minLongitude: Math.min(...longitudes),
    maxLongitude: Math.max(...longitudes),
  };
}

/**
 * Place each point inside the panel.
 *
 * Points that cannot be placed are returned as `null` **in their original positions**, so the
 * caller can keep the list aligned with the entities it came from. Dropping them would silently
 * renumber the pins and put the wrong label on the wrong dot.
 *
 * A single point, or several identical ones, has no spread to stretch across. Those are centred
 * rather than stretched: a zero span scaled up would be magnified noise, and placing the one
 * place of a trip in a corner implies a relationship to the panel's edges that does not exist.
 */
export function placePins(points: readonly (GeoPoint | null | undefined)[]): (PinPlacement | null)[] {
  const bounds = boundsOf(points);
  if (!bounds) return points.map(() => null);

  const low = INSET_PERCENT;
  const high = 100 - INSET_PERCENT;
  const latitudeSpan = bounds.maxLatitude - bounds.minLatitude;
  const longitudeSpan = bounds.maxLongitude - bounds.minLongitude;

  return points.map((point) => {
    if (!isUsable(point)) return null;

    const left =
      longitudeSpan < NEGLIGIBLE_SPAN_DEGREES
        ? (low + high) / 2
        : low + ((point.longitude - bounds.minLongitude) / longitudeSpan) * (high - low);

    // Latitude grows northward and `top` grows downward, so the fraction is inverted.
    const top =
      latitudeSpan < NEGLIGIBLE_SPAN_DEGREES
        ? (low + high) / 2
        : high - ((point.latitude - bounds.minLatitude) / latitudeSpan) * (high - low);

    return { left, top };
  });
}
