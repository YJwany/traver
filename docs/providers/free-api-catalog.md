# 免费 API 候选调研（已实测）

> **更新（已落地）**：本文调研的三个候选已经接入，但其中一项**来源被替换**，原因见下。
> 已接入：Open-Meteo 空气质量（+ 天气响应自带的海拔）、公共节假日、维基百科/维基导游景点简介。
> 实现细节与实测数据见 [free-sources.md](free-sources.md)。

> **重要更正**：下面 1.2 节推荐的 `Nager.Date` **不能用于商业用途**。它的服务条款写明
> "The Web API can be used for private or non-profit projects. **For commercial purposes we
> require active sponsorship.**" 因此它不满足「全部免费」的要求，最终改用 MIT 许可的
> [`vacanza/holidays`](https://github.com/vacanza/holidays) 本地库。该库不但是无条件免费，
> 数据还**更完整**（含清明节与全部调休休息日，而 Nager.Date 只有 6 个节日日期）。
> 详见 [free-sources.md](free-sources.md) 的「公共节假日」一节。

这份文档记录**从本机真实请求验证过**的免费 API 候选，供 Traver 后续扩展选用。

调研起点是 GitHub 上广为人知的 [public-apis](https://github.com/public-apis/public-apis) 索引。
该索引只是名录，**不能当作可用性证明**：本次实测中就有三个条目返回 HTTP 200 但实际不可用
（见「假阳性」一节）。所以下表每一项都是真的发过请求、看过返回内容的。

判定标准（三条同时满足才算可用）：

1. **无需 API Key、无需注册账号**
2. **返回真实数据**，不是空壳或错误信封
3. **延迟可接受**（能进请求路径，而不是让用户干等）

**另外还必须满足第 4 条：许可允许本项目用途。** 第 4 条是后来补上的判据——`Nager.Date`
在技术上前三条全过，却卡在许可上。

实测环境：本机直连，单个请求，无代理。延迟只代表当时的一次观测，不是 SLA。

## 一、可用（已实测通过）

### 1.1 Open-Meteo 家族 —— 与现有天气源同源，扩充成本最低

| 能力 | 端点 | 延迟 | 实测到的真实数据 |
|---|---|---|---|
| 空气质量 | `air-quality-api.open-meteo.com/v1/air-quality` | 0.34 s | `pm2_5=35.2`, `pm10=37.7`, `european_aqi=52`, `uv_index=3.7` |
| 海拔 | `api.open-meteo.com/v1/elevation` | 1.50 s | 成都海拔 501 m |
| 历史天气 | `archive-api.open-meteo.com/v1/archive` | 1.42 s | 过去日最高温/降水 |
| 气候预估 | `climate-api.open-meteo.com/v1/climate` | 1.49 s | 2030 年温度预估 |
| 海洋 | `marine-api.open-meteo.com/v1/marine` | 1.37 s | 浪高 |

**为什么值得接**：署名义务（CC BY 4.0）与现有 Open-Meteo 天气源完全一致，不引入新的许可复杂度。
空气质量直接强化已有的 `is_outdoor_friendly()` 判断——现在只看天气现象和降水，看不到 PM2.5 和紫外线。

注意：空气质量端点首次请求出现过一次 `SSL: UNEXPECTED_EOF_WHILE_READING`，重试即成功。
这说明它同样需要走 `FreeHttpClient` 的指数退避重试，不能只试一次。

### 1.2 节假日 —— 影响行程可行性的**硬约束**

| 来源 | 端点 | 延迟 | 实测 |
|---|---|---|---|
| ~~Nager.Date~~ | ~~`date.nager.at/api/v3/PublicHolidays/{year}/{cc}`~~ | 0.62 s | **已否决：商用需赞助**，见文首更正 |
| UK Bank Holidays | `www.gov.uk/bank-holidays.json` | 0.48 s | 英格兰/苏格兰/北爱，静态 JSON |
| isdayoff.ru | `isdayoff.ru/api/getdata` | 1.13 s | 俄罗斯/CIS 工作日判断 |
| **`holidays` 库（最终采用）** | 无网络请求 | 0 s | MIT 许可；2026 中国含清明节与全部调休休息日 |

Nager.Date 的国家列表（`/api/v3/AvailableCountries`）也无需密钥，返回 204 个国家。

**为什么节假日值得接**：节假日会同时影响**景点是否开放**和**拥挤程度**。在此之前系统完全不感知
节假日，排出来的行程可能把博物馆排在闭馆日，或在黄金周把行程排得过满。这是真正会改变结果的
输入，不是装饰性数据。

`holidays` 库与 Nager.Date 的实测差异（2026 中国）：

| 日期 | Nager.Date | holidays 库 |
|---|---|---|
| 2026-01-01 | 元旦 | 元旦 |
| 2026-02-17 | 春节 | 春节 |
| 2026-02-16 ~ 02-23 | — | **春节假期（含调休）** |
| 2026-04-05 / 04-06 | — | **清明节（含补假）** ← 完全遗漏 |
| 2026-10-01 | 国庆节 | 国庆节 |
| 2026-10-02 / 03 / 05 / 06 / 07 | — | **国庆假期（含调休）** |

也就是说被否决的那个 API 不只是许可问题，它**连清明节都没有**。

### 1.3 内容类 —— 补上「景点只有名字」这个最大短板

| 来源 | 端点 | 延迟 | 实测 |
|---|---|---|---|
| 中文维基百科摘要 | `zh.wikipedia.org/w/api.php` (`prop=extracts`) | 0.92 s | 杜甫草堂：「…1961 年 3 月 4 日公佈為第一批全國重點文物保護單位，2006 年 12 月被中国国家旅游局评为国家 4A 级旅游景区。」 |
| 维基百科地理检索 | `zh.wikipedia.org/w/api.php` (`list=geosearch`) | 0.92 s | 以宽窄巷子为圆心 1 km 内：少城街道 159.9 m、成都古蜀船棺合葬墓 299.5 m、福感寺 363.7 m、**宽窄巷子站 660 m** |
| 中文维基导游 | `zh.wikivoyage.org/w/api.php` | 1.20 s | 成都条目 57,675 字节 / 7,793 词，含真实旅行叙述 |
| Wikidata | `www.wikidata.org/w/api.php` | 1.00 s | Q4390891「Du Fu Thatched Cottage」/「park and museum in Chengdu」 |

**为什么值得接**：OSM 只给出名称和坐标，`description` 目前是空的。而维基百科返回的是**真实、
有来源、带许可**的文本，不是编造内容，完全符合「事实优先」原则。

`geosearch` 还有一个额外价值：它能按坐标反查附近的**地铁站**（实测返回「宽窄巷子站」及其距离）。
这比 OSM 的 `railway=station` 标签更容易筛出「离景点最近的地铁口」。

许可：维基百科/维基导游正文为 **CC BY-SA 4.0**，Wikidata 为 **CC0**。若展示正文，必须署名并
以相同许可共享——比 ODbL 要求更严格，接入前需要确认这符合产品预期。

### 1.4 其他可用项

| 来源 | 端点 | 延迟 | 实测 | 与 Traver 的关系 |
|---|---|---|---|---|
| Frankfurter（ECB） | `api.frankfurter.app/latest` | 1.44 s | 1 CNY = 0.14896 USD / 0.13063 EUR / 23.475 JPY | 预算目前硬编码 CNY，多币种时才需要 |
| fawazahmed0 currency-api | `cdn.jsdelivr.net/npm/@fawazahmed0/currency-api` | 0.44 s | 150+ 法币 + 加密货币 | 同上，且覆盖更广、延迟更低 |
| Photon（komoot） | `photon.komoot.io/api/` | 2.96 s | 成都市 `30.659867,104.063315` + 行政区 extent | 地理编码的第三个来源；现有 Open-Meteo + Nominatim 已够用 |
| sunrise-sunset.org | `api.sunrise-sunset.org/json` | 1.21 s | 日出/日落/各段晨昏蒙影 | 户外活动时间窗；但这可以本地算，不必依赖外部服务 |
| adsb.lol | `api.adsb.lol/v2/point/{lat}/{lon}/{radius}` | 2.30 s | 真实航班 CSN6949 / B-38M，含高度速度航向 | 航班追踪，与行程规划关系不大 |

## 二、假阳性（返回 200 但实际不可用）

这一节是本次调研最有价值的部分。以下条目如果只按「HTTP 200」判断，会被误认为已接好：

| 来源 | 表面结果 | 真实情况 |
|---|---|---|
| **transitous**（MOTIS） | `200` | **中国完全无覆盖**，详见下方 |
| **REST Countries** | `200` | 返回信封 `{"success": false, "errors": [{"message": "This API version has been deprecated…"}]}`。`v3.1`、`/all`、`v5` 三个路径全是这个结果，即该服务实际已失效 |
| **caldays** | `403` | 直接拒绝，不可用 |
| **Nager.Date** | `200`，数据正确 | 但服务条款只允许私人/非营利使用，**商用需赞助**。技术上可用、许可上不可用 |

这里有两类假阳性，都值得记录：前三行是**技术上**不可用（状态码骗人），最后一行是**许可上**
不可用（技术与数据都没问题，但用途被条款限制）。只测「能不能拿到数据」是不够的，还必须测
「能不能合法使用」。

### transitous 的完整实测（重要）

transitous 是 MOTIS 的公共实例，能返回**真实的公共交通换乘方案**：

| 城市 | 结果 |
|---|---|
| Berlin | 3 条方案 / 11 min / `['WALK', 'SUBWAY', 'WALK']` |
| Tokyo | 4 条方案 / 11 min / `['WALK', 'REGIONAL_RAIL', 'WALK']` |
| Paris | 6 条方案 / 32 min |
| **成都** | **0 条** |
| **北京** | **0 条** |
| **上海** | **0 条** |

中国的三个城市，无论用 `transitModes=TRANSIT` 还是 `directModes=WALK`，都是 **0 条方案**。

排查过程中有一个容易误判的坑：`transitModes=WALK` 在**柏林也返回 0 条**，正确写法是
`directModes=WALK`。所以「成都 0 条」必须先排除参数错误——用正确参数重测后，中国依然是 0 条，
而 Berlin/Tokyo 正常返回。结论是**区域无 GTFS 数据源**，不是调用方式问题。

**对 Traver 的意义**：transitous 无法补上「公共 OSRM 只支持驾车」这个缺口——恰恰因为当前
目的地是成都。如果将来要覆盖欧洲/日本，它会是很有价值的补充。

## 三、确认不可用

| 来源 | 现象 |
|---|---|
| OpenSky Network | 30 s 超时，未在时限内返回 |
| transport.rest（DB） | `503`，12 s |
| open.er-api.com | 20 s 超时 |
| WorldTimeAPI | 连接失败（DNS/连接层，0.73 s 即失败） |
| OpenTripPlanner 公共 demo | `404` |

## 四、关于「免费酒店价格」的结论

**没有找到任何免密钥的酒店房价/库存来源。** 具体核查：

- public-apis 索引中提及 hotel / lodging / booking / room rate 的行数为 **0**。
- 该索引 Transportation、Food & Drink 等分类下没有任何免密钥的酒店报价条目。
- 现实中提供真实房价的服务（Amadeus Self-Service、Hotelbeds、TripAdvisor Content API、
  Booking 等）都要求注册账号 + API Key，且多数免费额度仅限测试环境或非商用。

因此「免费 + 免密钥 + 真实房价」三者**无法同时满足**。现有设计保持不变是正确的：
`search_room_offers` 返回空成功，酒店预算标记 `is_estimate=true`，绝不编造房价。
要真实房价，必须接受注册/付费来源，这是一个产品决策，不是技术问题。

## 五、推荐接入顺序

按「价值 / 成本」排序：

1. ✅ **Open-Meteo 空气质量** —— **已接入**。复用现有 Provider 与署名；顺带发现天气响应自带海拔，因此不需要额外的海拔请求。
2. ✅ **公共节假日** —— **已接入**，但来源改为 MIT 许可的 `holidays` 库（Nager.Date 商用需赞助，且遗漏清明节与调休）。
3. ✅ **维基百科内容** —— **已接入**。景点从「只有名字」变成有真实介绍。匹配**只跟随 OSM 显式 `wikipedia`/`wikidata` 标签**，因为实测表明按名称匹配会经常出错（`火烈鸟` → `紅鸛科`、`人民公园` → 消歧义页）。
4. ⬜ **汇率**（Frankfurter 或 currency-api）—— 仅当预算要支持多币种时才需要。
5. ⬜ **transitous** —— 当前目的地用不上；若扩展到欧洲/日本再考虑。

落地后的验收方式与实测数据见 [free-sources.md](free-sources.md)，离线/真实网络测试分别位于
`tests/unit/test_new_free_sources.py` 与 `tests/integration/test_free_providers_live.py`。

## 六、复现方式

本文所有数字都来自一次性探测脚本，脚本本身放在系统临时目录、未纳入仓库。
如需复现，要点是：

- 用 `httpx` 直连，`User-Agent` 设为可识别应用的字符串（维基媒体要求带上联系方式）。
- 对返回 200 的响应**必须检查内容**，而不是只看状态码——这是发现假阳性的唯一方法。
- **必须读服务条款**。状态码和数据都正常、但用途被条款限制的情况（Nager.Date）不会在请求里
  暴露出来，只能靠读条款发现。
- 对 Open-Meteo 空气质量这类端点要带重试，首次请求可能因 TLS 中断失败；并注意它只支持
  `forecast_days` 0–7，超出会返回 400。
- 参数用错会伪装成「该地区无数据」。transitous 的样例就是如此：`transitModes=WALK` 在柏林也返回
  0 条，必须用 `directModes=WALK` 才能正确判断覆盖范围。
