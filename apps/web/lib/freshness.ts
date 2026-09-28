/**
 * How a datum's age is described to a reader.
 *
 * This is deliberately a module of its own rather than a few inline helpers, because
 * the wording *is* the feature: a tourist plan is only trustworthy if the reader can
 * tell how old each fact is and is not told something we cannot support.
 *
 * The distinction that matters, and the one that was previously got wrong: the server
 * sends two different times.
 *
 *  * `fetched_at` — when we asked the source. Always known.
 *  * `observed_at` — when the datum itself was produced. Only meaningful when
 *    `observed_at_basis` says the source reported it.
 *
 * `is_stale` speaks only to whether *our copy* has passed its validity window. It is
 * false for a source that publishes no timestamps at all, which says nothing about the
 * data's age. Labelling that case "实时" claimed knowledge we did not have.
 */

export type AnyRecord = Record<string, any>;

export const BASIS_PROVIDER_REPORTED = "provider_reported";
export const BASIS_LIVE_COMPUTED = "live_computed";
export const BASIS_SOURCE_SILENT = "source_silent";
export const BASIS_UNKNOWN = "unknown";

export function timeAgo(value?: string): string {
  if (!value) return "未知";
  const millis = new Date(value).getTime();
  if (Number.isNaN(millis)) return "未知";
  const minutes = Math.max(0, Math.floor((Date.now() - millis) / 60000));
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}

/** CSS classes for the three genuinely different states. */
export function freshnessClass(source: AnyRecord | undefined, estimated = false): string {
  if (estimated) return "status-chip status-estimated";
  // Our copy is past its validity window: a real, verifiable problem.
  if (source?.is_stale) return "status-chip status-stale";
  // Only these two bases support a claim of currency.
  if (source?.observed_at_basis === BASIS_LIVE_COMPUTED) return "status-chip status-fresh";
  if (source?.observed_at_basis === BASIS_PROVIDER_REPORTED) return "status-chip status-fresh";
  // Unknown or unclassified age is neither fresh nor stale. Muted, not green: green
  // reads as reassurance, and we have nothing to reassure with.
  return "status-chip status-estimated";
}

/** The label, which must never claim more than the basis supports. */
export function freshnessLabel(source: AnyRecord | undefined, estimated = false): string {
  if (estimated) return "估算";
  if (source?.is_stale) return "已过期";
  switch (source?.observed_at_basis) {
    // The datum is produced by this very request against the live network, so "now" is
    // a true statement about it.
    case BASIS_LIVE_COMPUTED:
      return "实时计算";
    // The source stamped the datum, so its real age can be stated.
    case BASIS_PROVIDER_REPORTED:
      return `数据 ${timeAgo(source?.observed_at)}`;
    // The source gave no observation time. Report when we fetched and say plainly that
    // the data's own time is unknown, rather than letting a fresh fetch imply fresh data.
    default:
      return `抓取于 ${timeAgo(source?.fetched_at)} · 数据时间未知`;
  }
}

/** Whether the age of the data itself is known, for callers that need to branch. */
export function dataAgeIsKnown(source: AnyRecord | undefined): boolean {
  return source?.observed_at_basis === BASIS_PROVIDER_REPORTED;
}

export function formatTimestamp(value?: string): string {
  if (!value) return "未知";
  return value.slice(0, 19).replace("T", " ");
}
