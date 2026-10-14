# 酒店供应源接入方案（房型与实时房价）

> 状态：**阶段 1、阶段 2 已实现**（2026-10-14）。阶段 1（契约改实体）与阶段 2（RollingGo MCP
> 适配层与 Provider）已落地并有测试；**响应字段结构未经真实 Key 验证**，见 §14。
> 事实纪律：本文中所有「现状」都标注了代码位置；所有「候选来源」都标注了核实方式，
> 未核实的一律写明未核实。凡是本文没写的，实现时不得假设其存在。

## 0. 一句话

Traver 目前 `search_room_offers` **没有任何来源**，住宿部分永远是估算额度。
本文给出接入真实酒店供应 API 的设计，并指出一个必须先解决的接口问题：

> **该能力的入参是 Traver 自己生成的随机 UUID，任何外部供应 API 都无法解析它。**
> 所以「接一个酒店价格源」不是「再加一个 Provider」能完成的——它需要一处接口改动。

## 1. 现状（代码事实）

| 事实 | 位置 |
| --- | --- |
| 能力契约是 `search_room_offers(hotel_ids: list[str], profile)` | `providers/base.py:79` |
| 调用时传的是 `[str(hotel.id) for hotel in hotels]` | `services/aggregation.py:456,464` |
| 而 `Hotel.id` 是 `Field(default_factory=uuid4)`，**进程内随机生成** | `domain/models.py:433` |
| `HotelOffer` / `HotelRoomType` / `HotelRatePlan` 三个模型**已存在** | `domain/models.py:446,457,468` |
| 免费源 Overpass 返回空成功，并声明 `UNSERVED_CAPABILITIES = {"search_room_offers"}` | `providers/free/overpass.py:446` |
| 清单里没有来源声明 `search_room_offers` | `providers/manifest.py` |
| `CAPABILITY_GAP_NOTES["search_room_offers"]` 明说「本工具尚未接入任何一家」 | `providers/manifest.py:687` |
| `Hotel.rating` / `Restaurant.rating` 在真实部署里恒为 `None` | `docs/architecture/realtime-data.md` |

现有两个实现（`mock.py`、`fixture.py`）能"工作"，只是因为**它们自己生成酒店**，
所以自己产出的 id 与随后收到的 id 在同一个夹具世界里自洽。真实供应 API 没有这个前提。

## 2. 候选来源（外部调研，2026-10-14）

| 来源 | 形态 | 凭据 | 价格能力 | 国内覆盖 | 核实程度 |
| --- | --- | --- | --- | --- | --- |
| **道旅 / RollingGo 酒店 MCP** | MCP over streamable-HTTP | 自助申请个人 Key，免费、无调用上限 | 实时库存价、房型、退改规则、取消政策、可下单 | **强**（中国供应链） | 已读官方 README |
| **Amadeus Self-Service** | REST + SDK | 免费测试额度（约 2000 请求/月），生产需审核 | Hotel Search + Offers（房型/价格/取消政策）+ 评分情感 | 弱 | 官方组织页 + 第三方指南 |
| **Xotelo** | REST JSON | 免费 | 多 OTA 报价（Hotels.com / Expedia / Booking / Agoda），带 `timestamp` | 弱 | 已读官方文档页 |
| **LiteAPI / Nuitée** | MCP-native + REST | sandbox key | 3M+ 房源 | 中 | 官网首页 |
| **TripO** | REST `/v1` | 需申请 | 800K+ 房源 | 中 | 官网首页 |
| **Hotelbeds** | REST | 测试 key，B2B 需签约 | 房型报价 | 中 | 第三方示例仓库 |
| RapidAPI `apidojo/booking`、`hotels4` | REST | RapidAPI key | 非官方 Booking / Hotels.com 数据 | 弱 | 市场页 |
| 抓取类（google-hotels-api / expedia-scraper / hotel_spider 美团携程） | 爬虫 | — | — | — | **排除** |

**排除抓取类的理由不是技术，是本项目的既有原则**：`README.md` 与
`docs/architecture/README.md` 都写着「不能用未经授权的网页抓取代替正式接口」。
接一个爬虫会把整个事实纪律（来源可归因、时间可标注）的口子打开，收益不抵代价。

**一个必须写明的边界**：这些来源给的是**价格**，不是**评价**。
`Hotel.rating` 是否因此可填，取决于来源是否同时给出**发布时间**——
需求 3 是「只能用近期的评价」，一个没有日期的 4.8 分和上个月的 4.8 分是同一个数字、两件不同的事
（`manifest.py:692` 已有同样的论述）。**若来源只给分数不给日期，`rating` 必须继续是 `None`，
分数走 `search_reviews` 而不是 `Hotel.rating`。** 这条不能因为「终于有来源了」就放松。

## 3. 真正的障碍：id 不匹配

外部供应 API 有两种寻址方式，**两种都与现有契约对不上**：

1. **按「目的地 + 日期 + 人数」搜索**（RollingGo、Amadeus、Xotelo 的 `/list`、TripO）
   → 它返回**它自己的**酒店与报价，没有「输入一批酒店 id」这个概念。
2. **按供应商自己的酒店 id 报价**（Amadeus Offers、Hotelbeds）
   → 需要先拿到供应商的 id，而 Traver 现在只把**自己的 UUID** 传下去。

而 `search_room_offers` 只收 `hotel_ids: list[str]`，传的是 `uuid4()`。
供应商拿到这串 UUID 后**无法反查**它是哪家酒店——名称、坐标、外部 id 一个都没有。

这正是本项目反复出现的**缺陷家族 1（两套词汇表对不上）**的形状：
契约两侧各有一套身份体系，语言不同，没有共享常量的机制，
而两侧的兜底都会让缺失看起来像正常工作（供应商会返回空，聚合层把它读成「无报价」，
预算照旧塌回估算额度——**与「这个来源本来就没报价」在外部完全一样**）。

所以：**先改契约，再接来源。**

## 4. 三种接入形态

### 形态 A：把 `search_room_offers` 的入参改成实体（**推荐**）

```python
# 现在
async def search_room_offers(self, hotel_ids: list[str], profile: TripProfile) -> ProviderResult
# 改成
async def search_room_offers(self, hotels: list[Hotel], profile: TripProfile) -> ProviderResult
```

理由是它**与本仓库已有的同类能力一致**：`enrich_attractions(attractions: list[Attraction], ...)`
和 `resolve_places(names, ...)` 收的都是实体/名称而不是 id。
改完之后供应商有两条路可用：

- 它自己也是 `HotelProvider` 时，读 `hotel.source_references[*].source_id`
  （那里存着**供应商自己的**酒店 id，`models.py:284`）直接报价；
- 酒店来自别的源（OSM / 高德）时，按 `canonical_name` + `location` 做实体解析再报价。

报价仍回填到 `hotel.id`，**下游链路一行不用改**。

**影响面（需一并改）**：`base.py` 契约、`mock.py`、`fixture.py`、`overpass.py` 三个实现、
`aggregation.py:464` 调用点、以及所有构造 `hotel_ids` 的测试。
这是一个**可枚举、可测试**的改动，不是开放式重构。

### 形态 B：新增能力 `search_hotel_offers(profile)`

按「目的地 + 日期 + 人数」查询，返回「供应源自己的酒店 + 报价」，
酒店经实体解析并入候选集。不改现有契约，但**多出一个与 `search_room_offers` 语义重叠的能力**。

本仓库刚刚花力气把 `search_hotels`（找酒店）与 `search_room_offers`（给房价）**拆成两个能力**
（`base.py:49-82` 的长注释），理由就是「找」与「定价」是两种来源的两种本事。
再加第三个「又找又定价」的能力，会把那次拆分重新搅浑。**不推荐单独用**。

### 形态 C：不动契约，靠名称+坐标匹配

供应商拿不到 id，只能自己按 `profile.destination` 搜一遍，再按名称匹配。
问题：**它搜到的酒店与传进来的那批可能对不上**，于是「有报价」和「报价贴错了酒店」
从外部看是一样的——这正是本项目最忌讳的失败形态（错误归因的数字戴着自信标签）。
**不推荐。**

## 5. 推荐方案

**形态 A + 分阶段**：

- **阶段 1（接口）**：把 `search_room_offers` 的入参从 id 改成 `Hotel` 实体，
  三个现有实现与调用点同步改，测试补齐。此阶段**不接任何真实来源**，
  所以它单独可合并、可回滚，且不改任何行为。
- **阶段 2（首个真实来源）**：接入一个来源（见 §6 的取舍），它同时实现
  `HotelProvider` 与 `RoomOfferProvider`，这样「它发现的酒店」与「它报的价」
  用的是同一套自己的 id，不依赖实体解析的准确率。
- **阶段 3（可选）**：接入第二个来源，此时才需要跨源实体解析，
  并按 `cross-source-validation.md` 记录分歧。

## 6. 来源取舍（需要你决定）

| 若目标是… | 选 | 代价 |
| --- | --- | --- |
| **国内目的地、要真实可订价** | 道旅 / RollingGo | 是 **MCP** 形态，Traver 没有 MCP 客户端，需要一层 streamable-HTTP 适配；条款含返佣/转售 |
| **最合规、能接受海外为主** | Amadeus | 测试环境是**模拟库存**，真数据要过生产审核；国内覆盖弱 |
| **接入成本最低、先跑通验证** | Xotelo | 基于 TripAdvisor，**非官方聚合**；国内酒店覆盖弱 |
| **只要一个能跑通的沙箱** | LiteAPI / Amadeus 测试环境 | 沙箱数据不是真实库存，**不能当报价展示给用户** |

**我的倾向**：阶段 2 用 **RollingGo**——它是唯一同时满足「个人 Key 自助申请」「实时可订价」
「国内覆盖强」的候选，正好补 Traver 最大的空白。代价是 MCP 适配层与返佣条款，两者都已知、可控。

## 7. 字段映射（以 RollingGo `getHotelDetail` 为例）

| 供应源字段（README 示例） | Traver 模型 | 说明 |
| --- | --- | --- |
| `hotelId` / `name` / `nameEn` | `Hotel.canonical_name` + `source_references[*].source_id` | 外部 id 进 `source_id`，不进 `Hotel.id` |
| `roomTypeId` / `roomName` / `bedTypeDescription` | `HotelRoomType.id` / `name` / `bed_type` | 外部 id 需映射（见下） |
| `ratePlanId` / `ratePlanName` | `HotelRatePlan.id` / `name` | |
| `cancellationPolicies[]` | `HotelRatePlan.cancellation_policy`（文本）| 结构要拍平成人话，`HotelOffer.cancellation_policy` 是 `str` |
| `currency` / `totalPrice` / `averagePrice` | `HotelOffer.price: Money` | 注意 `totalPrice` 是**总价**，`averagePrice` 是**单晚**；`HotelOffer` 有 `nights` 计算属性，勿混用 |
| `isOnRequest` | 需单独表达 | 「需申请」不等于「有房」，不能当成 `availability > 0` |
| （无） | `HotelOffer.tax` / `service_fee` | 来源没给就留 `None`，**不要填 0** |
| （无） | `Hotel.rating` | 见 §2 的边界：没日期就不填 |

**外部 id 的归属**：`HotelOffer.hotel_id` 是 Traver 的 UUID；
供应商的 `hotelId` 只能落在 `SourceReference.source_id`。
把外部 id 塞进 `Hotel.id` 会让「同一次规划里两家源报了同一家酒店」变成两个实体，
也会让 id 在两次运行之间变化——`Hotel.id` 是 UUID 就是为此。

## 8. 时效性与诚实性（本项目的硬约束）

1. **价格必须带抓取时间。** `SourceReference.observed_at` = 我们请求的时刻，
   `fetched_at` = 同一时刻，`observed_at_basis`：
   - 来源明确给了报价时间 → `PROVIDER_REPORTED`；
   - 来源只回实时结果、不给时间 → `SOURCE_SILENT`（**不得**因为「是实时查询」就写 `LIVE_COMPUTED`——
     实时查询的**是上游缓存**还是**真实库存**，我们不知道）。
2. **不缓存。** 与高德同理（`amap-contract.md` §服务条款）：把缓存的房价当实时房价展示，
   正是需求 3 要禁止的「吹嘘」。且多数供应条款禁止存储结果。若某来源允许缓存，
   再按 `MAX_AGE_SECONDS` 单独讨论。
3. **`hotel_offer` 的年龄上限是 12 小时**（`freshness.py` 已有），
   但 `hotel_availability` 是 30 分钟——**「还有房」比「多少钱」衰减快得多**，两者不要混。
4. **拿不到报价时仍然是估算，且已标注。** 现有的 `is_estimate=true` + 可配置额度这条路径
   **保留不动**；有报价的项才写 `is_estimate=false / status=observed` 并挂来源。
   两个方向都要有测试（一个恒为 `True` 的标记不携带信息，`manifest.py:224` 已有同样要求）。
5. **`isOnRequest` 必须如实表达**，不能当成有房。
6. **导出里的价格同样要印「抓取于 X」**（`export.md` 已定的规则，这里只是沿用）。

## 9. 失败可见性

沿用高德那次的教训（`amap-contract.md` §失败必须被上报）：
**Provider 自己 catch 掉失败，外部看到的就是成功**。

- 记录 `last_attempts` / `last_failures`；
- 有失败即 `status="degraded"` 并带 `errors`，让聚合层能区分
  「没有已配置的来源能报价」与「来源已配置但这次失败」——两者的建议相反；
- 配额超限与凭据错误**分开**（`AmapQuotaError` vs `AmapKeyError` 的教训：
  把配额说成钥匙错误，会让用户反复重填一个完全正常的 Key）。

## 10. 凭据与清单

新增一个 `ProviderManifest`（`manifest.py`）：

- `id` / `label` / `kind="data"`
- `status=STATUS_NEEDS_CREDENTIALS`（有 Key 才启用，无 Key 不影响免费源出攻略）
- `capabilities=("search_hotels", "search_room_offers")` —— **必须与实际能路由的方法一致**，
  否则 `unknown_capabilities()` 与防漂移测试会报出来
- `credentials=(CredentialField(key=..., label=..., help=..., docs_url=...),)`
  → 进凭据保险箱（AES-256-GCM、按属主隔离、界面保存免重启，`credentials.md`）
- `cost_note` / `freshness_note` / `caveats` 如实写：价格是实时查询、不缓存、
  以及**该来源的条款约束**（例如返佣/转售）

**两处必须同步改，否则会自相矛盾**：

1. `CAPABILITY_GAP_NOTES["search_room_offers"]` 现在写着「本工具尚未接入任何一家」——
   接入后这句就是假话，要么删掉，要么改成描述新来源。
2. 能力矩阵里 `search_room_offers` 会从 `capability_gaps`（无人能提供）**变成 unlockable**
   （填 Key 即可）。这两类在 UI 上必须区分开（`manifest.py:709` 的既有设计）。

## 11. 测试与守卫

- **契约测试**：新增 `tests/unit/test_web_api_contract.py` 风格的检查，
  确认清单声明的 `search_room_offers` 真的被 `providers_for()` 路由到该来源
  （`provider-system.md` 的防漂移测试，已抓过 `['amap:search_hotels']` 这类缺口）。
- **id 归属测试**：断言外部酒店 id 落在 `source_references[*].source_id`，
  **不出现在** `Hotel.id` 里。
- **诚实性测试**：无报价 → `is_estimate=true`；有报价 → `is_estimate=false` + 挂来源。
  两个方向都测（沿用 `manifest.py:224` 的既有要求）。
- **`isOnRequest` 测试**：断言它**不会**被当成 `availability > 0`。
- **评分边界测试**：来源只给分数不给日期时，`Hotel.rating` 仍为 `None`。
- **mutation harness**：至少 3 条变异（把 `source_id` 塞进 `Hotel.id`、
  把 `SOURCE_SILENT` 改成 `PROVIDER_REPORTED`、把失败吞成成功），
  全部必须被抓住——**没有触发的变异与一个不工作的守卫，从外面看是一样的**。
- **失败可见性 probe**：仿 `scripts/probe_amap_failure_visibility.py`，
  用总是失败的传输层复现 degraded 路径，**无需真实 Key**。
- **一个真实网络 probe**：仿 `scripts/probe_amap_transit_shape.py`，
  用真实 Key 打印实际返回的字段路径——**这是让假设变成事实的唯一办法**。

## 12. 未决问题（需要你拍板）

1. **选哪个来源做阶段 2？**（我倾向 RollingGo；见 §6）
2. **是否接受 MCP 形态**，即为此在 `providers/` 下写一层 streamable-HTTP 适配？
   还是优先找一个纯 REST 的来源（代价：国内覆盖弱）？
3. **是否接受返佣/转售条款**？RollingGo 的模式是「你加价、它返佣」，
   与「Traver 只做汇总、不自营预订」的定位是否冲突？
4. **评分要不要接**？若接，来源必须同时给发布时间，否则只能进 `search_reviews` 而非 `Hotel.rating`。
5. **是否需要「可下单」**？现在 Traver 的定位是规划工具，`HotelOffer` 只有价格、没有下单字段。
   若只要价格，`searchHotels` + `getHotelDetail` 就够；若要下单，是另一个量级的改动。

## 13. 与现有文档的关系

- `provider-system.md` — 清单与能力矩阵，本方案的 manifest 改动按它的规则走
- `credentials.md` — 凭据保险箱，本方案只填 `CredentialField`，不碰加密
- `realtime-data.md` — 时效性硬约束，§8 全部沿用它的口径
- `cross-source-validation.md` — 多源分歧，阶段 3 才用到
- `amap-contract.md` — 商业源接入的先例，§9 的失败可见性直接照搬它的结论

## 14. 实施状态（2026-10-14）

### 已完成

**阶段 1 — 契约改为实体**（与来源无关，单独可回滚）

- `RoomOfferProvider.search_room_offers(hotels: list[Hotel], profile)`（`providers/base.py`）
- 三个实现同步：`mock.py`、`fixture.py`（改为从**传入酒店的 `source_references`** 反查外部 id，
  不再重读夹具）、`free/overpass.py`（签名变更，行为不变）
- 调用点：`services/aggregation.py` 传 `hotels` 而不是 `str(hotel.id)`
- 新增 `tests/unit/test_room_offer_contract.py`：钉住「收到的是实体」与「外部 id 在
  `source_references[*].source_id` 上、不在 `Hotel.id` 里」，并用 `get_type_hints` 钉住参数类型
  ——`runtime_checkable` 只看方法是否存在，回退签名不会被它发现

**阶段 2 — RollingGo 适配层与 Provider**

- `providers/rollinggo/client.py`：MCP over streamable-HTTP 客户端。
  JSON-RPC 2.0 `tools/call`；`Accept: application/json, text/event-stream`（缺则服务端 400）；
  SSE 帧剥离（取最后一个可解析的帧）；JSON-RPC `error` 原样上报，不猜分类；
  HTTP 401/403 → `RollingGoKeyError`（唯一用户可修的失败）
- `providers/rollinggo/hotels.py`：实现 `HotelProvider` + `RoomOfferProvider`。
  只对**自己发出的**酒店报价（按 `source_references` 匹配，不做名称模糊匹配）；
  `isOnRequest` 不当作有库存；无金额的房型直接丢弃；
  每条引用 `observed_at_basis=SOURCE_SILENT`（来源不给数据自身时间，不写 `LIVE_COMPUTED`）
- `providers/free/http.py`：新增 `request_raw`（可设自定义头、可发 JSON body、返回未解析的
  response），并把重试逻辑抽成 `_retrying`，`request_json` 与它共用
- `config.py` + `.env.example`：`ROLLINGGO_ENABLED` / `ROLLINGGO_BASE_URL` /
  `ROLLINGGO_TIMEOUT_SECONDS` / `ROLLINGGO_REQUESTS_PER_SECOND` / `ROLLINGGO_TTL_SECONDS`
  （**没有** `ROLLINGGO_API_KEY`——Key 走凭据保险箱，按属主隔离）
- `manifest.py`：新增 `rollinggo` 条目（`needs_credentials`，
  `capabilities=("search_hotels", "search_room_offers")`）；
  **删除了 `CAPABILITY_GAP_NOTES["search_room_offers"]`**——该能力现在是「填 Key 可解锁」，
  再挂着「无人能提供」的说明就是反方向的新谎言。防漂移测试
  `test_the_note_map_does_not_describe_a_solvable_gap` 会拦住回退
- `main.py`：独立的 HTTP 客户端与限速（不再借用免费栈的，否则 `free_providers_enabled=false`
  会让保存的 Key 静默失效——高德踩过一次）；两个来源各自独立解析，一个 Key 不可用不影响另一个
- 测试：`tests/unit/test_rollinggo_provider.py`（25 条，含传输、映射、诚实性、能力接线）
- probe：`scripts/probe_rollinggo_shape.py`——打印真实信封与命中的 key 路径

### 未验证（诚实列出）

- **响应信封与 `getHotelDetail` 的入参名从未对真实服务验证过。** 项目 README 只描述搜索结果
  的语义、给了一个 `getHotelDetail` 输出示例，没有发布完整响应结构；`getHotelDetail` 的参数名
  只能从一个 CLI 示例推断。因此解析器**不假设单一布局**：在若干可能的 key 中定位数组，
  并把命中的 key 记进 `request_context.payload_key`。没有任何 key 命中时返回空——**不编造**。
  `scripts/probe_rollinggo_shape.py` 是让这个假设变成事实的唯一办法。
- 没有真实 Key 的端到端验证；没有浏览器 E2E。
- MCP 会话握手：README 的 cURL 示例直接 `tools/call`，故客户端不先 `initialize`。
  若真实服务要求会话，`RollingGoMcpClient.call_tool` 是唯一需要长这一步的地方。

### 仍未做（阶段 3）

- 第二个酒店来源与跨源实体解析（`cross-source-validation.md`）
- 评分：来源不提供住客评分，`Hotel.rating` 仍为空。若将来接入带评分的来源，
  必须同时有发布时间，否则只能进 `search_reviews`——见 §2 的边界
- 可下单：`HotelOffer` 只有价格，没有下单字段；这是另一个量级的改动
