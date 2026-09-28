"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { ApiError, apiRequest } from "../../lib/api-client";

type Trip = { id: string; profile: { destination: string; start_date: string; end_date: string; travelers: number }; budget?: { total: string; budget_limit?: string | null; over_budget: boolean }; current_version: number; updated_at: string };

/**
 * Page size for the list.
 *
 * The API clamps `limit` (rejecting an unbounded value), so the client must send
 * something explicit — "load everything" is no longer expressible, by design.
 */
const PAGE_SIZE = 50;

export default function TripsPage() {
  const [trips, setTrips] = useState<Trip[]>([]);
  const [error, setError] = useState("");
  const [needsKey, setNeedsKey] = useState(false);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      setTrips(await apiRequest<Trip[]>("/api/v1/trips", { params: { limit: PAGE_SIZE } }));
      setError("");
      setNeedsKey(false);
    } catch (reason) {
      setNeedsKey(reason instanceof ApiError && (reason.isUnauthorized || reason.isForbidden));
      setError(
        reason instanceof ApiError
          ? reason.status === 0
            ? `无法连接 API：${reason.detail}`
            : reason.detail
          : reason instanceof Error
            ? reason.message
            : "旅行列表加载失败",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  return (
    <main className="shell page">
      <header className="topbar"><Link className="brand" href="/"><span className="brand-mark" aria-hidden="true" />travel / workspace</Link><nav className="nav"><Link href="/trips/new">新建旅行</Link><Link href="/settings">设置</Link></nav></header>
      <div className="page-header"><div><div className="eyebrow">TRIPS / PERSISTED WORKSPACE</div><h1>我的旅行</h1></div><Link className="button" href="/trips/new">新建旅行 →</Link></div>
      {error && (
        <div className="error">
          {error}
          {needsKey && <> · <Link href="/settings" style={{ color: "inherit", textDecoration: "underline" }}>前往设置填入 API Key</Link></>}
        </div>
      )}
      {loading && !error && <div className="loading">正在读取旅行列表…</div>}
      {!loading && !error && trips.length === 0 && <div className="empty card">还没有已保存的旅行。<br /><Link href="/trips/new" style={{ color: "var(--blue)" }}>从一句话开始创建</Link></div>}
      <section style={{ display: "grid", gap: 12 }}>{trips.map((trip) => <Link href={`/trips/${trip.id}`} className="card" style={{ padding: 20, display: "flex", justifyContent: "space-between", gap: 16, alignItems: "center" }} key={trip.id}><div><div className="eyebrow">VERSION {trip.current_version}</div><h2 style={{ margin: "8px 0 6px", letterSpacing: "-.04em" }}>{trip.profile.destination}</h2><div className="muted">{trip.profile.start_date} — {trip.profile.end_date} · {trip.profile.travelers} 人</div></div><div style={{ textAlign: "right" }}><strong className="money">¥{trip.budget?.total ?? "—"}</strong><div className="muted" style={{ fontSize: 12 }}>已保存状态 →</div></div></Link>)}</section>
    </main>
  );
}
