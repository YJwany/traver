"use client";

import Link from "next/link";

import { ContentIngestion } from "../components/content-ingestion";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

/**
 * Supplying your own content.
 *
 * Its own page rather than a settings section, because it is not configuration: the
 * settings screen is about what the deployment *can* do, and this is about adding
 * something to it. It is also the one screen that works with nothing configured at all,
 * which is why it is reachable from the main navigation.
 */
export default function IngestionsPage() {
  return (
    <main className="shell page">
      <header className="topbar">
        <Link className="brand" href="/">
          <span className="brand-mark" aria-hidden="true" />
          travel / workspace
        </Link>
        <nav className="nav">
          <Link href="/trips">我的旅行</Link>
          <Link href="/trips/new">新建旅行</Link>
          <Link href="/ingestions">内容摄取</Link>
          <Link href="/settings">设置</Link>
        </nav>
      </header>

      <div className="page-header">
        <div>
          <div className="eyebrow">CONTENT / YOUR OWN SOURCES</div>
          <h1>内容摄取</h1>
        </div>
      </div>

      <ContentIngestion apiUrl={API_URL} />
    </main>
  );
}
