"use client";

import Link from "next/link";
import { useState } from "react";

import { ApiError, apiRequest } from "../../../lib/api-client";
import { PlanForm } from "../../components/plan-form";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

type ChatResult = {
  message?: string;
  unresolved_questions?: string[];
  trip_id?: string;
  trip_updated?: boolean;
};

type Mode = "form" | "chat";

/**
 * Two ways to start a trip, and the order is not arbitrary.
 *
 * The structured form comes first because it is the one that works with nothing
 * configured — no model, no API key for a data source. The natural-language path is the
 * enhancement: it understands a request you would rather not translate into fields, but it
 * needs a model to do that, so making it the only door would mean a traveller with no model
 * cannot use the product at all.
 */
export default function NewTripPage() {
  const [mode, setMode] = useState<Mode>("form");

  return (
    <main className="shell page">
      <header className="topbar">
        <Link className="brand" href="/">
          <span className="brand-mark" aria-hidden="true" />
          travel / workspace
        </Link>
        <nav className="nav">
          <Link href="/trips">我的旅行</Link>
          <Link href="/ingestions">内容摄取</Link>
          <Link href="/settings">设置</Link>
        </nav>
      </header>

      <div className="page-header">
        <div>
          <div className="eyebrow">NEW TRIP</div>
          <h1>先说说你想怎么走。</h1>
        </div>
      </div>

      <div className="example-row" style={{ marginBottom: 16 }}>
        <button
          type="button"
          className={`example ${mode === "form" ? "example-active" : ""}`}
          onClick={() => setMode("form")}
          aria-pressed={mode === "form"}
        >
          填写字段（不需要模型）
        </button>
        <button
          type="button"
          className={`example ${mode === "chat" ? "example-active" : ""}`}
          onClick={() => setMode("chat")}
          aria-pressed={mode === "chat"}
        >
          用一句话描述（需要模型）
        </button>
      </div>

      {mode === "form" ? <PlanForm apiUrl={API_URL} /> : <ChatStarter />}
    </main>
  );
}

/**
 * The natural-language entry point.
 *
 * Rendered only when chosen, so a deployment with no model never loads the code for the
 * path it cannot serve.
 */
function ChatStarter() {
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [reply, setReply] = useState("");
  const [questions, setQuestions] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [needsModel, setNeedsModel] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!message.trim()) return;
    setBusy(true);
    setError("");
    setReply("");
    setNeedsModel(false);
    try {
      const payload = await apiRequest<ChatResult>("/api/v1/ai/chat", {
        method: "POST",
        body: { message },
      });
      setReply(payload.message ?? "");
      setQuestions(payload.unresolved_questions ?? []);
      if (payload.trip_id && payload.trip_updated) {
        window.location.href = `/trips/${payload.trip_id}`;
      }
    } catch (reason) {
      // A missing model is a setup problem with a specific remedy — either configure one
      // or use the form — so it is reported as such rather than as a generic failure.
      setNeedsModel(reason instanceof ApiError && reason.status === 503);
      setError(
        reason instanceof ApiError
          ? reason.status === 0
            ? `无法连接 API：${reason.detail}`
            : reason.detail
          : reason instanceof Error
            ? reason.message
            : "规划请求失败",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="form-card card">
      <form onSubmit={submit}>
        <label htmlFor="trip-message">旅行需求</label>
        <textarea
          id="trip-message"
          className="textarea"
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          placeholder="例如：我和女朋友 2026 年 10 月 3 日到 7 日去成都，预算 5000，想多吃点，不想太赶，酒店离地铁近。"
        />
        <div className="example-row">
          {[
            "2026年10月去成都4天，两个人，预算5000，喜欢美食。",
            "去成都玩四天，预算5000，酒店预算1800，不想太赶。",
            "我想看酒店选择，但还没有决定具体日期。",
          ].map((item) => (
            <button className="example" type="button" key={item} onClick={() => setMessage(item)}>
              {item}
            </button>
          ))}
        </div>
        {reply && (
          <div className="notice" style={{ marginBottom: 14 }}>
            {reply}
            {questions.length > 0 && <div style={{ marginTop: 6 }}>{questions.join("；")}</div>}
          </div>
        )}
        {error && (
          <div className="error" style={{ marginBottom: 14 }}>
            {error}
            {needsModel && (
              <>
                {" · "}
                <Link href="/settings" style={{ color: "inherit", textDecoration: "underline" }}>
                  前往设置接入模型
                </Link>
                {" ，或改用左边的「填写字段」方式（不需要模型）"}
              </>
            )}
          </div>
        )}
        <button className="button" disabled={busy || !message.trim()} type="submit">
          {busy ? "正在理解需求…" : "理解并开始规划 →"}
        </button>
      </form>
      <p className="muted" style={{ margin: "22px 0 0", fontSize: 13, lineHeight: 1.55 }}>
        缺少出发日期时，系统会先询问，不会自行编造日期，也不会生成假实时价格。
      </p>
    </section>
  );
}
