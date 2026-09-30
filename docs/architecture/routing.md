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
- **骑行**：FOSSGIS **有** `bike` profile，但 `TravelMode` 没有 `bike` 成员，`resolve_travel_mode` 永远不会返回它，所以任何行程都不可能包含骑行腿。矩阵回答的是「这个工具能不能规划它」，不是「服务器能不能算它」——因为服务器支持就报成已覆盖，跟把一个永远返回空的桩算作覆盖是同一种错误。

## 推算时长必须按**模式**，而这曾经是按**配速**

上面那张表里写着「时长：按**该模式**的速度推算」，而代码里推算速度的那一处读的是另一个字段：

```python
speed = (
    _WALKING_METERS_PER_MINUTE
    if profile.pace in (Pace.RELAXED, Pace.BALANCED)
    else _DRIVING_METERS_PER_MINUTE
)
```

`travel_mode` 一次都没被读过。这是模式的**第三次推导**：`resolve_travel_mode`（`providers/base.py`）存在的全部理由就是它只能有一份——两个路由 provider 在一次聚合里都会被问到，各推各的就会在同一份行程里按两套假设算时长。实测（成都，约 13 公里的跳点）：

| 请求 | 用时 | 隐含速度 | 读起来像 |
| --- | --- | --- | --- |
| `pace=FULL` + `travel_mode=walk` | 44 分钟 | **18.2 km/h** | 跑 |
| `pace=RELAXED` + `travel_mode=drive` | 231 分钟 | **3.5 km/h** | 爬 |
| `pace=RELAXED` + `travel_mode=taxi` | 231 分钟 | 3.5 km/h | 爬 |

两个方向都错，而且**两个请求会得到同一个数字**——想走路的人和想开车的人看到一样的时长。

根子上是两个字段被接在了一起：`pace` 说的是**一天的密度**（塞得下几个点），`travel_mode` 说的是**怎么移动**。它们是独立的，而旧代码让前者替后者做了决定。于是 `FULL` 这个「今天想多跑两个点」的选择，静默地变成「假设你开车」。

为什么会漏掉：这句提示本来就存在，内容是「N 段路程按距离估算，不是实测路线」。**它是真的，但不够用**——读者知道这个数不是量出来的，却不知道它是**按什么**算出来的，所以分不出一个合理估算和一个荒谬估算。18.2 km/h 的步行时长就这样一直印在行程里。

修法与上一节同一个形状：速度由一个函数（`assumed_speed_for`）从 `resolve_travel_mode(profile)` 的结果取出，**数字和它的措辞一起返回**，两者不可能描述不同的模式。提示里因此写进了假设的速度本身：「2 段路程按距离估算（步行按每小时 4.5 公里估算），不是实测路线」——错误假设现在是**可读的**，而不只是可推断的。

三条不变式，各有测试：

- **模式决定速度，配速不决定**：同一个模式在三种配速下必须得到同一个数。这条不需要知道任何常量，也不需要阈值，所以它是对这个缺陷最干净的表述；
- **配速派生的默认值一字不变**：未设 `travel_mode` 时仍映射为 RELAXED/BALANCED → 步行、FULL → 驾车。库里已经存着按旧映射算出的行程，改掉它会在下一次修订时**静默改写每一份没有显式模式的计划**；
- **公交/地铁必须落在步行与驾车之间**：断的是**顺序**而不是取值。一个比步行还慢的公交估算会让优化器宁愿让人走十公里过去。

一句顺带的话：`_pace_allowance`（两个端点都没有坐标时那个兜底值）**故意**不读模式。没有距离就没有速度可套，此时按配速给一个「愿意假设多长的间隙」是它名字所说的那件事；拿模式去乘一个不存在的距离，是从偏好里凭空造出一个测量。

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

调用方过去在没有选中酒店时传 `(0.0, 0.0)`：几内亚湾的 Null Island，离成都约 11000 公里。它当时**看起来**无害，因为三行之外那个 id 守卫——**一个看起来真实的值，靠着别处的判断才安全**。

### 但它在两个地方不是无害的

上面那句「看起来无害」后来被证明是错的，而且错的方式正是这段话自己警告的：守卫看的是 **id**，所以只要 id 是真的，占位坐标就会被当真。四个调用点里有两个正是这个组合——真实的酒店 id 配字面量 `(0.0, 0.0)`：

```python
hotel_id = self._selected_hotel_id(preview, trip.selected_hotel_offer_id)
RouteOptimizer().optimize(trip.profile, hotel_id, (0.0, 0.0), ...)  # Null Island
```

于是**每一段没有实测路线的路程都从几内亚湾估时**。复现（2026-10-03 那天的一个武侯祠条目）：

```
travel_from_previous_minutes = 196688        # 136 天
start_time                   = 2027-02-16T23:38
```

更糟的是下游的**归因**：若该景点还带营业时间，条目会被丢掉，而用户被告知「营业时间与可用时段不匹配」。那句提示是对的（两个时间确实对不上），但它说的是**症状**，真正的原因在五个月之外——一个 136 天的路程时间被当成一个真实的早上通勤。

没有测试抓到它，原因有三个，都值得记：

- `add_attraction` / `remove_attraction` **两条修订路径一条测试都没有**（`POST /trips/{id}/revision` 上唯一被测的动作是 `change_budget`）；
- `test_route_optimizer.py` 钉的恰好是**能工作的那一半**——`hotel_id=None` 的守卫，而不是「id 有值但坐标是占位符」；
- 另外两处调用点（`hotel.id if hotel else None` 配 `hotel.location...` 配 `else (0.0, 0.0)`）是对的，所以**同一个模块里两种写法并存**，读代码的人看到哪一处都能找到先例。

修法不是把那两处改成正确的三元表达式，而是让四个调用点共用一个 `_selected_hotel_origin`，**返回一个 `(id, coordinates)` 对**：

- 没有选中酒店 → `(None, None)`；
- 有酒店 → `(id, 真实坐标)`；
- offer 指向一个不在行程里的酒店 → `(id, None)`：id 仍可用来查 leg，坐标**不可知**，所以是 `None`，而这里塞占位值正是本函数存在的原因。

第五个调用点以后不可能再忘掉一个分支，因为分支只有一份。剩下那一处 `_replace_hotel` 传的是**另一家**酒店的坐标，是对的，所以它继续自己构造。

验证方式与上一节同一个道理：缺陷在**调用点**而不在优化器，所以 `tests/integration/test_route_origin.py` 走真实 HTTP 修订端点、断言可观察的后果（路程分钟数有上界、开始时间不越过行程结束日），而不是直接调优化器。`scripts/mutate_route_origin.py` 把两处占位符**放回去**并要求测试失败——事后补写的回归测试必须这样验证，否则它可能因为完全无关的原因通过。

顺带修掉的还有「`None` 与占位值必须行为一致」那条断言的范围：它现在由 `_selected_hotel_origin` 的三种返回值分别覆盖，包括「id 有值而坐标不可知」这个此前不存在的组合。

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
