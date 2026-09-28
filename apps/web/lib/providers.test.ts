import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setApiKey } from "./auth";
import {
  STATUS_AVAILABLE,
  STATUS_NEEDS_CREDENTIALS,
  STATUS_PLANNED,
  activityLabel,
  capabilityClaims,
  coverage,
  deleteCredential,
  hasModel,
  isPlanned,
  kindLabel,
  modelEntry,
  saveCredential,
  statusLabel,
  unlockHints,
  vaultNotes,
  type CapabilityRow,
  type Catalog,
  type ProviderEntry,
  type VaultStatus,
} from "./providers";

const VALID_KEY = "trv_-b83TL_2eCDR4g8eymTfpusqkpMm4L8t3bkEhuEHkXw";

function provider(overrides: Partial<ProviderEntry> = {}): ProviderEntry {
  return {
    id: "openstreetmap",
    label: "OpenStreetMap",
    kind: "data",
    status: STATUS_AVAILABLE,
    capabilities: ["search_hotels"],
    aspects: {},
    cost_note: "免费",
    freshness_note: "缓存 1 小时",
    docs_url: "",
    caveats: [],
    active: true,
    credentials: [],
    missing_credentials: [],
    ...overrides,
  };
}

function row(overrides: Partial<CapabilityRow> = {}): CapabilityRow {
  return {
    capability: "search_hotels",
    label: "酒店搜索",
    covered: true,
    complete: true,
    aspects: [],
    missing_aspects: [],
    providers: ["openstreetmap"],
    ...overrides,
  };
}

function vault(overrides: Partial<VaultStatus> = {}): VaultStatus {
  return {
    enabled: true,
    locked_reason: null,
    key_source: "env",
    key_file: ".traver-credential-key",
    owner_only_permissions: true,
    ...overrides,
  };
}

function catalog(overrides: Partial<Catalog> = {}): Catalog {
  return {
    providers: [provider()],
    capabilities: [row()],
    registered: ["openstreetmap"],
    uncovered_capabilities: [],
    uncovered_aspects: [],
    capability_gaps: [],
    vault: vault(),
    ...overrides,
  };
}

/** Routing as the real API reports it: driving served, the rest absent. */
function routingRows(): CapabilityRow[] {
  return [
    row({
      capability: "calculate_routes",
      label: "路线规划",
      covered: true,
      complete: false,
      providers: ["osrm"],
      missing_aspects: ["transit", "walking", "cycling"],
      aspects: [
        { aspect: "driving", label: "驾车", covered: true, providers: ["osrm"] },
        { aspect: "transit", label: "公交/地铁", covered: false, providers: [] },
        { aspect: "walking", label: "步行", covered: false, providers: [] },
        { aspect: "cycling", label: "骑行", covered: false, providers: [] },
      ],
    }),
  ];
}

describe("status and activity labels", () => {
  it("labels a planned source as not implemented", () => {
    // The one label that must never soften: listing a source is not a claim it
    // works.
    const entry = provider({ status: STATUS_PLANNED, active: false });
    expect(statusLabel(entry)).toBe("尚未实现");
    expect(activityLabel(entry)).toBe("尚未实现");
    expect(isPlanned(entry)).toBe(true);
  });

  it("distinguishes missing credentials from merely disabled", () => {
    const needs = provider({
      status: STATUS_NEEDS_CREDENTIALS,
      active: false,
      missing_credentials: ["ai_api_key"],
    });
    expect(activityLabel(needs)).toBe("缺凭据");

    const disabled = provider({ status: STATUS_AVAILABLE, active: false });
    expect(activityLabel(disabled)).toBe("未启用");
  });

  it("reports an active source as in use", () => {
    expect(activityLabel(provider({ active: true }))).toBe("使用中");
  });

  it("falls back to the raw value for an unknown status instead of guessing", () => {
    expect(statusLabel(provider({ status: "something-new" }))).toBe("something-new");
    expect(kindLabel("something-new")).toBe("something-new");
  });
});

describe("coverage", () => {
  it("counts only the rows the server marked covered", () => {
    const result = coverage([
      row({ capability: "a", covered: true }),
      row({ capability: "b", covered: false }),
      row({ capability: "c", covered: true }),
      row({ capability: "d", covered: false }),
    ]);
    expect(result).toEqual({ covered: 2, complete: 2, total: 4, percent: 50 });
  });

  it("counts a partially served capability as covered but not complete", () => {
    // The whole reason `complete` exists: routing works for driving only, and a
    // full coverage bar would hide the transit gap.
    const result = coverage([
      row({ capability: "a", covered: true, complete: true }),
      row({ capability: "b", covered: true, complete: false }),
    ]);
    expect(result.covered).toBe(2);
    expect(result.complete).toBe(1);
    expect(result.percent).toBe(50);
  });

  it("reports zero of zero without dividing by zero", () => {
    expect(coverage([])).toEqual({ covered: 0, complete: 0, total: 0, percent: 0 });
  });
});

describe("capabilityClaims", () => {
  it("names the parts a source would fill when the capability is half-covered", () => {
    const amap = provider({
      id: "amap",
      status: STATUS_PLANNED,
      active: false,
      capabilities: ["calculate_routes"],
      aspects: { calculate_routes: ["driving", "transit", "walking", "cycling"] },
    });
    const claims = capabilityClaims(amap, routingRows());

    expect(claims).toHaveLength(1);
    // coveredElsewhere is true — driving does work — but the missing parts are
    // named, which is what stops "已有来源" from burying 公交/地铁.
    expect(claims[0].coveredElsewhere).toBe(true);
    expect(claims[0].missingAspectLabels).toEqual(["公交/地铁", "步行", "骑行"]);
  });

  it("claims only the parts nothing serves, not the whole capability", () => {
    // A source that serves driving too must not promise to add driving.
    const partial = provider({
      id: "other",
      status: STATUS_NEEDS_CREDENTIALS,
      active: false,
      capabilities: ["calculate_routes"],
      aspects: { calculate_routes: ["driving", "transit"] },
    });
    const claims = capabilityClaims(partial, routingRows());
    expect(claims[0].missingAspectLabels).toEqual(["公交/地铁"]);
  });

  it("reports no missing parts for an atomic capability that is covered", () => {
    const claims = capabilityClaims(provider({ capabilities: ["search_hotels"] }), [row()]);
    expect(claims[0].coveredElsewhere).toBe(true);
    expect(claims[0].missingAspectLabels).toEqual([]);
  });

  it("treats a capability no row mentions as not covered", () => {
    const claims = capabilityClaims(provider({ capabilities: ["brand_new"] }), []);
    expect(claims[0].coveredElsewhere).toBe(false);
    expect(claims[0].label).toBe("brand_new");
  });
});

describe("unlockHints", () => {
  it("names the source to configure for a capability nothing serves", () => {
    const hints = unlockHints(
      catalog({
        providers: [
          provider({
            id: "amap",
            label: "高德地图（AMap）",
            status: STATUS_NEEDS_CREDENTIALS,
            active: false,
            capabilities: ["calculate_routes"],
            missing_credentials: ["amap_web_service_key"],
          }),
        ],
        uncovered_capabilities: ["calculate_routes"],
        capabilities: [
          row({ capability: "calculate_routes", label: "路线规划", covered: false }),
        ],
      }),
    );

    expect(hints.configurable).toEqual([
      {
        providerId: "amap",
        label: "高德地图（AMap）",
        unlocks: ["路线规划"],
      },
    ]);
  });

  it("omits a source that declares no capabilities at all", () => {
    // The model is the case that matters: it has no capabilities, so it must
    // never be offered as the fix for a missing data capability.
    const hints = unlockHints(
      catalog({
        providers: [
          provider({
            id: "openai-compatible",
            kind: "ai",
            status: STATUS_NEEDS_CREDENTIALS,
            active: false,
            capabilities: [],
            missing_credentials: ["ai_base_url"],
          }),
        ],
        uncovered_capabilities: ["search_weather"],
        capabilities: [row({ capability: "search_weather", covered: false })],
      }),
    );
    expect(hints.configurable).toEqual([]);
    expect(hints.planned).toEqual([]);
  });

  it("separates an actionable gap from one that needs code", () => {
    const hints = unlockHints(
      catalog({
        providers: [
          provider({ active: false, capabilities: ["search_weather"] }),
          provider({
            id: "amap",
            status: STATUS_PLANNED,
            active: false,
            capabilities: ["calculate_routes"],
          }),
        ],
        uncovered_capabilities: ["search_weather", "calculate_routes"],
        capabilities: [
          row({ capability: "search_weather", label: "天气", covered: false }),
          row({ capability: "calculate_routes", label: "路线规划", covered: false }),
        ],
      }),
    );

    // "Fill in a key" and "wait for the code" are different answers; only one is
    // actionable, so they are never merged.
    expect(hints.configurable.map((hint) => hint.providerId)).toEqual(["openstreetmap"]);
    expect(hints.planned.map((hint) => hint.providerId)).toEqual(["amap"]);
  });

  it("lists nothing for an already-covered capability", () => {
    const hints = unlockHints(
      catalog({
        providers: [
          provider({
            id: "amap",
            status: STATUS_PLANNED,
            active: false,
            capabilities: ["calculate_routes"],
          }),
        ],
        uncovered_capabilities: [],
        capabilities: [row({ capability: "calculate_routes", covered: true })],
      }),
    );
    expect(hints.planned).toEqual([]);
  });

  it("offers a planned source for the parts a covered capability is missing", () => {
    // The case this whole distinction exists for: routing is "covered" by OSRM,
    // yet 公交/地铁 is absent, so 高德 is the answer to a real gap. Reporting it as
    // "already covered" would hide the one thing worth telling the user.
    const hints = unlockHints(
      catalog({
        providers: [
          provider({ id: "osrm", active: true, capabilities: ["calculate_routes"] }),
          provider({
            id: "amap",
            label: "高德地图（AMap）",
            status: STATUS_PLANNED,
            active: false,
            capabilities: ["calculate_routes"],
            aspects: { calculate_routes: ["driving", "transit"] },
          }),
        ],
        uncovered_capabilities: [],
        uncovered_aspects: [
          {
            capability: "calculate_routes",
            capability_label: "路线规划",
            aspect: "transit",
            label: "公交/地铁",
          },
        ],
        capabilities: routingRows(),
      }),
    );

    expect(hints.planned).toEqual([
      { providerId: "amap", label: "高德地图（AMap）", unlocks: ["路线规划：公交/地铁"] },
    ]);
  });

  it("does not offer a source that would only duplicate what works", () => {
    // 高德 serving driving too is not a gap; only the unserved parts count.
    const hints = unlockHints(
      catalog({
        providers: [
          provider({ id: "osrm", active: true, capabilities: ["calculate_routes"] }),
          provider({
            id: "amap",
            status: STATUS_PLANNED,
            active: false,
            capabilities: ["calculate_routes"],
            aspects: { calculate_routes: ["driving"] },
          }),
        ],
        uncovered_capabilities: [],
        capabilities: routingRows(),
      }),
    );
    expect(hints.planned).toEqual([]);
  });

  it("ignores sources that are already contributing", () => {
    const hints = unlockHints(
      catalog({
        providers: [provider({ active: true, capabilities: ["search_weather"] })],
        uncovered_capabilities: ["search_weather"],
        capabilities: [row({ capability: "search_weather", covered: false })],
      }),
    );
    expect(hints.configurable).toEqual([]);
  });
});

describe("model reporting", () => {
  it("reports a working model", () => {
    const entry = provider({ id: "openai-compatible", kind: "ai", active: true });
    expect(hasModel(catalog({ providers: [entry] }))).toBe(true);
    expect(modelEntry(catalog({ providers: [entry] }))?.id).toBe("openai-compatible");
  });

  it("reports no model without treating it as a data gap", () => {
    const entry = provider({
      id: "openai-compatible",
      kind: "ai",
      active: false,
      status: STATUS_NEEDS_CREDENTIALS,
      capabilities: [],
    });
    expect(hasModel(catalog({ providers: [entry] }))).toBe(false);
    // A model is not a capability, so it must not appear as one.
    expect(unlockHints(catalog({ providers: [entry] })).configurable).toEqual([]);
  });
});

describe("vaultNotes", () => {
  it("says a database leak still yields ciphertext in env mode", () => {
    const notes = vaultNotes(vault({ key_source: "env" }));
    expect(notes).toHaveLength(1);
    expect(notes[0].tone).toBe("ok");
    expect(notes[0].text).toContain("数据库泄露");
  });

  it("names the weaker file mode and the way out of it", () => {
    // Overstating this would be the easiest lie in the whole UI: the key really
    // does sit beside the data it protects.
    const notes = vaultNotes(vault({ key_source: "file" }));
    expect(notes[0].tone).toBe("info");
    expect(notes[0].text).toContain("能读取该文件的人可以解开全部凭据");
    expect(notes[0].text).toContain("CREDENTIAL_ENCRYPTION_KEY");
  });

  it("admits when the key file cannot be made owner-only", () => {
    const notes = vaultNotes(vault({ key_source: "file", owner_only_permissions: false }));
    expect(notes.some((note) => note.tone === "warn")).toBe(true);
  });

  it("reports a locked vault with the server's reason and a way forward", () => {
    const notes = vaultNotes(
      vault({ enabled: false, locked_reason: "MasterKeyError: too short" }),
    );
    expect(notes).toHaveLength(1);
    expect(notes[0].tone).toBe("warn");
    expect(notes[0].text).toContain("MasterKeyError: too short");
  });

  it("does not claim a key source it cannot identify", () => {
    const notes = vaultNotes(vault({ key_source: "unavailable" }));
    expect(notes[0].tone).toBe("warn");
  });
});

describe("credential calls", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    setApiKey(VALID_KEY);
    fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ stored: true }),
    } as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("writes a credential with PUT to the declared path", async () => {
    await saveCredential("openai-compatible", "ai_api_key", "sk-value");

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/v1/settings/credentials/openai-compatible/ai_api_key");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body)).toEqual({ value: "sk-value" });
  });

  it("escapes an id so a crafted value cannot reach another path", async () => {
    await saveCredential("a/../b", "k", "v");
    const [url] = fetchMock.mock.calls[0];
    expect(String(url)).not.toContain("/../");
    expect(String(url)).toContain("a%2F..%2Fb");
  });

  it("deletes a credential with DELETE", async () => {
    await deleteCredential("openai-compatible", "ai_api_key");
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/v1/settings/credentials/openai-compatible/ai_api_key");
    expect(init.method).toBe("DELETE");
  });
});
