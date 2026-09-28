# 路线：哪个服务器、哪个 profile、时长是实测还是推算

路线质量是接入高德的首要理由，但在高德之前，免费的 OSRM 已经在服务驾车与步行。这一层最容易出错的地方不是「能不能算」，而是**你问的东西和你拿到的东西是不是同一个**。

## 问题：服务器不会因为做不到而报错

OSRM 的 profile 写在 URL 里：`/table/v1/{profile}/{坐标}`。直觉上，问一个它没有的 profile 会失败。**公共 demo 服务器不是这样**：`/table/v1/foot/...` 返回 HTTP 200，并且返回与本车完全一致的结果。

2026-09-28 实测，西湖 → 灵隐寺（直线约 5.5 km）：

| 服务 | profile | 距离 | 时长 | 折算速度 |
| --- | --- | --- | --- | --- |
| FOSSGIS | `foot` | 8386 m | 6708 s | 4.5 km/h |
| FOSSGIS | `bike` | 8723 m | 1866 s | 16.8 km/h |
| FOSSGIS | `driving` | 9479 m | 706 s | 48.3 km/h |
| demo | `driving` | 9479 m | 706 s | 48.3 km/h |
| demo | `foot` | 9479 m | 706 s | **48.3 km/h** |

最后一行就是要防的事：相信它，就会在行程里写下「步行 12 分钟，9.5 公里」。这是**编造的事实**，而且是最贵的那一类——它直接决定用户一天能走几个景点。

而注意 FOSSGIS 的 `foot` 距离（8386 m）与 `driving` 距离（9479 m）**不同**。这个差异是 profile 为真的证据：行人走的是另一套路网。

## 三类信息，不能混

修法的核心是把三件事分开，任何一件混进另一件都会产生错误的数字：

| | 是什么 | 来源 | 缺了怎么办 |
| --- | --- | --- | --- |
| **距离** | 路网的属性 | 任何 profile 的路网都是真实路网 | 只能放弃 |
| **时长** | **模式**的属性 | 只有该模式自己的 profile 才有 | 按该模式的速度推算，并标注 |
| **覆盖** | 工具能不能给出这条腿 | 由前两者共同决定 | 见下 |

所以对「驾车专用服务器上的步行」这个组合：

- **距离借驾车路网**——同一批街道，只是少了行人专用道，作为路网距离是合理的；
- **时长绝不借**——4.5 km/h 与 48 km/h 不是同一个量，按 1.25 m/s 推算并 `is_estimate=True`;
- 结果仍然是一条**可用**的步行腿，比直接拒绝更有价值，因为距离确实是测出来的。

公交/地铁没有这种代理关系：公交既不按驾车路线走，距离也不一样。**所以它被拒绝，而不是拿驾车数据顶上。** 这正是高德不可替代的地方。

## 服务器登记表

`apps/api/src/travel_api/providers/free/osrm.py` 里的 `PUBLIC_ROUTING_SERVERS` 记录了每个部署真正支持的 profile：

```python
PublicRoutingServer(
    host="routing.openstreetmap.de",
    prefixes={"driving": "/routed-car", "bike": "/routed-bike", "foot": "/routed-foot"},
    label="FOSSGIS e.V.",
    freshness_note="OSM 路网数据约每两天重建一次（FOSSGIS 官方说明），比实时路况旧",
)
```

FOSSGIS 每个模式一个实例，路径前缀不同；demo 只有一个驾车实例挂在根上。用 `prefixes` 而不是假设 URL 形状，两者才能被同一段代码描述。

**默认用 FOSSGIS**，因为它在同样的使用政策下提供**更多**：demo 对每个请求都只回驾车路线，所以挂在 demo 上步行永远只能是推算；FOSSGIS 有全球真实步行与骑行 profile。两者的政策形状相同（有效 User-Agent、每秒最多 1 次请求、不得抓取），`free/http.py` 已经把限速实现好了。

**未登记的服务器按「只有驾车」处理。** 这是刻意的悲观默认：我们无法验证自建服务有没有步行 profile，而猜错的代价是把驾车时长写成步行。运维可以用 `OSRM_HONOURED_PROFILES` 显式声明，`OSRM_BASE_URL` 指向哪里也同样清楚。

## 时长来源写在数据里

每条腿的 `SourceReference.request_context` 都带：

```json
{
  "osrm_profile": "foot",
  "osrm_server": "routing.openstreetmap.de",
  "osrm_server_label": "FOSSGIS e.V.",
  "duration_source": "osrm_foot_profile",
  "duration_is_measured": true,
  "walking_speed_mps": null,
  "profiles_honoured": ["bike", "driving", "foot"],
  "data_freshness": "OSM 路网数据约每两天重建一次（FOSSGIS 官方说明），比实时路况旧"
}
```

推算时则是 `"duration_source": "derived_walk_speed"` 与 `"walking_speed_mps": 1.25`。这样**单看一条腿**就能判断它是观测还是推算，不需要知道部署指向了哪台服务器。

`data_freshness` 在这里不是装饰。产品要求是「只能用近期的数据」，而一条路线的新鲜度上限由路网图的更新频率决定，不由请求时刻决定——请求是实时的，但图可能已经两天没重建。把 FOSSGIS 官方说明原样记录，比转述成「实时」诚实。

## 能力矩阵上步行从 MISS 变成 ok

`OsmRoutingProvider.UNSERVED_ASPECTS` 从模式表**推导**，不是手写：

```python
{self.ROUTING_CAPABILITY: frozenset(declared - served)}
```

所以日后给 `TravelMode` 加一个 `bike` 成员并配上 profile，`cycling` 会自动从「缺失」里消失，不需要谁记得来改这里。

两个仍然缺失的，原因各不相同，且都是真的：

- **公交/地铁**：没有任何公共 OSRM 部署有班次数据。高德能补，所以它走 `uncovered_aspects` + 解锁提示，而不是 `capability_gaps`。
- **骑行**：FOSSGIS **有** `bike` profile，但 `TravelMode` 没有 `bike` 成员，`mode_for_pace` 永远不会返回它，所以任何行程都不可能包含骑行腿。矩阵回答的是「这个工具能不能规划它」，不是「服务器能不能算它」——因为服务器支持就报成已覆盖，跟把一个永远返回空的桩算作覆盖是同一种错误。

## 实测（真实数据，杭州）

`scripts/probe_live_routing.py`，2026-09-28，relaxed / full 各 5 个真实 POI、20 条腿：

```
=== 步行（relaxed） — 5 points, 20 legs ===
walk       1437 m      19 min    4.5        False  osrm_foot_profile
walk       4608 m      61 min    4.5        False  osrm_foot_profile
...
measured durations: 20/20

=== 驾车（full） — 5 points, 20 legs ===
drive      1455 m       2 min   43.7        False  osrm_driving_profile
drive      5508 m       6 min   55.1        False  osrm_driving_profile
...
measured durations: 20/20
```

步行 **20/20 为实测**，速度落在 4.4–4.6 km/h。改动之前（挂在 demo 上）这 20 条全部是推算。

## 钉住这条规则

`tests/unit/test_free_providers.py`：

- 真实 foot profile 上的步行时长是**观测**（`is_estimate=False`，`osrm_foot_profile`）；
- 驾车专用服务器上的步行时长是**推算**（`is_estimate=True`，`derived_walk_speed`，1.25 m/s），且 ~40 km/h 的驾车数字**没有**渗进去；
- 每个模式向支持它的部署请求**自己的 profile**：`["foot", "driving"]`；
- 不认识的主机按驾车处理，且可被显式声明修正；
- 公交被拒绝，不返回驾车时长；
- **声明与行为对比**：遍历模式表，provider 声称服务的每个 aspect 都必须真的产出腿，声称不服务的必须真的拒绝。

最后一条是原来那次「多报覆盖」缺陷所缺的东西——当时没有任何地方把声明和代码实际产出对起来看。

`tests/integration/test_free_providers_live.py` 则要求在真实数据上：步行速度落在 2.5–6.5 km/h，且 `is_estimate` 必须与 `duration_is_measured` **一致**（两个方向都查）。只断言 `is_estimate is True` 会把驾车专用服务器的行为钉死，在真能测量步行的服务器上误报失败；只查速度则会让推算被当成实测。

## 优化器起点：有 id 才有坐标

`RouteOptimizer.optimize` 收 `hotel_id` 与 `hotel_coordinates` 两个参数，而优化器只在 `hotel_id is not None` 时才使用坐标——因为一条腿的起点必须**同时**是一个可查询路线的主体（按 id 找 leg）和一个可推算距离的点（按坐标算 haversine）。只有坐标没有 id，就会得到一个「从一个不在行程里的地方出发」的时长。

调用方过去在没有选中酒店时传 `(0.0, 0.0)`：几内亚湾的 Null Island，离成都约 11000 公里。它当时无害，仅仅是因为三行之外那个 id 守卫——**一个看起来真实的值，靠着别处的判断才安全**。守卫哪天变了，每个没有酒店的行程的第一段路就会默默地从几内亚湾开始量，而界面上没有任何迹象表明一个占位值变成了真值。

现在调用方传 `None`，类型是 `tuple[float, float] | None`，并由 `test_no_hotel_means_the_first_leg_is_not_measured_from_a_placeholder` 钉住两件事：给了坐标却没有 id 时不得据此推算距离；`None` 与占位值的行为必须一致（否则占位值是承重的）。变异验证：去掉那个 id 守卫，测试失败。

## 打分用的「中心」也曾经是写死的

`score_hotel_offers` 曾把「这家酒店方不方便」量成到 `(30.6574, 104.0817)`（成都春熙路）的距离，并偏好 `{"市中心", "锦江区"}` 这两个区名——两者都是 `providers/mock.py` 里夹具数据的属性。非成都的行程因此拿到 transit 恒为 0.0（一个没有信息量的维度，却占 20% 权重）、以及一句「距中心行程点约 1600000 米」。详见 `decision_engine.py` 的模块文档，以及 `tests/unit/test_hotel_scoring.py`——那 12 个测试**全部**使用非成都目的地，因为原来所有测试都用成都，而用常量的同一个城市写的断言不可能失败。

## 同一个模块里的第三个：两套词汇表之间的相等比较

修完中心之后，同一段代码里还剩一个同形的缺陷：`itinerary_fit` 用

```python
0.9 if hotel.location.city == profile.destination else 0.2
```

判断「这家酒店是不是在行程所在城市」。**旅行者输入的是 `丽江`，Nominatim 的 `address.city` 是 `丽江市`，而高德是用旅行者自己的文本去查、再原样回传。** 于是相等比较对免费源的每一家酒店都答「不是同一个城市」，对高德的每一家都答「是」。

后果不是文案问题，而是**加了 Key 就换了排序**：同一家酒店，配不配 Key 分别得 0.2 和 0.9。区的比较更糟——Overpass 用 OSM 的 `suburb`（「西湖街道」），高德用 `businessarea`（「西湖」），两者描述同一片地方却字面不同。

`services/normalization.py` 的 `same_place_name()` 现在做这件事，并且是**三值**的：缺失的名字返回 `None`（无法判断），而不是 `False`（「是另一个地方」）。把这两种情况合并，正是本仓库反复出现的缺陷。五个自治区（新疆维吾尔自治区 → 新疆 等）用一张写死的五条表处理，而不是写一条会去猜中文语法的正则。

两个测试上的教训：

- 比较**两套词汇表**，就必须在**两套词汇表之间**测。我给区比较加的折叠逻辑，一开始被一个「把相等比较改回去」的变异完全绕过——因为原有两个测试两侧用的是同一个拼法，折叠与否看不出差别。现在补了一条「高德拼法 vs OSM 拼法」的测试，那个变异才被抓住。
- `ScoreBreakdown` 之前是手写字段列表拼出来的，而加权和是另一份列表。这就是 `itinerary_fit` 会「被从和里删掉、模型却仍要求它」的原因——好一点是 `KeyError`，坏一点是一份声称用了某个维度的分数。现在 breakdown 直接由 `measured` 构造（`ScoreBreakdown(**measured)`），并且有一条测试**从 breakdown 反算分数**，要求与存储值一致：两个列表若不一致，算术就对不上。
