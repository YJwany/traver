"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export default function NewTripPage() {
  const [message, setMessage] = useState("");
  const [reply, setReply] = useState("");
  const [questions, setQuestions] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!message.trim()) return;
    setBusy(true); setError(""); setReply("");
    try {
      const response = await fetch(`${API_URL}/api/v1/ai/chat`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "规划请求失败");
      setReply(payload.message); setQuestions(payload.unresolved_questions ?? []);
      if (payload.trip_id && payload.trip_updated) window.location.href = `/trips/${payload.trip_id}`;
    } catch (reason) { setError(reason instanceof Error ? reason.message : "规划请求失败"); }
    finally { setBusy(false); }
  }

  return (
    <main className="shell page">
      <header className="topbar"><Link className="brand" href="/"><span className="brand-mark" aria-hidden="true" />travel / workspace</Link><nav className="nav"><Link href="/trips">我的旅行</Link></nav></header>
      <div className="page-header"><div><div className="eyebrow">NEW TRIP / NATURAL LANGUAGE</div><h1>先说说你想怎么走。</h1></div></div>
      <section className="form-card card">
        <form onSubmit={submit}>
          <label htmlFor="trip-message">旅行需求</label>
          <textarea id="trip-message" className="textarea" value={message} onChange={(event) => setMessage(event.target.value)} placeholder="例如：我和女朋友 2026 年 10 月 3 日到 7 日去成都，预算 5000，想多吃点，不想太赶，酒店离地铁近。" />
          <div className="example-row">
            {["2026年10月去成都4天，两个人，预算5000，喜欢美食。", "去成都玩四天，预算5000，酒店预算1800，不想太赶。", "我想看酒店选择，但还没有决定具体日期。"].map((item) => <button className="example" type="button" key={item} onClick={() => setMessage(item)}>{item}</button>)}
          </div>
          {reply && <div className="notice" style={{ marginBottom: 14 }}>{reply}{questions.length > 0 && <div style={{ marginTop: 6 }}>{questions.join("；")}</div>}</div>}
          {error && <div className="error" style={{ marginBottom: 14 }}>{error}</div>}
          <button className="button" disabled={busy || !message.trim()} type="submit">{busy ? "正在理解需求…" : "理解并开始规划 →"}</button>
        </form>
        <p className="muted" style={{ margin: "22px 0 0", fontSize: 13, lineHeight: 1.55 }}>缺少出发日期时，系统会先询问，不会自行编造日期，也不会生成假实时价格。</p>
      </section>
    </main>
  );
}
