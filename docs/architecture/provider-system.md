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

状态 | 含义 |
|---|---|
| `available` | 已实现，无需任何凭据即可使用 |
| `needs_credentials` | 已实现，但需要使用者填入凭据 |
| `planned` | 只在计划中，**尚未实现** |

关键点：**列在清单里不等于能用**。`planned` 的源会出现在目录里，但**不贡献任何能力覆盖**，矩阵里也查不到它。`needs_credentials` 同理：它在**当前属主没有填凭据时**不贡献任何覆盖。有测试断言这两点——把设计中的东西、或把别人填了 Key 的东西算成自己的能力，都是自我吹嘘。

高德现在是 `needs_credentials`（已实现，需自带 Key）。判断"是否已实现"用 `is_usable`，判断"此刻是否真的生效"用 `_is_active`——两者不是一回事，混淆会把未配置的源报成可用。

`GET /api/v1/providers/catalog` 返回清单、能力矩阵和缺失凭据，用于渲染设置页。防漂移测试保证它不说谎：

1. 清单声明的每个能力，都必须是 `CAPABILITY_BY_METHOD` 真能路由的方法；
2. 无需凭据的源，声明的每个 provider 名都必须真的被 `build_free_providers()` 注册；
3. 需要凭据的源，声明的 provider 名必须真的由它自己的工厂构造出来（比对比常量更强：同时证明工厂还能跑）；
4. 声明 `aspects` 的能力必须自己也声明了该能力，且 aspect 名必须在 `CAPABILITY_ASPECTS` 里；
5. **每个已声明能力都必须真的能路由到该来源的 Provider**（向 `providers_for()` 提问，而不是比对名字）。第 1–4 条只检查名字与清单是否自洽，第 5 条才检查"调用能不能到达实现"。

每个可用源还必须声明自己的**时效性**（有测试强制）。原因见 [realtime-data.md](realtime-data.md)：旅游攻略面向未来，只能用近期数据。

### 按属主作用域：为什么注册表要能按人重建

注册表在 lifespan 里只构建一次，但高德的 Key 属于**某个人**。于是一个别扭的问题必须先回答：同一个进程里，两个属主怎么拿到不同的数据源？

`ProviderRegistry.for_owner(owner_id)` 返回一个视图：基础免费源 + 该属主自己的带凭据 Provider。三条规则：

- **每次请求都重建，刻意不缓存。** 缓存实例会保留它构造时的那把 Key，于是出现"我在设置页保存了 Key 却还是用旧的"和"我删掉了 Key 它还在用"——两个 bug 都只在缓存存在时出现。
- **没有凭据就返回原本的注册表本身**（而非副本），零配置路径的分配与行为完全不变。
- **工厂抛异常时退回免费源**，不让一个坏凭据拖垮整个规划：没有 Key 也照样能出完整攻略。

属主如何传到深处？AI 工具路径（`ToolOrchestrator` → handler → `trip_service.preview()`）的 handler 签名是**对外发布的 OpenAI 工具契约**，没有位置新增 `owner_id` 参数。因此属主用 `ContextVar` 承载（`services/owner_context.py`），由中间件在解析出 principal 后绑定、请求结束时 reset。

这里有一个必须记住的区别：`None` 表示"未指定"，`""` 表示**匿名属主**（认证关闭时的真实属主 id）。把 `""` 当成"未指定"去回退到环境属主，就是把别人的 Key 递给匿名请求。`resolve_owner()` 只在参数为 `None` 时才回退。

### 一个 Provider 一个名字

注册表以 **provider `name` 为键**，因此同一个名字只能有一个 Provider。高德最初把路由、POI、天气三个 Provider 都命名为 `"amap"`，注册时**只有最后一个活下来，另外两个被静默丢弃**——没有任何异常，没有日志。更糟的是能力矩阵当时只依据清单的**声明**判定覆盖，于是它继续显示"公交/地铁 ✓ 已覆盖"，而实际上根本不存在能执行它的路由 Provider。

两个修正都保留了：

1. 三个 Provider 各有名字（`amap-routing` / `amap-places` / `amap-weather`），`AMAP_PROVIDER_ID` 仍作为**来源** id 用于凭据与清单；
2. 矩阵改为向注册表**实际询问**每个能力有哪些 Provider 在用（`registry.providers_for()`），而不是从声明的名字推断。于是"声明了但没接上"必然显示为缺口。

这条规则不是高德特有的：注册表本来就假定一个名字对应一个实现（`openstreetmap` / `osrm` / `open-meteo`），高德只是第一个违反它的源。

### 找到酒店不等于能给房价

`CAPABILITY_BY_METHOD["search_hotels"]` 原先指向一个**同时要求** `search_room_offers` 的 Protocol。高德能列出酒店 POI 却没有房价，于是它连 `isinstance` 都过不了，**酒店搜索根本无法路由到它**——清单声明了 `search_hotels`，而调用永远到不了。声明、实现、路由三者单独看全都正确，只有向注册表提问才会暴露。

因此能力与 Protocol 拆开了：`search_hotels` → `HotelProvider`（只要能找酒店），`search_room_offers` → `RoomOfferProvider`（专管报价）。缺房价的源不会被询价，也不会因此失去酒店搜索；预算层照旧把这类成本标为估算。

有一条防漂移测试直接问注册表这个问题：清单里每个已实现来源声明的每个能力，`providers_for()` 都必须返回该来源的 Provider。上面两个 bug 都会被它一次抓住。

### 局部覆盖不等于完整覆盖（aspects）

一开始能力是扁平的：一个源回答了 `calculate_routes`，`路线规划` 就算"已覆盖"。这个模型会撒谎，而且是往两个方向撒谎：

- OSRM 的公共 demo 只提供**驾车**路线，完全没有公交/地铁。扁平模型下矩阵显示"路线规划 ✓ 已有来源"，于是"所有能力都有来源"——而实际上任何需要坐地铁的行程，规划器只能当它不存在。
- 高德列了 `calculate_routes`，于是它看起来**毫无必要**："已经有 OSRM 了"。这正是接入高德的主要理由被矩阵自己抹掉了。

高德接入后这条依然成立，而且更重要了：高德声明驾车/公交地铁/步行三个 aspect，**刻意不声明骑行**——`TravelMode` 里没有骑行、`mode_for_pace()` 也不可能返回它，所以没有任何行程会请求骑行路线。声明它只会让矩阵多一个永远无法兑现的勾。于是配置了 Key 之后矩阵会显示 `covered=true` 而 `complete=false`、`missing_aspects=["cycling"]`：**解锁了，但没解锁全部**，这正是要如实说出的话。

`CAPABILITY_ASPECTS` 把这类能力拆成若干部分（`calculate_routes` → 驾车/公交地铁/步行/骑行），每个源声明自己真正提供哪些。于是矩阵对一行给出两个独立事实：

| 字段 | 含义 |
|---|---|
| `covered` | 至少有一部分有来源 —— 驾车能算 |
| `complete` | **所有**部分都有来源 —— 路线规划真的没问题 |
| `missing_aspects` | 缺哪几块 |
| `aspects[]` | 每一块各自的 `covered` 与来源 |

`uncovered_aspects` 与 `uncovered_capabilities` 分开上报：能力缺失是功能不存在，aspect 缺失是**功能对部分行程有效、对其余静默失效**——后者更危险，因为界面上看不出任何异常。

omitting an aspect 是一个断言：*这个源不提供它*。OSRM 因此只声明 `driving`——它的步行时长是直线距离推算，不是真实路径，把它算作"提供步行"就是夸大。有测试断言 OSRM 未被记上 transit，并在把 aspects 改成"全都支持"时失败。

### 设置页由清单生成

`apps/web/app/components/provider-catalog.tsx` 整页由 `/providers/catalog` 渲染：新增一个源只需在 `manifest.py` 里加一条，界面会自动出现它的凭据表单、费用与时效说明——**不需要改 Web 代码**。这是"添加数据源无需改代码"从口号变成事实的地方。

界面上三处刻意的措辞：

- `planned` 的源显示为"尚未实现"；`needs_credentials` 的源显示为"需要凭据"，且它的能力标注为"可补/待实现"，绝不显示为可用；
- 「未接入模型」不作为故障呈现——本地规则引擎会接管，行程照样完整；
- 保险箱说明区分主密钥来源：环境变量 vs 密钥文件，并明说后者的弱点与 Windows 上无法设 ACL 的限制。

界面区分"已填入"与"已验证"：填了 Key 只说明"已保存"，不代表能连通。混为一谈会让一个错 Key 看起来一切正常。

Web 端的手写类型与实际 API JSON 之间没有编译期保证，因此有一份端到端脚本逐字段比对真实响应，并在路由 aspect 缺失时失败。

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
