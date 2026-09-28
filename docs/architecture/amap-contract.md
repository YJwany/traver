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
| **公交 / 地铁** | **完全没有** | **有（v5 transit）** |
| POI / 景点 / 餐饮 | Overpass（OSM tag 体系） | 有，国内覆盖显著更全 |
| 天气 | Open-Meteo | 有（adcode 维度） |
| 酒店**房价** | 无 | **也没有**——高德只提供酒店 POI，不提供房价 |

最后一行是刻意写明的：高德的 `search_hotels` 能力指的是**找到酒店**，不是**给出房价**。因此清单里高德不声明 `search_room_offers`，避免用「能找到酒店」暗示「有实时房价」。
