# 免费数据源

本项目的默认数据层全部使用**免费、无需密钥、无需注册**的服务。本文记录每个来源的用途、许可、合规数值，以及实测中发现的限制——这些限制直接决定了代码为什么这样写。

## 来源一览

| 用途 | 服务 | 端点 | 许可 / 署名 |
|---|---|---|---|
| POI（酒店、餐厅、景点） | Overpass API（OpenStreetMap 数据） | 见下方镜像列表 | ODbL 1.0 © OpenStreetMap contributors |
| 地理编码（主） | Open-Meteo Geocoding | `geocoding-api.open-meteo.com/v1/search` | CC BY 4.0 © Open-Meteo |
| 地理编码（兜底） | Nominatim | `nominatim.openstreetmap.org/search` | ODbL 1.0 © OpenStreetMap contributors |
| 天气 | Open-Meteo Forecast | `api.open-meteo.com/v1/forecast` | CC BY 4.0 © Open-Meteo |
| 空气质量 / 紫外线 | Open-Meteo Air Quality | `air-quality-api.open-meteo.com/v1/air-quality` | CC BY 4.0 © Open-Meteo |
| 路线 | OSRM demo（FOSSGIS） | `router.project-osrm.org` | ODbL 1.0 © OpenStreetMap contributors |
| 景点简介 | 中文维基百科 / Wikidata | `zh.wikipedia.org/w/api.php`、`www.wikidata.org/w/api.php` | CC BY-SA 4.0（维基百科）/ CC0（Wikidata） |
| 公共节假日 | `holidays` 库（本地数据） | 无网络请求 | MIT |

### 空气质量与海拔

空气质量来自 Open-Meteo 的**另一个端点**，与天气同源同许可，因此不引入新的署名义务。两个实测约束决定了实现方式：

1. **该端点不支持 `daily` 聚合**。任何 `daily=` 组合都返回 400（`Cannot initialize ForecastVariableDaily from invalid String value ...`），因此代码自己把逐小时值按天取**峰值**。取峰值而不是均值是有意的：某个下午 AQI 飙升正是「当天该安排室内活动」的信号，均值会把它抹掉。
2. **它的预报范围只有 7 天，而天气是 16 天**。实测 `forecast_days=7` 通过、`8` 返回 400（`Allowed range 0 to 7`）。早期实现沿用了天气的 16 天，结果整个空气质量请求被拒绝、所有 AQI 字段静默为空——这正是必须实测的原因，现已固定为 `MAX_AIR_QUALITY_DAYS = 7`。

AQI 的取值区间比时间轴更短（实测 7 天请求只返回约 117 个 AQI 值），所以**没有数据的日期字段保持 `None`，而不是填一个默认值**——把「未知」当成「空气优良」与编造价格是同一类错误。`outdoor_friendly` 只在数据确实存在时才因空气质量或紫外线翻转为「不建议户外」。

空气质量的失败是**尽力而为**：端点不可用时天气仍完整返回，只是没有 AQI 字段，并在日志中记录原因。

**海拔不需要额外请求**：天气端点的响应本身就带 `elevation`（成都实测 494 m），因此没有再调用 `/v1/elevation`。

### 公共节假日：为什么用本地库而不是 Web API

调研中首选的免费 API 是 `date.nager.at`，但**它的服务条款不允许本项目使用**：

> The Web API can be used for private or non-profit projects. **For commercial purposes we require active sponsorship.**

这与「全部使用免费数据源」的要求冲突。改用 MIT 许可的 [`vacanza/holidays`](https://github.com/vacanza/holidays) 库：本地数据、无网络、无密钥、无速率限制，且**商用无需付费**。

而且它**数据更完整**。以 2026 年中国国庆为例：

| 来源 | 2026 年覆盖 |
|---|---|
| `date.nager.at` | 仅 6 个节日日期：`01-01`、`02-17`、`05-01`、`06-19`、`09-25`、`10-01` |
| `holidays` 库 | 上述全部，**外加清明节、以及全部调休休息日**：`10-02`、`10-03`、`10-05`、`10-06`、`10-07`、春节 `02-16`~`02-23` 等 |

也就是说被拒绝的那个 API **完全遗漏了清明节**，也没有任何连续假期——而「连续多天假期」恰恰是影响景点开放与拥挤程度的硬约束。库中的名称还保留了真实措辞（`休息日（由 2026-02-14 调休）`）。

已知限制（如实记录，不做推断）：

- 节假日**时段**可知，但「某个具体景点当天有多挤」不可知，因此不派生任何拥挤度评分。
- 对于按行政区划分节假日的国家（德国、美国等），旅行画像中没有行政区信息，因此**只使用全国性日历**——这是少报，而不是编造一个地区性假期。
- 节假日只表示「可能关闭、可能拥挤」，**不代表某个景点一定闭馆**，因此它不会覆盖景点自身的数据。

### 景点简介：只跟随 OSM 的显式链接

景点简介来自维基百科正文（**CC BY-SA 4.0**，署名义务比 ODbL 更严格，且要求相同方式共享）。

**关键设计：绝不按名称猜测匹配。** 这是实测得出的结论，不是保守偏好。用真实成都数据尝试名称匹配的结果：

| 景点名 | 名称匹配的结果 | 问题 |
|---|---|---|
| `火烈鸟` | `紅鸛科` | 匹配到了**物种分类**条目，不是动物园展区 |
| `人民公园` | 消歧义页 | 正文讲的是海口/广州/上海/天津的人民公园，**不是成都的** |
| `望江楼公园` | 页面不存在 | 却存在名称相近的条目 |
| `成都永陵博物馆` | `王建墓` | **这是正确的**（永陵即王建墓） |

最后一行说明「要求条目坐标一致」也会**误杀正确匹配**。没有任何规则能稳定区分这些情况，因此采用唯一可靠的依据：**OSM 元素上由人工标注的 `wikipedia` / `wikidata` 标签**。

实测该标签在成都的覆盖率为 **7.6%**（21/278 个有名元素），但**凡是有标签的，匹配全部正确**，包括名称匹配永远做不到的情况：

| 景点 | OSM 标签 | 效果 |
|---|---|---|
| `永陵` | `wikipedia=zh:王建墓` | 名称匹配永远找不到这个条目 |
| `武侯祠` | `wikipedia=zh:成都武侯祠` | 直接绕开消歧义页 |
| `杜甫草堂博物馆` | `wikidata=Q4390891` | 仅有 QID 时通过 sitelink 解析出条目 |

因此取舍是：**覆盖率约 8%，但精度 100%**。对以事实为准的系统这是正确的方向——**空描述是诚实的，错误描述是编造**。

实现要点：

- `wikipedia` 标签格式为 `lang:Title`（如 `zh:王建墓`）；只查询中文维基，指向其他语言的链接会被**跳过**而不是用错误语言抓取。
- 只有 `wikidata` 标签时，先通过 Wikidata 的 `zhwiki` sitelink 解析出条目名。两个标签同时存在时**两条路径都会尝试**（早期实现用 `elif`，导致 QID 没有中文 sitelink 时会白白丢弃可用的 `wikipedia` 标签）。
- MediaWiki 支持**批量查询**（实测一次 10 个标题，9 个返回摘要），因此 50 余个景点只需 2 次请求，而不是 50 次。每批上限 20 个标题。
- **消歧义页与不存在的页面一律拒绝**（通过 `pageprops.disambiguation` 与 `missing` 判定）。
- 摘要截断到 600 字符：目的是识别地点，不是镜像整篇文章。

## 合规数值

Overpass 镜像按顺序尝试：

1. `https://overpass-api.de/api/interpreter`
2. `https://overpass.private.coffee/api/interpreter`
3. `https://maps.mail.ru/osm/tools/overpass/api/interpreter`

三个镜像都已用与 Provider 相同的查询形态实测过（`out center`，成都市区小范围），结论如下——**镜像列表可用，但没有任何一个能保证随时可用**：

| 镜像 | 实测结果 | 说明 |
|---|---|---|
| `overpass-api.de` | **504**（28.4 s） | 主站高峰期过载，属可重试状态 |
| `overpass.private.coffee` | **429**（2.0 s） | 服务存活但限流；很快返回，不会拖住回退 |
| `maps.mail.ru` | **200**（20.7 s，3 个元素） | 当时唯一可用的镜像 |

因此**镜像回退不是可选项而是必需项**。实测把第一个镜像换成不可达地址后，回退链路仍然成功返回 58 个景点，但整体耗时 60.6 s——这正好说明为什么 Provider 层预算（默认 90 s）必须显著大于单次 HTTP 预算，也说明了为什么不能把 `FREE_PROVIDER_TIMEOUT_SECONDS` 调小。

`429` 与 `504` 都按可重试处理，但**重试同一个已限流的镜像没有意义**：客户端会带指数退避重试，然后按顺序换下一个镜像。

## 合规数值

以下数值是**使用条款要求**，不是性能调参，且已在代码中强制（`providers/free/http.py` 的 `DEFAULT_HOST_INTERVALS` 与 `HostRateLimiter`）：

| 主机 | 最小请求间隔 | 依据 |
|---|---|---|
| `nominatim.openstreetmap.org` | 1.1 s | Nominatim 使用政策：绝对上限 1 次/秒 |
| `router.project-osrm.org` | 1.0 s | FOSSGIS：不得超过 1 次/秒 |
| `overpass-api.de` 及其镜像 | 1.0 s | Overpass 常规使用上限约 < 100 次/天 |
| `api.open-meteo.com` | 0.2 s | 免费额度 600 次/分钟 |
| `air-quality-api.open-meteo.com` | 0.2 s | 同上 |
| `geocoding-api.open-meteo.com` | 0.2 s | 同上 |
| `zh.wikipedia.org` | 1.0 s | 维基媒体要求机器人**串行**请求，不并行 |
| `www.wikidata.org` | 1.0 s | 同上 |

其他约束：

- **User-Agent 必须能标识应用**。实测结论（逐个 UA 请求 Nominatim）：

  | User-Agent | 结果 |
  |---|---|
  | `TraverTravelPlanner/0.1 (+https://example.invalid/traver)` | **200** |
  | `TraverTravelPlanner/0.1` | 200 |
  | `TraverTravelPlanner/0.1 +https://example.invalid/traver` | 200 |
  | `TraverTravelPlanner/0.1 (dev@example.com)` | **403 Access denied** |
  | 不设置（httpx 默认 UA） | **403 Access denied** |

  真正的规则是：**UA 里含邮箱地址会被拒绝**，httpx 默认 UA 也会被拒绝；而带 `(+https://...)` 的括号形式是被接受的。所以默认 UA 保持为带联系 URL 的形式，正式部署前请把 `example.invalid` 换成真实地址（不要填邮箱）。

- **Open-Meteo 免费额度为非商用**（< 10k 次/天）。
- **署名必须可见**。署名信息通过 `GET /api/v1/providers/attributions` 暴露，并写入每个 `SourceReference.request_context["attribution"]`。

## 请求预算：为什么是一个并集查询

三个 POI 类别**共用一个** Overpass 请求，结果按目的地缓存（`FREE_PROVIDER_POI_TTL_SECONDS`，默认 1 小时），三个能力方法共享同一份缓存。这是为了把请求量压到最低。

查询内部按类别分别输出，而不是共用一个 `out center N`：

```
[out:json][timeout:25];
nwr["tourism"~"^(hotel|hostel|guest_house|apartment)$"](bbox)->.hotels;
nwr["amenity"~"^(restaurant|cafe|fast_food)$"](bbox)->.food;
nwr["tourism"~"^(attraction|museum|viewpoint|gallery|zoo|theme_park)$"](bbox)->.sights;
.hotels out center 40;
.food out center 60;
.sights out center 60;
```

**实测原因**：使用单个共享的 `out center 120` 时，OSM 元素顺序会让酒店和餐厅吃掉全部配额，成都只能得到 **3 个景点**（52 个中的 3 个）。改为按类别限额后稳定得到 52 个，且仍然只是一个请求。

搜索范围取地理编码点周围约 ±0.045°（约 5 km），因为 Overpass 查询代价随面积增长。

## 免费数据层的三个硬限制

### 1. OSM 没有商业事实

OSM 是地理事实来源，不含房价、库存、评分、人均消费。因此：

- `search_room_offers` 返回**空成功**，不编造价格。
- 酒店预算项标记 `is_estimate=true`，使用可配置的估算额度（`BUDGET_FALLBACK_*`），而不是让总预算塌缩为 0——把最大的一笔开销静默抹掉比给出显式估算更糟。
- `rating`、`review_count`、`average_cost` 在没有真实来源时保持 `None`。

**`stars` 不等于 `rating`。** OSM 的 `stars` 标签是酒店的**星级评定**（一个分类），不是住客评分。两者语义不同、不可互换，因此：

- `Hotel.rating` 一律保持 `None`（`decision_engine` 用 `(hotel.rating or 0) / 5` 参与加权打分，把星级写进去会让"五星级分类"与"5.0 分住客评分"得到完全相同的分数，而没有任何评分的酒店反而拿 0 分）。
- 星级分类记录在 `SourceReference.request_context["star_classification"]`，信息不丢失且可审计。
- 解析走 `_parse_star_classification()`：只接受 `0`–`5` 的整数（容忍 `4.0`，拒绝 `4.5`/`6`/`-1`/`luxury`）。

数值解析 `_parse_decimal()` 会**保留前导符号**。此前它直接跳过非数字字符，`-1` 会被解析成 `1`：畸形的星级会变成合法星级，负数金额会变成正数价格。现在负值一律被拒绝，末尾的 `amount >= 0` 判断才真正可达。

### 2. 公共 OSRM demo 只提供驾车路径

这是最容易出错的地方。实测结果（成都两个相距约 4.8 km 的点）：

```
foot       200 distance=4818m duration=426.6s -> 40.7 km/h
walking    200 distance=4818m duration=426.6s -> 40.7 km/h
driving    200 distance=4818m duration=426.6s -> 40.7 km/h
car        200 distance=4818m duration=426.6s -> 40.7 km/h
bike       200 distance=4818m duration=426.6s -> 40.7 km/h
bicycle    200 distance=4818m duration=426.6s -> 40.7 km/h
```

`foot`、`walking`、`bike` 等任意 profile 名都**静默返回完全相同的驾车结果**，而不是报错。如果直接采信，行程里会出现"步行 7.8 km 仅需 9 分钟"这种物理上不可能的安排。

处理方式：

- 始终用服务器真正实现的 profile（`driving`）取路径。
- 驾车模式：采用实测距离与时长。
- 步行模式：保留实测**道路距离**，但按明确步速（`WALKING_SPEED_METERS_PER_SECOND = 1.25`，约 4.5 km/h）推算时长，并设 `is_estimate=True`。
- 在 `SourceReference.request_context` 中记录 `duration_source` 与 `walking_speed_mps`，把限制写进数据本身，而不只写在文档里。

要获得真实的步行路径，请自建 OSRM 实例并设置 `OSRM_BASE_URL`。

### 3. 公共端点会间歇性拒绝连接

实测本机到这些服务的连接存在明显的瞬时失败（`ConnectError`，首次连接失败、重试即成功），且部分主机存在 IPv4/IPv6 单栈不可达（例如 `geocoding-api.open-meteo.com` 的 IPv4 超时而 IPv6 正常，`nominatim` 两者间歇不可达）。

因此重试不是可选项：

- `FreeHttpClient` 内置 `FREE_PROVIDER_RETRY_COUNT` 次重试与**指数退避**（0.5 → 1 → 2 → 4 秒封顶）。每次重试仍经过按主机限流器，所以合规性不受影响。
- 曾有过的配置 `PROVIDER_BACKOFF_SECONDS=0.1` 会让全部重试在 0.2 秒内耗尽，必然撞在同一段故障窗口内——这正是需要指数退避的原因。
- 地理编码是所有其他免费 Provider 的前置条件，因此它有两级来源（Open-Meteo → Nominatim）。两级都失败才会抛出 `GeoResolutionError`。

## 地理编码与来源选择

地理编码是所有其他免费 Provider 的前置条件（天气、POI、路线都需要坐标），因此它有两级来源：

1. **Open-Meteo Geocoding**（默认首选）— 免密钥、快，并且返回天气预报端点所需的 IANA 时区。
2. **Nominatim**（兜底）— 在 Open-Meteo 无匹配结果**或结果不可靠**时使用；额外提供真实的行政区 `boundingbox`。

`FREE_PROVIDER_NOMINATIM_FALLBACK=false` 可关闭兜底，把地理编码限制在单一来源。

`GeoResolver` 还接受显式的 `sources` 顺序，用于把解析固定到某一个来源（测试与运维排查都靠它）：

```python
GeoResolver(http, sources=("nominatim",))  # 只用 Nominatim
GeoResolver(http, nominatim_fallback_enabled=False)  # 只用 Open-Meteo
```

来源名与顺序由 `KNOWN_SOURCES` 校验，拼错会在构造时立刻报错，而不是被逐来源的错误处理吞掉、最后伪装成"无法解析目的地坐标"。

### "返回了结果"不等于"返回了正确结果"

兜底最初只在来源**报错或返回空**时触发，这留下了一个更危险的失败模式：来源成功返回了一个**错误**的城市。实测两个都发生过：

- **`巴黎` 解析到美国肯塔基州**（偏约 8,300 km）。GeoNames 的 `PPLC` 是国家首都、`PPLA2` 是次级行政驻地，而原评分用 `startswith("PPLA")` 判定最高档——`"PPLC"` 不匹配这个前缀，于是巴黎（PPLC，人口 213 万）被降为第 1 档，而 Paris, Texas（PPLA2，人口 9,870）拿到第 2 档。评分元组先比档位再比人口，小镇因此胜出。`罗马` 同理解析到美国乔治亚州。
- **`丽江` 解析到湖南的同名小镇**（偏约 1,154 km）。Open-Meteo 对该名称只返回这一个 `PPL`、且无人口数据。因为它是"非空答案"，兜底从未执行，用户拿到的是湖南某小镇的完整行程——**一个看起来完全合理的错误计划**。

现在两条判据互补：

- `_feature_rank()` 按 `PPLC` > `PPLA` > `PPLA2..5` > `PPL` 分档（国家首都位次最高）。
- 只有当候选是行政驻地**且**有真实人口数据时才标记为可信（`confidence >= 0.8`）；否则标记为 **provisional**，继续询问下一个来源。若所有来源都给不出可信答案，仍然采用 provisional 结果——不完美坐标也好过拒绝规划。

修复后实测（默认来源、真实网络）：`丽江` 从 1/0/1 变为 35/53/32（酒店/餐饮/景点），`巴黎` 从 8/30/4 变为 40/62/56。

### 行政区质心与定居点

Nominatim 的 `boundingbox` 是行政区范围（成都跨度约 1.3 度纬度），对单个 Overpass 查询来说太大，因此会围绕解析点收敛到 `FREE_PROVIDER_BBOX_RADIUS_DEGREES` 之内。

这带来第二个陷阱：行政区的坐标是**该区域的质心**，可能远离它命名的城镇。`平遥` 解析到平遥县域质心，距平遥古城约 11 km——落在 ±0.045°（约 5 km）搜索框之外，于是尽管 OSM 完整收录了平遥古城，该目的地仍返回 0 个 POI。

因此 Nominatim 请求 `limit=5`（同一次请求，无额外开销），并优先选择定居点类型的结果：判据是 `addresstype`（退化到 `type`），取值属于 `city`/`town`/`village`/`hamlet`/`municipality`/`borough`。

> 判据**不能**用 `class`：实测线上 jsonv2 响应里根本没有该字段（值为 `None`）。曾有一版实现与配套测试都基于 `class == "place"`，测试通过但线上永不匹配——**用不存在的响应形状写的测试等于没有测试**。现在的测试载荷按线上实际形状构造。

为避免把"错城市"换成另一种"错城市"，这个优选受 `_NOMINATIM_REFINEMENT_KM`（50 km）约束：只能在同一locality 内微调，绝不会跳到远处的同名地点。

修复后实测：`平遥` 从 0/0/0 变为 36/20/37。

## 双超时设计

`build_free_providers` 接受两个超时，二者含义不同：

- `FREE_PROVIDER_REQUEST_TIMEOUT_SECONDS`（默认 25 s）限制**单次 HTTP 请求**。
- `FREE_PROVIDER_TIMEOUT_SECONDS`（默认 90 s）限制**一次 Provider 调用**，它可能包含镜像回退、限流等待和 HTTP 层重试。

必须满足 `Provider 预算 > 尝试次数 × 单次请求预算`，否则第一个慢镜像就会耗尽全部预算，镜像回退形同虚设。

## 路线点集的选择

路线矩阵在候选集确定**之后**才计算，因为 OSRM 需要真实坐标。点集选择有两个要点：

- 不能按列表顺序截断。优化器从每个列表的前部取用条目，若共用一个配额，先出现的类别会挤掉其他类别（曾导致所有餐厅路段静默退化为估算值）。
- 按类别交错取样，每类额度由行程长度推导（`attractions_per_day × days × 3` 与 `days × 4`，含跳过余量），上限 `MAX_ROUTE_POINTS = 40`。

公共 OSRM 的 table 服务上限约 100 个坐标，一次请求返回完整矩阵，因此额度同时受请求规模与 n² 增长约束。

## 如何验证

离线测试用 `httpx.MockTransport` 回放录制形态的响应，不访问网络：

```powershell
pytest -q
```

真实网络端到端测试默认跳过，显式开启后真的请求上述服务：

```powershell
$env:TRAVER_LIVE_TESTS='1'
python -m pytest tests/integration/test_free_providers_live.py -q -s
```

这些测试断言真实数据可用，并且不会出现被编造的事实：步行速度必须落在 2.5–6.5 km/h 的物理合理区间、不得出现凭空的房价、总预算不得为 0、每个开放数据引用的 `request_context` 都必须带署名。其中一组测试把 `GeoResolver` 固定到 `("nominatim",)` 直接验证 Nominatim，因为正常流程中 Open-Meteo 成功时兜底路径根本不会被执行。

真实数据还断言了归一化、去重与决策的不变量：实体必须有名称、非 0 坐标与来源引用；去重后**不得存在"名称匹配且相距 500 m 以内"的两个实体**（与 `_same_entity()` 一致）；没有报价时决策结果必须为空。

去重断言并非空谈。在成都真实数据上关闭去重后：

```
dedup ON : attractions/restaurants/hotels = (52, 57, 37)   可合并重复对 = 0
dedup OFF: attractions/restaurants/hotels = (58, 57, 37)   可合并重复对 = 7
  - 西南民族博物馆 ~ 民族博物馆 @ 8m
  - 杜甫草堂博物馆 ~ 成都杜甫草堂博物馆-观景台 @ 213m
  - 杜甫草堂博物馆 ~ 杜甫草堂 @ 198m
  - 成都永陵博物馆 ~ 永陵 @ 192m
  - 宽窄巷子 ~ 宽窄巷子 @ 29m
```

也就是说 OSM 里同名同地的重复节点确实存在（例如"宽窄巷子"在 29 m 内有两个节点），去重把它们从 58 合并到 52；一旦去重失效，上面的不变量断言就会失败。

新增的三个来源同样有真实数据断言：

- **空气质量**：必须至少有一天带 AQI（否则说明请求范围又超出了端点的 0–7 天限制）；第 11 天之后必须**全部为空**，不得填默认值；凡是 AQI > 60 或 UV ≥ 8 的日期，`outdoor_friendly` 必须为 `False`。
- **海拔**：必须落在成都的合理区间（300–800 m），且天气仍保持 16 天范围。
- **节假日**：国庆期间必须解析出**调休连续休息日**（`10-02`、`10-03`），`10-08` 必须不是假期；清明节必须存在（被否决的那个 API 遗漏了它）。
- **维基简介**：**没有显式 `wikipedia`/`wikidata` 标签的景点一律不得有简介**——这是防止退化成按名称猜测的核心断言；有简介的景点必须带 `wikipedia_url` 与 `domain=content_reference` 的来源，且署名许可必须是 `CC BY-SA 4.0`。

实测一次真实聚合的结果（成都，6 个带显式链接的景点）：

```
wikidata 批量请求: ids=Q4390891|Q1540709|Q3366223|Q10951691|Q7507431|Q1069942   （1 次请求）
wikipedia 批量请求: titles=杜甫草堂|王建墓|宽窄巷子|四川科技馆|成都武侯祠|安顺廊桥  （1 次请求）
wikipedia enrichment: 6 linked, 6 described, 0 skipped
```

即 **6 个带链接的景点全部命中，且只用了 2 次请求**。其中 `成都永陵博物馆` 命中的条目是 `王建墓`、`武侯祠` 命中的是 `成都武侯祠`——这两个都是名称匹配**永远无法正确得到**的（前者名称不同，后者会落到消歧义页）。

对照组的错误匹配（`火烈鸟` → `紅鸛科`、`人民公园` → 消歧义页）在这些景点上都**没有**产生简介，因为它们没有显式链接——这正是设计要保证的行为。
