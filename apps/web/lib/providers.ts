"use client";

/**
 * Provider catalogue and capability matrix.
 *
 * Traver ships no data and binds to no model, so the settings page has one job
 * that matters: answer "what can this deployment do right now, and what is
 * missing". The API already computes that (`GET /api/v1/providers/catalog`); this
 * module keeps the presentation honest.
 *
 * Three rules the helpers here exist to enforce, because each is a way a UI can
 * quietly lie:
 *
 *   1. A `planned` source must never look like a capability. It is shown as
 *      "尚未实现" and its capabilities are marked "接入后才会提供", never as
 *      something the deployment can already do.
 *   2. "No model configured" is not a fault. The local planner produces a
 *      complete itinerary, so it is reported as a working mode, not an error.
 *   3. A capability is only "covered" when the server says so. Nothing here
 *      infers coverage from the fact that some provider mentions a capability —
 *      that is exactly the self-flattering bug the server-side matrix is built
 *      to avoid.
 */

import { apiRequest } from "./api-client";

export const STATUS_AVAILABLE = "available";
export const STATUS_NEEDS_CREDENTIALS = "needs_credentials";
export const STATUS_PLANNED = "planned";

export type CredentialField = {
  key: string;
  label: string;
  help: string;
  docs_url: string;
  secret: boolean;
  required: boolean;
  configured: boolean;
};

export type ProviderEntry = {
  id: string;
  label: string;
  kind: string;
  status: string;
  capabilities: string[];
  /** Which parts of a decomposable capability this source serves, if any. */
  aspects: Record<string, string[]>;
  cost_note: string;
  freshness_note: string;
  docs_url: string;
  caveats: string[];
  active: boolean;
  credentials: CredentialField[];
  missing_credentials: string[];
};

/** One part of a capability, such as 驾车 within 路线规划. */
export type AspectRow = {
  aspect: string;
  label: string;
  covered: boolean;
  providers: string[];
  /**
   * Who can close this gap: `"source"` if supplying a credential or a new source would,
   * `"tool"` if only a change to this program would.
   *
   * Optional so a client built against this version keeps working with an older API. When
   * absent the gap is treated as source-closable, which is the conservative reading: it
   * matches what every aspect but cycling actually is, and it never invents a limitation
   * the server did not state.
   */
  closable_by?: "source" | "tool";
  /** Why the gap exists, when the server chose to explain it. */
  note?: string;
  /**
   * Whether any request can name this aspect at all.
   *
   * The mechanical form of `closable_by === "tool"`, exposed separately because it is the
   * fact and `closable_by` is the advice: an aspect can be requestable and still uncovered
   * (公交/地铁 without a key), but an unrequestable one is uncovered no matter what is
   * configured. Optional for the same backward-compatibility reason.
   */
  requestable?: boolean;
};

export type CapabilityRow = {
  capability: string;
  label: string;
  covered: boolean;
  /**
   * Whether *every* part is served. Distinct from `covered` on purpose: routing
   * is covered by driving while transit is absent, and collapsing the two would
   * either hide the transit gap or deny that driving works.
   */
  complete: boolean;
  aspects: AspectRow[];
  missing_aspects: string[];
  providers: string[];
};

/** A part of a capability that nothing serves. */
export type UncoveredAspect = {
  capability: string;
  capability_label: string;
  aspect: string;
  label: string;
  /** See `AspectRow.closable_by`. Optional for the same backward-compatibility reason. */
  closable_by?: "source" | "tool";
  note?: string;
  /** See `AspectRow.requestable`. */
  requestable?: boolean;
};

export type VaultStatus = {
  enabled: boolean;
  locked_reason: string | null;
  key_source: string;
  key_file: string;
  owner_only_permissions: boolean;
};

export type Catalog = {
  providers: ProviderEntry[];
  capabilities: CapabilityRow[];
  registered: string[];
  uncovered_capabilities: string[];
  uncovered_aspects: UncoveredAspect[];
  /**
   * Uncovered capabilities no catalogue entry can unlock, each with why and what would.
   *
   * Optional so a client built against this version can still talk to an older API without
   * crashing the settings page. When it is absent the panel simply does not render; the
   * count in `uncovered_capabilities` is still shown, so nothing is claimed that is false —
   * only less is explained.
   */
  capability_gaps?: CapabilityGap[];
  vault: VaultStatus;
};

/**
 * A capability nothing in the catalogue can serve, with the reason.
 *
 * Distinct from `uncovered_capabilities` for the same reason the server keeps them apart:
 * "nobody serves this" and "you have not configured it yet" have opposite remedies, and a
 * bare list of ids cannot tell them apart.
 */
export type CapabilityGap = {
  capability: string;
  label: string;
  note: string;
  /**
   * Who can close it. Always `"source"` for a whole capability: if any entry served it,
   * its manifest would say so and it would be reported as unlockable instead of as a gap.
   * Present anyway so the two gap kinds share a shape and a client can treat them alike.
   */
  closable_by?: "source" | "tool";
};

/** Which credential names are set for this caller, and where each came from. */
export type CredentialListing = {
  vault: VaultStatus;
  credentials: Record<string, Record<string, string>>;
};

export type ModelTestResult = {
  ok: boolean;
  model: string;
  latency_ms: number | null;
  reply: string | null;
  error: string | null;
};

export async function fetchCatalog(): Promise<Catalog> {
  return apiRequest<Catalog>("/api/v1/providers/catalog");
}

export async function fetchCredentials(): Promise<CredentialListing> {
  return apiRequest<CredentialListing>("/api/v1/settings/credentials");
}

/**
 * Store one credential.
 *
 * The response deliberately carries no value, and neither does this function:
 * there is no path by which a saved secret comes back into the browser.
 */
export async function saveCredential(
  providerId: string,
  credentialKey: string,
  value: string,
): Promise<void> {
  await apiRequest(
    `/api/v1/settings/credentials/${encodeURIComponent(providerId)}/${encodeURIComponent(credentialKey)}`,
    { method: "PUT", body: { value } },
  );
}

export async function deleteCredential(
  providerId: string,
  credentialKey: string,
): Promise<void> {
  await apiRequest(
    `/api/v1/settings/credentials/${encodeURIComponent(providerId)}/${encodeURIComponent(credentialKey)}`,
    { method: "DELETE" },
  );
}

export async function testModel(): Promise<ModelTestResult> {
  return apiRequest<ModelTestResult>("/api/v1/settings/model/test", { method: "POST" });
}

// -- labels ----------------------------------------------------------------

const STATUS_LABELS: Record<string, string> = {
  [STATUS_AVAILABLE]: "可用",
  [STATUS_NEEDS_CREDENTIALS]: "待配置",
  [STATUS_PLANNED]: "尚未实现",
};

export function statusLabel(entry: ProviderEntry): string {
  return STATUS_LABELS[entry.status] ?? entry.status;
}

/**
 * The single word describing this source's current contribution.
 *
 * `active` is the server's answer, so it is trusted; the refinement is only
 * about *why* an engine is not contributing. A planned source is never "enabled
 * but idle" — it does not exist yet, and saying otherwise would imply otherwise.
 */
export function activityLabel(entry: ProviderEntry): string {
  if (entry.status === STATUS_PLANNED) return "尚未实现";
  if (entry.active) return "使用中";
  if (entry.missing_credentials.length > 0) return "缺凭据";
  return "未启用";
}

export function isPlanned(entry: ProviderEntry): boolean {
  return entry.status === STATUS_PLANNED;
}

/** Kind labels; unknown kinds fall back to the raw value rather than a guess. */
export function kindLabel(kind: string): string {
  if (kind === "data") return "数据源";
  if (kind === "ai") return "模型";
  if (kind === "content") return "内容";
  return kind;
}

// -- the matrix ------------------------------------------------------------

export type Coverage = {
  /** Capabilities with at least one source. */
  covered: number;
  /** Capabilities with every part served. */
  complete: number;
  total: number;
  /** 0–100, rounded, of *fully* served capabilities. */
  percent: number;
};

/**
 * Coverage counts complete capabilities for the percentage.
 *
 * Using `covered` instead would let a deployment that can plan driving but has no
 * transit advertise a full bar. The partial count is kept so the difference is
 * visible rather than rounded away.
 */
export function coverage(rows: CapabilityRow[]): Coverage {
  const total = rows.length;
  const covered = rows.filter((row) => row.covered).length;
  const complete = rows.filter((row) => row.covered && row.complete).length;
  return {
    covered,
    complete,
    total,
    percent: total === 0 ? 0 : Math.round((complete / total) * 100),
  };
}

export type CapabilityClaim = {
  capability: string;
  label: string;
  coveredElsewhere: boolean;
  /** Parts of this capability, if it decomposes, that nothing serves today. */
  missingAspects: string[];
  /** The labels of those parts, for display. */
  missingAspectLabels: string[];
  /**
   * The subset of `missingAspects` that this source would actually close.
   *
   * Kept apart from `missingAspects` because they are not the same promise. 公交/地铁 is
   * missing today and 高德 closes it — "可补". Cycling is missing today and **nothing**
   * closes it, because the tool has no way to request a bike route; listing it under the
   * same word tells someone to go and find a data source for a gap that only a code change
   * can close, and the search cannot succeed.
   */
  closableAspectLabels: string[];
  /** Labeled parts this source lists that only a change to the tool could ever serve. */
  toolOnlyAspectLabels: string[];
};

/**
 * What this source serves, and — for a capability it lists — which of that
 * capability's parts are still unserved.
 *
 * The aspects are the reason this is not a simple boolean. A planned source that
 * lists `calculate_routes` while driving already works is not "already covered";
 * it is the fix for 公交/地铁, and saying "already covered" would bury the single
 * most useful thing the UI can tell a user planning a trip in China.
 *
 * The split between `closableAspectLabels` and `toolOnlyAspectLabels` comes from the
 * server's own `closable_by` on each aspect, so the distinction is stated once, where the
 * routing modes are defined, rather than re-derived per component.
 */
export function capabilityClaims(
  entry: ProviderEntry,
  rows: CapabilityRow[],
): CapabilityClaim[] {
  const byCapability = new Map(rows.map((row) => [row.capability, row]));
  return entry.capabilities.map((capability) => {
    const row = byCapability.get(capability);
    const served = entry.aspects[capability];
    // Aspects this source would serve that no provider serves today. For an
    // atomic capability both lists are empty and the boolean decides.
    const missing = (row?.aspects ?? []).filter(
      (aspect) => !aspect.covered && (served ? served.includes(aspect.aspect) : true),
    );
    const closable = missing.filter((aspect) => aspect.closable_by !== "tool");
    const toolOnly = missing.filter((aspect) => aspect.closable_by === "tool");
    return {
      capability,
      label: row?.label ?? capability,
      // A capability no row mentions is not covered; do not assume otherwise.
      coveredElsewhere: row?.covered === true,
      missingAspects: missing.map((aspect) => aspect.aspect),
      missingAspectLabels: missing.map((aspect) => aspect.label),
      closableAspectLabels: closable.map((aspect) => aspect.label),
      toolOnlyAspectLabels: toolOnly.map((aspect) => aspect.label),
    };
  });
}

export type UnlockHint = {
  providerId: string;
  label: string;
  /** Human description of what this source would add. */
  unlocks: string[];
};

/**
 * What is actually missing, and which sources would supply it.
 *
 * Two kinds of gap are folded together because to a user they are the same
 * problem — "the trip needs something the program cannot do":
 *
 *   * a capability with no source at all;
 *   * a part of a capability with no source (公交/地铁 while driving works).
 *
 * Split by whether the fix is available today: "fill in a key" is actionable,
 * "wait for the code" is not, and merging them would waste the user's time.
 *
 * A gap no source can close is therefore excluded from `unlocks` rather than listed
 * hopefully. Cycling is the case: no manifest claims it and no credential would add it,
 * because the tool has no way to request a bike route. Putting it in a source's unlock
 * list would send the reader looking for something that does not exist.
 */
export function unlockHints(catalog: Catalog): {
  configurable: UnlockHint[];
  planned: UnlockHint[];
} {
  const uncovered = new Set(catalog.uncovered_capabilities);
  const configurable: UnlockHint[] = [];
  const planned: UnlockHint[] = [];

  for (const entry of catalog.providers) {
    if (entry.active) continue;
    // A model has no capabilities, so it is never the fix for a data gap; it is
    // reported separately because the local planner means it is not a gap.
    const unlocks: string[] = [];
    for (const claim of capabilityClaims(entry, catalog.capabilities)) {
      const wholeCapabilityMissing = uncovered.has(claim.capability) && !claim.coveredElsewhere;
      if (wholeCapabilityMissing) {
        unlocks.push(claim.label);
      } else if (claim.closableAspectLabels.length > 0 && claim.coveredElsewhere) {
        // Only claim the parts nothing serves *and* that this source could actually
        // serve. Promising the whole capability would be false when another source
        // already half-covers it, and promising a tool-only part would be false outright.
        unlocks.push(`${claim.label}：${claim.closableAspectLabels.join("、")}`);
      }
    }
    if (unlocks.length === 0) continue;
    const hint: UnlockHint = { providerId: entry.id, label: entry.label, unlocks };
    if (isPlanned(entry)) planned.push(hint);
    else configurable.push(hint);
  }
  return { configurable, planned };
}

/**
 * Whether the deployment has at least one working model.
 *
 * Reported separately from the capability matrix because a model is not a data
 * capability: without one the local planner still produces a full itinerary, so
 * this is information rather than a gap.
 */
export function hasModel(catalog: Catalog): boolean {
  return catalog.providers.some((entry) => entry.kind === "ai" && entry.active);
}

export function modelEntry(catalog: Catalog): ProviderEntry | undefined {
  return catalog.providers.find((entry) => entry.kind === "ai");
}

// -- vault honesty ---------------------------------------------------------

export type VaultNote = {
  tone: "ok" | "warn" | "info";
  text: string;
};

/**
 * Plain-language description of how stored credentials are protected.
 *
 * The wording tracks what the encryption actually defends against. It says
 * "database leak" rather than "secure", and it names the two configurations
 * differently, because a key file sitting beside the data is genuinely weaker
 * than a key held only in the environment — claiming otherwise would be the
 * kind of overstatement this project is trying to avoid.
 */
export function vaultNotes(vault: VaultStatus): VaultNote[] {
  if (!vault.enabled) {
    const reason = vault.locked_reason ?? "未提供可用主密钥";
    return [
      {
        tone: "warn",
        text: `保险箱不可用（${reason}），暂时无法保存任何凭据。仍可用环境变量配置，或设置 CREDENTIAL_ENCRYPTION_KEY 后重启。`,
      },
    ];
  }

  const notes: VaultNote[] = [];
  if (vault.key_source === "env") {
    notes.push({
      tone: "ok",
      text: "主密钥来自环境变量，不在数据库、也不在数据库备份中；数据库泄露时凭据仍是密文。",
    });
  } else if (vault.key_source === "file") {
    notes.push({
      tone: "info",
      text: `主密钥来自密钥文件（${vault.key_file}）。数据库泄露时凭据仍是密文，但能读取该文件的人可以解开全部凭据；想避免这一点请改用 CREDENTIAL_ENCRYPTION_KEY。`,
    });
    if (!vault.owner_only_permissions) {
      // Windows os.chmod cannot set an ACL, so this is a real limitation rather
      // than a cosmetic one.
      notes.push({
        tone: "warn",
        text: "当前平台上无法把密钥文件限制为仅属主可读，同一台机器上的其他账户可能读到它。",
      });
    }
  } else {
    notes.push({ tone: "warn", text: "无法确定主密钥来源，请检查服务端配置。" });
  }
  return notes;
}
