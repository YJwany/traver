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

from collections.abc import Iterable
from dataclasses import dataclass, field

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
        capabilities=(
            "search_hotels",
            "search_room_offers",
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
        label="OSRM 路线（公共 demo）",
        kind="data",
        status=STATUS_AVAILABLE,
        capabilities=("calculate_routes",),
        provider_names=("osrm",),
        cost_note="免费、免密钥",
        freshness_note="按请求实时计算，默认缓存 10 分钟",
        docs_url="https://project-osrm.org/",
        caveats=(
            "公共 demo 只提供驾车路线，步行与骑行没有真实路径",
            "因此步行时长按实测直线距离推算并标注为估算",
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
        status=STATUS_PLANNED,
        capabilities=(
            "search_hotels",
            "search_room_offers",
            "search_restaurants",
            "search_attractions",
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
        provider_names=("amap",),
        cost_note="个人认证开发者非商业用途每月有免费配额，超出后按量计费",
        freshness_note="按请求实时查询，国内 POI 与路线显著优于 OSM",
        docs_url="https://lbs.amap.com/",
        caveats=(
            "尚未实现：状态为 planned，现在不提供任何能力",
            "公交/地铁规划是 OSRM 完全没有的能力，这是接入它的主要理由",
            "服务条款禁止存储或缓存其结果，接入时需按实时查询设计",
            "个人免费配额自注册认证之日起计一年，之后需付费",
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


def capability_matrix(registered_names: Iterable[str]) -> list[dict]:
    """Which capabilities are reachable, and what would unlock the rest.

    ``registered_names`` are the provider ``name`` values actually present in the
    running registry. A capability counts as covered only when an *implemented*
    manifest that serves it also has a provider registered — so this reports what
    the process can do now, not what the catalogue mentions.
    """
    registered = set(registered_names)
    matrix: list[dict] = []
    for capability in CAPABILITY_ORDER:
        active: list[str] = []
        for manifest in PROVIDER_CATALOG:
            if not manifest.is_usable or capability not in manifest.capabilities:
                continue
            if manifest.provider_names and not (set(manifest.provider_names) & registered):
                continue
            active.append(manifest.id)
        matrix.append(
            {
                "capability": capability,
                "label": CAPABILITY_LABELS[capability],
                "covered": bool(active),
                "providers": active,
            }
        )
    return matrix


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
