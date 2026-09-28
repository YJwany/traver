/**
 * How a place mentioned in the traveller's own note is described on screen.
 *
 * The server decides where a name is; this decides what to *say* about it, and the wording
 * carries the honesty. Four outcomes exist and they are not interchangeable:
 *
 *   - `located`       — we have a coordinate, here;
 *   - `elsewhere`     — the name exists and is ~900 km away, which is a fact about the note;
 *   - `not_found`     — a source was asked and had nothing;
 *   - `not_attempted` — nobody was asked, and saying "没有找到" would blame the place.
 *
 * The last two produce the same absence of a coordinate and mean opposite things, so they get
 * separate sentences. This is the same defect the freshness panels guard against, one layer
 * out: an absent lookup presented as the result of one.
 *
 * Everything here is a pure function of the payload, deliberately. The decisions are what
 * matter and they are cheap to test directly, instead of through a rendered tree.
 */

export type PlaceResolutionStatus =
  | "located"
  | "elsewhere"
  | "not_found"
  | "not_attempted";

export type Coordinates = { latitude: number; longitude: number };

/** One place the traveller's material named, as the server resolved it. */
export type ResolvedPlace = {
  name: string;
  status: PlaceResolutionStatus;
  /** Present only when `status === "located"`. */
  coordinates: Coordinates | null;
  display_name: string | null;
  match_confidence: number | null;
  straight_line_from_centre_km: number | null;
  reason: string;
  source_reference: Record<string, unknown> | null;
};

/** What a mention line should say, plus how to colour it. */
export type MentionWording = { text: string; tone: "ok" | "warn" | "unknown" };

function distance(km: number | null): string {
  if (km === null || Number.isNaN(km)) return "";
  return km >= 10 ? `约 ${Math.round(km)} 公里` : `约 ${km.toFixed(1)} 公里`;
}

/**
 * The sentence for one resolved place.
 *
 * Every branch states what is known and stops. `match_confidence === null` is left unsaid
 * rather than rendered as a number, because the server reports `null` precisely when the
 * source did not state one — and a confidence value printed from a default would look
 * measured.
 */
export function resolvedPlaceWording(place: ResolvedPlace): MentionWording {
  const where = place.display_name && place.display_name !== place.name
    ? `（${place.display_name}）`
    : "";
  switch (place.status) {
    case "located": {
      const km = distance(place.straight_line_from_centre_km);
      return {
        text: `已定位${where}${km ? ` · 距目的地中心 ${km}` : ""} · 只是文中提到的地点，没有被排进行程`,
        tone: "ok",
      };
    }
    case "elsewhere": {
      const km = distance(place.straight_line_from_centre_km);
      return {
        text: `找到同名地点${where}${km ? `，但在 ${km} 外` : ""}，很可能不是笔记所指的那个`,
        tone: "warn",
      };
    }
    case "not_found":
      return {
        text: `已查询地名定位来源，没有返回这个名称的结果${where}`,
        tone: "unknown",
      };
    case "not_attempted":
      return { text: "这次规划没有可用的地名定位来源，因此没有查询过这个地名", tone: "unknown" };
    default: {
      // An outcome this build does not know about. Reported as unknown rather than guessed at,
      // so a server that adds a fifth status cannot be silently mis-described here.
      const status: string = place.status;
      return { text: `未知的定位结果（${status}）`, tone: "unknown" };
    }
  }
}

/**
 * The sentence for a mention whose place could not be attached to anything we already have.
 *
 * `places` is the whole list from the plan, and whether it is `undefined` matters:
 *
 *   - **absent** — an older snapshot/api does not report resolutions at all, so nothing is
 *     claimed about *why* the mention is loose. Asserting "没有这个地点" here would be a claim
 *     about the world made out of a missing field, which is the manufactured-verification bug
 *     in its fourth appearance.
 *   - **present** — the plan did try, so the mention may have a resolution and the wording
 *     comes from it. A mention with no entry in a present list means no lookup ran.
 */
export function unmatchedMentionWording(
  mentionName: string,
  places: ResolvedPlace[] | undefined | null,
): MentionWording {
  if (!Array.isArray(places)) {
    return { text: "文中提到（这份记录没有地名定位信息）", tone: "unknown" };
  }
  const found = places.find((place) => place.name === mentionName);
  if (found) return resolvedPlaceWording(found);
  return { text: "文中提到，这次规划没有对它做地名定位", tone: "unknown" };
}

/** The places worth a line of their own, in the order the server gave them. */
export function locatedPlaces(places: ResolvedPlace[] | undefined | null): ResolvedPlace[] {
  if (!Array.isArray(places)) return [];
  return places.filter((place) => place.status === "located" && place.coordinates !== null);
}

/** How many of each outcome, for the panel heading. Absent input gives no counts at all. */
export function resolutionCounts(
  places: ResolvedPlace[] | undefined | null,
): { total: number; located: number; elsewhere: number; unresolved: number } | null {
  if (!Array.isArray(places)) return null;
  return {
    total: places.length,
    located: places.filter((place) => place.status === "located").length,
    elsewhere: places.filter((place) => place.status === "elsewhere").length,
    unresolved: places.filter(
      (place) => place.status === "not_found" || place.status === "not_attempted",
    ).length,
  };
}
