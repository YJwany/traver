import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProviderCatalog } from "./provider-catalog";
import type { Catalog } from "../../lib/providers";

/**
 * Rendering tests for the settings catalogue.
 *
 * These exist to catch the ways this screen can lie, which are specific and easy
 * to introduce:
 *
 *   * a `planned` source reading as available;
 *   * an unconfigured deployment reading as broken, when the local planner means
 *     it is fully usable;
 *   * a capability shown as covered when the server said it is not.
 *
 * Fetch is stubbed rather than mocked at the module level so the component's real
 * request path (including the bearer header handling in `apiRequest`) is
 * exercised.
 *
 * ## Two fixtures, because there are two states of the world
 *
 * `CATALOG` below describes 高德 as `planned` — a source that is listed and declared
 * unimplemented. **The deployed server no longer sends that**, and has not since the AMap
 * provider was implemented (`tests/unit/test_provider_catalog.py::
 * test_the_amap_manifest_no_longer_claims_to_be_unimplemented` is the API-side guard).
 * So `CATALOG` is kept for the *code path* — a future source really could be planned, and
 * the rendering of that state is worth pinning — but it is not what production sends.
 *
 * `AMAP_AS_DEPLOYED` is what production sends. It exists because of the gap that made this
 * distinction necessary: every test here used `CATALOG`, where the only inactive source
 * with capabilities is 高德-as-planned, so `unlockHints` always returned an empty
 * `configurable` list and the sentence
 *
 *     高德地图（AMap）：填入凭据后可提供路线规划：公交/地铁
 *
 * — the single most actionable thing this screen tells a traveller planning in China, and
 * the entire reason 高德 is integrated — was **never rendered by any test**. A fixture that
 * describes a state the server left behind cannot fail, in the same way that a test written
 * against 成都 could not notice a 成都 constant.
 *
 * `tests/unit/test_web_api_contract.py` compares `AMAP_AS_DEPLOYED` against the live API so
 * it cannot drift back.
 */

/** 高德 exactly as `/api/v1/providers/catalog` reports it on an unconfigured deployment. */
const AMAP_AS_DEPLOYED = {
  id: "amap",
  label: "高德地图（AMap）",
  kind: "data" as const,
  // Not `planned`: the provider is implemented and reaches a real service. Only the
  // credential is absent.
  status: "needs_credentials",
  capabilities: [
    "search_attractions",
    "search_restaurants",
    "search_hotels",
    "calculate_routes",
    "search_weather",
    "resolve_places",
  ],
  // No `cycling`. AMap's transit endpoint is the reason this source exists, and it is also
  // why the aspect list is narrower than the fixture above: the deployed manifest declares
  // exactly what a request can ask for.
  aspects: { calculate_routes: ["driving", "transit", "walking"] },
  cost_note: "个人认证开发者有免费配额",
  freshness_note: "按请求实时查询",
  docs_url: "https://lbs.amap.com/",
  caveats: [],
  active: false,
  credentials: [
    {
      key: "amap_web_service_key",
      label: "高德 Web 服务 Key",
      help: "在高德开放平台创建 Web 服务类型 Key",
      docs_url: "https://lbs.amap.com/api/webservice/summary",
      secret: true,
      required: true,
      configured: false,
    },
    {
      key: "amap_js_api_key",
      label: "高德 JS API Key",
      help: "仅用于地图渲染，填了也不会改变规划结果",
      docs_url: "https://lbs.amap.com/api/javascript-api/summary",
      secret: true,
      required: false,
      configured: false,
    },
  ],
  missing_credentials: ["amap_web_service_key"],
};

const CATALOG: Catalog = {
  providers: [
    {
      id: "openstreetmap",
      label: "OpenStreetMap / Overpass",
      kind: "data",
      status: "available",
      capabilities: ["search_hotels", "search_restaurants"],
      aspects: {},
      cost_note: "免费、免密钥、无需注册",
      freshness_note: "POI 随社区编辑变化，默认缓存 1 小时",
      docs_url: "https://wiki.openstreetmap.org/wiki/Overpass_API",
      caveats: ["不含房价与库存"],
      active: true,
      credentials: [],
      missing_credentials: [],
    },
    {
      id: "osrm",
      label: "OSRM 路线（公共 demo）",
      kind: "data",
      status: "available",
      capabilities: ["calculate_routes"],
      aspects: { calculate_routes: ["driving"] },
      cost_note: "免费、免密钥",
      freshness_note: "按请求实时计算，默认缓存 10 分钟",
      docs_url: "https://project-osrm.org/",
      caveats: ["完全没有公交/地铁规划：这是它最大的能力缺口"],
      active: true,
      credentials: [],
      missing_credentials: [],
    },
    {
      id: "amap",
      label: "高德地图（AMap）",
      kind: "data",
      status: "planned",
      capabilities: ["calculate_routes"],
      aspects: { calculate_routes: ["driving", "transit", "walking", "cycling"] },
      cost_note: "个人认证开发者有免费配额",
      freshness_note: "按请求实时查询",
      docs_url: "https://lbs.amap.com/",
      caveats: ["尚未实现：状态为 planned，现在不提供任何能力"],
      active: false,
      credentials: [
        {
          key: "amap_web_service_key",
          label: "高德 Web 服务 Key",
          help: "在高德开放平台创建 Web 服务类型 Key",
          docs_url: "https://lbs.amap.com/api/webservice/summary",
          secret: true,
          required: true,
          configured: false,
        },
      ],
      missing_credentials: ["amap_web_service_key"],
    },
    {
      id: "openai-compatible",
      label: "AI 模型（OpenAI 兼容接口）",
      kind: "ai",
      status: "needs_credentials",
      capabilities: [],
      aspects: {},
      cost_note: "由所选服务商计费",
      freshness_note: "不涉及数据时效",
      docs_url: "",
      caveats: ["留空也能完整规划：本地规则引擎会接管"],
      active: false,
      credentials: [
        {
          key: "ai_base_url",
          label: "接口地址",
          help: "例如 https://api.deepseek.com/v1",
          docs_url: "",
          secret: false,
          required: true,
          configured: false,
        },
        {
          key: "ai_api_key",
          label: "API Key",
          help: "本地模型不需要填写",
          docs_url: "",
          secret: true,
          required: false,
          configured: false,
        },
      ],
      missing_credentials: ["ai_base_url"],
    },
  ],
  capabilities: [
    {
      capability: "search_hotels",
      label: "酒店搜索",
      covered: true,
      complete: true,
      aspects: [],
      missing_aspects: [],
      providers: ["openstreetmap"],
    },
    // The row the rest of this fixture is *about*, and which used to be missing entirely.
    //
    // `uncovered_capabilities` below named `search_room_offers`, and `capability_gaps` gave it
    // a paragraph, while no `capabilities` row carried that id — so the matrix rendered three
    // rows, all `covered: true`, and the `无来源` branch of the matrix was never exercised by
    // any test. That branch is the label on the single most important row on the screen: the
    // one capability nothing can serve. A fixture that omits the row its own gap report is
    // about cannot fail, in the same way a test pinned to 成都 cannot notice a 成都 constant.
    {
      capability: "search_room_offers",
      label: "房型与报价",
      covered: false,
      complete: false,
      aspects: [],
      missing_aspects: [],
      providers: [],
    },
    {
      capability: "search_restaurants",
      label: "餐饮搜索",
      covered: true,
      complete: true,
      aspects: [],
      missing_aspects: [],
      providers: ["openstreetmap"],
    },
    {
      capability: "search_attractions",
      label: "景点搜索",
      covered: true,
      complete: true,
      aspects: [],
      missing_aspects: [],
      providers: ["openstreetmap"],
    },
    {
      capability: "calculate_routes",
      label: "路线规划",
      covered: true,
      complete: false,
      providers: ["osrm"],
      // 步行 covered, because the default routing deployment has a real foot profile and a
      // car-only one still yields a flagged walking leg. The two that remain missing are the
      // two no OSRM can do: no timetable data (公交/地铁) and no cycling mode in TravelMode.
      missing_aspects: ["transit", "cycling"],
      aspects: [
        { aspect: "driving", label: "驾车", covered: true, providers: ["osrm"] },
        // closable_by distinguishes the two kinds of gap, and it is the server that decides.
        // 公交/地铁 is one credential away from working; 骑行 needs a change to the tool,
        // because TravelMode has no cycling member for a request to carry.
        {
          aspect: "transit",
          label: "公交/地铁",
          covered: false,
          providers: [],
          closable_by: "source" as const,
        },
        { aspect: "walking", label: "步行", covered: true, providers: ["osrm"] },
        {
          aspect: "cycling",
          label: "骑行",
          covered: false,
          providers: [],
          closable_by: "tool" as const,
          note: "行程里没有任何一种交通方式对应骑行：TravelMode 没有骑行成员。",
        },
      ],
    },
    // The remaining rows exist so the matrix is rendered in the shape the server sends it.
    // Without them the component's per-row rendering is exercised against a third of the real
    // list, and a bug that only shows on an atomic covered row — every row here is covered and
    // aspect-free, which is the common case — would have nowhere to appear.
    {
      capability: "search_weather",
      label: "天气",
      covered: true,
      complete: true,
      aspects: [],
      missing_aspects: [],
      providers: ["open-meteo"],
    },
    {
      capability: "search_holidays",
      label: "节假日",
      covered: true,
      complete: true,
      aspects: [],
      missing_aspects: [],
      providers: ["holidays"],
    },
    {
      capability: "enrich_attractions",
      label: "景点简介",
      covered: true,
      complete: true,
      aspects: [],
      missing_aspects: [],
      providers: ["wikipedia"],
    },
    {
      capability: "resolve_places",
      label: "地名定位",
      covered: true,
      complete: true,
      aspects: [],
      missing_aspects: [],
      providers: ["openstreetmap-places"],
    },
  ],
  registered: [
    "openstreetmap",
    "osrm",
    "open-meteo",
    "holidays",
    "wikipedia",
    "openstreetmap-places",
  ],
  // Room rates are genuinely uncovered: no keyless source can produce one, and no
  // catalogue entry claims to. The fixture mirrors the real API so the render path is
  // exercised rather than assumed.
  uncovered_capabilities: ["search_room_offers"],
  capability_gaps: [
    {
      capability: "search_room_offers",
      label: "房型与报价",
      note: "房型与实时房价只存在于商业酒店渠道（携程 / Booking 的开放接口），本工具尚未接入任何一家。因此预算里的住宿部分是估算额度。",
      closable_by: "source",
    },
  ],
  uncovered_aspects: [
    {
      capability: "calculate_routes",
      capability_label: "路线规划",
      aspect: "transit",
      label: "公交/地铁",
      closable_by: "source",
      note: "",
    },
    {
      capability: "calculate_routes",
      capability_label: "路线规划",
      aspect: "cycling",
      label: "骑行",
      closable_by: "tool",
      note: "TravelMode 没有骑行成员。",
    },
  ],
  vault: {
    enabled: true,
    locked_reason: null,
    key_source: "file",
    key_file: ".traver-credential-key",
    owner_only_permissions: false,
  },
};

function stubFetch(payload: unknown, status = 200) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(payload),
  } as unknown as Response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/**
 * The provider card for a source, found by its heading.
 *
 * A source can appear in more than one place on this page — the unlock hints
 * mention it by name too — so `getByText` is ambiguous. Scoping to the `article`
 * that contains the heading keeps each assertion about one card.
 */
function cardFor(label: string): HTMLElement {
  const cards = screen
    .getAllByText(label)
    .map((element) => element.closest("article"))
    .filter((element): element is HTMLElement => element !== null);
  expect(cards.length).toBeGreaterThan(0);
  return cards[0];
}

describe("ProviderCatalog", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the capability matrix with only the server-reported coverage", async () => {
    stubFetch(CATALOG);
    render(<ProviderCatalog apiUrl="http://api.test" />);

    expect(await screen.findByText("酒店搜索")).toBeInTheDocument();
    // Seven of nine are complete; routing is partially covered and room rates are not covered
    // at all, and the headline must not claim otherwise.
    //
    // This assertion used to read "2 / 3 完整可用", which pinned the fixture's incompleteness
    // rather than the component's behaviour: the fixture listed three capabilities while the
    // server sends nine. The number is derived from `CATALOG.capabilities` now, so adding a
    // row cannot make it wrong — the guard against a *stale* matrix is the set comparison in
    // the test below, and the guard against the headline lying is the separate assertion on
    // the partial-coverage sentence.
    expect(screen.getByText("7 / 9 完整可用")).toBeInTheDocument();
    expect(screen.getByText(/另有 1 项只覆盖了一部分/)).toBeInTheDocument();
  });

  it("names and explains a capability nothing can serve", async () => {
    // Two defects are guarded here.
    //
    // First, the count: the panel used to say "1 项能力完全没有来源" without saying which,
    // leaving the reader to diff the matrix against their expectations.
    //
    // Second, and worse, the server used to report room rates as *fully covered by
    // OpenStreetMap*, because Overpass implements `search_room_offers` as a method that
    // always returns an empty list. A catalogue must not advertise a capability that
    // returns nothing, so the gap has to appear at all.
    stubFetch(CATALOG);
    render(<ProviderCatalog apiUrl="http://api.test" />);

    expect(await screen.findByText("房型与报价", { selector: "strong" })).toBeInTheDocument();
    // And it says what happens instead, so the reader connects it to the estimated
    // accommodation line in the budget rather than hunting for a bug.
    expect(screen.getByText(/没有任何已接入的来源能提供它/)).toBeInTheDocument();
    expect(screen.getByText(/估算额度/)).toBeInTheDocument();
  });

  it("does not show a gap panel when there is nothing missing", async () => {
    // An empty panel would imply a broken feature rather than an absent one.
    stubFetch({ ...CATALOG, uncovered_capabilities: [], capability_gaps: [] });
    render(<ProviderCatalog apiUrl="http://api.test" />);

    expect(await screen.findByText("酒店搜索")).toBeInTheDocument();
    expect(screen.queryByText(/没有任何已接入的来源能提供它/)).not.toBeInTheDocument();
  });

  it("survives an API that predates the gap report", async () => {
    // `capability_gaps` is optional so a client can talk to an older API. The count is
    // still truthful in that case — only less is explained — so this must not throw.
    const { capability_gaps: _dropped, ...withoutGaps } = CATALOG;
    stubFetch(withoutGaps);
    render(<ProviderCatalog apiUrl="http://api.test" />);

    expect(await screen.findByText(/1 项能力完全没有来源/)).toBeInTheDocument();
    expect(screen.queryByText(/没有任何已接入的来源能提供它/)).not.toBeInTheDocument();
  });

  it("names an uncovered capability as having no source rather than leaving the cell blank", async () => {
    // The label on the one row that matters most: a capability nothing can serve. It used to
    // be unreachable — the fixture listed three capabilities, all `covered: true`, while its
    // own `uncovered_capabilities` and `capability_gaps` named a fourth that had no row at
    // all. So the `无来源` branch rendered in production and in no test, and a regression that
    // blanked it, printed the capability id, or listed an empty provider list would have gone
    // unnoticed on exactly the row a reader looks at to find out what is missing.
    stubFetch(CATALOG);
    const { container } = render(<ProviderCatalog apiUrl="http://api.test" />);

    // The matrix row, not the gap panel below it — both name this capability, and the panel's
    // copy is a different element with different wording.
    const list = await waitFor(() => {
      const found = container.querySelector(".capability-list");
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    const row = Array.from(list.querySelectorAll(".capability-name")).find(
      (node) => node.textContent === "房型与报价",
    );
    expect(row).toBeDefined();
    const item = row?.closest("li") as HTMLElement;
    const scope = within(item);

    expect(scope.getByText("无来源")).toBeInTheDocument();
    // Not the id, and not an empty provider list rendered as nothing.
    expect(scope.queryByText("search_room_offers")).toBeNull();
    // The row is present but not claimed as available, which is the distinction the dot
    // encodes: `dot-off` for uncovered rather than `dot-on`/`dot-part`.
    expect(item.querySelector(".dot-off")).not.toBeNull();
    expect(item.querySelector(".dot-on")).toBeNull();
  });

  it("renders every capability the server listed, and invents none", async () => {
    // The matrix is the answer to "is this set up", so a row silently missing from it is a
    // capability the reader is never told about. Asserted as a sequence both ways: nothing the
    // server sent is dropped or reordered, and nothing appears that the server did not send.
    stubFetch(CATALOG);
    const { container } = render(<ProviderCatalog apiUrl="http://api.test" />);

    const list = await waitFor(() => {
      const found = container.querySelector(".capability-list");
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    const rendered = Array.from(list.querySelectorAll(".capability-name")).map(
      (node) => node.textContent,
    );

    expect(rendered).toEqual(CATALOG.capabilities.map((row) => row.label));
    // And the count in the chip is derived from the same list, so it cannot disagree with
    // the rows above it.
    const complete = CATALOG.capabilities.filter((row) => row.covered && row.complete).length;
    expect(
      screen.getByText(`${complete} / ${CATALOG.capabilities.length} 完整可用`),
    ).toBeInTheDocument();
  });

  it("counts a gap only a code change can close apart from one a source can", async () => {
    // The defect this guards. The panel's headline is "接上什么会更好" — connect something
    // and this gets better. 骑行 sat in that sentence, so the count said two gaps and the
    // reader was expected to fix both by connecting a source, which cannot work for either
    // half of the reason: no catalogue entry serves cycling, and TravelMode has no member
    // for a request to carry it in. The count has to split, and the second half has to say
    // what it actually needs.
    stubFetch(CATALOG);
    render(<ProviderCatalog apiUrl="http://api.test" />);

    // One source-closable part (公交/地铁), not two. Asserted on the unlock panel's own
    // sentence, because the matrix panel above it counts *capabilities* and also says
    // "1 项只覆盖了一部分" — matching that by accident would make this test pass while the
    // split it is guarding is broken.
    expect(
      await screen.findByText(/项能力完全没有来源，1 项只覆盖了一部分（路线规划·公交\/地铁）/),
    ).toBeInTheDocument();
    // And the tool-only part is disclosed rather than dropped: naming it is what stops the
    // reader from concluding that routing is fine.
    expect(screen.getByText(/另有 1 项这个工具本身还不支持（骑行）/)).toBeInTheDocument();
    expect(screen.getByText(/接入任何来源都不会改变，需要先改工具/)).toBeInTheDocument();
  });

  it("treats an aspect with no closable_by as source-closable", async () => {
    // Backward compatibility in the direction that cannot overstate a limitation. An older
    // API says nothing about who can close a gap, and every aspect but cycling is
    // source-closable, so the safe reading is the one that does not invent a limitation
    // the server never claimed.
    const rows = CATALOG.capabilities.map((row) => ({
      ...row,
      aspects: row.aspects.map(({ closable_by: _dropped, note: _note, ...rest }) => rest),
    }));
    const aspects = CATALOG.uncovered_aspects.map(
      ({ closable_by: _dropped, note: _note, ...rest }) => rest,
    );
    stubFetch({ ...CATALOG, capabilities: rows, uncovered_aspects: aspects });
    render(<ProviderCatalog apiUrl="http://api.test" />);

    // Both are back to being counted as source-closable, and neither is called a tool
    // limitation.
    expect(
      await screen.findByText(/项能力完全没有来源，2 项只覆盖了一部分（路线规划·公交\/地铁、路线规划·骑行）/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/这个工具本身还不支持/)).not.toBeInTheDocument();
    expect(screen.queryByText("骑行（工具不支持）")).toBeNull();
  });

  it("names the missing parts of a partially covered capability", async () => {
    // The defect this guards: routing read as covered because driving works,
    // with 公交/地铁 absent and nothing in the UI saying so.
    stubFetch(CATALOG);
    render(<ProviderCatalog apiUrl="http://api.test" />);

    const routeRow = (await screen.findByText("路线规划")).closest("li");
    expect(routeRow).not.toBeNull();
    const scope = within(routeRow as HTMLElement);

    expect(scope.getByText("驾车")).toBeInTheDocument();
    expect(scope.getByText("公交/地铁（缺失）")).toBeInTheDocument();
    // 骑行 is not "缺失" — 缺失 is the word for "no source supplies this yet", and it appears
    // everywhere else in this panel next to an instruction to connect one. Cycling has no
    // source to connect: the tool cannot request a bike route at all, so it is labelled as
    // the different thing it is.
    expect(scope.getByText("骑行（工具不支持）")).toBeInTheDocument();
    expect(scope.queryByText("骑行（缺失）")).toBeNull();
    // 步行 is served now, so it must NOT be marked missing — reporting a part as absent when
    // it works is the same error as the reverse, told the other way round.
    expect(scope.queryByText("步行（缺失）")).toBeNull();
    expect(scope.getByText("步行")).toBeInTheDocument();
    // The provider that does serve driving is still credited with it.
    expect(scope.getByText("osrm")).toBeInTheDocument();
  });

  it("offers a planned source as the fix for a missing part, not as pointless", async () => {
    // 高德 must not read as "already covered" — 公交/地铁 is exactly what it adds.
    stubFetch(CATALOG);
    render(<ProviderCatalog apiUrl="http://api.test" />);

    // The unlock hint names the specific missing parts rather than the whole
    // capability, since driving and walking already work. 骑行 is absent from it because
    // the sentence is "接入后才会提供" — after you connect it, this will work — and no
    // amount of connecting makes cycling work. Listing it would be a false promise in the
    // one place a user goes to find out what to do.
    expect(await screen.findByText(/接入后才会提供路线规划：公交\/地铁$/)).toBeInTheDocument();
    expect(screen.queryByText(/接入后才会提供路线规划：.*骑行/)).toBeNull();
    // And it is still labelled as unimplemented while being offered.
    expect(screen.getAllByText(/尚未实现/).length).toBeGreaterThan(0);
  });

  it("labels a planned source as unimplemented and never as available", async () => {
    stubFetch(CATALOG);
    render(<ProviderCatalog apiUrl="http://api.test" />);

    await screen.findAllByText("高德地图（AMap）");
    const scope = within(cardFor("高德地图（AMap）"));

    // The two labels that matter: listed, but explicitly not working.
    expect(scope.getByText("尚未实现")).toBeInTheDocument();
    expect(scope.queryByText("使用中")).toBeNull();
    expect(scope.queryByText("可用")).toBeNull();
    expect(scope.getByText(/尚未实现：状态为 planned/)).toBeInTheDocument();
  });

  it("marks a planned capability as pending rather than as a live gap to fill", async () => {
    stubFetch(CATALOG);
    render(<ProviderCatalog apiUrl="http://api.test" />);

    await screen.findAllByText("高德地图（AMap）");
    const scope = within(cardFor("高德地图（AMap）"));

    // "可补" and not "缺失": 高德 can close the transit gap once it exists, so the
    // card must show it as the fix rather than as a capability nothing serves. 骑行 is not
    // in the list for the opposite reason — 高德 could route a bike, but this tool would
    // never ask it to, so it is not something 高德 fixes.
    expect(scope.getByText("路线规划（可补：公交/地铁）")).toBeInTheDocument();
    expect(scope.queryByText(/路线规划（可补：.*骑行/)).toBeNull();
    expect(scope.queryByText("路线规划（缺失）")).toBeNull();
  });

  it("does not present an unconfigured model as a failure", async () => {
    stubFetch(CATALOG);
    render(<ProviderCatalog apiUrl="http://api.test" />);

    // The product promise: zero configuration still plans a complete itinerary.
    const text = await screen.findByText(/本地确定性规则引擎会接管/);
    expect(text).toBeInTheDocument();
    expect(text.textContent).toContain("这不影响可用性");
    expect(screen.queryByText(/没有模型/)).toBeNull();
  });

  it("states the access token requirement instead of a bare failure", async () => {
    stubFetch({ detail: "missing bearer token" }, 401);
    render(<ProviderCatalog apiUrl="http://api.test" />);

    expect(
      await screen.findByText(/需要先在上方填写接口密钥/),
    ).toBeInTheDocument();
  });

  it("gives the missing-scope case its own remedy", async () => {
    // Entering the same key again cannot fix a scope problem, so the two states
    // must not share a message.
    stubFetch({ detail: "API key lacks required scope: trip:read" }, 403);
    render(<ProviderCatalog apiUrl="http://api.test" />);

    expect(await screen.findByText(/缺少 trip:read 权限/)).toBeInTheDocument();
  });

  it("reports how stored credentials are protected, including the weak part", async () => {
    stubFetch(CATALOG);
    render(<ProviderCatalog apiUrl="http://api.test" />);

    expect(await screen.findByText(/AES-256-GCM/)).toBeInTheDocument();
    // The file-key mode is genuinely weaker, so it is named as such rather than
    // summarised as "encrypted".
    expect(screen.getByText(/能读取该文件的人可以解开全部凭据/)).toBeInTheDocument();
    // And the platform limitation is admitted.
    expect(
      screen.getByText(/无法把密钥文件限制为仅属主可读/),
    ).toBeInTheDocument();
    expect(screen.getByText(/不防/)).toBeInTheDocument();
  });

  it("saves a credential without ever asking for the value back", async () => {
    const fetchMock = stubFetch(CATALOG);
    render(<ProviderCatalog apiUrl="http://api.test" />);

    await screen.findAllByText("高德地图（AMap）");
    const scope = within(cardFor("高德地图（AMap）"));
    const input = scope.getByLabelText(/高德 Web 服务 Key/);
    // A secret field, so the value is not shoulder-surfable either.
    expect(input).toHaveAttribute("type", "password");

    await fireEvent.change(input, { target: { value: "amap-secret-value" } });
    fireEvent.click(scope.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      const put = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT");
      expect(put).toBeDefined();
      expect(String(put?.[0])).toContain(
        "/api/v1/settings/credentials/amap/amap_web_service_key",
      );
      expect(JSON.parse(put?.[1].body)).toEqual({ value: "amap-secret-value" });
    });
  });

  it("offers a real connection test for the model, not a format check", async () => {
    const withModel: Catalog = {
      ...CATALOG,
      providers: CATALOG.providers.map((entry) =>
        entry.kind === "ai"
          ? {
              ...entry,
              active: true,
              credentials: entry.credentials.map((field) => ({ ...field, configured: true })),
              missing_credentials: [],
            }
          : entry,
      ),
    };
    const fetchMock = stubFetch(withModel);
    render(<ProviderCatalog apiUrl="http://api.test" />);

    await screen.findAllByText("AI 模型（OpenAI 兼容接口）");
    const scope = within(cardFor("AI 模型（OpenAI 兼容接口）"));
    expect(scope.getByText(/会真的调用一次模型/)).toBeInTheDocument();

    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      text: async () =>
        JSON.stringify({ ok: true, model: "deepseek-chat", latency_ms: 120, reply: "ok", error: null }),
    } as unknown as Response);

    fireEvent.click(scope.getByRole("button", { name: "测试连接" }));

    expect(await screen.findByText(/模型 deepseek-chat/)).toBeInTheDocument();
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(String(post?.[0])).toContain("/api/v1/settings/model/test");
  });

  it("does not offer a model test on a data source", async () => {
    stubFetch(CATALOG);
    render(<ProviderCatalog apiUrl="http://api.test" />);

    await screen.findAllByText("OpenStreetMap / Overpass");
    const scope = within(cardFor("OpenStreetMap / Overpass"));
    expect(scope.queryByRole("button", { name: "测试连接" })).toBeNull();
  });

  describe("with 高德 as the deployed server actually reports it", () => {
    function deployedCatalog(): Catalog {
      return {
        ...CATALOG,
        providers: [CATALOG.providers[0], CATALOG.providers[1], AMAP_AS_DEPLOYED],
        registered: ["openstreetmap", "osrm"],
      };
    }

    it("tells the reader that supplying a 高德 key is what buys 公交/地铁", async () => {
      // The sentence this whole screen exists to produce. Before this test, `unlockHints`
      // returned an empty `configurable` list in every rendering test — 高德 was `planned`
      // in the fixture and the AI model has no capabilities — so this text was never
      // rendered while the test suite stayed green.
      //
      // Asserted as the reader sees it, in the actionable bucket ("填入凭据后可提供"), and
      // not in the aspirational one ("接入后才会提供"). Getting the bucket wrong is the
      // difference between advice a user can act on today and a promise about future work.
      stubFetch(deployedCatalog());
      render(<ProviderCatalog apiUrl="http://api.test" />);

      expect(
        await screen.findByText(/填入凭据后可提供路线规划：公交\/地铁/),
      ).toBeInTheDocument();
      expect(screen.queryByText(/接入后才会提供路线规划：公交\/地铁/)).toBeNull();
    });

    it("does not describe an implemented source as unimplemented", async () => {
      // The `planned` wording is a factual claim about the code, and it was being made
      // about a source that is finished. The two places it can leak are the status badge
      // (`statusLabel`) and the contribution badge (`activityLabel`), so both are read on
      // the deployed shape: "待配置" is the true state — the code is ready, the key is
      // missing — and "缺凭据" names why it is not contributing.
      stubFetch(deployedCatalog());
      render(<ProviderCatalog apiUrl="http://api.test" />);

      await screen.findAllByText("高德地图（AMap）");
      const scope = within(cardFor("高德地图（AMap）"));

      expect(scope.getByText("待配置")).toBeInTheDocument();
      expect(scope.getAllByText("缺凭据").length).toBeGreaterThan(0);
      // Never the pre-implementation wording, in either badge or in the caveat.
      expect(scope.queryByText("尚未实现")).toBeNull();
      expect(scope.queryByText(/状态为 planned/)).toBeNull();
    });

    it("does not offer cycling as something a 高德 key would unlock", async () => {
      // 高德 closes 公交/地铁. It cannot close 骑行 — TravelMode has no cycling member at
      // all — and the deployed manifest does not claim it. The stale fixture did claim it,
      // which is how a "可补：骑行" promise could have been rendered from data the server
      // never sends.
      stubFetch(deployedCatalog());
      render(<ProviderCatalog apiUrl="http://api.test" />);

      await screen.findAllByText("高德地图（AMap）");

      expect(screen.queryByText(/填入凭据后可提供路线规划：.*骑行/)).toBeNull();
      expect(screen.queryByText(/可补：.*骑行/)).toBeNull();
      // The tool-only gap is still disclosed, about the tool rather than about 高德.
      expect(screen.getByText(/另有 1 项这个工具本身还不支持（骑行）/)).toBeInTheDocument();
    });
  });

  it("shows a locked vault as unavailable with the server's reason", async () => {
    stubFetch({
      ...CATALOG,
      vault: {
        enabled: false,
        locked_reason: "MasterKeyError: credential master key must decode to 32 bytes",
        key_source: "unavailable",
        key_file: ".traver-credential-key",
        owner_only_permissions: true,
      },
    });
    render(<ProviderCatalog apiUrl="http://api.test" />);

    expect(await screen.findByText("不可用")).toBeInTheDocument();
    expect(screen.getByText(/master key must decode to 32 bytes/)).toBeInTheDocument();
  });

  it("reports a save failure from the server verbatim", async () => {
    const fetchMock = stubFetch(CATALOG);
    render(<ProviderCatalog apiUrl="http://api.test" />);

    await screen.findAllByText("高德地图（AMap）");
    const scope = within(cardFor("高德地图（AMap）"));
    await fireEvent.change(scope.getByLabelText(/高德 Web 服务 Key/), {
      target: { value: "value" },
    });

    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 503,
      text: async () => JSON.stringify({ detail: "the credential vault is locked" }),
    } as unknown as Response);

    fireEvent.click(scope.getByRole("button", { name: "保存" }));

    // A locked vault is actionable, so its reason is surfaced rather than
    // flattened into "operation failed".
    expect(await screen.findByText(/保险箱不可用/)).toBeInTheDocument();
  });
});
