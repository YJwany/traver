"""Provider manifests: what each pluggable source can do, and what it needs.

Traver ships no data of its own and binds to no model. Everything comes from a
source the operator plugs in, so "which sources exist, what do they unlock, and
what is still missing" has to be answerable by the program rather than by
reading the source tree.

That is what a manifest is for. One manifest per source declares:

* which capabilities it serves, named after the ``ProviderRegistry`` method it
  implements, so the matrix cannot drift from what the code actually routes;
* which credentials it needs, so the settings page can be generated instead of
  hand-written;
* whether it is available now, needs credentials, or is not built yet.

The last field exists to keep this document honest. Listing a source here is not
a claim that it works: ``status`` is the claim, and a source that has only been
designed is marked ``planned`` and contributes nothing to the capability matrix.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from travel_api.providers.amap import AMAP_PROVIDER_NAMES
from travel_api.providers.base import CAPABILITY_BY_METHOD

#: Human labels for the registry methods, used by the settings UI. Kept beside
#: the manifests rather than in the Web app so adding a capability does not
#: require touching two repositories.
CAPABILITY_LABELS: dict[str, str] = {
    "search_hotels": "酒店搜索",
    "search_room_offers": "房型与报价",
    "search_restaurants": "餐饮搜索",
    "search_attractions": "景点搜索",
    "calculate_routes": "路线规划",
    "search_weather": "天气",
    "search_holidays": "节假日",
    "enrich_attractions": "景点简介",
}

#: The parts a capability decomposes into, for capabilities where one provider
#: serving *something* is not the same as the capability being served.
#:
#: Routing is the case that forced this. OSRM answers ``calculate_routes``, so a
#: flat capability model reports routing as covered — while OSRM's public demo
#: offers driving only, with no transit at all. The matrix then says "everything
#: is covered" and a source that adds 公交/地铁 looks pointless, which is both
#: false and the opposite of the truth for a domestic itinerary. A capability
#: listed here is only "complete" when every aspect has a provider.
#:
#: A capability with no entry is atomic and behaves exactly as before.
CAPABILITY_ASPECTS: dict[str, dict[str, str]] = {
    "calculate_routes": {
        "driving": "驾车",
        "transit": "公交/地铁",
        "walking": "步行",
        "cycling": "骑行",
    },
}

#: How a routing aspect maps onto the ``TravelMode`` a request would carry.
#:
#: This mapping did not exist before, and its absence is the whole reason the 公交/地铁
#: defect survived: the aspect vocabulary ("driving", "walking", "transit") and the mode
#: vocabulary ("drive", "walk", "transit") are two names for the same thing, and nothing in
#: the code said which aspect corresponded to which mode. So when ``mode_for_pace`` could
#: only ever return ``WALK`` or ``DRIVE``, nothing noticed that ``transit`` had no producer
#: — the two lists were never compared.
#:
#: Writing the correspondence down makes the comparison possible, and
#: ``scripts/probe_reachable_aspects.py`` performs it. An aspect absent from this mapping
#: must be declared in ``ASPECTS_NEEDING_TOOL_SUPPORT``, or it is an aspect no request can
#: name.
ASPECT_TRAVEL_MODES: dict[str, str] = {
    "driving": "drive",
    "walking": "walk",
    "transit": "transit",
    # No "cycling": TravelMode has no such member, which is what puts it in
    # ASPECTS_NEEDING_TOOL_SUPPORT below rather than in this table.
}

#: Aspects that **no configuration can ever activate**, because the tool itself has no way
#: to ask for them.
#:
#: This distinction is the difference between two pieces of advice, and only one of them
#: helps:
#:
#:   * "no source serves this yet, supply one" — actionable today, and the capability
#:     matrix already says which source and which credential;
#:   * "the tool cannot express this at all" — not actionable, and telling someone to
#:     connect a source would send them looking for something that does not exist.
#:
#: Cycling is the second kind. ``TravelMode`` has no cycling member, so no itinerary can
#: contain a bike leg and no routing request is ever made for one. FOSSGIS *does* publish a
#: real ``bike`` profile, which is exactly what makes the mistake tempting: the data exists,
#: so the gap looks like a supply problem. It is a demand problem — the matrix answers "can
#: this tool plan it", not "could a server route it".
ASPECTS_NEEDING_TOOL_SUPPORT: dict[str, str] = {
    "cycling": (
        "行程里没有任何一种交通方式对应骑行：TravelMode 没有骑行成员，规划器也不会请求它，"
        "所以接入任何来源都不会出现骑行路线。要支持骑行需要先改这个工具本身，"
        "而不是再填一个 Key。"
    ),
}

#: How a gap can be closed, so a client never has to guess.
#:
#: ``source`` — a credential or a new source would do it. ``tool`` — only a change to this
#: program would. Rendering the two identically is what turns a roadmap item into an
#: instruction the user cannot follow.
CLOSABLE_BY_SOURCE = "source"
CLOSABLE_BY_TOOL = "tool"

#: Canonical capability order, so the matrix renders identically every time.
CAPABILITY_ORDER: tuple[str, ...] = tuple(CAPABILITY_LABELS)

# -- status values ---------------------------------------------------------

#: Implemented and usable right now with no credentials at all.
STATUS_AVAILABLE = "available"
#: Implemented, but inert until the operator supplies credentials.
STATUS_NEEDS_CREDENTIALS = "needs_credentials"
#: Declared in the plan, not written yet. Contributes no capabilities.
STATUS_PLANNED = "planned"


@dataclass(frozen=True)
class CredentialField:
    """One credential a source needs.

    ``key`` is the stable identifier used as the storage key; it is deliberately
    not the environment variable name, because the same logical credential can
    arrive from a ``.env`` file or from the settings page and must map to one
    record either way.
    """

    key: str
    label: str
    help: str = ""
    docs_url: str = ""
    #: Whether the value is a secret. Secrets are masked on read and encrypted at
    #: rest; a non-secret (a base URL, a model name) is stored in clear text
    #: because masking it would only make the UI harder to use.
    secret: bool = True
    required: bool = True


@dataclass(frozen=True)
class ProviderManifest:
    """A pluggable source, described for both the matrix and the settings UI."""

    id: str
    label: str
    #: ``data`` fetches facts, ``ai`` is a model, ``content`` ingests text.
    kind: str
    status: str
    capabilities: tuple[str, ...] = ()
    #: For capabilities that decompose into aspects (see ``CAPABILITY_ASPECTS``),
    #: which aspects this source actually serves. A capability served by nobody
    #: in full is reported with its missing parts rather than as covered, because
    #: "driving works" is not "routing works".
    #:
    #: Omitting an aspect is a claim that the source does not serve it. That
    #: matters for honesty in both directions: OSRM's demo has no real walking or
    #: cycling paths (its walking times are straight-line estimates, labelled as
    #: such downstream), so it declares driving only rather than overstating.
    aspects: tuple[tuple[str, tuple[str, ...]], ...] = ()
    credentials: tuple[CredentialField, ...] = ()
    #: Registry ``name`` values this manifest corresponds to, used to tell an
    #: active source from a merely declared one. Empty for sources that do not
    #: register under a provider name (models, ingestion endpoints).
    provider_names: tuple[str, ...] = ()
    #: For a manifest with no ``provider_names``: the credential keys that must
    #: *all* be present before it counts as active. ``main.py`` only builds the
    #: model adapter when both the base URL and the key are set, so reporting a
    #: configured model as inactive — or an unconfigured one as active — would be
    #: a plain falsehood in the settings UI.
    activation_keys: tuple[str, ...] = ()
    cost_note: str = ""
    #: How current this source's data is, in plain language. Timeliness decides
    #: whether a datum may be presented as usable for a future trip, so it is
    #: part of the manifest rather than buried in the code.
    freshness_note: str = ""
    docs_url: str = ""
    #: Free-text caveats worth surfacing before a user spends time configuring it.
    caveats: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_usable(self) -> bool:
        """Whether this manifest can contribute capabilities right now.

        ``planned`` sources cannot: announcing a capability nothing implements
        would make the matrix a wish list instead of a statement of fact.
        """
        return self.status in {STATUS_AVAILABLE, STATUS_NEEDS_CREDENTIALS}


# -- the catalogue ---------------------------------------------------------

PROVIDER_CATALOG: tuple[ProviderManifest, ...] = (
    ProviderManifest(
        id="openstreetmap",
        label="OpenStreetMap / Overpass",
        kind="data",
        status=STATUS_AVAILABLE,
        # ``search_room_offers`` is deliberately absent. It used to be listed, and the
        # effect was that the capability matrix reported 房型与报价 as fully covered by this
        # source — in a deployment where no source at all can produce a room rate. The
        # provider still implements the method (it must, to share one dataset across
        # hotels, restaurants and attractions) and still returns an empty success, which is
        # the honest answer. Implementing it was mistaken for serving it, which is exactly
        # the kind of unearned claim this catalogue exists to prevent.
        capabilities=(
            "search_hotels",
            "search_restaurants",
            "search_attractions",
        ),
        provider_names=("openstreetmap",),
        cost_note="免费、免密钥、无需注册",
        freshness_note="POI 随社区编辑变化，默认缓存 1 小时",
        docs_url="https://wiki.openstreetmap.org/wiki/Overpass_API",
        caveats=(
            "不含房价与库存：无商业报价时预算按估算额度呈现并标注为估算",
            "国内省会级城市覆盖充足，县级与古镇类目的地依赖当地测绘贡献",
            "同一目的地只发一次合并查询，按类别分别限额",
        ),
    ),
    ProviderManifest(
        id="osrm",
        label="OSRM 路线（默认 FOSSGIS）",
        kind="data",
        status=STATUS_AVAILABLE,
        capabilities=("calculate_routes",),
        provider_names=("osrm",),
        cost_note="免费、免密钥",
        freshness_note="按请求实时计算；OSM 路网图约每两天重建一次，默认缓存 10 分钟",
        docs_url="https://routing.openstreetmap.de/about.html",
        # 驾车与步行。步行在 FOSSGIS 上是**实测**（真实 foot profile，4.5 km/h 量级，
        # 与驾车路线距离不同即证据）；在只有驾车图的部署上退化为「实测路网距离 + 按步行
        # 速度推算时长」，并把该腿标注为估算。两条路都能给出可用结果，所以步行算已覆盖；
        # 具体是实测还是推算由每条腿的 is_estimate 与 request_context 说明。
        #
        # 公交/地铁与骑行不在这里，provider 的 UNSERVED_ASPECTS 也会独立地给出同样的结论。
        aspects=(("calculate_routes", ("driving", "walking")),),
        caveats=(
            "完全没有公交/地铁规划：OSRM 不含班次数据，这是它最大的能力缺口",
            "默认指向 FOSSGIS（有真实步行 profile）；公共 demo 只有驾车，步行时长只能推算",
            "若把 osrm_base_url 指向自建服务，请用 osrm_honoured_profiles 声明它支持的 profile，"
            "否则按「只有驾车」处理",
        ),
    ),
    ProviderManifest(
        id="open-meteo",
        label="Open-Meteo 天气与空气质量",
        kind="data",
        status=STATUS_AVAILABLE,
        capabilities=("search_weather",),
        provider_names=("open-meteo",),
        cost_note="免费、免密钥",
        freshness_note="16 天预报，空气质量取逐小时峰值；默认缓存 15 分钟",
        docs_url="https://open-meteo.com/",
        caveats=("预报值而非实测，UI 必须标明这一点",),
    ),
    ProviderManifest(
        id="holidays",
        label="公共节假日（holidays 库）",
        kind="data",
        status=STATUS_AVAILABLE,
        capabilities=("search_holidays",),
        provider_names=("holidays",),
        cost_note="MIT 许可，本地数据，无网络请求",
        freshness_note="随库版本更新，年度发布",
        docs_url="https://pypi.org/project/holidays/",
    ),
    ProviderManifest(
        id="wikipedia",
        label="维基百科 / Wikidata 景点简介",
        kind="content",
        status=STATUS_AVAILABLE,
        capabilities=("enrich_attractions",),
        provider_names=("wikipedia",),
        cost_note="免费、免密钥（CC BY-SA 4.0，需署名）",
        freshness_note="条目文本变化缓慢，默认缓存 7 天",
        docs_url="https://www.wikidata.org/",
        caveats=("只跟随 OSM 显式 wikipedia/wikidata 标签，绝不按名称猜测",),
    ),
    ProviderManifest(
        id="amap",
        label="高德地图（AMap）",
        kind="data",
        status=STATUS_NEEDS_CREDENTIALS,
        capabilities=(
            "search_attractions",
            "search_restaurants",
            "search_hotels",
            "calculate_routes",
            "search_weather",
        ),
        credentials=(
            CredentialField(
                key="amap_web_service_key",
                label="高德 Web 服务 Key",
                help="在高德开放平台创建「Web 服务」类型 Key；用于 POI、地理编码、路线与天气",
                docs_url="https://lbs.amap.com/api/webservice/summary",
            ),
            CredentialField(
                key="amap_js_api_key",
                label="高德 Web端(JS API) Key",
                help="用于前端地图渲染；与 Web 服务 Key 是两种不同类型，不能混用",
                docs_url="https://lbs.amap.com/api/javascript-api-v2/summary",
                required=False,
            ),
        ),
        provider_names=AMAP_PROVIDER_NAMES,
        #: Only three aspects, not four. ``TravelMode`` has no cycling member and
        #: ``mode_for_pace`` cannot return one, so nothing can ever request a cycling
        #: route: claiming it would put a capability in the matrix that no
        #: itinerary can actually exercise.
        aspects=(("calculate_routes", ("driving", "transit", "walking")),),
        cost_note="个人认证开发者非商业用途每月有免费配额，超出后按量计费",
        freshness_note="按请求实时查询并如实标注抓取时间；不缓存，这既是时效性要求也是服务条款要求",
        docs_url="https://lbs.amap.com/",
        caveats=(
            "需要自己的 Web 服务 Key；未配置时不影响免费数据源出攻略",
            "公交/地铁规划是 OSRM 完全没有的能力，这是接入它的主要理由",
            "服务条款禁止存储或缓存其结果，因此本程序对它不做任何缓存，每次规划都是实时请求",
            "个人免费配额自注册认证之日起计一年，之后需付费",
        ),
    ),
    ProviderManifest(
        id="user-content",
        label="你自己提供的内容（小红书 / 抖音 / 美团 / 携程 / 同程等）",
        kind="content",
        status=STATUS_AVAILABLE,
        capabilities=(),
        provider_names=(),
        # No ``activation_keys``: there is nothing to configure. The path works the
        # moment the process runs, which is what `_is_active` falls through to.
        cost_note="免费。由你自己的浏览器读取你已登录的页面，Traver 不抓取也不代你登录",
        freshness_note="按内容自身的发布时间判断；页面没写时间时如实标注为「发布时间未知」，不当作最新内容",
        docs_url="",
        caveats=(
            "这些平台都没有公开 API。Traver 不会抓取它们，也不保存你的登录态",
            "方式是用浏览器书签（小工具）在你自己的浏览器里读取当前页面，或直接把正文粘贴进来",
            "提取是规则式的：价格、时长、开放/闭馆时间、提示、警告、地名，每条都附上原文出处",
            "提取出来的是「某人说的」，不是测量值：它只以估算和提示的形式进入行程，"
            "绝不会覆盖高德的实测路线或报价",
            "地名只有在地理编码成功后才能进入路线；没有坐标时只作为「文中提到的地点」列出",
        ),
    ),
    ProviderManifest(
        id="openai-compatible",
        label="AI 模型（OpenAI 兼容接口）",
        kind="ai",
        status=STATUS_NEEDS_CREDENTIALS,
        credentials=(
            CredentialField(
                key="ai_base_url",
                label="接口地址",
                help="例如 https://api.deepseek.com/v1 或本地 Ollama 的 http://127.0.0.1:11434/v1",
                secret=False,
            ),
            CredentialField(
                key="ai_model",
                label="模型名称",
                help="例如 deepseek-chat、gpt-4o-mini、qwen-plus；留空使用默认 gpt-4o-mini",
                secret=False,
                # Not required: Settings.ai_model has a default, so declaring this
                # mandatory would have the UI report a missing model on a working
                # setup.
                required=False,
            ),
            CredentialField(
                key="ai_api_key",
                label="API Key",
                help="本地模型（Ollama / LM Studio 等）不需要填写；不填且无接口地址时停用模型，回落为本地确定性规则规划",
                required=False,
            ),
        ),
        # Only the base URL gates activation. A keyless local server is a valid
        # and common choice — Ollama is the usual way to run a model yourself — so
        # requiring a key here would have reported a working local model as
        # inactive, and main.py would never have built its adapter.
        activation_keys=("ai_base_url",),
        cost_note="由所选服务商计费，Traver 不代收",
        freshness_note="不涉及数据时效；只负责理解需求并编排工具调用",
        caveats=(
            "留空也能完整规划：本地规则引擎会接管，这是「渐进增强」的一部分",
            "不绑定任何厂商，任何 OpenAI 兼容接口都可以接入",
        ),
    ),
)


def manifest_for(provider_id: str) -> ProviderManifest | None:
    """Look up one manifest, or ``None`` when the id is unknown."""
    for manifest in PROVIDER_CATALOG:
        if manifest.id == provider_id:
            return manifest
    return None


def unknown_capabilities() -> list[str]:
    """Capabilities declared by a manifest that the registry does not route.

    A non-empty result means a manifest claims something nothing can serve, which
    would make the capability matrix misleading. Exposed as a function so a test
    can assert it stays empty instead of trusting the manifests by eye.
    """
    known = set(CAPABILITY_BY_METHOD)
    declared = {capability for m in PROVIDER_CATALOG for capability in m.capabilities}
    return sorted(declared - known)


def _is_active(manifest: ProviderManifest, registered: set[str], present: set[str]) -> bool:
    """Whether this source is contributing right now.

    Three cases, because "active" means something different for each kind of
    source: a data provider is active when it is registered; a model is active
    when its activation credentials are present; a planned source is never
    active.
    """
    if not manifest.is_usable:
        return False
    if manifest.provider_names:
        return bool(set(manifest.provider_names) & registered)
    if manifest.activation_keys:
        return set(manifest.activation_keys) <= present
    return manifest.status == STATUS_AVAILABLE


def _serves_capability_now(
    manifest: ProviderManifest,
    capability: str,
    registered: set[str],
    live: Mapping[str, set[str]] | None,
) -> bool:
    """Whether this source can serve ``capability`` *right now*.

    ``registered`` alone is not enough, and getting this wrong is how the matrix
    overstates. A source declares several capabilities but may register several
    providers to cover them; if only some of those providers are live, crediting the
    whole declaration would advertise a capability nothing can execute. That is not
    hypothetical — 高德's three providers once shared one registry name, so two were
    dropped on registration while the matrix, reading only the declaration, kept
    reporting 公交/地铁 as covered.

    ``live`` maps a capability to the names of the providers that actually implement
    it (derived from the registry's own capability routing, so it cannot drift from
    the code). When it is given, a source counts only if one of its provider names is
    among them. When it is not, the coarser name-overlap test is used, which keeps
    the pure-manifest callers working.

    Sources with no ``provider_names`` — models and ingestors — are not provider
    backed and are gated elsewhere, by their activation credentials.
    """
    if not manifest.provider_names:
        return True
    if live is None:
        return bool(set(manifest.provider_names) & registered)
    return bool(set(manifest.provider_names) & live.get(capability, set()))


def _active_providers_for(
    capability: str,
    registered: set[str],
    live: Mapping[str, set[str]] | None = None,
) -> list[str]:
    """Implemented manifests with a live provider that serve ``capability``."""
    active: list[str] = []
    for manifest in PROVIDER_CATALOG:
        if not manifest.is_usable or capability not in manifest.capabilities:
            continue
        if not _serves_capability_now(manifest, capability, registered, live):
            continue
        active.append(manifest.id)
    return active


def _provider_declines_aspect(
    manifest: "ProviderManifest",
    capability: str,
    aspect: str,
    live: Mapping[str, set[str]] | None,
    unserved_aspects: "Mapping[str, Mapping[str, frozenset[str]]] | None",
) -> bool:
    """Whether every live provider of this manifest declares it cannot serve ``aspect``.

    A manifest's ``aspects`` is a *static* claim, so it has to hold for every configuration a
    user might pick — which is impossible when the answer depends on which server they pointed
    the provider at. OSRM is the case: pointed at FOSSGIS it routes pedestrians from a real
    foot profile, pointed at the demo server it can only borrow the car network's distance and
    derive the duration.

    Where the two disagree the provider wins, because the provider is the one that knows its
    configuration. The direction is enforced here rather than trusted: this can only ever
    *remove* an aspect, so a provider cannot declare its way into extra coverage — the same
    rule ``UNSERVED_CAPABILITIES`` follows, and for the same reason.
    """
    if not unserved_aspects or not manifest.provider_names:
        return False
    names = [name for name in manifest.provider_names if name]
    if live is not None:
        registered_here = live.get(capability, set())
        names = [name for name in names if name in registered_here]
    if not names:
        return False
    return all(
        aspect in unserved_aspects.get(name, {}).get(capability, frozenset()) for name in names
    )


def aspect_is_requestable(aspect: str) -> bool:
    """Whether a request can name this aspect at all.

    True when some ``TravelMode`` member corresponds to it, per ``ASPECT_TRAVEL_MODES``.
    An aspect that is neither requestable nor declared in
    ``ASPECTS_NEEDING_TOOL_SUPPORT`` is an undeclared dead end: the matrix will offer it as
    a gap a source could fill, and no source can.
    """
    return aspect in ASPECT_TRAVEL_MODES


def aspect_closable_by(aspect: str) -> str:
    """Who could close a gap in this aspect: a source, or only a change to the tool."""
    return CLOSABLE_BY_TOOL if aspect in ASPECTS_NEEDING_TOOL_SUPPORT else CLOSABLE_BY_SOURCE


def _aspect_rows(
    capability: str,
    registered: Iterable[str],
    live: Mapping[str, set[str]] | None = None,
    unserved_aspects: Mapping[str, Mapping[str, frozenset[str]]] | None = None,
) -> list[dict]:
    """Per-aspect coverage for a capability that decomposes.

    Empty for an atomic capability, so callers can treat "no aspects" and "not
    decomposable" identically and the flat capabilities keep their old shape.
    """
    declared = CAPABILITY_ASPECTS.get(capability)
    if not declared:
        return []
    rows: list[dict] = []
    for aspect, label in declared.items():
        providers: list[str] = []
        for manifest in PROVIDER_CATALOG:
            if not manifest.is_usable or capability not in manifest.capabilities:
                continue
            if not _serves_capability_now(manifest, capability, registered, live):
                continue
            served = dict(manifest.aspects).get(capability, ())
            if aspect not in served:
                continue
            if _provider_declines_aspect(manifest, capability, aspect, live, unserved_aspects):
                continue
            providers.append(manifest.id)
        rows.append(
            {
                "aspect": aspect,
                "label": label,
                "covered": bool(providers),
                "providers": providers,
                # Stated for every aspect, covered or not, so a client never has to infer
                # whether the gap it is describing is one the reader can act on.
                "closable_by": aspect_closable_by(aspect),
                "requestable": aspect_is_requestable(aspect),
                "note": ASPECTS_NEEDING_TOOL_SUPPORT.get(aspect, ""),
            }
        )
    return rows


def capability_matrix(
    registered_names: Iterable[str],
    live_providers: Mapping[str, set[str]] | None = None,
    unserved_aspects: Mapping[str, Mapping[str, frozenset[str]]] | None = None,
) -> list[dict]:
    """Which capabilities are reachable, and what would unlock the rest.

    ``registered_names`` are the provider ``name`` values actually present in the
    running registry. A capability counts as covered only when an *implemented*
    manifest that serves it also has a provider registered — so this reports what
    the process can do now, not what the catalogue mentions.

    ``live_providers`` optionally maps each capability to the names of the providers
    that genuinely implement it, taken from the registry's own routing. Supplying it
    tightens the check from "one of this source's providers is registered" to "a
    provider that can actually serve *this* capability is registered", which is what
    stops a partially registered source from being credited with everything it
    declared. Callers that only have names may omit it.

    ``unserved_aspects`` optionally maps a provider name to the aspects it declares it
    cannot serve (``{provider: {capability: {aspect, ...}}}``). A manifest's ``aspects``
    is a static claim that must hold for every configuration; a provider knows which
    server it was actually pointed at. Where they disagree the provider wins, downward
    only — see ``_provider_declines_aspect``.

    For a capability that decomposes (``CAPABILITY_ASPECTS``), ``covered`` means
    *at least one* aspect is served and ``complete`` means all of them are. The
    two are reported separately, and a partially served capability carries its
    missing parts in ``missing_aspects``, because "驾车可用" must not be presented
    as "路线规划没问题".
    """
    registered = set(registered_names)
    matrix: list[dict] = []
    for capability in CAPABILITY_ORDER:
        active = _active_providers_for(capability, registered, live_providers)
        aspects = _aspect_rows(capability, registered, live_providers, unserved_aspects)
        missing = [row["aspect"] for row in aspects if not row["covered"]]
        matrix.append(
            {
                "capability": capability,
                "label": CAPABILITY_LABELS[capability],
                "covered": bool(active),
                # An atomic capability is complete when it is covered.
                "complete": bool(active) and not missing,
                "aspects": aspects,
                "missing_aspects": missing,
                "providers": active,
            }
        )
    return matrix


#: Why a capability nothing can serve is missing, and what would fill it.
#:
#: A nameless gap is not actionable. The catalogue used to report only a *count* of
#: uncovered capabilities, so a deployment where room rates were unavailable said "1 项能力
#: 完全没有来源" and left the reader to find which one by diffing the matrix against their
#: expectations. Since the whole premise of this tool is "plug in what you need", the
#: useful statement is which capability is missing and which kind of source provides it.
#:
#: Only capabilities that *no* catalogue entry serves belong here. If some source could
#: unlock it, the unlock hint already says so and a note would be redundant — worse, it
#: would read as if nothing could, which is the opposite of the truth.
CAPABILITY_GAP_NOTES: dict[str, str] = {
    "search_room_offers": (
        "房型与实时房价只存在于商业酒店渠道（携程 / Booking / Expedia 的开放接口，或高德的酒店开放平台），"
        "本工具尚未接入任何一家。因此预算里的住宿部分是估算额度，并已标注为估算——"
        "它不会拿一个编造的房价冒充实时报价。"
    ),
}


def capability_gaps(
    registered_names: Iterable[str],
    live_providers: Mapping[str, set[str]] | None = None,
    unserved_aspects: Mapping[str, Mapping[str, frozenset[str]]] | None = None,
) -> list[dict]:
    """Uncovered capabilities that no catalogue entry can unlock, with the reason.

    Kept separate from ``uncovered_capabilities`` (a bare list of ids) so existing callers
    keep working, and so a client can tell "nobody serves this" apart from "you have not
    configured it yet" — two situations with opposite remedies.
    """
    matrix = capability_matrix(registered_names, live_providers, unserved_aspects)
    # Anything an inactive-but-implemented source declares is unlockable by credentials, so
    # it is excluded here and reported through the unlock hints instead.
    unlockable = {
        capability
        for entry in PROVIDER_CATALOG
        if entry.status in {STATUS_AVAILABLE, STATUS_NEEDS_CREDENTIALS}
        for capability in entry.capabilities
    }
    gaps: list[dict] = []
    for row in matrix:
        if row["covered"] or row["capability"] in unlockable:
            continue
        gaps.append(
            {
                "capability": row["capability"],
                "label": row["label"],
                "note": CAPABILITY_GAP_NOTES.get(row["capability"], ""),
                # A capability no entry declares is a supply gap by construction: if any
                # source could serve it, its manifest would say so and it would have been
                # filtered out as unlockable above.
                "closable_by": CLOSABLE_BY_SOURCE,
            }
        )
    return gaps


def uncovered_aspects(
    registered_names: Iterable[str],
    live_providers: Mapping[str, set[str]] | None = None,
    unserved_aspects: Mapping[str, Mapping[str, frozenset[str]]] | None = None,
) -> list[dict]:
    """Parts of a covered capability that nothing serves.

    Separate from ``uncovered_capabilities`` on purpose. A missing capability means
    the feature is absent; a missing aspect means the feature works for some
    journeys and silently not for others — which is worse, because nothing in the
    UI would otherwise hint at it. A trip that needs 公交/地铁 would just be
    planned as if driving were the only option.

    Each gap carries ``closable_by`` so the caller can tell "connect a source" from "this
    tool cannot do it yet". Both are real gaps; they are not the same advice, and only one
    of them is followable.
    """
    gaps: list[dict] = []
    for row in capability_matrix(registered_names, live_providers, unserved_aspects):
        for aspect in row["aspects"]:
            if not aspect["covered"]:
                gaps.append(
                    {
                        "capability": row["capability"],
                        "capability_label": row["label"],
                        "aspect": aspect["aspect"],
                        "label": aspect["label"],
                        "closable_by": aspect["closable_by"],
                        "requestable": aspect["requestable"],
                        "note": aspect["note"],
                    }
                )
    return gaps


def catalog_as_dicts(
    registered_names: Iterable[str] = (),
    *,
    configured_credentials: dict[str, set[str]] | None = None,
) -> list[dict]:
    """Serialise the catalogue for the settings page.

    ``configured_credentials`` maps a provider id to the credential keys that
    already have a value, so the UI can show "configured" without ever receiving
    the secret itself.
    """
    registered = set(registered_names)
    configured = configured_credentials or {}
    payload: list[dict] = []
    for manifest in PROVIDER_CATALOG:
        present = configured.get(manifest.id, set())
        payload.append(
            {
                "id": manifest.id,
                "label": manifest.label,
                "kind": manifest.kind,
                "status": manifest.status,
                "capabilities": list(manifest.capabilities),
                # Which parts of a decomposable capability this source serves, so
                # the UI can say what an inactive source would actually add
                # instead of only that it mentions a capability.
                "aspects": {capability: list(served) for capability, served in manifest.aspects},
                "cost_note": manifest.cost_note,
                "freshness_note": manifest.freshness_note,
                "docs_url": manifest.docs_url,
                "caveats": list(manifest.caveats),
                "active": _is_active(manifest, registered, present),
                # Only ever the *names* of configured credentials plus whether the
                # value is a secret, never the value itself.
                "credentials": [
                    {
                        "key": item.key,
                        "label": item.label,
                        "help": item.help,
                        "docs_url": item.docs_url,
                        "secret": item.secret,
                        "required": item.required,
                        "configured": item.key in present,
                    }
                    for item in manifest.credentials
                ],
                "missing_credentials": [
                    item.key
                    for item in manifest.credentials
                    if item.required and item.key not in present
                ],
            }
        )
    return payload
