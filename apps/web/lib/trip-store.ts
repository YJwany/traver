"use client";

import { useCallback, useEffect, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
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
  reload: () => Promise<void>;
  send: (message: string) => Promise<AnyRecord>;
};

export function useTripStore(tripId: string): TripStore {
  const [trip, setTrip] = useState<AnyRecord | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [attributions, setAttributions] = useState<Attribution[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [lastResult, setLastResult] = useState<AnyRecord | null>(null);
  const [error, setError] = useState("");

  const reload = useCallback(async () => {
    const [tripResponse, conversationResponse, attributionResponse] = await Promise.all([
      fetch(`${API_URL}/api/v1/trips/${tripId}`),
      fetch(`${API_URL}/api/v1/trips/${tripId}/conversation`),
      fetch(`${API_URL}/api/v1/providers/attributions`),
    ]);
    if (!tripResponse.ok) throw new Error("旅行加载失败");
    setTrip(await tripResponse.json());
    if (conversationResponse.ok) setMessages(await conversationResponse.json());
    // A missing attribution list must not break the workspace, but it must also
    // not be silently dropped when it is present.
    if (attributionResponse.ok) setAttributions(await attributionResponse.json());
  }, [tripId]);

  useEffect(() => {
    reload().catch((reason) => setError(reason instanceof Error ? reason.message : "旅行加载失败")).finally(() => setLoading(false));
  }, [reload]);

  async function send(message: string) {
    setBusy(true); setError("");
    try {
      const response = await fetch(`${API_URL}/api/v1/ai/chat`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ trip_id: tripId, message }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail ?? "AI 请求失败");
      setLastResult(result);
      await reload();
      return result;
    } catch (reason) {
      const messageText = reason instanceof Error ? reason.message : "AI 请求失败";
      setError(messageText); throw reason;
    } finally { setBusy(false); }
  }

  return { trip, budget: trip?.budget ?? null, itinerary: trip?.itinerary ?? null, sources: trip?.preview?.source_references ?? [], weather: trip?.preview?.weather ?? null, attributions, messages, loading, busy, revision: trip?.change_history?.at(-1) ?? null, lastResult, error, reload, send };
}
