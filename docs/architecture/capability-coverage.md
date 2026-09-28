# 能力覆盖：只报告真的能拿到数据的来源

Traver 不带数据。所以「哪些来源接上了」就是「这个程序能做什么」的全部答案，而设置页的能力矩阵是用户唯一会去看的那一屏。这一屏一旦多报一分，用户就会按一个不存在的承诺去规划。

本文记录这条规则，以及它所修掉的一个真实缺陷。

## 缺陷：实现了方法 ≠ 提供了能力

Overpass（OpenStreetMap）一个 provider 同时服务酒店、餐厅、景点，因此它必须实现 `RoomOfferProvider` 协议里的 `search_room_offers`。它的实现是**刻意返回空结果**：

```python
async def search_room_offers(self, hotel_ids, profile) -> ProviderResult:
    # OpenStreetMap carries no rates or availability. Returning an empty
    # success keeps the hotel component explicitly estimated downstream
    # instead of inventing a price.
    return ProviderResult(provider=self.name, data=[], source_references=[])
```

这个行为是对的——编一个房价比不返回更糟。但注册表用 `isinstance` 判断谁能服务某个能力，而 `isinstance` 分不出「真的会给数据」和「永远返回空」。于是能力矩阵报告：

```
[FULL] search_room_offers   providers=['openstreetmap']
```

而**这个部署里没有任何来源能产生一个房价**（高德也没有接入，且它声明的能力里不含房型报价）。设置页因此宣称「房型与报价：完整可用」，而它的 manifest 注意事项里同时写着「不含房价与库存」。同一个页面上自相矛盾，且偏向于吹嘘。

更糟的是，这条缺陷被一个断言钉住了：

```python
assert payload["uncovered_capabilities"] == [], \
    "the default deployment should leave no capability uncovered"
```

测试在保护 bug。它通过，只是因为那个永远返回空的桩被算作了覆盖。

## 三层修复，因为任何一层单独都可能错

| 层 | 机制 | 防止什么 |
| --- | --- | --- |
| Provider | `UNSERVED_CAPABILITIES = frozenset({"search_room_offers"})` | 来源**能说**「这个我答不了」。没有这个声明，其他层无从得知 |
| Registry | `serving_providers_for()` ≠ `providers_for()` | 把「能被调用」和「会给出答案」分开 |
| Manifest | 从 openstreetmap 的 `capabilities` 里移除 `search_room_offers` | 目录不再声明 provider 拿不出的能力 |
| 目录 | `capability_gaps` 带原因 | 用户看到的是「缺什么、为什么、要接什么」，不是「缺 1 项」 |

### 为什么两个注册表方法必须都存在

它们回答不同的问题，混在一起会两边都坏：

- **`providers_for(method)`——结构性的，用于分发。** 桩必须**继续被调用**：它靠返回空结果来拒绝，而这个空结果本身有用——它出现在 `provider_audit` 里，表示「我们问过，那边没有」。这和「我们从没问过」是两件事。
- **`serving_providers_for(method)`——声明性的，用于报告。** 面向用户的一切都必须用它。问结构性问题就是上面那条假话的来源。

### 为什么用「声明」而不是「看返回结果是否为空」

一个自然的想法是运行时探测：「调用一次，返回空就当作不服务」。否掉它的理由：

1. **需要网络请求。** 设置页会变成一次真实调用，可能花掉几十秒，还依赖第三方可用性。
2. **空结果有合法含义。** 一个真的能报价的来源，对某个特定城市完全可能返回空（该城市无房源）。把「这次没有」当成「永远没有」会让能力在矩阵里随机闪烁。
3. **声明来自写代码的人。** 只有他知道那个 `return ProviderResult(data=[])` 是「我没有这个数据」还是「临时查不到」。

## 报出来的缺口长什么样

```
[FULL] search_hotels       providers=['openstreetmap']
[NONE] search_room_offers  providers=[]
[PART] calculate_routes    providers=['osrm']
         ok  driving      驾车      ['osrm']
         MISS transit     公交/地铁  []
         MISS walking     步行      []
         MISS cycling     骑行      []
```

`uncovered_capabilities` 是个 id 列表（计数用），`capability_gaps` 带上原因：

```json
{
  "capability": "search_room_offers",
  "label": "房型与报价",
  "note": "房型与实时房价只存在于商业酒店渠道（携程 / Booking / Expedia 的开放接口，或高德的酒店开放平台），本工具尚未接入任何一家。因此预算里的住宿部分是估算额度，并已标注为估算——它不会拿一个编造的房价冒充实时报价。"
}
```

两者分开是刻意的：**「没人能提供」和「你还没配置」的补救方式完全相反**。公交地铁属于后者——高德能提供，只差一个 Key，所以它走 `uncovered_aspects` ＋ 解锁提示，绝不出现在 `capability_gaps` 里。把可解的缺口标成死局，等于让用户放弃一个 Key 就能解决的事。

## 这句话是不是真的

上面那条 note 断言了行为：**预算里的住宿是估算额度，并且标了是估算**。这是关于行为的断言，所以它被验证而不是被相信：

```
--- 没有任何来源能报价 ---
  hotel   1600  status=estimated  is_estimate=True   refs=0
  food     480  status=estimated  is_estimate=True   refs=0
  ...
--- 有一个真实报价 ---
  hotel   1800  status=observed   is_estimate=False  refs=1
  food     480  status=estimated  is_estimate=True   refs=0
```

两个方向都有测试钉住（`tests/unit/test_budget_strategy.py`）。**只钉一个方向是不够的**：一个永远为 `True` 的标记不携带任何信息，而原本唯一的反向覆盖在一个需要联网的测试里——联网不可用时就跳过了。

## 防止复发

`tests/unit/test_capability_coverage_honesty.py` 在每一层都钉住这条规则，因为每一层单独都可能出错：

- provider 声明了它答不了的能力；
- 声明里的名字是**真实存在的能力**（`{"search_room_offer"}` 少个 s 会静默无效，比报错更糟）；
- 两个注册表方法对「未声明」的 provider 给出相同答案（过滤只能移除声明过的）；
- 没有免费来源被算作提供房型报价；
- 缺口被命名并解释；
- 只能靠凭据解决的缺口**不**被报成死局；
- manifest 声明的每个能力都有 provider **真的**服务它——**逐能力**检查，不是逐来源。只断言「这个来源有某个已注册 provider」在它声明的那个能力无人服务时依然通过，而原始 bug 正是这样活下来的。

两条突变都被测试抓住：把 provider 的声明清空 → 2 个测试失败；把 manifest 的错误声明加回来 → 5 个测试失败。两个修复互相独立，所以单独撤掉任一个，覆盖结论仍然正确——这是有意的纵深防御。

## 顺带一提：高德的房型

高德的 Web 服务 API **不提供**房型与实时房价（POI 搜索返回的数据里没有房价/评分/评论数/门票价）。要拿到房价需要接入商业酒店渠道。所以 `search_room_offers` 在当前 catalogue 里**没有**任何 `needs_credentials` 的候选——这也是为什么它进的是 `capability_gaps` 而不是解锁提示。
