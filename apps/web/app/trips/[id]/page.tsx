"use client";

import Link from "next/link";
import { FormEvent, useMemo, useState } from "react";
import type { CSSProperties } from "react";
import { useTripStore } from "../../../lib/trip-store";
import type { Attribution } from "../../../lib/trip-store";
import { freshnessClass, freshnessLabel, formatTimestamp } from "../../../lib/freshness";
import { submitRefreshJob } from "../../../lib/planning-jobs";
import { useJobProgress } from "../../../lib/use-job-progress";
import { JobProgressPanel } from "../../components/job-progress";
import { Disagreements } from "../../components/disagreements";
import { AgeUnknownPanel } from "../../components/freshness-coverage";
import { ExportPanel } from "../../components/export-panel";
import { IngestedPlacesPanel } from "../../components/ingested-places";
import { ReviewsPanel } from "../../components/reviews";
import { unmatchedMentionWording, type ResolvedPlace } from "../../../lib/ingested-places";
import { placePins } from "../../../lib/map";
import { PACE_LABELS, TRAVEL_MODE_LABELS } from "../../components/plan-form";

type AnyRecord = Record<string, any>;
type Trip = AnyRecord;

const typeNames: Record<string, string> = { attraction: "景点", restaurant: "餐厅", hotel: "酒店", transfer: "交通", buffer: "缓冲" };

function formatMoney(value: unknown) { return value === null || value === undefined ? "—" : `¥${value}`; }
function formatTime(value: string) { return value?.slice(11, 16) ?? ""; }
function sourceFor(item: AnyRecord) { return item.source_reference ?? item.source_references?.[0] ?? item.source_reference; }

// The labelling rules live in lib/freshness.ts so they can be tested directly: they are
// the part of this screen that can mislead, and they were previously wrong in a way no
// test could see.

function StaleFactBanner({ facts }: { facts: AnyRecord[] }) {
  if (!facts.length) return null;
  return <div className="panel" style={{ marginTop: 18, borderColor: "var(--amber)" }}>
    <div className="panel-heading">
      <h2>时效性提示 / stale data</h2>
      <span className="status-chip status-stale">{facts.length} 项</span>
    </div>
    <div className="panel-body">
      {facts.map((fact: AnyRecord, index: number) => <div key={`${fact.source}:${fact.source_id}:${index}`} className="entity-card">
        <div className="entity-title"><span>{fact.source}</span><span className="status-chip status-stale">{fact.data_type}</span></div>
        <div className="entity-meta">{fact.detail}</div>
        <div className="entity-meta">
          抓取时间 {formatTimestamp(fact.fetched_at)} · 判定时间 {formatTimestamp(fact.evaluated_at)}
        </div>
      </div>)}
      <div className="entity-meta" style={{ marginTop: 10 }}>
        这些数据自身的时间已超出该类事实可接受的范围，因此不应作为「当前信息」使用。抓取时间只说明我们何时去问，不代表数据本身很新。
      </div>
    </div>
  </div>;
}

function MapPanel({ trip }: { trip: Trip }) {
  const entities = [
    ...(trip.preview?.hotels ?? []).slice(0, 2).map((item: AnyRecord) => ({ ...item, pinType: "hotel" })),
    ...(trip.preview?.restaurants ?? []).slice(0, 6).map((item: AnyRecord) => ({ ...item, pinType: "food" })),
    ...(trip.preview?.attractions ?? []).slice(0, 6).map((item: AnyRecord) => ({ ...item, pinType: "attraction" })),
  ];
  // The box is the trip's own points and nothing else. It used to be seeded with two literals,
  // 30.6 and 104.0 — 成都 — so the box was the union of the trip and one fixed city. A 成都 trip
  // looked right, which is why it survived; every other destination was drawn as a smear in the
  // bottom-right corner. The rule now lives in lib/map.ts, where it has tests.
  const placements = placePins(entities.map((item) => item.location?.coordinates));
  const drawable = entities.filter((_, index) => placements[index] !== null);
  if (!drawable.length) {
    // An empty panel, not a default box: a default box would claim a location we do not have.
    return <div className="map"><div className="map-grid" /><div className="map-label">{trip.profile.destination} · 坐标视图</div><div className="empty">这次计划里没有可用的坐标，无法给出位置示意图。</div></div>;
  }
  const position = (index: number): CSSProperties => {
    const placement = placements[index]!;
    return { left: `${placement.left}%`, top: `${placement.top}%` };
  };
  return <div className="map"><div className="map-grid" /><div className="map-label">{trip.profile.destination} · 示意图（非按比例）</div>{entities.map((item, index) => placements[index] === null ? null : <span className={`pin ${item.pinType}`} style={position(index)} title={item.canonical_name} key={`${item.id}-${index}`} />)}<div className="map-legend"><span><i className="legend-dot" style={{ background: "var(--blue)" }} />酒店</span><span><i className="legend-dot" style={{ background: "#e07b13" }} />餐厅</span><span><i className="legend-dot" style={{ background: "#8d57d8" }} />景点</span></div></div>;
}

function aqiLabel(index: number | null | undefined): string | null {
  if (index === null || index === undefined) return null;
  if (index <= 20) return "优";
  if (index <= 40) return "良";
  if (index <= 60) return "中等";
  if (index <= 80) return "较差";
  if (index <= 100) return "很差";
  return "极差";
}

function WeatherPanel({ weather }: { weather: AnyRecord | null }) {
  if (!weather) return <div className="empty">暂无天气数据。免费天气源在目的地可解析时提供 16 天预报。</div>;
  return <div className="panel-body">
    <div className="entity-meta" style={{ marginBottom: 10 }}>
      {weather.location_name} · {weather.timezone}
      {weather.elevation_m !== null && weather.elevation_m !== undefined ? <> · 海拔 {Math.round(weather.elevation_m)} m</> : null}
      {" · 数据来自 Open-Meteo"}
    </div>
    <div className="weather-strip">
      {weather.days?.slice(0, 8).map((day: AnyRecord) => <div className="weather-day" key={day.day_date}><div className="weather-date">{day.day_date}</div><div className="weather-temp">{Math.round(day.temperature_min_c)}–{Math.round(day.temperature_max_c)}°C</div><div className="weather-meta">{day.condition}{day.precipitation_probability !== null && day.precipitation_probability !== undefined ? <><br />降水概率 {day.precipitation_probability}%</> : null}{aqiLabel(day.air_quality_index) ? <><br />空气 {aqiLabel(day.air_quality_index)}（EAQI {Math.round(day.air_quality_index)}{day.pm2_5_ug_m3 !== null && day.pm2_5_ug_m3 !== undefined ? ` · PM2.5 ${Math.round(day.pm2_5_ug_m3)}` : ""}）</> : null}{day.uv_index_max !== null && day.uv_index_max !== undefined ? <><br />紫外线 {Number(day.uv_index_max).toFixed(1)}</> : null}{day.outdoor_friendly === false ? <><br /><span className="weather-indoor">不建议户外</span></> : null}</div></div>)}
    </div>
    <div className="entity-meta" style={{ marginTop: 12 }}>
      天气与空气质量为 Open-Meteo 预报值（CC BY 4.0），不是实测观测。空气质量端点仅覆盖未来 7 天，超出范围的日期留空而非填默认值。
    </div>
  </div>;
}

function HolidayPanel({ holidays }: { holidays: AnyRecord | null }) {
  if (!holidays) return <div className="empty">暂无节假日数据。公共节假日由本地 MIT 许可的 holidays 数据库提供，无需网络。</div>;
  const items: AnyRecord[] = holidays.holidays ?? [];
  const tripDays = items.length;
  return <div className="panel-body">
    <div className="entity-meta" style={{ marginBottom: 10 }}>
      {holidays.country_code} · {holidays.years?.join(" / ")} · 数据来自 vacanza/holidays（MIT）
    </div>
    {tripDays === 0
      ? <div className="entity-meta">行程期内没有公共节假日。</div>
      : <div className="weather-strip">
        {items.map((holiday: AnyRecord) => <div className="weather-day" key={holiday.holiday_date}><div className="weather-date">{holiday.holiday_date}</div><div className="weather-meta" style={{ marginTop: 4 }}><span className="weather-indoor">{holiday.name}</span>{holiday.nationwide === false ? <><br />仅部分地区</> : null}</div></div>)}
      </div>}
    <div className="entity-meta" style={{ marginTop: 12 }}>
      节假日是会影响开放与拥挤程度的硬事实（含调休产生的连续休息日），但它不代表某个具体景点一定闭馆，因此不会覆盖景点自身的数据。
    </div>
  </div>;
}

function IngestedNotesPanel({ notes, places }: { notes: AnyRecord[]; places: ResolvedPlace[] | undefined }) {
  // Nothing supplied means nothing to say. An empty panel would imply the feature is
  // broken rather than unused.
  if (!notes.length) return null;
  const cautions = notes.filter((note: AnyRecord) => note.kind === "warning" || note.kind === "opening_hours");
  return <section className="panel" style={{ marginTop: 18 }}>
    <div className="panel-heading">
      <h2>你提供的内容 / your notes</h2>
      <span className="muted">{notes.length} 条 · {cautions.length} 条需要注意</span>
    </div>
    <div className="panel-body">
      {notes.map((note: AnyRecord, index: number) => <div className="entity-card" key={`${note.ingestion_id}:${index}`}>
        <div className="entity-title">
          <span>
            {note.matched
              ? <>已关联到 <strong>{note.matched_entity_name}</strong></>
              : note.kind === "place_mention"
                // The sentence comes from the resolution when there is one. A bare 「本系统
                // 没有这个地点」 was a claim about the place, and it was wrong in two of the
                // four cases: the name may have been located, or nobody may have looked.
                ? unmatchedMentionWording(String(note.text ?? ""), places).text
                : "未关联到具体地点"}
          </span>
          <span className={note.kind === "warning" || note.kind === "opening_hours" ? "status-chip status-stale" : "status-chip status-estimated"}>
            {note.kind}
          </span>
        </div>
        <div className="entity-meta">{note.text}</div>
        <blockquote className="fact-evidence">{note.evidence}</blockquote>
        <div className="entity-meta">
          {/* Both times, and "the page did not say" is stated rather than left blank. */}
          {note.published_at
            ? `原文发布于 ${note.published_at.slice(0, 10)}`
            : `原文未标注发布时间 · 抓取于 ${formatTimestamp(note.ingested_at)}`}
          {" · "}{note.platform}
          {note.url ? <> · <a href={note.url} target="_blank" rel="noreferrer noopener">原文</a></> : null}
        </div>
      </div>)}
      <div className="entity-meta" style={{ marginTop: 10 }}>
        这些是你自己提供的内容，属于「某人说的」而不是实测值：它们只作为提示与估算参考，
        不会覆盖高德等来源的实测路线、报价或评分。文中提到的地点会按目的地做地名定位，
        结果列在下面；定位到的也只是「文中提到的地点」，不会替你决定要去。
      </div>
    </div>
  </section>;
}

function AttributionFooter({ attributions }: { attributions: Attribution[] }) {
  if (attributions.length === 0) return null;
  return <footer className="attribution">
    <strong>数据来源与署名</strong>
    <div className="attribution-list">
      {attributions.map((item) => <span key={item.provider}>{item.text} · {item.licence} · <a href={item.url} target="_blank" rel="noreferrer noopener">{new URL(item.url).host}</a></span>)}
    </div>
    <div style={{ marginTop: 6 }}>价格与库存缺失时保持为空并标记为估算，不由模型臆测。</div>
  </footer>;
}

function SourceChip({ source, estimated = false }: { source?: AnyRecord; estimated?: boolean }) {
  if (!source && !estimated) return null;
  return <><span className="source-chip">{estimated ? "系统计算" : source?.source ?? "来源未知"}</span><span className={freshnessClass(source, estimated)}>{freshnessLabel(source, estimated)}</span></>;
}

/**
 * Re-run every source for a trip that already exists.
 *
 * This is the honest form of "refresh": the facts on the page were fetched at a recorded
 * time and the traveller needs to know whether they are still current. Re-querying takes as
 * long as the original plan — the same providers, the same tens of seconds — so it goes
 * through the job API rather than a blocking request that would freeze the browser. The
 * existing trip stays on screen while it runs, which is the point: the comparison is the
 * useful part, not a spinner.
 */
function RefreshPanel({ tripId, onRefreshed }: { tripId: string; onRefreshed: () => Promise<void> }) {
  const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
  const { job, busy, error, notice, follow, cancel } = useJobProgress(apiUrl);

  async function start() {
    try {
      const created = await submitRefreshJob(tripId);
      await follow(created.id);
      // Only after the job reached a terminal state, so a failed refresh leaves the
      // previously loaded trip exactly as it was rather than blanking it.
      await onRefreshed();
    } catch {
      /* useJobProgress keeps the visible error */
    }
  }

  return (
    <>
      <div className="credential-help" style={{ marginTop: 10 }}>
        <button className="button secondary" type="button" onClick={start} disabled={busy}>
          {busy ? "正在重新拉取…" : "重新拉取数据 →"}
        </button>
        <span className="muted" style={{ fontSize: 12 }}>
          会用当前配置的数据源重新查询一遍，耗时与首次规划相当。
        </span>
      </div>
      {error && !job && (
        <div className="error" style={{ marginTop: 10 }} role="status">
          {error}
        </div>
      )}
      {job && (
        <JobProgressPanel
          job={job}
          title={job.status === "succeeded" ? "重新拉取完成" : "正在重新拉取数据"}
          onCancel={cancel}
          notice={notice}
        />
      )}
    </>
  );
}

export default function TripWorkspacePage({ params }: { params: { id: string } }) {
  const { trip, messages, loading, busy, error, lastResult, weather, attributions, needsCredentials, send, reload } = useTripStore(params.id);
  const [input, setInput] = useState("");
  async function submit(event: FormEvent) { event.preventDefault(); if (!input.trim()) return; const message = input; setInput(""); try { await send(message); } catch { /* store keeps the visible error state */ } }
  const selectedOffer = useMemo(() => trip?.preview?.hotel_offers?.find((offer: AnyRecord) => offer.id === trip.selected_hotel_offer_id), [trip]);
  if (loading) return <main className="shell page"><div className="loading">正在打开旅行工作区…</div></main>;
  if (error && !trip) return <main className="shell page">
    <div className="error">{error}</div>
    {needsCredentials && <p className="muted" style={{ marginTop: 12 }}>行程按属主隔离：服务端启用认证时，只有创建它的 API Key 能读取。</p>}
    <div style={{ display: "flex", gap: 10, marginTop: 18, flexWrap: "wrap" }}>
      {needsCredentials && <Link className="button" href="/settings">前往设置填入 API Key</Link>}
      <Link className="button secondary" href="/trips">返回旅行列表</Link>
    </div>
  </main>;
  if (!trip) return null;
  const budget = trip.budget; const sources = trip.preview?.source_references ?? [];
  return <main className="shell page">
    <header className="topbar"><Link className="brand" href="/"><span className="brand-mark" aria-hidden="true" />travel / workspace</Link><nav className="nav"><Link href="/trips">我的旅行</Link><Link href="/trips/new">新建旅行</Link><Link href="/ingestions">内容摄取</Link><Link href="/settings">设置</Link></nav></header>
    <section className="trip-header"><div><div className="eyebrow">TRIP / VERSION {trip.current_version}</div><h1>{trip.profile.destination}</h1><div className="trip-meta"><span>{trip.profile.start_date} — {trip.profile.end_date}</span><span>{trip.profile.travelers} 位旅行者</span><span>{PACE_LABELS[trip.profile.pace] ?? trip.profile.pace}</span>{trip.profile.travel_mode ? <span>城际：{TRAVEL_MODE_LABELS[trip.profile.travel_mode] ?? trip.profile.travel_mode}</span> : null}</div></div><div className="budget-head"><strong>{formatMoney(budget?.total)} <span>/ {formatMoney(budget?.budget_limit)}</span></strong><span>{budget?.over_budget ? "已超预算" : `剩余 ${formatMoney(budget?.remaining)}`}</span></div></section>
    {error && <div className="error" style={{ marginTop: 18 }}>{error}</div>}
    <RefreshPanel tripId={params.id} onRefreshed={reload} />
    {lastResult?.change && <section className="diff"><h3>已保存 Version {lastResult.version} · 本次修改</h3><p>动作：{lastResult.change.action}</p><p>预算影响：{lastResult.change.impact?.difference ? `${lastResult.change.impact.difference}（程序计算）` : "已重新计算"}</p><p>所有变化均来自结构化 Revision 与 Change History。</p></section>}
    {lastResult && lastResult.warnings?.length > 0 && <div className="notice" style={{ margin: "18px 0" }}>{lastResult.warnings.join("；")}</div>}
    <StaleFactBanner facts={trip.preview?.stale_facts ?? []} />
    <AgeUnknownPanel preview={trip.preview} />
    <IngestedNotesPanel notes={trip.preview?.ingested_notes ?? []} places={trip.preview?.ingested_places} />
    <ReviewsPanel reviews={trip.preview?.reviews ?? []} />
    {/* Reported after the notes, because it is about them: these are the names in your own
        material that our sources did not have. Passing the raw field, not `?? []`, so an api
        that does not report resolutions renders nothing instead of "we looked and found none". */}
    <IngestedPlacesPanel places={trip.preview?.ingested_places} />
    <section className="workspace-grid"><div className="panel"><div className="panel-heading"><h2>行程 / itinerary</h2><span className="muted">{trip.itinerary?.conflicts?.length ?? 0} 个冲突</span></div><div className="panel-body">{trip.itinerary?.days?.map((day: AnyRecord) => <div className="day" key={day.id}><h3>Day {day.day_index} <span className="muted">· {day.day_date}</span></h3>{day.items.length === 0 && <div className="muted">暂无可行项目</div>}{day.items.map((item: AnyRecord) => <div className="itinerary-item" key={item.id}><div className="item-time">{formatTime(item.start_time)}</div><div><div className="item-title">{item.title}</div><div className="item-type">{typeNames[item.item_type] ?? item.item_type} · {item.visit_duration_minutes} 分钟 · 路程 {item.travel_from_previous_minutes} 分钟</div></div></div>)}</div>)}</div></div><div className="panel"><div className="panel-heading"><h2>地图 / route view</h2><span className="muted">路线由 Route Optimizer 计算</span></div><MapPanel trip={trip} /></div></section>
    <section className="lower-grid"><div className="panel"><div className="panel-heading"><h2>酒店 / offers</h2><span className="muted">{trip.preview?.hotels?.length ?? 0} 家 · {trip.preview?.hotel_offers?.length ?? 0} 个报价</span></div><div className="panel-body">{trip.preview?.hotel_offers?.length ? trip.preview.hotel_offers.slice(0, 6).map((offer: AnyRecord) => { const hotel = trip.preview.hotels.find((item: AnyRecord) => item.id === offer.hotel_id); const source = sourceFor(offer); return <div className="entity-card" key={offer.id}><div className="entity-title"><span>{hotel?.canonical_name ?? "Hotel offer"}{offer.id === trip.selected_hotel_offer_id && <span className="source-chip">已选择</span>}</span><span className="money">{formatMoney(offer.total_price)}</span></div><div className="entity-meta">{hotel?.rating ? `评分 ${hotel.rating} · ` : ""}{hotel?.location?.district ?? ""}<br />房型与取消政策保留在 Offer 层</div>{hotel && <Disagreements entity={hotel} />}<SourceChip source={source} /></div>; }) : <div className="empty">免费数据源提供酒店地理与名称事实，但不含房价与库存，因此没有报价。预算中的酒店金额是可配置的估算额度，标记为估算。</div>}</div></div>
      <div className="panel"><div className="panel-heading"><h2>美食 / restaurants</h2><span className="muted">{trip.preview?.restaurants?.length ?? 0} 家</span></div><div className="panel-body">{trip.preview?.restaurants?.slice(0, 8).map((item: AnyRecord) => { const source = sourceFor(item); return <div className="entity-card" key={item.id}><div className="entity-title"><span>{item.canonical_name}</span><span className="money">{formatMoney(item.average_cost?.value)}</span></div><div className="entity-meta">{item.cuisine?.join(" / ") || "餐厅"}{item.rating ? ` · 评分 ${item.rating}` : ""}<br />{item.location?.address}</div><Disagreements entity={item} /><SourceChip source={source} /></div>; })}</div></div>
      <div className="panel"><div className="panel-heading"><h2>预算 / sources</h2><span className={budget?.over_budget ? "status-chip status-stale" : "status-chip status-fresh"}>{budget?.over_budget ? "超预算" : "预算内"}</span></div><div className="panel-body">{budget?.items?.map((item: AnyRecord) => <div className="budget-row" key={item.category}><div>{item.category}<small>{item.status}{item.is_estimate ? " · 估算" : " · 有来源"}</small></div><span className="money">{formatMoney(item.amount)}</span></div>)}<div style={{ marginTop: 18 }} className="entity-meta">来源与时间（{sources.length} 条引用）· 抓取时间与数据自身时间分别标注，不把「刚抓到」当作「数据很新」</div>{sources.slice(0, 6).map((source: AnyRecord) => <div key={`${source.source}:${source.source_id}`}><SourceChip source={source} /></div>)}</div></div>
    </section>
    <section className="panel" style={{ marginTop: 18 }}><div className="panel-heading"><h2>天气 / forecast</h2><span className="muted">{weather?.days?.length ?? 0} 天</span></div><WeatherPanel weather={weather} /></section>
    <section className="panel" style={{ marginTop: 18 }}><div className="panel-heading"><h2>节假日 / public holidays</h2><span className="muted">{trip.preview?.holidays?.holidays?.length ?? 0} 天</span></div><HolidayPanel holidays={trip.preview?.holidays ?? null} /></section>
    <section className="panel" style={{ marginTop: 18 }}><div className="panel-heading"><h2>景点 / attractions</h2><span className="muted">{trip.preview?.attractions?.length ?? 0} 个</span></div><div className="panel-body" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: "0 20px" }}>{trip.preview?.attractions?.map((item: AnyRecord) => { const source = sourceFor(item); return <div className="entity-card" key={item.id}><div className="entity-title"><span>{item.canonical_name}</span><span>{item.ticket_price ? formatMoney(item.ticket_price.value) : ""}</span></div><div className="entity-meta">建议停留 {item.recommended_duration_minutes} 分钟 · {item.location?.address}</div>{item.description ? <div className="entity-meta entity-description">{item.description}{item.wikipedia_url ? <> <a href={item.wikipedia_url} target="_blank" rel="noreferrer noopener">维基百科</a></> : null}</div> : null}<Disagreements entity={item} /><SourceChip source={source} /></div>; })}</div></section>
    <section className="chat-history">{messages.filter((message) => message.role === "user" || message.role === "assistant").map((message) => <div className="chat-message" key={message.id}><strong>{message.role === "user" ? "你" : "AI"}</strong>{message.content}</div>)}</section>
    <section className="panel" style={{ marginTop: 18 }}><div className="panel-heading"><h2>修改历史 / versions</h2><span className="muted">{trip.change_history?.length ?? 0} 次</span></div><div className="panel-body">{trip.change_history?.length ? trip.change_history.slice().reverse().map((change: AnyRecord) => <div className="entity-card" key={change.id}><div className="entity-title"><span>Version {change.version} · {change.action}</span><span className="muted">{change.changed_at?.slice(0, 16).replace("T", " ")}</span></div><div className="entity-meta">原因：{change.reason} · 预算影响：{change.impact?.difference ?? "已重算"}</div></div>) : <div className="empty">还没有修改记录。自然语言修改会在这里留下版本。</div>}</div></section>
    <ExportPanel trip={trip} />
    <AttributionFooter attributions={attributions} />
    <div className="chat-dock"><form className="chat-inner" onSubmit={submit}><textarea className="textarea chat-input" value={input} onChange={(event) => setInput(event.target.value)} placeholder="例如：把酒店预算降到 1200 元，或者让第三天不要这么累。" disabled={busy} /><button className="button" type="submit" disabled={busy || !input.trim()}>{busy ? "正在重算…" : "发送 →"}</button></form></div>
  </main>;
}
