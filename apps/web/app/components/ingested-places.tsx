"use client";

/**
 * The places the traveller's own material named, and what happened to each one.
 *
 * This panel exists for one reason: a mention with no coordinate used to be a dead end. The
 * plan said the name was in the text and that our sources did not have it, which is true and
 * useless — a 小红书 post names restaurants and viewpoints a POI query never returns, and that
 * is most of what makes the note worth saving.
 *
 * Now a name is looked up, and the panel reports one of four outcomes. Three of them produce
 * no coordinate and mean entirely different things, which is what makes the wording load
 * bearing rather than cosmetic:
 *
 *   - **located** — we have a point, and the sentence also says it was *not* scheduled. A
 *     mention is evidence somebody liked a place; it is not this traveller's decision to go
 *     there, and the plan does not make that decision for them.
 *   - **elsewhere** — the name exists about 900 km away. Reported with the distance, because
 *     that is a fact about the note: dropping it would look like the lookup failed.
 *   - **not_found** — a source was asked and had nothing.
 *   - **not_attempted** — no source could be asked. This must never render as 「没有找到」,
 *     which is a claim about the place made out of a fact about our configuration.
 *
 * The list is deliberately reported apart from the attractions, because it *is* a different
 * kind of thing: nothing here has a rating, a price or opening hours, and nothing here is on
 * a day's schedule. Merging them would make 「笔记里提到的店」 and 「查到的景点」 the same in
 * every consumer at once.
 *
 * An absent `ingested_places` is not an empty one. An older snapshot/api does not report
 * resolutions at all, and this renders nothing rather than claiming the lookups ran and found
 * nothing — the manufactured-verification failure, in its latest appearance.
 */

import { resolutionCounts, resolvedPlaceWording, type ResolvedPlace } from "../../lib/ingested-places";

//: The tone a given status is displayed in. Kept as a table so a status cannot be rendered
//: without someone deciding what it looks like — the two unresolved outcomes share a colour
//: because neither is a warning, and they are told apart by their sentence.
const TONE_CLASS: Record<string, string> = {
  located: "status-chip status-fresh",
  elsewhere: "status-chip status-stale",
  not_found: "status-chip status-estimated",
  not_attempted: "status-chip status-estimated",
};

export function IngestedPlacesPanel({ places }: { places: ResolvedPlace[] | undefined | null }) {
  // Absent is not empty. See the module docstring: silence is the only honest rendering.
  if (!Array.isArray(places)) return null;

  const counts = resolutionCounts(places);
  if (!counts) return null;

  // Nothing was located and nothing was even attempted: there is no source. Saying "0 处"
  // would read as a result rather than as the absence of one, so the panel is not rendered at
  // all and the mentions themselves carry the explanation.
  if (counts.total === 0) return null;

  return (
    <section className="panel" style={{ marginTop: 18 }}>
      <div className="panel-heading">
        <h2>文中提到的地点 / places your notes named</h2>
        <span className="muted">
          {counts.total} 个地名 · 已定位 {counts.located}
          {counts.elsewhere ? ` · 在别处 ${counts.elsewhere}` : ""}
          {counts.unresolved ? ` · 未定位 ${counts.unresolved}` : ""}
        </span>
      </div>
      <div className="panel-body">
        {places.map((place, index) => {
          const wording = resolvedPlaceWording(place);
          const km = place.straight_line_from_centre_km;
          const coordinates = place.coordinates;
          return (
            <div className="entity-card" key={`${place.name}:${index}`}>
              <div className="entity-title">
                <span>
                  <strong>{place.name}</strong>
                  {place.display_name && place.display_name !== place.name ? (
                    <> → {place.display_name}</>
                  ) : null}
                </span>
                <span className={TONE_CLASS[place.status] ?? "status-chip status-estimated"}>
                  {place.status}
                </span>
              </div>
              <div className="entity-meta">{wording.text}</div>
              {/* The source's own explanation, shown rather than summarised: it is what lets a
                  reader disagree with the match instead of merely accepting it. */}
              {place.reason ? <div className="entity-meta">判定依据：{place.reason}</div> : null}
              {coordinates ? (
                <div className="entity-meta">
                  坐标 {coordinates.latitude.toFixed(4)}, {coordinates.longitude.toFixed(4)}
                  {km !== null && !Number.isNaN(km) ? ` · 直线距离 ${km.toFixed(1)} 公里` : null}
                </div>
              ) : null}
            </div>
          );
        })}
        <div className="entity-meta" style={{ marginTop: 10 }}>
          这些是<strong>笔记里提到的</strong>地点，不是查到的景点：没有评分、票价或营业时间，
          也不会被排进某一天。同名地点按「离目的地最近」消歧，超过 80 公里的会标为 elsewhere
          并给出距离。距离是直线距离，只能说明「是不是同一个地方」，不能用来推算通行时间。
        </div>
      </div>
    </section>
  );
}
