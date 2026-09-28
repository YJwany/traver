"use client";

/**
 * How much of a plan's data age is actually known, stated at plan level.
 *
 * This exists because an empty staleness banner is ambiguous, and the ambiguity resolved
 * the wrong way. `stale_facts` empty means *nothing was provably too old*; it does not mean
 * everything was checked. On the free stack those are far apart: only the POI source
 * publishes an observation time, so a typical plan has several sources whose data age is
 * unknowable while the warning banner stays hidden — and a hidden warning reads as "all
 * current".
 *
 * So there are three states, and each gets wording that a reader can act on differently:
 *
 *   * verified  — every reference stated its own age and none was past its horizon;
 *   * stale     — handled by its own amber panel, because the remedy is a re-fetch;
 *   * unverifiable — the source does not say when its data was produced.
 *
 * The third is not a defect to fix. A source that does not publish a production time cannot
 * be made to, and reporting that as staleness would be the same unfounded assertion in the
 * other direction — the reason the two lists are kept apart on the server too. What can be
 * done is to say it, in neutral rather than warning colours.
 *
 * The per-source chips already say 「数据时间未知」, but only for the first few references
 * and only per entity. This is the rollup.
 */

export type AnyRecord = Record<string, any>;

export function freshnessReferenceCount(preview: AnyRecord | undefined): number {
  return preview?.source_references?.length ?? 0;
}

export function AgeUnknownPanel({ preview }: { preview: AnyRecord | undefined }) {
  const stale: AnyRecord[] = preview?.stale_facts ?? [];
  const unverifiable: AnyRecord[] | undefined = preview?.age_unknown_facts;
  const total = freshnessReferenceCount(preview);

  // Nothing to speak about. "0 of 0 verified" would be an empty boast.
  if (total === 0) return null;

  // An API from before this field existed sends nothing, and absent is not empty. Treating
  // the two alike would render 「全部可验证」 for a plan that was never checked against
  // anything — inventing a verification because a key was missing, which is the exact
  // failure this whole area keeps producing. Silence is the only honest reading.
  if (!Array.isArray(unverifiable)) return null;

  // Both lists empty is a real, checkable state, so it is asserted rather than left to be
  // inferred from silence.
  if (stale.length === 0 && unverifiable.length === 0) {
    return (
      <div className="panel" style={{ marginTop: 18 }}>
        <div className="panel-heading">
          <h2>时效性 / freshness</h2>
          <span className="status-chip status-fresh">全部可验证</span>
        </div>
        <div className="panel-body">
          <div className="entity-meta">
            {total} 条来源引用都给出了数据自身的时间，且都在该类事实可接受的范围内。
          </div>
        </div>
      </div>
    );
  }

  // Stale facts have their own panel; if that is the only finding, this one stays out of the
  // way rather than repeating the count.
  if (unverifiable.length === 0) return null;

  // Grouped by source, because that is the actionable unit: it is the *source* that does or
  // does not publish a time, and one source accounts for several references.
  const bySource = new Map<string, { count: number; dataTypes: Set<string> }>();
  for (const fact of unverifiable) {
    const entry = bySource.get(fact.source) ?? { count: 0, dataTypes: new Set<string>() };
    entry.count += 1;
    if (fact.data_type) entry.dataTypes.add(fact.data_type);
    bySource.set(fact.source, entry);
  }

  return (
    <div className="panel" style={{ marginTop: 18 }}>
      <div className="panel-heading">
        <h2>时效性 / freshness</h2>
        <span className="status-chip status-estimated">{unverifiable.length} 项无法确认</span>
      </div>
      <div className="panel-body">
        {Array.from(bySource.entries()).map(([source, entry]) => (
          <div key={source} className="entity-meta">
            <span className="source-chip">{source}</span> {entry.count} 条 ·{" "}
            {Array.from(entry.dataTypes).join("、")} · 未提供数据自身的时间
          </div>
        ))}
        <div className="entity-meta" style={{ marginTop: 10 }}>
          这些来源没有说明数据是什么时候产生的，所以我们只知道「什么时候抓的」，不知道数据本身有多新。
          抓取时间是准确的，但它不能证明数据是新的——上面每一条来源标签里都同时标注了抓取时间与「数据时间未知」。
          {stale.length === 0
            ? "另外，本行程没有出现「数据自身时间已超出可接受范围」的条目；这不等于全部数据都被核实过，只等于没有任何一条能被证明过期。"
            : ""}
        </div>
      </div>
    </div>
  );
}
