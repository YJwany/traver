"use client";

/**
 * Provider catalogue UI: what this deployment can do, and what is missing.
 *
 * The whole screen is generated from the server's catalogue, so adding a source
 * to `providers/manifest.py` makes it appear here with its own credential form —
 * no edit to this file. That is what makes "adding a data source needs no code
 * change" true rather than aspirational.
 *
 * Two reporting rules are load-bearing:
 *
 *   1. **渐进增强 is shown, not asserted.** The header states what works right
 *      now. With zero credentials that is a complete itinerary from free sources
 *      plus the local planner, so an unconfigured deployment reads as "usable,
 *      and here is what would make it better" — not as an error state.
 *   2. **A planned source is labelled as unimplemented** wherever it appears, and
 *      its capabilities are marked as coming later. Listing a source must never
 *      look like a capability.
 */

import { useCallback, useEffect, useState } from "react";

import { ApiError } from "../../lib/api-client";
import {
  activityLabel,
  capabilityClaims,
  coverage,
  fetchCatalog,
  hasModel,
  isPlanned,
  kindLabel,
  modelEntry,
  saveCredential,
  deleteCredential,
  statusLabel,
  testModel,
  unlockHints,
  vaultNotes,
  type Catalog,
  type ModelTestResult,
  type ProviderEntry,
} from "../../lib/providers";

/** Per-provider feedback, keyed by provider id. */
type Notice = { tone: "ok" | "error"; text: string };

function CapabilityMatrix({ catalog }: { catalog: Catalog }) {
  const total = coverage(catalog.capabilities);
  const partial = total.covered - total.complete;

  return (
    <section className="card catalog-panel" aria-labelledby="matrix-heading">
      <div className="api-key-head">
        <div>
          <div className="eyebrow">CAPABILITIES / MATRIX</div>
          <h2 id="matrix-heading" style={{ margin: "6px 0 4px" }}>
            当前能力
          </h2>
        </div>
        <span className={partial > 0 ? "status-chip status-stale" : "status-chip status-fresh"}>
          {total.complete} / {total.total} 完整可用
        </span>
      </div>

      <div className="meter" role="img" aria-label={`完整能力覆盖 ${total.percent}%`}>
        <span style={{ width: `${total.percent}%` }} />
      </div>

      {partial > 0 && (
        <p className="muted" style={{ fontSize: 12.5, lineHeight: 1.65, margin: "0 0 12px" }}>
          另有 {partial} 项只覆盖了一部分，下面标出缺哪一块。部分覆盖不等于可用：
          缺的那部分会被静默跳过。
        </p>
      )}

      <ul className="capability-list">
        {catalog.capabilities.map((row) => (
          <li key={row.capability} className="capability-item">
            <span
              className={
                !row.covered ? "dot dot-off" : row.complete ? "dot dot-on" : "dot dot-part"
              }
              aria-hidden="true"
            />
            <span className="capability-name">{row.label}</span>
            <span className="capability-source">
              {row.covered ? row.providers.join("、") : "无来源"}
            </span>
            {/* A partially served capability lists what is missing, so "驾车可用"
                is never read as "路线规划没问题". */}
            {row.aspects.length > 0 && (
              <span className="aspect-row">
                {row.aspects.map((aspect) => (
                  <span
                    key={aspect.aspect}
                    className={aspect.covered ? "aspect aspect-on" : "aspect aspect-off"}
                    title={
                      aspect.covered
                        ? `由 ${aspect.providers.join("、")} 提供`
                        : "当前没有任何来源提供"
                    }
                  >
                    {aspect.label}
                    {aspect.covered ? "" : "（缺失）"}
                  </span>
                ))}
              </span>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

function UnlockPanel({ catalog }: { catalog: Catalog }) {
  const hints = unlockHints(catalog);
  const model = modelEntry(catalog);
  const modelReady = hasModel(catalog);
  // Tolerated as absent so a client built against this version can still read an older API.
  const gaps = catalog.capability_gaps ?? [];

  return (
    <section className="card catalog-panel" aria-labelledby="unlock-heading">
      <div className="eyebrow">PROGRESSIVE ENHANCEMENT</div>
      <h2 id="unlock-heading" style={{ margin: "6px 0 10px" }}>
        现在能用什么，接上什么会更好
      </h2>

      <p className="muted" style={{ fontSize: 13, lineHeight: 1.7, margin: "0 0 14px" }}>
        {catalog.uncovered_capabilities.length === 0 && catalog.uncovered_aspects.length === 0
          ? "所有能力都有来源且覆盖完整，零配置即可完整规划。继续接入只会让结果更准、更贴近实时。"
          : `当前有 ${catalog.uncovered_capabilities.length} 项能力完全没有来源，${
              catalog.uncovered_aspects.length
            } 项只覆盖了一部分（${catalog.uncovered_aspects
              .map((gap) => `${gap.capability_label}·${gap.label}`)
              .join("、")}）。其余由免费数据源覆盖。`}
      </p>

      {/* The gaps nothing can fill, named and explained.
          A count alone would leave the reader to find which capability is absent and what
          would fix it — the opposite of useful in a tool whose premise is that you plug in
          whatever you need. This is also where a bare "1 项能力完全没有来源" stops being
          misleading: it is one specific thing, and here is why. */}
      {gaps.length > 0 && (
        <ul className="note-list" style={{ marginBottom: 14 }}>
          {gaps.map((gap) => (
            <li key={gap.capability} className="vault-note vault-note-warn">
              <strong>{gap.label}</strong>：没有任何已接入的来源能提供它。
              {gap.note ? ` ${gap.note}` : ""}
            </li>
          ))}
        </ul>
      )}

      <div className="unlock-grid">
        <div className="unlock-block">
          <div className="unlock-title">
            <span className={modelReady ? "dot dot-on" : "dot dot-off"} aria-hidden="true" />
            AI 模型
          </div>
          <p className="muted" style={{ fontSize: 12.5, lineHeight: 1.65, margin: "6px 0 0" }}>
            {modelReady
              ? `已接入${model?.label ? `（${model.label}）` : ""}，需求理解与工具编排由模型负责。`
              : "未接入。本地确定性规则引擎会接管：行程照样完整生成，只是理解自然语言与编排工具的能力弱一些。这不影响可用性。"}
          </p>
        </div>

        <div className="unlock-block">
          <div className="unlock-title">数据源</div>
          {hints.configurable.length === 0 && hints.planned.length === 0 ? (
            <p className="muted" style={{ fontSize: 12.5, lineHeight: 1.65, margin: "6px 0 0" }}>
              没有缺失的数据能力。
            </p>
          ) : (
            <ul className="unlock-list">
              {hints.configurable.map((hint) => (
                <li key={hint.providerId}>
                  <strong>{hint.label}</strong>：填入凭据后可提供{hint.unlocks.join("、")}
                </li>
              ))}
              {hints.planned.map((hint) => (
                <li key={hint.providerId} className="unlock-planned">
                  <strong>{hint.label}</strong>：<em>尚未实现</em>，接入后才会提供
                  {hint.unlocks.join("、")}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </section>
  );
}

function VaultPanel({ catalog }: { catalog: Catalog }) {
  const notes = vaultNotes(catalog.vault);

  return (
    <section className="card catalog-panel" aria-labelledby="vault-heading">
      <div className="api-key-head">
        <div>
          <div className="eyebrow">CREDENTIAL VAULT</div>
          <h2 id="vault-heading" style={{ margin: "6px 0 4px" }}>
            凭据保存方式
          </h2>
        </div>
        <span
          className={
            catalog.vault.enabled ? "status-chip status-fresh" : "status-chip status-stale"
          }
        >
          {catalog.vault.enabled ? "已加密" : "不可用"}
        </span>
      </div>

      <p className="muted" style={{ fontSize: 13, lineHeight: 1.7, margin: "0 0 10px" }}>
        你填入的第三方 Key 使用 AES-256-GCM 加密后存库，按属主隔离——同一部署的其他人读不到，
        接口也<b>不会把已保存的值回显出来</b>。
      </p>

      <ul className="note-list">
        {notes.map((note) => (
          <li key={note.text} className={`vault-note vault-note-${note.tone}`}>
            {note.text}
          </li>
        ))}
      </ul>

      <p className="muted" style={{ fontSize: 12, lineHeight: 1.7, margin: "10px 0 0" }}>
        它防的是<b>数据库泄露</b>、密文篡改和跨属主搬移，<b>不防</b>
        能读取整个文件系统或能在服务进程内执行代码的攻击者。完整说明见
        <code>docs/architecture/credentials.md</code>。
      </p>
    </section>
  );
}

function CredentialForm({
  entry,
  busyKey,
  notice,
  drafts,
  onDraft,
  onSave,
  onDelete,
  onTestModel,
  modelTest,
  testingModel,
}: {
  entry: ProviderEntry;
  busyKey: string | null;
  notice?: Notice;
  drafts: Record<string, string>;
  onDraft: (key: string, value: string) => void;
  onSave: (key: string) => void;
  onDelete: (key: string) => void;
  onTestModel: () => void;
  modelTest: ModelTestResult | null;
  testingModel: boolean;
}) {
  if (entry.credentials.length === 0) return null;

  return (
    <div className="credential-form">
      {entry.credentials.map((field) => {
        const fieldId = `${entry.id}-${field.key}`;
        const busy = busyKey === field.key;
        const draftValue = drafts[field.key] ?? "";
        return (
          <div key={field.key} className="credential-row">
            <label htmlFor={fieldId}>
              {field.label}
              {field.required ? (
                <span className="req" aria-hidden="true">
                  *
                </span>
              ) : (
                <span className="muted" style={{ fontWeight: 400 }}>
                  （可选）
                </span>
              )}
            </label>
            <div className="credential-input">
              <input
                id={fieldId}
                className="textarea"
                style={{ minHeight: 0, padding: "9px 11px" }}
                type={field.secret ? "password" : "text"}
                autoComplete="off"
                spellCheck={false}
                value={draftValue}
                placeholder={field.configured ? "已配置，留空则保持不变" : field.help}
                onChange={(event) => onDraft(field.key, event.target.value)}
              />
              <button
                className="button"
                type="button"
                disabled={busy || draftValue.trim() === ""}
                onClick={() => onSave(field.key)}
              >
                {busy ? "保存中…" : "保存"}
              </button>
              {field.configured && (
                <button
                  className="button secondary"
                  type="button"
                  disabled={busy}
                  onClick={() => onDelete(field.key)}
                >
                  清除
                </button>
              )}
            </div>
            <div className="credential-help">
              <span className={field.configured ? "status-chip status-fresh" : "status-chip status-estimated"}>
                {field.configured ? "已配置" : "未配置"}
              </span>
              {field.help && <span className="muted">{field.help}</span>}
              {field.docs_url && (
                <a href={field.docs_url} target="_blank" rel="noreferrer noopener">
                  申请地址
                </a>
              )}
            </div>
          </div>
        );
      })}

      {entry.kind === "ai" && (
        <div className="credential-row">
          <div className="credential-input">
            <button
              className="button secondary"
              type="button"
              onClick={onTestModel}
              disabled={testingModel}
            >
              {testingModel ? "测试中…" : "测试连接"}
            </button>
            <span className="muted" style={{ fontSize: 12 }}>
              会真的调用一次模型，而不是只检查 Key 的格式。
            </span>
          </div>
          {modelTest && (
            <div className={modelTest.ok ? "notice" : "error"} style={{ marginTop: 8 }} role="status">
              {modelTest.ok
                ? `连通正常${modelTest.model ? `，模型 ${modelTest.model}` : ""}${
                    modelTest.latency_ms ? `，耗时 ${Math.round(modelTest.latency_ms)} ms` : ""
                  }。`
                : `测试失败：${modelTest.error ?? "未知错误"}`}
            </div>
          )}
        </div>
      )}

      {notice && (
        <div className={notice.tone === "ok" ? "notice" : "error"} role="status">
          {notice.text}
        </div>
      )}
    </div>
  );
}

function ProviderCard({
  entry,
  catalog,
  busyKey,
  notice,
  drafts,
  onDraft,
  onSave,
  onDelete,
  onTestModel,
  modelTest,
  testingModel,
}: {
  entry: ProviderEntry;
  catalog: Catalog;
  busyKey: string | null;
  notice?: Notice;
  drafts: Record<string, string>;
  onDraft: (key: string, value: string) => void;
  onSave: (key: string) => void;
  onDelete: (key: string) => void;
  onTestModel: () => void;
  modelTest: ModelTestResult | null;
  testingModel: boolean;
}) {
  const planned = isPlanned(entry);
  const claims = capabilityClaims(entry, catalog.capabilities);

  // Two badges: where the source stands, and whether it is contributing. They
  // collapse to the same word for a planned source ("尚未实现" twice), which reads
  // as a rendering bug and dilutes the one label that matters — so a repeated
  // label is shown once.
  const status = statusLabel(entry);
  const activity = activityLabel(entry);

  return (
    <article className={`card provider-card${planned ? " provider-planned" : ""}`}>
      <div className="api-key-head">
        <div>
          <h3 style={{ margin: "0 0 4px" }}>
            {entry.label}
            <span className="provider-kind">{kindLabel(entry.kind)}</span>
          </h3>
        </div>
        <div className="provider-badges">
          <span className="status-chip status-estimated">{status}</span>
          {activity !== status && (
            <span
              className={
                entry.active
                  ? "status-chip status-fresh"
                  : planned
                    ? "status-chip status-stale"
                    : "status-chip status-estimated"
              }
            >
              {activity}
            </span>
          )}
        </div>
      </div>

      {claims.length > 0 && (
        <div className="provider-claims">
          {claims.map((claim) => {
            // Three states, because two would lie. A source can serve a
            // capability that is already served in full (its value is quality, not
            // existence), or fill a part nothing serves (a real gap), or be the
            // only source for a capability nothing has.
            const state =
              claim.missingAspectLabels.length > 0
                ? "gap"
                : claim.coveredElsewhere
                  ? "held"
                  : "open";
            const suffix =
              state === "gap"
                ? `（可补：${claim.missingAspectLabels.join("、")}）`
                : state === "held"
                  ? "（已有来源）"
                  : planned
                    ? "（待实现）"
                    : "（缺失）";
            return (
              <span
                key={claim.capability}
                className={
                  state === "gap" ? "claim claim-gap" : state === "held" ? "claim claim-held" : "claim claim-open"
                }
                title={
                  state === "gap"
                    ? `该能力已有来源，但这个来源尚缺的${claim.missingAspectLabels.join("、")}它可补齐`
                    : state === "held"
                      ? "已有其他来源提供该能力"
                      : planned
                        ? "当前没有来源提供，但该来源尚未实现"
                        : "当前没有来源提供，配置后可解锁"
                }
              >
                {claim.label}
                {suffix}
              </span>
            );
          })}
        </div>
      )}

      <dl className="provider-meta">
        <div>
          <dt>费用</dt>
          <dd>{entry.cost_note || "未说明"}</dd>
        </div>
        <div>
          <dt>时效性</dt>
          <dd>{entry.freshness_note || "未说明"}</dd>
        </div>
      </dl>

      {entry.caveats.length > 0 && (
        <ul className="caveat-list">
          {entry.caveats.map((caveat) => (
            <li key={caveat}>{caveat}</li>
          ))}
        </ul>
      )}

      {entry.docs_url && (
        <div className="entity-meta">
          <a href={entry.docs_url} target="_blank" rel="noreferrer noopener">
            官方文档
          </a>
        </div>
      )}

      <CredentialForm
        entry={entry}
        busyKey={busyKey}
        notice={notice}
        drafts={drafts}
        onDraft={onDraft}
        onSave={onSave}
        onDelete={onDelete}
        onTestModel={onTestModel}
        modelTest={modelTest}
        testingModel={testingModel}
      />
    </article>
  );
}

export function ProviderCatalog({ apiUrl }: { apiUrl: string }) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [notices, setNotices] = useState<Record<string, Notice>>({});
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [modelTest, setModelTest] = useState<ModelTestResult | null>(null);
  const [testingModel, setTestingModel] = useState(false);

  const load = useCallback(async () => {
    try {
      setCatalog(await fetchCatalog());
      setError("");
    } catch (reason) {
      // 401 and 403 need different remedies, so they are not flattened into one
      // message: entering the key again cannot fix a missing scope.
      if (reason instanceof ApiError && reason.isUnauthorized) {
        setError("需要先在上方填写接口密钥，之后才能读取数据源与模型状态。");
      } else if (reason instanceof ApiError && reason.isForbidden) {
        setError("当前密钥缺少 trip:read 权限，无法读取配置状态。");
      } else {
        setError(reason instanceof Error ? reason.message : "无法读取数据源状态");
      }
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function run(providerId: string, key: string, action: () => Promise<void>, okText: string) {
    setBusy(key);
    setNotices((current) => {
      const { [providerId]: _dropped, ...rest } = current;
      return rest;
    });
    try {
      await action();
      setDrafts((current) => ({ ...current, [key]: "" }));
      setNotices((current) => ({ ...current, [providerId]: { tone: "ok", text: okText } }));
      await load();
    } catch (reason) {
      // A locked vault answers 503 with an actionable reason; surfacing it
      // verbatim is more useful than a generic failure message.
      const text =
        reason instanceof ApiError
          ? reason.status === 503
            ? `保险箱不可用：${reason.detail}`
            : reason.detail
          : reason instanceof Error
            ? reason.message
            : "操作失败";
      setNotices((current) => ({ ...current, [providerId]: { tone: "error", text } }));
    } finally {
      setBusy(null);
    }
  }

  if (error) {
    return (
      <section className="card catalog-panel">
        <div className="eyebrow">PROVIDERS / CATALOG</div>
        <h2 style={{ margin: "6px 0 10px" }}>数据源与模型</h2>
        <div className="error" role="status">
          {error}
          <div style={{ marginTop: 6, fontSize: 13 }}>
            数据源状态来自 <code>{apiUrl}/api/v1/providers/catalog</code>。
          </div>
        </div>
      </section>
    );
  }

  if (!catalog) {
    return (
      <section className="card catalog-panel">
        <div className="loading">正在读取数据源状态…</div>
      </section>
    );
  }

  const dataProviders = catalog.providers.filter((entry) => entry.kind !== "ai");
  const aiProviders = catalog.providers.filter((entry) => entry.kind === "ai");

  // One renderer for every card. The two lists differ only in how a save is
  // described and whether a model test is offered, so building the shared props
  // once keeps the two sections from drifting apart.
  //
  // `loaded` is passed in rather than closing over `catalog`: a closure would
  // lose the null-check narrowing, and the explicit parameter makes it plain that
  // this only ever runs with a loaded catalogue.
  function cards(loaded: Catalog, entries: ProviderEntry[], onTest: () => void) {
    return entries.map((entry) => (
      <ProviderCard
        key={entry.id}
        entry={entry}
        catalog={loaded}
        busyKey={busy}
        notice={notices[entry.id]}
        drafts={drafts}
        onDraft={(key, value) => setDrafts((current) => ({ ...current, [key]: value }))}
        onSave={(key) =>
          void run(
            entry.id,
            key,
            () => saveCredential(entry.id, key, drafts[key] ?? ""),
            entry.kind === "ai"
              ? "已保存。模型配置按请求解析，下一条消息即生效，无需重启。"
              : "已保存。已保存的值不会被回显，可用「测试连接」验证是否有效。",
          )
        }
        onDelete={(key) =>
          void run(
            entry.id,
            key,
            () => deleteCredential(entry.id, key),
            "已清除，回退到服务端环境变量的值。",
          )
        }
        onTestModel={onTest}
        modelTest={entry.kind === "ai" ? modelTest : null}
        testingModel={entry.kind === "ai" && testingModel}
      />
    ));
  }

  function startModelTest() {
    setTestingModel(true);
    setModelTest(null);
    void testModel()
      .then(setModelTest)
      .catch((reason: unknown) =>
        setModelTest({
          ok: false,
          model: "",
          latency_ms: null,
          reply: null,
          error: reason instanceof Error ? reason.message : "测试失败",
        }),
      )
      .finally(() => setTestingModel(false));
  }

  return (
    <>
      <CapabilityMatrix catalog={catalog} />
      <UnlockPanel catalog={catalog} />
      <VaultPanel catalog={catalog} />

      <section className="catalog-section">
        <div className="page-header" style={{ marginBottom: 12 }}>
          <div>
            <div className="eyebrow">PROVIDERS / DATA SOURCES</div>
            <h2 style={{ margin: "4px 0 0" }}>数据源</h2>
          </div>
        </div>
        <div className="provider-grid">{cards(catalog, dataProviders, () => undefined)}</div>
      </section>

      <section className="catalog-section">
        <div className="page-header" style={{ marginBottom: 12 }}>
          <div>
            <div className="eyebrow">PROVIDERS / MODELS</div>
            <h2 style={{ margin: "4px 0 0" }}>AI 模型</h2>
          </div>
        </div>
        <div className="provider-grid">{cards(catalog, aiProviders, startModelTest)}</div>
      </section>
    </>
  );
}
