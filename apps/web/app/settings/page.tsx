"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { ApiKeySettings } from "../components/api-key-settings";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

type Health = {
  status: string;
  ready: boolean;
  authentication: string;
  database: string;
  environment?: string;
  database_error?: string;
};

/**
 * Settings / diagnostics.
 *
 * Also surfaces the API's readiness report, because the single most common
 * setup failure is a mismatch between the browser's configuration and the
 * server's auth mode. Showing `authentication` from the API itself makes that
 * mismatch visible instead of leaving the user staring at a 401.
 *
 * The API URL is displayed because a build-time `NEXT_PUBLIC_API_URL` mistake is
 * invisible otherwise.
 */
export default function SettingsPage() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    fetch(`${API_URL}/api/v1/health`, { cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then(setHealth)
      .catch((reason) => setError(reason instanceof Error ? reason.message : "无法连接"));
  }, []);

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
        </nav>
      </header>

      <div className="page-header">
        <div>
          <div className="eyebrow">SETTINGS / CONNECTION</div>
          <h1>连接与密钥</h1>
        </div>
      </div>

      <ApiKeySettings apiUrl={API_URL} />

      <section className="card" style={{ marginTop: 18, padding: 22 }}>
        <div className="eyebrow">API / STATUS</div>
        <h2 style={{ margin: "6px 0 14px" }}>服务端状态</h2>

        {error && (
          <div className="error">
            无法读取 {API_URL}/api/v1/health：{error}
            <div style={{ marginTop: 6, fontSize: 13 }}>
              请确认 API 已启动，且 <code>CORS_ALLOW_ORIGINS</code> 包含当前页面地址。
            </div>
          </div>
        )}

        {health && (
          <div className="api-key-form">
            <div className="entity-meta">
              API 地址：<code>{API_URL}</code>
            </div>
            <div className="entity-meta">
              就绪：<strong>{health.ready ? "ready" : "not ready"}</strong>
              {health.ready ? null : `（数据库：${health.database}）`}
            </div>
            <div className="entity-meta">
              认证模式：<strong>{health.authentication}</strong>
              {health.authentication === "api-key"
                ? " —— 需要提供密钥"
                : " —— 服务端未启用认证，任何可访问该端口的客户端都能读写全部行程"}
            </div>
            {health.environment && (
              <div className="entity-meta">
                环境：<code>{health.environment}</code>
              </div>
            )}
            {health.database_error && (
              <div className="error" style={{ marginTop: 10 }}>
                数据库错误：{health.database_error}
              </div>
            )}
          </div>
        )}
      </section>
    </main>
  );
}
