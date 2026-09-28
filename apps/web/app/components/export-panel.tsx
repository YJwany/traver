"use client";

/**
 * Exporting a plan, and the one thing about it worth saying in the UI.
 *
 * The buttons are ordinary downloads. The reason this is a component rather than two
 * anchors is the note above them: an exported file *leaves* the application. Inside the
 * app, a number sits next to the source chip and the freshness panel that explain it.
 * In a downloaded Markdown file or an imported calendar it does not — the reader has the
 * numbers and nothing else, and they look exactly as authoritative a year later.
 *
 * The server writes the disclosures into both documents. This panel states, before the
 * click, what the file will and will not carry, so the reader is not surprised by a
 * caveat later — and so that a reader who never opens the file still knows that the
 * export is a snapshot rather than a live link.
 *
 * The calendar caveat is specific and easy to get wrong. Itinerary times are stored as
 * naive destination local time, so they are written to iCalendar as *floating* time: no
 * `Z`, no `TZID`. That is the honest encoding, but it means a client renders them in the
 * device's own zone. A plan for 东京 imported on a phone set to 伦敦 shows 09:30 London
 * time — the numbers shift while looking unchanged. Saying so here is cheaper than a
 * traveller arriving at the wrong hour.
 */

import { useState } from "react";

import { downloadExport } from "../../lib/export";
import type { ExportFormat } from "../../lib/export";

type AnyRecord = Record<string, any>;

const FORMATS: { format: ExportFormat; label: string; hint: string }[] = [
  {
    format: "md",
    label: "下载 Markdown",
    hint: "完整记录：行程、限制、预算、每条来源的两个时间，以及你交进来的内容本身",
  },
  { format: "ics", label: "下载日历", hint: "逐项事件，可导入手机日历" },
];

export function ExportPanel({ trip }: { trip: AnyRecord }) {
  const [busy, setBusy] = useState<ExportFormat | null>(null);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");

  const destination: string = trip?.profile?.destination ?? "";
  const itemCount =
    trip?.itinerary?.days?.reduce(
      (total: number, day: AnyRecord) => total + (day.items?.length ?? 0),
      0,
    ) ?? 0;
  const sourceCount = trip?.preview?.source_references?.length ?? 0;
  const unverifiable = Array.isArray(trip?.preview?.age_unknown_facts)
    ? trip.preview.age_unknown_facts.length
    : null;

  async function run(format: ExportFormat) {
    setBusy(format);
    setError("");
    setSaved("");
    try {
      const filename = await downloadExport(trip.id, format, destination);
      setSaved(filename);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "导出失败");
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="panel" style={{ marginTop: 18 }}>
      <div className="panel-heading">
        <h2>导出 / export</h2>
        <span className="muted">
          {itemCount} 个行程项 · {sourceCount} 条来源
        </span>
      </div>
      <div className="panel-body">
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          {FORMATS.map((entry) => (
            <button
              key={entry.format}
              type="button"
              className="button secondary"
              onClick={() => run(entry.format)}
              disabled={busy !== null}
              title={entry.hint}
            >
              {busy === entry.format ? "正在导出…" : entry.label}
            </button>
          ))}
        </div>
        {saved && (
          <div className="entity-meta" style={{ marginTop: 10 }}>
            已保存 {saved}
          </div>
        )}
        {error && (
          <div className="error" style={{ marginTop: 10 }}>
            {error}
          </div>
        )}

        <div className="entity-meta" style={{ marginTop: 12 }}>
          导出的文件是<strong>快照</strong>：里面的数字不会自动更新，文件里写明了它是什么时候生成的。
          Markdown 版会逐条列出每个来源的「抓取时间」与「数据自身时间」，并单独说明哪些数据
          无法判断年龄；请把它当作那一刻的记录，而不是一直有效的攻略。
        </div>
        <div className="entity-meta" style={{ marginTop: 8 }}>
          附录里也会写入<strong>你自己交进来的内容</strong>：正文片段、原文链接、发布时间与抓取时间，
          以及文中提到的地点被定位到了哪里。行程里的景点随时可以重新抓取，而你保存的那篇内容
          可能明天就被删掉——所以那份副本留在文件里，不留在我们这里。
        </div>
        {unverifiable === null ? (
          // Absent is not empty. This API predates the field, so nothing here can be said
          // about coverage — and claiming「全部可验证」would invent a check that never ran.
          <div className="entity-meta" style={{ marginTop: 8 }}>
            这份数据里没有时效性核查记录，因此无法说明导出文件里数据的可信程度。
          </div>
        ) : unverifiable > 0 ? (
          <div className="entity-meta" style={{ marginTop: 8 }}>
            其中 {unverifiable} 条来源没有提供数据自身的时间，文件里会标注为「无法确认」——
            这不等于过期，也不等于数据是新的，只等于无法判断。
          </div>
        ) : null}
        <div className="entity-meta" style={{ marginTop: 8 }}>
          日历版把行程时间按<strong>目的地当地时间</strong>写入（09:30 就是当地 09:30）。
          因为行程里没有记录时区，日历不会做换算，你的日历客户端会按设备时区显示它——
          如果设备时区与目的地不同，显示出来的时刻会不一样。所有事件都是「暂定」，不是预订。
        </div>
      </div>
    </section>
  );
}
