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
 */

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
    },
  ],
  registered: ["openstreetmap", "osrm"],
  // Room rates are genuinely uncovered: no keyless source can produce one, and no
  // catalogue entry claims to. The fixture mirrors the real API so the render path is
  // exercised rather than assumed.
  uncovered_capabilities: ["search_room_offers"],
  capability_gaps: [
    {
      capability: "search_room_offers",
      label: "房型与报价",
      note: "房型与实时房价只存在于商业酒店渠道（携程 / Booking 的开放接口），本工具尚未接入任何一家。因此预算里的住宿部分是估算额度。",
    },
  ],
  uncovered_aspects: [
    {
      capability: "calculate_routes",
      capability_label: "路线规划",
      aspect: "transit",
      label: "公交/地铁",
    },
    {
      capability: "calculate_routes",
      capability_label: "路线规划",
      aspect: "walking",
      label: "步行",
    },
    {
      capability: "calculate_routes",
      capability_label: "路线规划",
      aspect: "cycling",
      label: "骑行",
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
    // Two of three are complete; routing is not, and the headline must not claim
    // otherwise.
    expect(screen.getByText("2 / 3 完整可用")).toBeInTheDocument();
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
    expect(scope.getByText("步行（缺失）")).toBeInTheDocument();
    // The provider that does serve driving is still credited with it.
    expect(scope.getByText("osrm")).toBeInTheDocument();
  });

  it("offers a planned source as the fix for a missing part, not as pointless", async () => {
    // 高德 must not read as "already covered" — 公交/地铁 is exactly what it adds.
    stubFetch(CATALOG);
    render(<ProviderCatalog apiUrl="http://api.test" />);

    // The unlock hint names the specific missing parts rather than the whole
    // capability, since driving already works.
    expect(
      await screen.findByText(/接入后才会提供路线规划：公交\/地铁、步行、骑行/),
    ).toBeInTheDocument();
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
    // card must show it as the fix rather than as a capability nothing serves.
    expect(scope.getByText("路线规划（可补：公交/地铁、步行、骑行）")).toBeInTheDocument();
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
