# Traver 交接文档

> 生成时间：本文件最后写入时。**所有"已验证"的数字都是本轮实际跑出来的**，不是回忆；
> 没有验证的一律标注为未验证。

---

## 1. 这是什么

**用一句话**：Traver 是一个「自带密钥、插件化」的旅行规划聚合工具 —— 它自己不含数据，
所有数据源（高德 / OSM / 天气 / OSRM / 小红书……）和 AI 模型都由使用者自己接。

用户原话（产品身份，贯穿所有决策）：

> 咱们目前做的其实跟旅途星辰差不多就是一个相当于汇总的工具，里面的模型以及各种api都需要自己接……
> 这就是一套工具，然后需要什么都需要自己接。

> 你提供的越多那么安排的就越详细，提供的少也会有攻略只不过要差一点。

对标对象：旅途星辰（`1sdv/TripStar`）。**没有拷贝过它的任何代码**，只是功能对标。

### 三条硬性要求（用户明确说过，优先级最高）

1. **时效性** ——「旅游做攻略肯定都是针对于未来的，只能用近期的评价或者什么都是近期的，
   不能是那种几年前那种」。
2. **路线一定要好** —— 这是接高德的核心原因。
3. **每份数据必须如实标注抓取时间** ——「什么时候拉取到的数据就标注为什么时候的」，
   并且「不要吹嘘你懂吧」。

第 3 条是本项目一切设计的约束：**宁可说"不知道"，也不要把旧数据/缓存说成实时的**。

---

## 2. 仓库与运行方式（全部已验证）

- 仓库：`https://github.com/YJwany/traver`（private），分支 `main`，用户 `YJwany`
- 工作目录：`C:\Users\27875\Desktop\Traver`

### 环境约定（PowerShell）

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONPATH='C:\Users\27875\Desktop\Traver\apps\api\src'
# 需要 gh / npm / npx 时，前面补上 PATH：
$env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path','User')
```

### 后端

```powershell
python -m ruff format .            # 203 files left unchanged
python -m ruff check .             # All checks passed!
python -m pytest tests/ -q         # 1020 passed, 8 skipped（2026-10-14 实测）
```

API 在 `/api/v1`，存活探针 `/api/v1/health`。

### 前端（`cd apps/web`）

```powershell
npx tsc --noEmit     # exit 0
npx next lint        # ✔ No ESLint warnings or errors
npx vitest run       # 232 passed (16 files)（2026-10-14 实测）
npx next build       # exit 0，7 条路由
```

构建路由：`/`、`/ingestions`、`/settings`、`/trips`、`/trips/new`、`/trips/[id]`（动态）。

### 迁移

alembic 漂移检查通过（跑完记得删掉工作区根目录生成的 `local-drift.db`）。

---

## 3. 当前 git 状态 —— ⚠️ 有 5 个提交没推上去

**原因：网络出口不通。** `Test-NetConnection github.com -Port 443` → `False`，
`Invoke-WebRequest` 对 `github.com` / `api.github.com` / `pypi.org` 都失败，
`git push` 报 `schannel: failed to receive handshake`。

未推送（都在本地 `main`）：

| commit | 内容 |
| --- | --- |
| `ea37a2c` | 按**模式**而不是配速推算时长（走路 4.5 / 公交地铁 15 / 打车驾车 24 km/h） |
| `774919b` | 让 mutation harness 无法把变异泄漏到源码里 |
| `27c0f6e` | 让五个预算策略真正产生差异 |
| `0da3dbb` | 没有评分来源时不再写 `0.0` |
| `d948a3d` | 两个"会过期"的测试 + 时钟平移检测工具 |

`origin/main` 停在 `163d8e3`（"Give reviews a date, because the requirement was about dates"）。
**网络恢复后第一件事：`git push`。** 注意 `git push` 在 pwsh 下会打 `[exit code: 1]`
但其实成功 —— 看有没有 `main -> main` 那行。

**⚠️ 工作树现在并不干净**（2026-10-14 更正：之前这份文档写的"工作树干净"当时就已经不对）。
未提交的内容包括本轮的 RollingGo 接入，以及更早遗留的一处：

- `M tests/integration/test_planning_job_api.py` —— 新增
  `test_the_explicit_owner_is_honoured_with_no_ambient_owner`（mutation harness 发现的缺口）
- 本轮的改动见 §13
- `HANDOVER.md` 自身**未被 git 跟踪**（`?? HANDOVER.md`）

提交前请先确认这两部分是要一起提交还是分开。

---

## 4. 七个子目标的状态（诚实版）

| # | 子目标 | 状态 |
| --- | --- | --- |
| 1 | Provider 清单 + 能力注册表 | **完成**。加数据源不用改代码 |
| 2 | 凭据保险箱（AES-256-GCM，按属主隔离） | **完成** |
| 3 | 自动渲染的设置页 + 能力矩阵 | **完成**（渐进增强：0 配置可用） |
| 4 | 高德 Provider + 无 Key 回退 OSM | **代码完成，未用真 Key 验证过** |
| 5 | 时效性硬约束 | **完成**，见 §5 |
| 6 | 通用内容摄取（书签工具 + 粘贴） | **完成** |
| 7 | 体验对齐 | **部分完成**，见下 |

第 7 项逐条：

- 异步任务 + 进度推送：**完成**（`planning-jobs.md`；注意 job registry 是**进程内**的，
  多 worker 需要共享存储，已记录在文档里）
- 导出：**完成**（Markdown / JSON / CSV / ICS + 前端导出面板）
- 多源交叉验证：**完成**（`cross-source-validation.md`、`disagreements` 面板）
- 可视化：**只有示意图，不是地图**。没有底图、没有瓦片服务、没有投影。
- **多城市：不存在**（已 grep 确认，API 源码里没有任何多城市字段）
- **i18n：不存在**（已 grep 确认，`apps/web` 源码里没有 `i18n` / `useTranslation` /
  `locale` / `next-intl` 的任何使用；只有 `node_modules` 里的类型定义命中）

---

## 5. 时效性（第 5 项）的实现要点

### 能力注册表：10 个能力

`CAPABILITY_BY_METHOD` 有 10 项（`providers/base.py:210`）：

```
search_hotels, search_room_offers, search_restaurants, search_attractions,
calculate_routes, search_weather, search_holidays, enrich_attractions,
resolve_places, search_reviews
```

两个查询函数的区别很关键：

- `providers_for(method)` —— **派发**用，宽松（不认识的方法回退到所有 provider）
- `serving_providers_for(method)` —— **上报**用，排除 `UNSERVED_CAPABILITIES`

「结构上实现了」≠「真的能提供」：`search_room_offers` 存在于接口上是因为有的源确实报价，
没有报价的源必须返回空结果而不是编一个价格。空结果是对的，但那不是覆盖率，
`isinstance` 分辨不出这两者 —— 这就是 `UNSERVED_CAPABILITIES` 存在的原因。

### 第 10 个能力 `search_reviews`：**已建模、已路由、已算年龄、已上报，但没有源能提供**

这是当前最诚实也最容易被误读的状态，有测试 `test_no_shipped_source_serves_reviews` 钉住：

- 没有任何免费源发布住客评分。OSM 的 `stars` 是**自我声明的分类**，不是客人评分；
  高德把 `rating` / `review_count` 列在自己的 `absent_fields` 里。
- 所以 `Hotel.rating` / `Restaurant.rating` 在真实部署里**永远是 `None`**。

### 两个判断（有测试 + mutation 钉住）

1. **没有日期 ⇒ `None`，不是 `0`。**
2. **年龄由 aggregation 从被接受的评价里推导，不信任 provider 报的。**

### `ElementAge` —— 唯一的年龄统计口径

`ElementAge.of(moments, *, now, horizon_seconds, member_kind=EDITED)`，
`None`（不是一堆 0）当没有任何成员带时间戳时。OSM 和评价两条路径共用它。

`ElementAgeKind` 区分 `EDITED`（OSM 元素，措辞「最后修改于」）和
`PUBLISHED`（评价，措辞「写于」）—— 因为 OSM 元素是 wiki，会被反复编辑，
说它「写于」是错的。

### `MAX_AGE_SECONDS`（缓存 TTL ≠ 数据年龄）

```
weather 3600 / route 3600 / hotel_availability 1800 / hotel_offer 43200
restaurant 86400 / hotel 86400 / attraction 86400
review 18*30*86400 = 540 天
```

`user_content` 和 `place_lookup` **故意不在表里**（`max_age_for()` 返回 `None`），
两者原因不同，`freshness.py` 里都写了注释说明 —— 这一点很重要，因为
`aggregation.py` 确实会发出 `("place_lookup", ref)` 引用，缺 key 意味着这些引用被
`audit` 完全跳过。

---

## 6. ⚠️ 本项目反复产生的缺陷家族（最高价值的交接内容）

这一节比代码更重要。下面每一条都是**实际发生过**的，不是理论。

### 家族 1：一套程序其余部分无法产出或接受的词汇

一个地方定义了枚举/字符串，另一个地方处理不了它，于是回退到某个"看起来还行"的行为：

- 不认识的 step 名 → 直接打印原始标识符
- 不认识的 `JobStatus` → `isTerminal` 为 false → **永远轮询下去**
- 不认识的 chat role → 空面板
- 不认识的 `ObservationBasis` → 「数据时间未知」

**检验方法：要测两个词汇表之间的比较，就要跨越词汇表去测。**
现有守卫：`test_web_api_contract.py`(26)、`test_place_name_matching.py`(17)、
`test_hotel_scoring.py`(21)、`test_route_protection.py`，以及
`probe_shared_vocabularies.py`。

### 家族 2：一个"常量"其实只是某个 fixture 的属性

已抓到过的具体实例：

- 成都市中心坐标硬编码在 `decision_engine` 里
- 城区集合 `{"市中心", "锦江区"}`
- `(0.0, 0.0)` 当作酒店起点（Null Island）
- 地图面板 bounds `30.6/104.0`
- `city == destination`
- 「快照通常只有几分钟」

**为什么长期不可见：写在与 fixture 同一假设下的测试不可能失败。**

### 家族 3（本轮新发现）：fixture 里是绝对时刻，断言却拿它和"现在"比 → 测试会在某个日历日过期

两个实例，都是在**本轮**发现的：

- `data_age_seconds < 3 * 86400` → 3 天后变红（就是今天）
- `date(2027, 6, 1)` 当作 "far future" → **2027-05-16** 之后失效

而且第一个还有第二个引信藏在后面：它的注释说自己是"按比例而不是绝对值断言的"，
但下一行就是绝对值；再下面两行的**比例**同样会衰减（分子分母都在随 now 增长，
比值趋向 1）。**真正不变的是差值** —— 因为它是两个 fixture 常量相减。

工具：`scripts/clock_shift_plugin.py`

```powershell
$env:CLOCK_SHIFT_DAYS='730'
python -m pytest tests/ -q -p clock_shift_plugin
```

已验证：`+730` 和 `+3650` 天都是 **992 passed, 8 skipped**。

**只有向前平移有意义。** 向后平移报的是假阳性（`data_age_seconds` 会在 0 处 clamp；
`expires_at=datetime(2020,1,1)` 这种"钉住过去"的 fixture 会不再处于过去）——
这个不对称性写在插件 docstring 里了，别再花时间重新发现一遍。

### 家族 4：一个地方写了"从没测到就是 None"，另一个地方写了 0

`ScoreBreakdown.rating` 的 docstring 明说 `None` 表示"没测到"、
`0.0` 读起来是"最差"，所以**故意不用 0.0**。而 `decision_engine` 写的是
`(hotel.rating or 0) / 5`。

这不只是记错一个字段：总分是按"实际测到的维度"归一化的，所以那个 0 还坐在分母上，
把每个分数都压低了 —— 对一个**沉默的来源**施加了静默惩罚。

**为什么没有测试抓到**：`providers/mock.py` 返回 4.8 和 4.6，
而 `test_hotel_scoring.py` 里每个测试都用那个 fixture —— 它带着一个**真实世界不存在的字段**。

### 家族 5：一个"文档里写了的行为"，实际没有代码产生它

五个预算策略（`strict`/`balanced`/`comfort`/`time_first`/`food_first`）实际上是
**同一个行为的五个名字**：四个权重算出来后没有任何地方读过，`calculate` 只用了 `name`
当标签。同一个 profile 下五个策略返回**完全相同**的总价和逐项金额。
而它是用户可选设置 —— 有文档、可选、且完全无效。

### 家族 6：mutation harness 会撒谎

这个项目里 harness **已经骗过四次**：

1. 通过/失败极性搞反
2. 没有逐 case 还原 → case N 跑在 case N-1 的变异上
3. 测试列表漏掉了产生者（钉住消费者，两个产生者都没覆盖）
4. **本轮**：harness 因为 case 元组格式错误崩溃，刚写上去的变异**留在磁盘上**；
   下一次运行把它读成"原始文件"，还原步骤又把变异写了回去 ——
   等于**把自己的修复回滚掉并报告成功**

第 4 条促成了 `scripts/_mutation_harness.py`：所有 harness 现在共用它，
它会在**碰任何文件之前**校验 case 列表、从**进入时写的 sidecar 备份**还原
（不信任磁盘上当前的内容）、在 `finally` 里还原、并对每个不同的测试目标都先验证 baseline。

**一条铁律：没有触发的变异，和一个不工作的守卫，从外面看是一样的。**

现有 9 个 harness，共 **46 个变异，全部 caught，无幸存者**：

| harness | 变异数 |
| --- | --- |
| `mutate_reviews_capability.py` | 12 |
| `mutate_rollinggo.py` | 10 |
| `mutate_budget_weights.py` | 9 |
| `mutate_travel_mode_speed.py` | 6 |
| `mutate_decision_engine.py` | 4 |
| `mutate_catalog_fixture.py` | 3 |
| `mutate_route_origin.py` | 2 |
| `mutate_refresh_jobs.py` | — |
| `mutate_web_api_contract.py` | 4 |

---

## 7. 最近修掉的两个真实 bug（都在本轮）

### 7.1 路线起点：拿真实酒店 id 配 `(0.0, 0.0)`

`RouteOptimizer.optimize` 需要 hotel id 和坐标**要么都有要么都没有**；它的守卫读的是
**id**，所以「真 id + `(0.0, 0.0)`」是唯一危险的组合。

复现：`travel_from_previous_minutes = 196688`（136 天），
`day_date = 2026-10-03` 却算出 `start_time = 2027-02-16T23:38`；
配上 09:00–18:00 的营业时间，这一项会被**丢掉**，并且归咎于
「营业时间与可用时段不匹配」—— 也就是把 bug 伪装成营业时间问题。

只有 `add_attraction` / `remove_attraction` 受影响；`_recalculate_restaurants`
和 `_change_pace` 本来就是对的 —— **所以模块里同时存在两种写法**。

修法：`_selected_hotel_origin(preview, offer_id) -> (UUID | None, (float,float) | None)`
统一返回三种状态，四个调用点都改用它。

### 7.2 时长按**配速**推算，而不是按**模式**

`_derive_minutes` 读 `profile.pace`，从不读 `profile.travel_mode`。

复现（成都约 13.3 km 的一跳）：
`FULL + walk` = 44 分钟（18.2 km/h），`RELAXED + drive` = 231 分钟（3.5 km/h）。

现在：步行 4.5 / 公交地铁 15 / 打车 24 / 驾车 24 km/h，并且**把速度写进冲突说明里**。
配速推出的默认值保持不变，所以已存的计划不会被改写。

`resolve_travel_mode(profile)`（`providers/base.py:16`）是唯一的模式解析点：
显式的 `travel_mode` 优先，否则 RELAXED/BALANCED→WALK、FULL→DRIVE。

---

## 8. 还不知道 / 没验证 / 没做

### 未验证（诚实列出）

- **`scripts/probe_amap_transit_shape.py` 从未对真实服务跑过**（没有 `TRAVER_AMAP_KEY`）。
  高德的公交/地铁返回结构只在 mock 上验证过。
- 没有真实高德 Key 的端到端验证。
- 没有 metrics / tracing（只有带 `request_id` 的结构化日志）。
- 没有浏览器 E2E，没有负载测试。

### 已知但**尚未修**的问题（都低于标准线）

- `apps/web` 把 Traver 自己的 API key 存在 `localStorage` 里（`roadmap.md` 的"未完成"里记着）
- 没有 `LICENSE` 文件。**待决定**：MIT（推荐）/ GPL-2.0 / 不加。没有拷贝 TripStar 代码，
  所以选择是自由的。
- `attraction` 是第四个兜底额度，但**没有任何策略缩放它**（因为 `BudgetStrategy` 没有
  `attraction_weight`）。已在 docstring 里如实说明，没有假装它被处理了。

### 测试覆盖缺口（已用命令确认）

- `apps/web/lib/reviews.ts`、`apps/web/lib/trip-store.ts` **没有测试**
- 以下组件**没有测试**：`api-key-settings.tsx`、`content-ingestion.tsx`、
  `job-progress.tsx`、`plan-form.tsx`
- `apps/web/app/trips/[id]/page.tsx` **没有测试文件** —— 这正是 `30.6/104.0`
  那个地图 bug 能存活下来的原因。**新增 UI 一律抽成 `app/components/*.tsx` 并带自己的测试，
  不要内联在这个页面里。**
- `plan-form.tsx` 虽然没有单元测试，但由 `mutate_web_api_contract.py` 钉住
  （它还原 "packed" 那个 `Pace` 缺陷）

### 工作区里的历史遗留

仓库根目录有 5 个游离的 `.db` 文件（`phase2_gate.db`、`phase2_gate_latest.db`、
`travel.db`、`travel_alembic.db`、`travel_alembic_phase2.db`）—— 不是本项目产生的，
也没被 gitignore 掉。

---

## 9. 工具清单

### 15 个 probe（`scripts/probe_*.py`）

`shared_vocabularies`、`reachable_aspects`、`place_resolution`、`route_honesty`、
`prose_guard`、`freshness_labels`、`age_unknown_over_http`、`export_documents`、
`osrm_profiles`、`amap_failure_visibility`、`transit_without_key`、`osm_element_age`、
`live_routing`、`amap_transit_shape`、`element_age_timebomb`、`rollinggo_shape`

### 其他

- `scripts/healthcheck.py`、`scripts/e2e_refresh_job.py`
- `scripts/_mutation_harness.py` —— 共用 harness runner
- `scripts/clock_shift_plugin.py` —— 时钟平移检测器

---

## 10. PowerShell / 工具陷阱（踩过的）

| 陷阱 | 正确做法 |
| --- | --- |
| `python - <<'PY'` heredoc | **pwsh 不支持**。用 `write` 工具写临时脚本再跑 |
| `Select-String -Path` 遇到含 `[id]` 的路径 | 报"字符串缺少终止符"。用 `grep` 工具 |
| `gh run view --json jobs --jq '...'` | `--jq` 表达式被按空格拆开 → `accepts at most 1 arg(s)`。用 `gh run list --limit N --json ...` |
| `git push` 打 `[exit code: 1]` | stderr 的噪音，其实成功了。看 `main -> main` 行 |
| CRLF 警告 | `core.autocrlf=true`，几乎每个 .py 都会警告。确认是否真有改动：`git diff --ignore-all-space <file> \| Measure-Object -Line`，0 行 = 只是行尾 |
| 控制台显示中文乱码 | `Get-Content` 的显示问题，`read` 工具的字节级读取才是权威 |
| `Out-File -Encoding utf8` 写 commit message | 会带 BOM。用 `write` 工具写到 `.git/COMMIT_MSG_*.txt` 再 `git commit -F` |
| 含中文的文件 | **一律用 `edit`/`write` 工具**，不要用 pwsh 重定向 |

### 其他技术约定

- `ruff format` 行宽 **99**（正好 100 的行会被放过）
- `Settings` **没有 `env_prefix`** → 环境变量是裸名（`DATABASE_URL`、`AUTH_ENABLED`、
  `AMAP_MAX_ROUTE_POINTS`……），写 `TRAVEL_API_*` 会被**静默忽略**
- `AUTH_ENABLED=false` ⇒ principal 是 `AUTH_ANONYMOUS_OWNER`（`local-dev`），
  所以本地开发不用配 key
- Web 用 `fireEvent`（**没有** `@testing-library/user-event`）
- `tsc` 禁止展开 `NodeListOf<Element>` → 用 `Array.from(...)`
- CI 有 3 个 job（API / Migrations / Web）+ "No schema drift" + "No secret in the client bundle"；
  CI Lint 对**整个仓库**跑 `ruff check .` / `ruff format --check .`

---

## 11. 下一步建议（按价值排序）

1. **网络恢复后立刻 `git push`**（5 个提交，都已验证）
2. 用真实高德 Key 跑 `probe_amap_transit_shape.py` —— 公交/地铁是要求 2 的核心，
   而它从未对真实服务验证过
3. 把 `apps/web` 的 API key 从 `localStorage` 挪走
4. 决定 `LICENSE`
5. 多城市 / i18n（第 7 项剩下的两个明确缺失的能力）
6. 补 §8 里列的测试缺口，优先 `trips/[id]/page.tsx` 相关逻辑

---

## 12. 交接时必须遵守的原则

**事实纪律**：不吹嘘、不编造数据。具体到可执行的程度：

- 没验证的就说没验证。§8 里那些"未验证"是真未验证，不要在下一次对话里变成"已完成"
- 数据永远带抓取时间；不知道就说不知道（`SOURCE_SILENT` 路径），
  **绝不用一个看起来合理的默认值填上**
- 估算和实测必须分开标注（`is_estimate`、`ObservationBasis`）
- 一个能力"建模了"不等于"能提供" —— 用 `UNSERVED_CAPABILITIES` 明说
- 加任何断言前先问：**如果代码写错了，这个测试会失败吗？**
  如果不会，那它只是在复述 fixture 的假设

---

## 13. 本轮新增（2026-10-14）：酒店供应源接入（阶段 1 + 阶段 2）

背景：`search_room_offers` 此前**没有任何来源**，住宿部分永远是估算额度。用户要求找
GitHub 上的酒店价格方案，并选定「国内强」的 **道旅 / RollingGo**（MCP 形态，个人 Key 自助申请）。

完整设计与状态见 [docs/architecture/hotel-supply-integration.md](docs/architecture/hotel-supply-integration.md)。

### 阶段 1 —— 契约从 id 改成实体（这一步是前提，不是优化）

`search_room_offers` 收到的是 `str(hotel.id)`，而 `Hotel.id` 是 `uuid4()` 随机生成的，
外部供应 API **无法反查**。所以「接一个房价源」不是「加一个 Provider」能完成的。

- `RoomOfferProvider.search_room_offers(hotels: list[Hotel], profile)`（`providers/base.py`）
- 实现同步：`mock.py`、`fixture.py`（改为从传入酒店的 `source_references` 反查外部 id）、
  `free/overpass.py`
- 调用点：`services/aggregation.py` 传 `hotels`
- 新测试 `tests/unit/test_room_offer_contract.py`（用 `get_type_hints` 钉住参数类型——
  `runtime_checkable` 只看方法存在，签名回退它发现不了）

### 阶段 2 —— RollingGo MCP 适配层与 Provider

- `providers/rollinggo/{client,hotels,__init__}.py`
- `providers/free/http.py` 新增 `request_raw`（自定义头 / JSON body / 不解析 body），
  重试逻辑抽为 `_retrying`，`request_json` 与它共用
- 配置：5 个 `ROLLINGGO_*`（`config.py` + `.env.example`）。**没有 API Key 设置**——
  Key 走凭据保险箱、按属主隔离，与高德同一套
- `manifest.py`：新增 `rollinggo` 条目；**删除了 `CAPABILITY_GAP_NOTES["search_room_offers"]`**
  （该能力现在「填 Key 可解锁」，留着「无人能提供」的说明就是反方向的新谎言）
- `main.py`：独立 HTTP 客户端与限速（不借免费栈的——高德踩过
  `free_providers_enabled=false` 导致 Key 静默失效的坑）；两个来源各自独立解析

### 三个诚实性决定（都有测试钉住，10 个变异全部被抓住）

1. **报价只贴到它自己发出的酒店上**，别的来源找到的酒店不按名称匹配 ——
   贴错酒店的报价与贴对的在外部看起来一样
2. **`observed_at_basis=SOURCE_SILENT`**，不是 `LIVE_COMPUTED` ——
   来源不给「数据自身时间」，实时查询不等于数据本身很新
3. **`isOnRequest` 不当作有库存；无金额的房型直接丢弃**（不写 0）

### 前端：设置页 fixture 的漂移（这一处差点漏掉）

`apps/web/app/components/provider-catalog.test.tsx` 里有一份手写的「服务端会发什么」副本，
由 `tests/unit/test_web_api_contract.py` 与真实响应比对。**这份副本在我改完后端之后变成假的**：

- `CATALOG.capability_gaps` 里还挂着「房型与报价 = 死局」——服务端已经不再这么说了
  （RollingGo 让它变成「填 Key 可解锁」）
- 没有 RollingGo 条目，所以 `unlockHints` 造不出那句话，
  **「填入凭据后可提供房型与报价」从来没被任何测试渲染过**

这正是本文件 §6 家族 2 的形状（fixture 描述一个世界已经离开的状态）。
已修：fixture 的 gap 列表对齐服务端；新增 `ROLLINGGO_AS_DEPLOYED` 与两条渲染测试；
新增 `test_the_fixture_gap_list_names_exactly_the_gaps_the_server_reports`
（比对 gap 的 **id 集合**，不比对文案——fixture 的 note 是合法简写）。

守卫验证：`scripts/mutate_catalog_fixture.py`，3 个变异全部被抓。
其中 **M2 第一次没被抓**（把 fixture 的 `status` 改回 `planned`），因为我的断言只读了
**服务端**的状态、没读 fixture 自己的——已补上 fixture 侧断言。
**又一条「比对只读一侧就看不见另一侧漂移」。**

### 未验证（重要）
- **响应信封与 `getHotelDetail` 的入参名从未对真实服务验证过。** 项目 README 只描述搜索
  结果的语义、给了一个 `getHotelDetail` 输出示例，没有发布完整响应结构。因此解析器
  **不假设单一布局**：在若干可能的 key 中定位数组，把命中的 key 记进
  `request_context.payload_key`，没有任何 key 命中时返回空——**不编造**。
  `scripts/probe_rollinggo_shape.py` 是让这个假设变成事实的**唯一**办法。
  **下一次对话不要把它写成"已完成验证"。**
- 用户尚未提供真实 RollingGo Key。

### 下一步（接 §11）

1. 拿到真实 Key 后跑 `python scripts/probe_rollinggo_shape.py`，用输出校正
   `HOTEL_LIST_KEYS` / `ROOM_LIST_KEYS` 与 `getHotelDetail` 的参数名
2. 网络恢复后 `git push`（现在有 5 个旧提交 + 本轮的改动）
3. 第二个酒店来源与跨源实体解析（阶段 3）
