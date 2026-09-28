"use client";

import { useCallback, useEffect, useState } from "react";

import { ApiError, apiRequest } from "./api-client";

type AnyRecord = Record<string, any>;
type Message = { id: string; role: "user" | "assistant" | "tool" | "system"; content: string; tool_name?: string | null };

/** ODbL / CC BY 4.0 require visible credit for the free data sources. */
export type Attribution = { provider: string; text: string; url: string; licence: string };

export type TripStore = {
  trip: AnyRecord | null;
  budget: AnyRecord | null;
  itinerary: AnyRecord | null;
  sources: AnyRecord[];
  weather: AnyRecord | null;
  attributions: Attribution[];
  messages: Message[];
  loading: boolean;
  busy: boolean;
  revision: AnyRecord | null;
  lastResult: AnyRecord | null;
  error: string;
  /** True when the failure was 401/403, i.e. the fix is credentials. */
  needsCredentials: boolean;
  reload: () => Promise<void>;
  send: (message: string) => Promise<AnyRecord>;
};

function messageFor(reason: unknown, fallback: string): string {
  if (reason instanceof ApiError) {
    if (reason.status === 0) return `${fallback}：无法连接 API（${reason.detail}）`;
    if (reason.isUnauthorized) return "需要 API Key：请在页面顶部填入有效的密钥。";
    if (reason.isForbidden) return "当前 API Key 没有执行该操作的权限。";
    if (reason.isRateLimited) return "请求过于频繁，请稍后重试。";
    return reason.detail || fallback;
  }
  return reason instanceof Error ? reason.message : fallback;
}

function isCredentialFailure(reason: unknown): boolean {
  return reason instanceof ApiError && (reason.isUnauthorized || reason.isForbidden);
}

export function useTripStore(tripId: string): TripStore {
  const [trip, setTrip] = useState<AnyRecord | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [attributions, setAttributions] = useState<Attribution[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [lastResult, setLastResult] = useState<AnyRecord | null>(null);
  const [error, setError] = useState("");
  const [needsCredentials, setNeedsCredentials] = useState(false);

  const reload = useCallback(async () => {
    // The trip is required; the conversation and attribution list are not, so a
    // failure there must not blank out a workspace that otherwise loaded fine.
    // `allSettled` is what keeps that distinction, rather than a shared catch.
    const [tripResult, conversationResult, attributionResult] = await Promise.allSettled([
      apiRequest<AnyRecord>(`/api/v1/trips/${tripId}`),
      apiRequest<Message[]>(`/api/v1/trips/${tripId}/conversation`),
      apiRequest<Attribution[]>("/api/v1/providers/attributions"),
    ]);

    if (tripResult.status === "rejected") throw tripResult.reason;
    setTrip(tripResult.value);

    if (conversationResult.status === "fulfilled") setMessages(conversationResult.value);
    // A missing attribution list must not break the workspace, but it must also
    // not be silently dropped when it is present.
    if (attributionResult.status === "fulfilled") setAttributions(attributionResult.value);
  }, [tripId]);

  useEffect(() => {
    reload()
      .then(() => {
        setError("");
        setNeedsCredentials(false);
      })
      .catch((reason) => {
        setError(messageFor(reason, "旅行加载失败"));
        setNeedsCredentials(isCredentialFailure(reason));
      })
      .finally(() => setLoading(false));
  }, [reload]);

  async function send(message: string) {
    setBusy(true);
    setError("");
    try {
      const result = await apiRequest<AnyRecord>("/api/v1/ai/chat", {
        method: "POST",
        body: { trip_id: tripId, message },
      });
      setLastResult(result);
      await reload();
      return result;
    } catch (reason) {
      setError(messageFor(reason, "AI 请求失败"));
      setNeedsCredentials(isCredentialFailure(reason));
      throw reason;
    } finally {
      setBusy(false);
    }
  }

  return {
    trip,
    budget: trip?.budget ?? null,
    itinerary: trip?.itinerary ?? null,
    sources: trip?.preview?.source_references ?? [],
    weather: trip?.preview?.weather ?? null,
    attributions,
    messages,
    loading,
    busy,
    revision: trip?.change_history?.at(-1) ?? null,
    lastResult,
    error,
    needsCredentials,
    reload,
    send,
  };
}
