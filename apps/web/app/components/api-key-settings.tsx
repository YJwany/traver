"use client";

/**
 * API key entry.
 *
 * The key is the one piece of configuration the Web app cannot infer, so it
 * needs a visible home. This component deliberately reports *whether* a key is
 * stored without ever rendering it back: echoing a secret into the DOM puts it
 * in screenshots, extensions and DOM dumps.
 *
 * `initialKey` is read on mount rather than during render because `localStorage`
 * does not exist while the server renders this component; reading it in render
 * would produce a hydration mismatch.
 */

import { FormEvent, useEffect, useState } from "react";

import { clearApiKey, getApiKey, looksLikeApiKey, setApiKey } from "../../lib/auth";

type VerifyState = "idle" | "checking" | "valid" | "invalid" | "unreachable";

export function ApiKeySettings({ apiUrl }: { apiUrl: string }) {
  const [current, setCurrent] = useState("");
  const [draft, setDraft] = useState("");
  const [verify, setVerify] = useState<VerifyState>("idle");
  const [message, setMessage] = useState("");
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setCurrent(getApiKey());
    setMounted(true);
  }, []);

  function save(event: FormEvent) {
    event.preventDefault();
    const value = draft.trim();
    if (value && !looksLikeApiKey(value)) {
      setVerify("invalid");
      setMessage("密钥格式不正确：应以 trv_ 开头。");
      return;
    }
    setApiKey(value);
    setCurrent(value);
    setDraft("");
    setVerify("idle");
    setMessage(value ? "已保存到本机浏览器。" : "已清除本机保存的密钥。");
  }

  async function test() {
    setVerify("checking");
    setMessage("");
    try {
      const response = await fetch(`${apiUrl}/api/v1/trips`, {
        headers: current ? { Authorization: `Bearer ${current}` } : {},
      });
      if (response.status === 401) {
        setVerify("invalid");
        setMessage("API 拒绝了该密钥（401）：可能已撤销、输入错误，或服务端未启用认证。");
      } else if (response.status === 403) {
        setVerify("invalid");
        setMessage("密钥有效但缺少 trip:read 权限（403）。");
      } else if (response.ok) {
        setVerify("valid");
        setMessage("连接正常，密钥可用。");
      } else {
        setVerify("invalid");
        setMessage(`服务端返回 HTTP ${response.status}。`);
      }
    } catch {
      setVerify("unreachable");
      setMessage(`无法连接 ${apiUrl}：请确认 API 已启动，且 CORS_ALLOW_ORIGINS 包含当前地址。`);
    }
  }

  // Before mount, storage has not been read, so showing "no key" would be a
  // claim the component cannot yet support.
  const hasKey = mounted && Boolean(current);

  return (
    <section className="card api-key-card" aria-labelledby="api-key-heading">
      <div className="api-key-head">
        <div>
          <div className="eyebrow">API KEY / AUTHENTICATION</div>
          <h2 id="api-key-heading" style={{ margin: "6px 0 4px" }}>接口密钥</h2>
        </div>
        <span className={hasKey ? "status-chip status-fresh" : "status-chip status-estimated"}>
          {!mounted ? "读取中" : hasKey ? "已配置" : "未配置"}
        </span>
      </div>

      <p className="muted" style={{ fontSize: 13, lineHeight: 1.6, margin: "0 0 14px" }}>
        当服务端 <code>AUTH_ENABLED=true</code> 时必须提供。密钥仅保存在本机浏览器的
        localStorage 中，不会写入前端构建产物——<code>NEXT_PUBLIC_*</code> 会被内联进
        JavaScript 包，因此这里刻意不使用它。
      </p>

      <form onSubmit={save} className="api-key-form">
        <label htmlFor="api-key-input">
          {hasKey ? "替换密钥（留空并保存可清除）" : "粘贴密钥"}
        </label>
        <input
          id="api-key-input"
          className="textarea"
          style={{ minHeight: 0, padding: "10px 12px" }}
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="trv_..."
        />
        <div className="api-key-actions">
          <button className="button" type="submit">保存</button>
          <button className="button secondary" type="button" onClick={test} disabled={verify === "checking"}>
            {verify === "checking" ? "测试中…" : "测试连接"}
          </button>
          {hasKey && (
            <button
              className="button secondary"
              type="button"
              onClick={() => {
                clearApiKey();
                setCurrent("");
                setVerify("idle");
                setMessage("已清除本机保存的密钥。");
              }}
            >
              清除
            </button>
          )}
        </div>
      </form>

      {message && (
        <div
          className={verify === "valid" ? "notice" : "error"}
          style={{ marginTop: 12 }}
          role="status"
        >
          {message}
        </div>
      )}

      {hasKey && (
        <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>
          当前密钥：<code>{current.slice(0, 12)}…</code>（仅显示前缀）
        </div>
      )}

      <p className="muted" style={{ fontSize: 12, marginTop: 12, lineHeight: 1.6 }}>
        创建密钥：<code>python -m travel_api.cli create-key --owner &lt;你&gt; --label &lt;设备&gt;</code>
      </p>
    </section>
  );
}
