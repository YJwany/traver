"use client";

/**
 * The content-ingestion screen: paste what you are reading, or run the bookmarklet.
 *
 * The design brief for this page is a single sentence — the traveller must be able to
 * tell, for every extracted line, *why* it was read that way and *how old* the advice is.
 * Everything below serves that:
 *
 *   * each fact shows the sentence it came from, so a wrong reading is visible
 *     immediately rather than after it reaches a plan;
 *   * each fact shows the rule that matched, so the reading can be judged rather than
 *     merely accepted;
 *   * the note's publication date and our fetch date are shown as two separate facts,
 *     and "the page did not say" is a visible value rather than a blank;
 *   * confidence is described as confidence in the *reading*, never in the claim.
 *
 * The page deliberately does not offer to fetch a URL. None of these platforms has a
 * public API, so fetching one would mean scraping it with the traveller's session —
 * which is both a terms violation and a privacy problem. The bookmarklet runs in the
 * traveller's own browser instead, and this page says so plainly.
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { ApiError } from "../../lib/api-client";
import {
  adviceAge,
  confidenceLabel,
  createIngestion,
  deleteIngestion,
  factKindMeta,
  fetchBookmarklet,
  listIngestions,
  type Bookmarklet,
  type ExtractedFact,
  type Ingestion,
} from "../../lib/ingestions";

function FactRow({ fact }: { fact: ExtractedFact }) {
  const meta = factKindMeta(fact.kind);
  return (
    <li className={`fact-row fact-${meta.tone}`}>
      <div className="fact-head">
        <span className={`claim claim-${meta.tone}`}>{meta.label}</span>
        <strong className="fact-text">{fact.text}</strong>
        <span className="status-chip status-estimated">{confidenceLabel(fact.confidence)}</span>
      </div>
      {/* The quote is the feature. Without it the traveller is asked to believe a
          reading; with it they can check it in one glance. */}
      <blockquote className="fact-evidence">{fact.evidence}</blockquote>
      <div className="entity-meta">识别依据：{fact.reason}</div>
    </li>
  );
}

function IngestionCard({ item, onDelete }: { item: Ingestion; onDelete: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const counts = item.facts.reduce<Record<string, number>>((accumulator, fact) => {
    accumulator[fact.kind] = (accumulator[fact.kind] ?? 0) + 1;
    return accumulator;
  }, {});

  return (
    <article className="card ingestion-card">
      <div className="api-key-head">
        <div>
          <h3 style={{ margin: "0 0 4px" }}>
            {item.title || item.url || "粘贴的文本"}
            <span className="provider-kind">{item.platform_label}</span>
          </h3>
          <div className="entity-meta">
            {/* Both times, always, and "not stated" spelled out. */}
            {adviceAge(item.published_at, item.ingested_at)}
            {item.author ? ` · 作者 ${item.author}` : ""}
          </div>
        </div>
        <div className="provider-badges">
          <span className="status-chip status-estimated">{item.facts.length} 条</span>
          <button className="button secondary" type="button" onClick={() => onDelete(item.id)}>
            删除
          </button>
        </div>
      </div>

      <div className="provider-claims">
        {Object.entries(counts).map(([kind, count]) => (
          <span key={kind} className={`claim claim-${factKindMeta(kind).tone}`}>
            {factKindMeta(kind).label} × {count}
          </span>
        ))}
      </div>

      {item.notes.length > 0 && (
        <ul className="note-list">
          {item.notes.map((note) => (
            <li key={note} className="vault-note vault-note-warn">
              {note}
            </li>
          ))}
        </ul>
      )}

      <button className="button secondary" type="button" onClick={() => setOpen((value) => !value)}>
        {open ? "收起原文与逐条依据" : "查看原文与逐条依据"}
      </button>

      {open && (
        <>
          {item.facts.length > 0 && (
            <ul className="fact-list">
              {item.facts.map((fact, index) => (
                <FactRow key={`${fact.kind}:${fact.char_start}:${index}`} fact={fact} />
              ))}
            </ul>
          )}
          <details style={{ marginTop: 12 }}>
            <summary className="muted" style={{ fontSize: 12.5, cursor: "pointer" }}>
              原文（{item.input_chars} 字符，逐字保存以便核对）
            </summary>
            <pre className="ingestion-source">{item.content_text}</pre>
          </details>
        </>
      )}
    </article>
  );
}

function BookmarkletPanel({ bookmarklet }: { bookmarklet: Bookmarklet | null }) {
  if (!bookmarklet) return null;
  return (
    <section className="card catalog-panel">
      <div className="api-key-head">
        <div>
          <div className="eyebrow">INGESTION / BOOKMARKLET</div>
          <h2 style={{ margin: "6px 0 4px" }}>在你自己已登录的页面上一键取值</h2>
        </div>
      </div>

      <p className="muted" style={{ fontSize: 13, lineHeight: 1.7, margin: "0 0 12px" }}>
        小红书、抖音、美团、大众点评、携程、同程都<b>没有公开 API</b>。Traver 不会去抓取它们，
        也不保存你的登录态。这里提供的是一个浏览器书签：它在你自己的浏览器、你自己已登录的页面上运行，
        只把当前页面的标题、正文、作者、发布时间和网址发回来。
      </p>

      <ol className="unlock-list" style={{ marginBottom: 12 }}>
        {bookmarklet.steps.map((step) => (
          <li key={step}>{step}</li>
        ))}
      </ol>

      {/* A draggable link is the whole delivery mechanism, so it is a real anchor and
          not a button: dragging an <a> into the bookmarks bar is what browsers support. */}
      <a
        className="button"
        href={bookmarklet.href}
        onClick={(event) => event.preventDefault()}
        draggable
        title="拖到书签栏"
      >
        拖我到书签栏 ↑
      </a>

      <details style={{ marginTop: 12 }}>
        <summary className="muted" style={{ fontSize: 12.5, cursor: "pointer" }}>
          查看脚本内容（自己审一遍再拖）
        </summary>
        <pre className="ingestion-source">{bookmarklet.href}</pre>
      </details>

      <ul className="note-list" style={{ marginTop: 12 }}>
        {bookmarklet.notes.map((note) => (
          <li key={note} className="vault-note vault-note-info">
            {note}
          </li>
        ))}
      </ul>
    </section>
  );
}

export function ContentIngestion({ apiUrl }: { apiUrl: string }) {
  const [text, setText] = useState("");
  const [url, setUrl] = useState("");
  const [publishedAt, setPublishedAt] = useState("");
  const [items, setItems] = useState<Ingestion[] | null>(null);
  const [bookmarklet, setBookmarklet] = useState<Bookmarklet | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    try {
      setItems(await listIngestions());
      setError("");
    } catch (reason) {
      if (reason instanceof ApiError && reason.isUnauthorized) {
        setError("需要先在设置页填写接口密钥，之后才能读取与保存内容。");
      } else if (reason instanceof ApiError && reason.isForbidden) {
        setError("当前密钥缺少 trip:read 权限。");
      } else {
        setError(reason instanceof Error ? reason.message : "无法读取已保存的内容");
      }
    }
  }, []);

  useEffect(() => {
    void load();
    // Best-effort: the bookmarklet is a convenience, and a failure to load it must not
    // block the paste path, which is the one that always works.
    void fetchBookmarklet()
      .then(setBookmarklet)
      .catch(() => setBookmarklet(null));
  }, [load]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!text.trim()) return;
    setBusy(true);
    setNotice("");
    try {
      const created = await createIngestion({
        text,
        url: url.trim() || null,
        // Empty string means "the page did not say", which the server records as an
        // absent date rather than as today.
        published_at: publishedAt.trim() || null,
      });
      setNotice(
        `已保存：识别到 ${created.facts.length} 条可结构化内容。${
          created.published_at ? "" : "原文没有标注发布时间，已如实记为「未知」。"
        }`,
      );
      setText("");
      setUrl("");
      setPublishedAt("");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    try {
      await deleteIngestion(id);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "删除失败");
    }
  }

  if (error && items === null) {
    return (
      <section className="card catalog-panel">
        <div className="eyebrow">INGESTION / CONTENT</div>
        <h2 style={{ margin: "6px 0 10px" }}>内容摄取</h2>
        <div className="error" role="status">
          {error}
          <div style={{ marginTop: 6, fontSize: 13 }}>
            接口地址：<code>{apiUrl}/api/v1/ingestions</code>
          </div>
        </div>
      </section>
    );
  }

  return (
    <>
      <section className="card catalog-panel">
        <div className="api-key-head">
          <div>
            <div className="eyebrow">INGESTION / PASTE</div>
            <h2 style={{ margin: "6px 0 4px" }}>粘贴你要参考的内容</h2>
          </div>
        </div>

        <p className="muted" style={{ fontSize: 13, lineHeight: 1.7, margin: "0 0 12px" }}>
          把小红书笔记、点评、攻略的正文粘贴进来。Traver 会用<b>规则</b>从中提取价格、时长、
          开放/闭馆时间、提示、警告和地名，每一条都附上<b>原文出处</b>与识别依据，
          方便你直接核对——它不会猜，也不会编。
        </p>

        <form onSubmit={submit}>
          <textarea
            className="textarea"
            style={{ minHeight: 160 }}
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder="把正文粘贴到这里，例如：推荐去灵隐寺，建议游玩 2 小时。门票 ¥75。周一闭馆，注意别白跑。"
            spellCheck={false}
          />
          <div className="unlock-grid" style={{ marginTop: 12 }}>
            <label className="credential-row">
              <span>来源网址（可选）</span>
              <input
                className="textarea"
                style={{ minHeight: 0, padding: "9px 11px" }}
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                placeholder="https://www.xiaohongshu.com/explore/..."
                spellCheck={false}
              />
            </label>
            <label className="credential-row">
              <span>原文发布时间（可选，页面没写就留空）</span>
              <input
                className="textarea"
                style={{ minHeight: 0, padding: "9px 11px" }}
                value={publishedAt}
                onChange={(event) => setPublishedAt(event.target.value)}
                placeholder="2024-05-01 或 2024年05月01日"
                spellCheck={false}
              />
            </label>
          </div>
          <div className="credential-help" style={{ marginTop: 10 }}>
            <button className="button" type="submit" disabled={busy || !text.trim()}>
              {busy ? "正在提取…" : "提取并保存"}
            </button>
            {/* Said out loud, because leaving it blank is the honest choice and would
                otherwise feel like an omission the traveller should fix. */}
            <span className="muted" style={{ fontSize: 12 }}>
              留空表示「页面没有写发布时间」。Traver 会如实标注为「未知」，而不是当作最新内容。
            </span>
          </div>
        </form>

        {notice && (
          <div className="notice" style={{ marginTop: 12 }} role="status">
            {notice}
          </div>
        )}
        {error && (
          <div className="error" style={{ marginTop: 12 }} role="status">
            {error}
          </div>
        )}
      </section>

      <BookmarkletPanel bookmarklet={bookmarklet} />

      <section className="catalog-section">
        <div className="page-header" style={{ marginBottom: 12 }}>
          <div>
            <div className="eyebrow">INGESTION / SAVED</div>
            <h2 style={{ margin: "4px 0 0" }}>已保存的内容</h2>
          </div>
          <span className="muted" style={{ fontSize: 12.5 }}>
            这些内容会作为来源进入下一份行程，只作提示与估算，不会覆盖实测数据
          </span>
        </div>
        {items === null ? (
          <div className="loading">正在读取…</div>
        ) : items.length === 0 ? (
          <div className="empty">
            还没有保存任何内容。粘贴一段，或者用上面的书签在小红书/点评页面上取一次。
          </div>
        ) : (
          <div className="provider-grid">
            {items.map((item) => (
              <IngestionCard key={item.id} item={item} onDelete={remove} />
            ))}
          </div>
        )}
      </section>
    </>
  );
}
