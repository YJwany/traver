"use client";

/**
 * Where two sources said different things about the same place.
 *
 * The merge records disagreements, but recording is only half the fix: a stored conflict
 * that never reaches the screen leaves the page exactly as misleading as before — one
 * ticket price displayed beside two source names, which reads as agreement.
 *
 * Three commitments:
 *
 * **Every claim is listed, not only the losing one.** "Two sources, ¥60 and ¥80" and
 * "three sources, two saying ¥60" are different situations, and only the full list lets a
 * reader tell them apart.
 *
 * **The retained value is named, with its source.** The number in the card above is the
 * one a traveller will act on, so the panel has to make clear which claim that number is.
 *
 * **An empty panel must not read as verification.** When there is nothing to show this
 * renders nothing — deliberately, because a panel that always appeared would be ignored —
 * and when it does appear it says that agreement is not proof, only the absence of a
 * conflict among the sources that happened to speak.
 */

type AnyRecord = Record<string, any>;

const FIELD_LABELS: Record<string, string> = {
  ticket_price: "门票",
  average_cost: "人均",
  opening_hours: "营业时间",
  rating: "评分",
  review_count: "评价数",
};

/** The field a person would call this, falling back to the raw name rather than nothing. */
export function fieldLabel(field: string): string {
  return FIELD_LABELS[field] ?? field;
}

function formatObserved(value: unknown): string {
  if (typeof value !== "string" || !value) return "时间未知";
  return value.slice(0, 16).replace("T", " ");
}

export function Disagreements({ entity }: { entity: AnyRecord }) {
  const findings: AnyRecord[] = entity?.disagreements ?? [];
  if (!findings.length) return null;

  return (
    <div className="disagreement">
      <div className="disagreement-head">
        <span className="status-chip status-stale">来源不一致</span>
        <span className="muted">{findings.length} 个字段上，不同来源给了不同的值</span>
      </div>
      {findings.map((finding) => (
        <div className="disagreement-body" key={finding.field}>
          <div className="entity-meta">
            <strong>{fieldLabel(finding.field)}</strong>
            {" · 当前显示的是 "}
            <strong>{finding.chosen_value_text}</strong>
            {finding.chosen_source ? `（来自 ${finding.chosen_source}）` : ""}
          </div>
          <ul className="disagreement-claims">
            {(finding.claims ?? []).map((claim: AnyRecord, index: number) => (
              <li key={`${claim.source}:${index}`}>
                <span className="source-chip">{claim.source}</span> {claim.value_text}
                {/* Each claim's own observation time, because that difference is the
                    whole reason recency was chosen as the tie-break — hiding the dates
                    would hide the justification for the retained value. */}
                <span className="muted"> · 观测 {formatObserved(claim.observed_at)}</span>
              </li>
            ))}
          </ul>
          <div className="entity-meta muted">{finding.chosen_reason}</div>
        </div>
      ))}
      <div className="entity-meta muted" style={{ marginTop: 8 }}>
        这些差异没有被自动抹平，也没有被平均成一个折中值：不同来源确实给了不同的答案，
        上面列出的是全部原始说法。没有这一栏不代表数据已被核实，只代表说过的来源彼此一致。
      </div>
    </div>
  );
}
