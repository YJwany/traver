# 高德（AMap）Web 服务接口契约

本文记录 Traver 实现高德数据源所依据的**接口事实**，以及每一条事实的来源。目的是把「查证过的」和「猜的」分开——猜错字段名不会报错，只会让路线、POI 或天气**静默变空**，因此这里尽量只写已核实的内容，并标注核实方式。

## 核实方式

高德的官方文档页（`lbs.amap.com/api/webservice/guide/api/...`）正文被导航栏大量稀释，`web_fetch` 只能取到前部内容，**不足以拿到响应字段结构**。因此字段级的核实改用两个独立证据源：

1. **交叉验证端点**：用 `gh search code` 搜索真实调用代码，确认端点路径确实存在且广泛使用。
2. **交叉验证字段**：`yaklang/yaklang` 的 `common/amap/apiv5.go` 与 `common/amap/api.go` 是完整的 Go 客户端，**带 `json:` tag**，tag 即线上字段名。Go 结构体 tag 是编译期绑定的成品代码，比文档转述可靠。

> 凡本文未列出者，一律视为**未核实**，实现中不得假设其存在。

## 通用约定

| 项目 | 事实 |
| --- | --- |
| 基础域名 | `https://restapi.amap.com` |
| 鉴权 | `key` 作为**查询参数**传递（因此有泄露到日志的风险，见下） |
| 编码 | UTF-8 |
| 成功判定 | `status` 等于**字符串** `"1"` |
| 失败 | `status` 为 `"0"`，`info` 为原因文本，`infocode` 为错误码 |

**`status` 是字符串 `"1"`，不是数字、也不是布尔值。** `if payload.get("status")` 会同时接受 `"0"`，从而把**每一个失败都当成成功**——这是本项目专门写测试钉住的一点（`tests/unit/test_amap_client.py`）。

### 坐标顺序与精度

- 线上格式一律为 `"经度,纬度"`（`lon,lat`），逗号分隔，如 `117.500244,40.417801`。
- 官方说明：**经纬度小数点不超过 6 位**。
- Traver 内部一律 latitude 优先，因此在**唯一一处**（`parse_location`）完成交换，并有针对天安门真实坐标的测试。若不交换，数值本身仍然"合法"，**下游无法察觉**，只会在错误的地方规划行程。

### 错误码（已核实部分）

| infocode | 含义 | 处理 |
| --- | --- | --- |
| `10001` | key 不正确或过期 | 归为「钥匙问题」，提示用户去设置页改 |
| `10002` | 没有权限使用相应的服务 | 同上（常见于 Key 类型选错，如用了 JS API Key） |
| `10003` | 访问过于频繁 | 归为「配额/频率问题」 |
| `10004` | 单位时间访问过于频繁（日） | 同上 |
| `10044` | 账号维度日调用量超限 | 同上 |

「钥匙问题」和「配额问题」分开，因为**补救方式不同**：把配额超限说成钥匙错误，会让用户反复重填一个完全正常的 Key。

## 路线规划

### 公交/地铁（接入高德的首要理由）

```
GET /v5/direction/transit/integrated
```

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `origin` | 是 | `lon,lat` |
| `destination` | 是 | `lon,lat` |
| `city` | **是** | 城市名或城市编码 |
| `cityd` | 跨城时必填 | 目的城市 |
| `strategy` | 否 | 策略，默认最快捷 |
| `show_fields` | 否 | `cost` / `navi` / `polyline` |
| `nightflag` | 否 | 是否考虑夜班车，0 或 1 |
| `date` / `time` | 否 | `yyyy-MM-dd` / `HH:mm`，默认当天与当前时间 |
| `max_trans` | 否 | 最大换乘次数，默认 10 |
| `alt_count` | 否 | 返回方案条数，默认 3，取值 1–5 |
| `extensions` | 否 | `base`（默认）或 `all` |

响应结构（已核实字段名）：

```
route.origin / route.destination / route.taxi_cost
route.transits[]                 # 换乘方案列表
  .distance                      # 方案总距离，米
  .duration                      # 方案预计耗时，秒
  .walking_distance              # 步行距离，米
  .cost { duration, taxi_cost, transit_fee }
  .segments[]                    # 换乘路段
    .walking { distance, duration, steps[] }
    .bus     { buslines[] }      # 公交
    .railway { railway[] }       # 地铁
    .entrance { name, location } # 地铁入口
    .exit     { name, location } # 地铁出口
```

公交/地铁线路对象字段一致：

```
name / id / type / distance / duration / polyline
departure_stop / arrival_stop / via_stops[] { name, id, location }
```

**要点**：`city` 是**必填**。缺了它不是返回空结果，而是直接报错，因此实现里必须先拿到城市名再调用。

### 驾车 / 步行 / 骑行

| 方式 | 端点 | 备注 |
| --- | --- | --- |
| 驾车 | `GET /v3/direction/driving` | 有 `strategy`（0 最快、1 避收费、2 最短、3 避高速…） |
| 步行 | `GET /v3/direction/walking` | 官方上限 100 km |
| 骑行 | `GET /v5/direction/bicycling` | |
| 电动车 | `GET /v5/direction/electrobike` | 本程序未接入 |

```
route.paths[]
  .distance            # 米
  .duration            # 秒
  .strategy / .tolls / .toll_distance / .traffic_lights / .restriction
  .cost { duration, taxi_cost, transit_fee }
  .steps[] { instruction, road, distance, duration, polyline, action, assistant_action, toll_road, toll_cost }
```

**重要差异（已核实）**：`/v5/direction/walking` 的路径对象**只有 `distance`，没有 `duration`**。步行的耗时必须由距离除以一个明确标注的速度推导，并标记为估算——不能假设字段存在，否则步行耗时永远是空。

**单位**：`distance` 一律**米**，`duration` 一律**秒**，且**均为字符串**。

## 地理编码

| 用途 | 端点 |
| --- | --- |
| 地址 → 坐标 | `GET /v3/geocode/geo` |
| 坐标 → 地址 | `GET /v3/geocode/regeo` |

正编码响应：

```
count
geocodes[] { formatted_address, country, province, city, citycode, district,
             township, street, number, adcode, location, level }
```

逆编码响应：`regeocode { formatted_address, addressComponent{...}, pois[], roads[], roadinters[] }`。
注意 `addressComponent` 是**驼峰**命名，与其余下划线风格不同；`level` 用于判断匹配粒度（如「省」「市」「区」）。

## POI 搜索

| 用途 | 端点 |
| --- | --- |
| 关键字搜索 | `GET /v3/place/text` |
| 周边搜索 | `GET /v3/place/around` |

```
count
pois[] { id, name, type, tel, direction, distance, location, address, poiweight, businessarea }
```

请求侧可用 `city` / `citylimit` / `keywords` / `types` / `location` / `radius`（默认 3000，最大 50000）/ `sortrule` / `page` / `offset` / `extensions`。
`types` 是高德自己的分类编码体系（如 `050000` 餐饮、`100000` 住宿），与 OSM 的 tag 体系**不通用**，需要单独映射。

## 天气

```
GET /v3/weather/weatherInfo
```

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `city` | 是 | **adcode（行政区编码）**，不是城市名 |
| `extensions` | 否 | `base` 实况（默认）、`all` 预报 |

```
lives[] { province, city, adcode, weather, temperature, winddirection,
          windpower, humidity, reporttime }
forecasts[] { city, adcode, province, reporttime,
              casts[] { date, week, dayweather, nightweather, daytemp, nighttemp,
                        daywind, nightwind, daypower, nightpower } }
```

**`reporttime` 是这条数据的关键**：它是高德给出的**观测/发布时间**，与我们的抓取时间是两个不同的概念，两者都要如实展示——抓取时间说明「我们什么时候问的」，`reporttime` 说明「这个数据本身有多旧」。

**`extensions=base` 用的是 adcode 而非城市名**，因此天气查询依赖先做一次地理编码拿到 `adcode`。

## 距离测量

```
GET /v3/distance
```

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `origins` | 是 | 多个起点用 `|` 分隔 |
| `destination` | 是 | 单个终点 |
| `type` | 否 | `0` 直线、`1` 驾车、`3` 步行 |

```
results[] { origin_id, dest_id, distance, duration }
```

这是一对多（多个起点 → 单一终点），不是完整的 N×N 矩阵；要得到矩阵需要按终点轮询。

## 服务条款约束（影响实现，不只是文档）

1. **禁止存储或缓存结果**。因此 Traver 对高德**不做任何内存或持久化缓存**，每次规划都是实时请求。这一点与时效性要求方向一致：缓存的酒店价格被当作实时价格展示，正是必须避免的「吹嘘」。
2. 需要展示 `© 高德地图` 归属标识（见 `providers/free/attribution.py`）。
3. 个人开发者免费配额**自认证之日起一年**，之后需付费。

## 与免费数据源的关系

| 能力 | 免费源 | 高德 |
| --- | --- | --- |
| 驾车 / 步行 / 骑行路线 | OSRM（仅驾车 profile，步行耗时靠推导） | 有，且国内路网更准 |
| **公交 / 地铁** | **完全没有** | **有（v5 transit），但必须在行程里显式选择** |
| POI / 景点 / 餐饮 | Overpass（OSM tag 体系） | 有，国内覆盖显著更全 |
| 天气 | Open-Meteo | 有（adcode 维度） |
| 酒店**房价** | 无 | **也没有**——高德只提供酒店 POI，不提供房价 |

最后一行是刻意写明的：高德的 `search_hotels` 能力指的是**找到酒店**，不是**给出房价**。因此清单里高德不声明 `search_room_offers`，避免用「能找到酒店」暗示「有实时房价」。

## 公交/地铁：实现了不等于能用

「公交/地铁是 OSRM 完全没有的能力，这是接入高德的主要理由」——这句话在清单里挂了很久，但**当时它做不到**。

原因是选路逻辑只有一个入口：

```python
mode_for_pace(pace) -> TravelMode.WALK if pace in (RELAXED, BALANCED) else TravelMode.DRIVE
```

`Pace` 只有 `relaxed`/`balanced`/`full` 三个成员，所以这个函数**永远返回不了 `TravelMode.TRANSIT`**。而 `_transit_legs` 只能由 `calculate_routes` 里那一个 `if mode is TravelMode.TRANSIT` 分支调用。于是：

- `_transit_legs` 有完整实现、有端点、有针对固定报文的测试；
- capability 矩阵在配置了 Key 时如实报告 `transit: covered`；
- 而**没有任何一次请求能产生一条公交/地铁腿**。

这是「实现了方法 ≠ 服务了该能力」最纯粹的形态：所有单元测试都通过，因为每个部件都正确，缺的是把它们连起来的那根线。

修法是让出行方式可以被**要求**，而不是只能从节奏推导：

- `TripProfile.travel_mode: TravelMode | None = None`。`None` 表示「按节奏推导」，也就是这个字段存在之前所有行程的行为，所以不设置它不改变任何东西；
- `resolve_travel_mode(profile)` 放在 `providers/base.py`，两个 provider 共用。放在共享位置是有原因的：注册表对同一次聚合会同时调用两个 provider，如果它们对方式判断不一致，两边的腿会基于不同假设计算却被合并进同一份行程，而载荷里没有任何东西能对账；
- `mode_for_pace` 保留为 `resolve_travel_mode` 的兜底分支，并且有一条测试专门钉住「它永远返回不了 transit」这个事实，防止将来有人再直接调用它而让能力重新变成死的。

接口层：`/trips` 建单与 `PATCH` 都接受该字段；网页表单多了一个「城际交通」选择器（跟着节奏走 / 公交地铁 / 步行 / 驾车 / 打车）。选「公交/地铁」而没有高德 Key 时不会报错——OSRM 会**拒绝**而不是拿驾车数据顶替，路程由规划器估算并标注为估算，矩阵里也一直是「缺失」。

而「估算」这个披露**不足以说明原因**。它是行程级的一条计数（`7 leg(s) use distance-based travel estimates`），没有把结果和造成它的那个请求连起来；一个明确选了「公交/地铁」的人看到估算的分钟数，最自然的理解就是这些数字是实测的。所以 `_disclose_unserved_travel_mode()` 把原因接到结果上，形状与能力矩阵的缺口说明一致——**你要什么、实际发生了什么、怎样才会变**：

> 你要求按「公交 / 地铁」规划，但没有可用的数据源能算这种路线，因此本行程的路程时间是按距离估算的，不是实测结果。接入高德 Web 服务 Key（公交/地铁是免费源完全没有的能力）之后，这些时间会变成实测值。

三个约束写进了测试：

1. **只在显式请求时出现。** 由节奏推导出的方式（`travel_mode is None`）是规划器自己的默认选择，不是「没被服务」，而且「N 段按距离估算」那条已经覆盖了它；在那里加警告等于给零配置的默认路径平白加一条免责声明。
2. **补救办法按方式区分。** 错的补救比没有补救更糟——它让人去配一个根本不会起作用的凭据。告诉一个要求「步行」的人「公交/地铁需要高德 Key」就是这种错误：免费路线源本来就有真实步行 profile。
3. **命名来自枚举旁边的 `TRAVEL_MODE_LABELS`。** 有一条测试对 `TravelMode` 全体断言每个成员都有中文名，所以将来加成员却忘了起名会在测试里失败，而不是让 `travel_mode: bus` 这种标识符混进中文句子。

`tests/unit/test_travel_mode_disclosure.py` 钉住以上三点，并覆盖「被服务时不出现」与「不覆盖行程已有的其他结论」两个方向。

`tests/unit/test_amap_routes.py` 里有一个回归测试 `test_a_transit_request_actually_queries_the_transit_endpoint`，断言请求打到 `/v5/direction/transit/integrated` 且**没有**打到 `/v3/distance`——后者正是「用驾车时长冒充公交」的失败形态。

## 路线点数预算与「只算了一部分」

`AMAP_MAX_ROUTE_POINTS`（默认 25，可配置）决定一次行程里高德会为多少个地点算路线。超出预算的地点，其路程由规划器估算。

两点要交代清楚：

1. **预算是用户的成本决定，所以可配。** 免费个人配额是月调用量，每个超出矩阵扫掠的点都多一次请求。默认值刻意与免费 OSRM provider 的预算一致，这样「填了 Key」永远不会让**能被计算的路段变少**——否则接入高德反而退化，而用户不会知道。
2. **被截断时要如实说出来。** source reference 里同时记录 `points_offered`、`points_routed`、`point_budget` 与 `truncated`，并在 `note` 里写成一句人话（「本次只对 N 个地点计算了路线（共提供 M 个）」）。只有 `point_count` 一个数字时，客户端会把 5 个点当成整趟行程的规模；而带 `truncated` 的载荷让「这段是实测的」和「这段是估算的」不再长得一样。

`/v3/distance` 每次请求最多 100 个 origin（高德文档）。这是**协议属性而非偏好**，所以它是 `providers/amap/routing.py` 里的常量 `AMAP_MAX_MATRIX_ORIGINS`，并且**结构性钳制** `max_points` ——和 OSRM provider 钳制自己的坐标上限是同一个做法。做成设置项反而有害：把它调高会发出高德必然拒绝的请求，一次扫掠的所有路段一起丢失。

## 三个 Provider，三个注册名

`build_amap_providers()` 返回三个对象，各自有独立的注册名：

| 注册名 | 能力 |
|---|---|
| `amap-routing` | `calculate_routes`（驾车 / 公交地铁 / 步行；公交地铁需在行程里显式选择 `travel_mode`） |
| `amap-places` | `search_hotels`、`search_restaurants`、`search_attractions` |
| `amap-weather` | `search_weather` |

不能共用 `"amap"`：注册表以名字为键，共用会让**只有最后一个被注册，其余被静默丢弃**——没有异常、没有日志，规划只是数据变少。`AMAP_PROVIDER_ID = "amap"` 仍是**来源** id（设置页与清单用它），`provider_names` 则列出上面三个真实注册名。

三个对象共享一个 `AmapClient` 与一个 `AmapGeocoder`：共享客户端才能共享限速与连接池；共享地理编码器意味着一次行程只解析一次城市（天气端点要 `adcode`，只有地理编码能给）。
