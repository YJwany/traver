"use client";

/**
 * Client for the content-ingestion endpoints.
 *
 * The API is small on purpose. What matters here is that the *shape* of an extraction is
 * faithfully carried to the screen: every fact arrives with the sentence it came from,
 * the reason it was read that way, and — separately — when we received the note and when
 * the page said it was published. Flattening those two times into one would undo the
 * server's honesty at the last step.
 */

import { apiRequest } from "./api-client";

export type ExtractedFact = {
  kind: string;
  text: string;
  /** The exact span of the supplied text this was read from. */
  evidence: string;
  char_start: number;
  char_end: number;
  value: Record<string, unknown>;
  confidence: string;
  reason: string;
};

export type Ingestion = {
  id: string;
  platform: string;
  platform_label: string;
  url: string | null;
  title: string | null;
  author: string | null;
  ingested_at: string;
  /** The page's own publication date. `null` means the page did not state one. */
  published_at: string | null;
  facts: ExtractedFact[];
  content_text: string;
  input_chars: number;
  notes: string[];
};

export type Bookmarklet = {
  href: string;
  base_url: string;
  steps: string[];
  notes: string[];
};

export type FactKindMeta = { label: string; tone: "fact" | "caution" | "mention" };

//: How each kind of fact is described and coloured.
//:
//: A caution is marked differently from a fact because it changes what can be planned: a
//: closed day removes an option, whereas a price is context. Showing them identically
//: would make the reader do that triage themselves.
export const FACT_KINDS: Record<string, FactKindMeta> = {
  price: { label: "价格", tone: "fact" },
  duration: { label: "时长", tone: "fact" },
  opening_hours: { label: "开放/闭馆", tone: "caution" },
  warning: { label: "提醒", tone: "caution" },
  tip: { label: "建议", tone: "mention" },
  place_mention: { label: "提到的地点", tone: "mention" },
};

export function factKindMeta(kind: string): FactKindMeta {
  return FACT_KINDS[kind] ?? { label: kind, tone: "mention" };
}

/** Confidence wording. Deliberately about the *reading*, never about the truth. */
export function confidenceLabel(confidence: string): string {
  switch (confidence) {
    case "high":
      return "识别明确";
    case "medium":
      return "识别一般";
    case "low":
      return "仅为线索";
    default:
      return confidence;
  }
}

/**
 * How old the advice is, which is not the same as when we received it.
 *
 * Returns both facts as a sentence, because a reader needs both and the pair is what
 * prevents a fresh fetch from reading as fresh advice.
 */
export function adviceAge(publishedAt: string | null, ingestedAt: string): string {
  if (publishedAt) {
    return `原文发布于 ${formatDate(publishedAt)}`;
  }
  return `原文未标注发布时间（抓取于 ${formatDate(ingestedAt)}）`;
}

export function formatDate(value: string): string {
  return value.slice(0, 10);
}

export async function createIngestion(payload: {
  text: string;
  url?: string | null;
  title?: string | null;
  published_at?: string | null;
}): Promise<Ingestion> {
  return apiRequest<Ingestion>("/api/v1/ingestions", { method: "POST", body: payload });
}

export async function listIngestions(): Promise<Ingestion[]> {
  return apiRequest<Ingestion[]>("/api/v1/ingestions");
}

export async function deleteIngestion(id: string): Promise<{ deleted: boolean }> {
  return apiRequest<{ deleted: boolean }>(`/api/v1/ingestions/${id}`, { method: "DELETE" });
}

export async function fetchBookmarklet(): Promise<Bookmarklet> {
  return apiRequest<Bookmarklet>("/api/v1/content/bookmarklet");
}
