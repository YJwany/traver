/**
 * The type of a review as the server sends it.
 *
 * Written out rather than left as `Record<string, any>` because the whole point of a review is
 * the *optional* date. With a loose type, `review.published_at` and `review.age_days` would
 * accept anything and a missing date would render as `undefined` in the middle of a Chinese
 * sentence; with this type, the compiler forces each use to handle the absent case, which is the
 * case the requirement is about.
 *
 * `published_at` and `age_days` are both nullable for the reason the server documents: `None`
 * means the source did not say when the review was written, and that is a different fact from
 * "written today". Nothing here fills the gap in — see `components/reviews.tsx`.
 */
export type Review = {
  id?: string;
  /** The entity the review is about, by the entity's own id. */
  subject_id: string;
  rating: number | null;
  /**
   * The source's own scale maximum, when it is not 5.
   *
   * Recorded separately so a 9/10 is not silently rewritten as a 4.5: two scales are not
   * comparable, and normalising one into the other would invent a precision the source never
   * claimed.
   */
  rating_scale_max: number | null;
  text: string | null;
  /** When the reviewer wrote it, or null when the source did not say. */
  published_at: string | null;
  /** Days since the reviewer wrote it, or null when nobody said. */
  age_days: number | null;
  /** Which source's scale this score belongs to, so two scores are never averaged. */
  source: string;
};
