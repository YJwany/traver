# Provider System

Provider 层是外部数据的唯一入口。它负责获取与适配外部数据，并把结果包装成带来源引用的结构化事实；平台特有字段不允许渗透进 Travel Engine。

## 能力路由（Capability Routing）

Provider 只需声明它**真正能提供**的能力，不需要实现完整接口。`providers/base.py` 用 `runtime_checkable` Protocol 定义能力，`registry.py` 据此把每次聚合调用路由到对应 Provider：

| 能力 Protocol | 方法 |
|---|---|
| `HotelProvider` | `search_hotels(profile)`、`search_room_offers(hotel_ids, profile)` |
| `FoodProvider` | `search_restaurants(profile)` |
| `AttractionProvider` | `search_attractions(profile)` |
| `RouteProvider` | `calculate_routes(profile, points: list[RoutePoint])` |
| `WeatherProvider` | `search_weather(profile)` |
| `HealthProvider` | `health()` |

`CAPABILITY_BY_METHOD` 维护方法名到能力的映射。这样地理数据源（OpenStreetMap）不必伪造它没有的商业数据，天气数据源也不必返回空的酒店列表。未知方法会回退到全部 Provider。`TravelProvider` 是全能力 Protocol，保留给有意覆盖所有类别的 mock/fixture Provider。

所有方法返回结构化 `ProviderResult`，包含 `status`、`provider`、`source_references`、`data`、`errors`。返回空数据集是一个**成功**结果，不是错误——这一点让"OSM 没有房价"能被诚实表达。

## 当前注册的 Provider

默认启用的是免费免密钥数据层（见 [free-sources.md](../providers/free-sources.md)）：

- `openstreetmap`（Overpass API）— 酒店、餐厅、景点
- `open-meteo` — 天气预报
- `osrm` — 路线矩阵

`FREE_PROVIDERS_ENABLED=true`、`MOCK_PROVIDER_ENABLED=false` 是默认值，因此默认运行使用真实数据，合成数据不会静默混入。离线与确定性测试显式地反过来设置。

`ctrip-mock`、`tongcheng-mock`、`meituan-mock` 与 `FixtureTravelProvider` 使用稳定 fixture 模拟不同来源与价格差异，仅在 `MOCK_PROVIDER_ENABLED=true` 或测试中注册。

## Provider 清单与能力矩阵

Traver 定位是"汇总工具"：它自己不带任何数据，也不绑定任何模型，所有来源都由使用者接入。因此"现在有哪些来源、它们解锁了什么、还差什么"必须是**程序能回答的问题**，而不是只能靠读源码才知道。

`providers/manifest.py` 为每个来源声明一份清单：能力、所需凭据、费用说明、**时效性说明**、以及诚实的状态。

| 状态 | 含义 |
|---|---|
| `available` | 已实现，无需任何凭据即可使用 |
| `needs_credentials` | 已实现，但需要使用者填入凭据 |
| `planned` | 只在计划中，**尚未实现** |

关键点：**列在清单里不等于能用**。`planned` 的源（目前是高德）会出现在目录里，但**不贡献任何能力覆盖**，矩阵里也查不到它。有测试断言这一点——把设计中的东西算成能力，就是自我吹嘘。

`GET /api/v1/providers/catalog` 返回清单、能力矩阵和缺失凭据，用于渲染设置页。两条防漂移测试保证它不说谎：

1. 清单声明的每个能力，都必须是 `CAPABILITY_BY_METHOD` 真能路由的方法；
2. 清单声明的每个 provider 名，都必须真的被 `build_free_providers()` 注册。

每个可用源还必须声明自己的**时效性**（有测试强制）。原因见 [realtime-data.md](realtime-data.md)：旅游攻略面向未来，只能用近期数据。

凭据如何保存与加密见 [credentials.md](credentials.md)。

## 运行时保障

统一 `ProviderRuntime` 负责：

- **超时**：基线为 `PROVIDER_TIMEOUT_SECONDS`；Provider 可用自身 `timeout_seconds` 覆盖。免费社区端点会覆盖，因为 Overpass 合法地需要数秒。
- **重试**：`PROVIDER_RETRY_COUNT` / `PROVIDER_BACKOFF_SECONDS`。免费 Provider 的瞬时失败主要由 `FreeHttpClient` 内部按指数退避重试，因为那一层同时掌握按主机限流，能保证每次重试仍合规。
- **错误分类**：瞬时错误（超时、连接失败、429/5xx）可重试；永久错误（4xx、非法响应）从不重试，也不会触发熔断——重试一个 404 只会浪费共享服务的配额。
- **熔断**：按 Provider 隔离，依据**连续失败次数**（`consecutive_failures`）而不是累计失败数，因此历史抖动不会让健康 Provider 一直处于 degraded。一次调用无论内部重试多少次，只计一次失败。

`ProviderHealth` 暴露 request/success/failure/timeout 统计与状态；聚合审计写入 `provider_requests` / `provider_responses`。非 `HealthProvider` 的 Provider 状态为 `unknown`。

## 真实接入要求

新增 Provider 必须独立实现 API client、认证、超时、重试、速率限制、熔断、日志、Normalization、SourceReference、缓存策略和测试。

规则：

1. 商业事实（价格、库存、评分）必须有真实来源，并携带 `SourceReference`；缺失时留空并标记 `is_estimate`，不得填充猜测值。
2. 遵守来源的速率与署名要求，速率限制写在代码里而不是文档里。
3. 禁止用不稳定网页抓取或未经授权的接口冒充正式接口。
4. 只声明自己真正提供的能力。
