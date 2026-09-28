# Roadmap

> 各 Phase 下的测试数量是**该阶段当时**的计数，用于记录当时的覆盖范围，不代表当前总数。
> 当前总数见 Phase 6 一节：298 个离线测试（含 29 个 Web 端 Vitest）+ 8 个可选真实网络测试。

## Phase 0 — 项目侦察与架构底座（完成）

- 空工程确认、技术栈选择、目录骨架
- 架构、数据模型、Provider、AI、决策、实时性文档
- `.env.example`、Docker Compose、README

## Phase 1 — 基础骨架（完成）

- FastAPI、版本化 API、配置、日志、CORS、健康检查
- `PostgreSQL/PostGIS、Redis Compose 服务`：Redis 服务仍在 compose 中，但只作为 `extras` profile 供本地实验，API 不读取它（见文末"配置卫生"）
- Next.js Web 骨架（当时是占位；功能在 Phase 3B 落地，认证与管理页在 Phase 6 补齐）

## Phase 2 — 持久化 Travel Engine（完成）

- TripProfile、Hotel/Room/Rate/Offer、Restaurant、Attraction、Route、Budget、SourceReference
- SQLAlchemy 核心表、Alembic 初始迁移和可重复 upgrade/downgrade
- Repository、Unit of Work、事务 rollback、Trip 状态持久化和 API 回读
- Offer/Route/Source Snapshot 历史、FreshnessService 和数据 TTL 策略
- 时间窗/营业时间/旅行时间/节奏 Buffer 的启发式路线规划
- BudgetStrategy、事实/估算状态和超预算重分配候选
- 局部 Trip Revision、版本号和 Change History
- FixtureLoader、Provider timeout/retry/circuit breaker/health metrics
- ToolRegistry、AIProvider/AIRequest/AIResponse 契约
- 18 个单元/集成测试覆盖上述核心闭环

## Phase 3 — Provider Framework（完成）

- Protocol 合同、Registry、health、Mock Provider
- 三个模拟商业来源，支持无 API Key 运行
- Provider 失败隔离边界

## Phase 3A — Normalization / Entity Resolution（完成）

- 独立 Location/POI canonical entity 表
- 名称、地址、电话、品牌、地理距离的可配置匹配置信度
- 来源记录与快照持久化（基础能力已完成，Canonical POI/匹配置信度仍需增强）
- 景点去重合并别名与来源引用，并优先保留字段更完整的记录

## Phase 3B — AI Travel Workspace（完成）

- RequirementAgent：显式 / 推断 / 未知字段和缺日期澄清
- OpenAI-compatible adapter 与本地 deterministic fallback
- ToolRegistry OpenAI schema、读写权限和调用上限
- TravelAIService、会话上下文、工具调用记录、Trip Conversation
- `/ai/chat`、Trip 列表和会话 API
- Next.js 创建旅行、旅行列表、Travel Workspace、AI Chat、地图坐标视图、预算/来源/新鲜度/版本 diff
- 29 个 pytest 测试，覆盖自然语言创建、缺日期、会话持久化、Adapter/Tool Orchestration 和局部酒店替换

## Phase 4 — 免费真实数据层（完成）

全部免费、无需密钥，并已用真实网络数据端到端验证：

- 能力路由：Provider 只声明真正提供的能力（`HotelProvider`/`FoodProvider`/`AttractionProvider`/`RouteProvider`/`WeatherProvider`），注册中心据此分发
- OpenStreetMap/Overpass POI 源：酒店、餐厅、景点共用一个按类别限额的并集查询，按目的地缓存
- Open-Meteo 天气源：16 天预报、WMO 中文映射、户外适宜性判断
- OSRM 路线源：单次 table 请求取完整矩阵，配驾车成本估算
- 地理编码：Open-Meteo 优先、Nominatim 兜底（结果不可靠时也会触发），带 TTL 缓存与按主机限流
- 合规内建：Nominatim 1.1 s、OSRM 1.0 s、Overpass 1.0 s 的按主机最小间隔；署名经 API 暴露
- 事实纪律：无商业来源时不编造价格；步行时长按实测距离推算并标记为估算；预算缺失项以显式估算额度呈现而非塌缩为 0
- 可靠性：HTTP 层指数退避重试、镜像回退、按能力隔离的熔断、非 `HealthProvider` 状态为 `unknown`
- 默认免费源开启、mock 源关闭，避免合成数据混入真实结果
- 204 个离线测试 + 8 个可选的真实网络端到端测试（含直接验证 Nominatim 的用例）
- **路线按 profile 分服务器登记（后续修正）**：默认改指 FOSSGIS，步行由推算升级为实测；详见
  [architecture/routing.md](architecture/routing.md)

详见 [providers/free-sources.md](providers/free-sources.md)。

## Phase 4C — 扩展免费来源：空气质量、节假日、景点简介（完成）

在 Phase 4 基础上按「真实数据实测 + 许可合规」两个标准扩展了三个来源：

- **Open-Meteo 空气质量与紫外线**：逐小时值按天取**峰值**（端点不支持 `daily` 聚合），用于强化 `outdoor_friendly`；同时发现天气响应自带海拔，省掉了一次额外请求
- **公共节假日**：改用 MIT 许可的 `holidays` 本地库而非免费 Web API——首选 API 的服务条款只允许私人/非营利使用，且遗漏清明节与调休连续假期
- **景点简介**：来自中文维基百科/Wikidata（CC BY-SA 4.0），**只跟随 OSM 显式 `wikipedia`/`wikidata` 标签**，绝不按名称猜测
- 新增能力 `HolidayProvider` 与 `ContentProvider`（内容增强在去重之后运行），并接入 `TripPreview.holidays` 与 `Attraction.description`
- 64 个新增离线测试 + 4 个新增真实网络测试

选择来源时新增第 4 条判据：**许可必须允许本项目用途**。`Nager.Date` 技术上前三条全过（免密钥、
200、数据正确），却卡在许可上——这类问题不会在请求里暴露，只能读服务条款发现。

详见 [providers/free-sources.md](providers/free-sources.md) 与 [providers/free-api-catalog.md](providers/free-api-catalog.md)。

## Phase 4B — Decision / Budget / Route 增强

- 配置化 Decision 权重持久化和多候选替换排序
- 预订时间、餐厅营业窗口、交通矩阵和跨日路线约束
- 就近优先的景点排序已落地；OR-Tools/Constraint Solver 评估（当前保持可解释 Greedy Heuristic）

## Phase 5 — Real Providers / Production Data

- ~~自建 OSRM 实例以支持真实步行/骑行路径（公共 demo 只提供驾车路径）~~ → **已用 FOSSGIS 达成步行部分**：
  默认指向 `routing.openstreetmap.de`，步行变为实测（20/20 腿，4.4–4.6 km/h）。骑行仍未达成，
  因为 `TravelMode` 没有 `bike` 成员——服务器支持不等于工具能规划，见
  [architecture/routing.md](architecture/routing.md)。自建实例仍可通过 `OSRM_BASE_URL` +
  `OSRM_HONOURED_PROFILES` 接入
- 高德/天气与正式授权商业 Provider（房价、库存、评分）
- 面向出站调用的商业来源凭据轮换、配额与数据用途审查
  （入站认证与限流见 Phase 6；这里指的是**调用第三方**时的凭据与合规治理）

## Phase 6 — Production hardening（部分完成）

### 已完成

**写入门禁修复（先于功能，因为它是正确性漏洞）**

- `POST /tools/{name}` 曾无条件传入 `allow_write=True`，使 AI 工具路径上的写意图检查可被任何直接调用该端点的客户端完全绕过；现由请求本身推导，并补齐 HTTP 层回归测试
- 同一个路由还曾**完全没有认证依赖**：`confirm_write` 是确认而非权限，所以无凭据的调用方传 `?confirm_write=true` 就能通过注册表的写守卫；而工具 handler 按对外发布的工具契约只收 `trip_id`、不收 owner，于是在仓储层以「不过滤」查行程——任何人的行程都能按 id 读到、改掉。现要求凭据、按工具 `kind` 校验作用域，并在调用期间显式绑定调用者属主；四个变异（去掉认证依赖、去掉作用域校验、服务层不再读环境属主、把 404 变回未映射异常）全部被测出
- 这类缺口的根因不是某条路由写错了，而是**测试逐条点名自己检查的路由**，没被点到的就是没人看的。因此新增 `tests/integration/test_route_protection.py`：它不列路由，而是从应用自己的 OpenAPI 文档里读出全部 38 条，逐条以无凭据身份请求，要求 401；只有写进 `PUBLIC_ROUTES` 并附上理由的才允许公开。变异验证：给 `tools` 路由摘掉认证依赖、以及新增一条忘记带依赖的路由，都会被立刻抓住
- 该守卫顺带问出了第三条公开路由 `GET /providers/health`。核查后确认它安全并保留了公开：它读的是**基础注册表**，而带凭据的 Provider 由 `for_owner` 的工厂按请求构建、从不注册进去，所以它报不出任何属主的来源；载荷只有共享来源列表与计数，不含凭据值，而来源列表本就已经由公开的 `/providers/attributions` 公布。公开它与 `/health` 报告 `credential_vault: locked` 是同一个取舍：运维方以为自己在跑已配置的部署、实际在跑 mock，才是最该被看见的失败
- 写意图正则此前会把否定句判为写入请求（"不要预订"被读成预订）；现在先检测否定

**认证与租户隔离**

- Bearer API Key，服务端只保存 SHA-256 摘要与 12 字符显示前缀，明文不落库
- 行程按属主隔离：一把密钥读不到另一把密钥创建的行程
- `python -m travel_api.cli` 提供 create-key / list-keys / revoke-key
- `AUTH_ENABLED` 默认 `false` 以便本机开发，且 `/api/v1/health` 会报告当前认证模式——未加固的部署不会被误认为已加固
- 属主只有一个来源（principal，关闭认证时即 `AUTH_ANONYMOUS_OWNER`）。此前路由层在未启用认证时返回 `None` 表示「不过滤」，而中间件同时把环境属主绑成了 `local-dev`：同一次请求的写入与读取各用一个答案，于是**默认本机模式下保存的笔记永远不进行程**（带密钥时两个答案相同，故不可见）。现在两条路径共用一条规则，并补了无密钥模式的端到端回归测试

**入站限流与边界配置**

- 按客户端（优先按属主、退化到 IP）的固定窗口限流，AI 端点有更紧的独立预算
- 限流器**返回等待秒数而非抛异常**：在 HTTP 中间件里抛 `HTTPException` 会绕过 FastAPI 的异常处理器，最终表现为 500
- CORS 来源可配置；就绪探针真正往返数据库，而不是只判断进程存活

**工程基建**

- 乐观锁（`expected_version`，冲突返回 409）与有界列表分页
- `requirements.lock` 与 `apps/web/package-lock.json`，CI 与本地解析到同一组版本
- 生产镜像：非 root、不挂载源码、从锁文件安装、就绪探针有独立退出码契约
- `docker-compose.yml` 增加 `prod` profile，`POSTGRES_PASSWORD` 无默认值（未设置则拒绝启动）
- CI 三个作业：API（ruff + pytest）、Migrations（upgrade→downgrade→upgrade + autogenerate 漂移检查）、Web（tsc + lint + vitest + build + 产物密钥扫描）
- Web 端 API Key 入口与 `.env` 之外的运行时配置；密钥刻意不走 `NEXT_PUBLIC_*`，因为该前缀会被内联进构建产物

**署名履行**（原列在 Phase 6 待办，实际上早已随 Phase 4C 落地）

- Workspace 页面渲染 ODbL / CC BY 4.0 要求的可见署名，来源与许可经 `/api/v1/providers/attributions` 提供
- 天气与节假日各有独立面板，并标明数据性质（预报值而非实测、节假日不覆盖景点自身数据）

**地理编码正确性**（与 TripStar 对比时发现；这是国内城市最常见的失败点）

对比 [1sdv/TripStar](https://github.com/1sdv/TripStar) 后实测排查国内目的地覆盖度，结果不是数据源不足，而是解析到了错误城市：

- **`巴黎` 解析到美国肯塔基州**（偏约 8,300 km）、**`罗马` 解析到美国乔治亚州**。原评分用 `startswith("PPLA")` 判最高档，而 `"PPLC"`（国家首都）不匹配该前缀，于是人口 213 万的巴黎被降档，人口 9,870 的 Paris, Texas 胜出。评分元组先比档位再比人口，人口无法补偿。
- **`丽江` 解析到湖南同名小镇**（偏约 1,154 km）。Open-Meteo 对该名称只返回这一个 `PPL` 且无人口；因为是"非空答案"，Nominatim 兜底从未执行——**一个看起来完全合理的错误行程，比报错更危险**。
- **`平遥` 返回 0 个 POI**。解析到平遥县域质心，距古城约 11 km，落在 ±0.045°（约 5 km）搜索框之外。
- 判据不能用 `class`：实测线上 jsonv2 响应没有该字段。曾有一版实现与测试都基于 `class == "place"`——**测试通过但线上永不匹配**。现按线上真实形状构造载荷。

修复：分档改为 `PPLC` > `PPLA` > `PPLA2..5` > `PPL`；候选仅在"行政驻地且有人口"时才可信，否则标记 provisional 并继续询问下一来源；Nominatim 优先定居点类型（`addresstype`）但受 50 km 局部约束，避免跳到远处同名地。真实网络实测：`丽江` 1/0/1 → 35/53/32，`巴黎` 8/30/4 → 40/62/56，`平遥` 0/0/0 → 36/20/37。详见 [providers/free-sources.md](providers/free-sources.md)。

### 未完成

- 浏览器 E2E 与负载测试
- 可观测性目前只有带 `request_id` 的结构化日志，**没有 metrics 与 tracing**
- Web 端仍把 **Traver 自己的访问密钥**存在 `localStorage`：对同源脚本可读，一次 XSS 即可窃取。第三方 Key 已改为服务端保险箱存储、经 API 写入，不再经过浏览器存储；但访问密钥本身仍是前端持有，面向公网部署应改为后端签发 httpOnly Cookie 会话
- 会话级权限细分（当前只有 `trip:read` / `trip:write` 两个 scope）
- 国内 POI 深度仍弱于高德：OSM 在省会级城市充足（实测 6/6 打满限额），但县级/古镇类目的地依赖当地测绘贡献。**高德已实现**（见 Phase 6B），接入方式为"有 Key 启用、无 Key 回退免费源"的可选 Provider

## Phase 6B — 插件化与自带密钥（进行中）

目标：把 Traver 做成真正的"汇总工具"——自己不提供数据、不绑定模型，一切由使用者接入；0 配置必须可用，每接一个源解锁一批能力（渐进增强）。

**已完成的第 1、2 步**

- Provider 清单（`providers/manifest.py`）：每个来源声明能力、所需凭据、费用、**时效性**与诚实状态。`planned` 的源不贡献任何能力覆盖，且明确标注"尚未实现"
- 能力矩阵与 `GET /api/v1/providers/catalog`：回答"现在能做什么、还差什么"，只报凭据名与配置状态，**从不返回值**
- 两条防漂移测试：声明的能力必须是注册表真能路由的方法；声明的 provider 名必须真被注册（此前若靠猜名字，会把可用源静默报成缺失）
- 凭据保险箱：AES-256-GCM 加密可逆、按属主隔离、环境变量作兜底、界面保存免重启。属主/来源/凭据名绑定为 AAD，因此把密文搬到别的属主会认证失败而不是泄漏
- 按属主解析模型：同一部署可多人使用不同模型与 Key；`/settings/model/test` 真发一次请求验证连通性
- **修复一个正好踩在核心路径上的崩溃**：`main.py` 用 `default_max_tokens=` 构造适配器，而参数名是 `max_tokens`，`TypeError` 在 lifespan 内抛出使进程在响应任何请求前就死掉——即"接入自己的模型"这一最基本的用法根本无法工作。此前无测试发现，因为**所有测试都在未配置模型的状态下运行**，崩溃只存在于没有任何测试进入的那个状态
- 修复默认值被谎报为"已配置"：`ai_model` 有默认值 `gpt-4o-mini`，原先会被报成"环境已配置"；现用 `model_fields_set` 区分"显式提供"与"取默认值"
- 修复本地模型不可用：原先要求地址与 Key 同时存在，导致 Ollama/LM Studio 这类**无需 Key** 的自建模型无法配置；现只以 `ai_base_url` 为启用条件

**已完成的第 3 步：高德 Provider 与按属主的作用域**

- 高德 Web 服务接入（`providers/amap/`）：路线（含**公交/地铁**）、POI、地理编码、天气。接口契约逐字段核实并记录在 [`docs/architecture/amap-contract.md`](architecture/amap-contract.md)，未核实者一律不假设存在
- **公交/地铁**是免费源完全没有的能力（公共 OSRM 只有驾车 profile），也是接入高德的首要理由；能力矩阵因此按**方面（aspect）**而非仅按能力统计，未配置 Key 时"公交/地铁"始终显示为缺口
- 按属主作用域：注册表在启动时构建一次，而 Key 属于某个人，因此 `ProviderRegistry.for_owner()` **每次请求**用该属主的 Key 重建带凭据的 Provider。缓存实例会导致"我保存了 Key 却还用旧的"以及"移除后仍在使用"两个 bug，故**刻意不缓存**
- 环境属主（`services/owner_context.py`）：AI 工具路径的 handler 签名是对外发布的 OpenAI 工具契约，没有位置放 owner，因此用 `ContextVar` 承载属主。`""`（匿名属主）与 `None`（未指定）**必须区分**——把匿名请求提升为环境属主，就是把别人的 Key 递出去
- 修复一个只有集成测试才能发现的真实缺陷：带凭据的工厂原先挂在**免费源**的 HTTP 客户端上，于是 `FREE_PROVIDERS_ENABLED=false` 时根本没有客户端、工厂从未安装，保存的高德 Key **静默无效**。现高德拥有独立客户端与独立限速
- 数值/单位核实并测试钉住：`status` 是字符串 `"1"`（真值判断会把 `"0"` 当成功，即**每个失败都被当成成功**）；坐标线上为 `"经度,纬度"` 而内部一律纬度优先（交换后数值仍合法，**下游无法察觉**，只会把行程规划到别处）；`/v3/distance` 的 `origin_id` 是 origins 参数的 **1 起下标**
- 诚实性细节：`/v5/direction/walking` **没有 duration 字段**，步行耗时由明确标注的速度推导并标记为估算；高德不提供评分与价格，这些字段保持为空而**不填默认值**；公交票价缺失时**不套用打车单价**（否则地铁行程会被按出租车计价）；高德**不提供酒店房价**，故清单中不声明 `search_room_offers`
- 不缓存：高德服务条款禁止存储或缓存结果，因此对它**不做任何缓存**，每次规划都是实时请求，每条数据带真实抓取时间。这一条同时满足时效性要求与法务要求，方向一致
- 归属标识：`© 高德地图` 加入归属清单，并让匹配顺序先于模糊规则——否则名为 `amap-driving-routes` 的源会因含 "route" 被误记为 OSRM 的功劳

**两个"声明了但接不上"的缺陷（由端到端测试发现，而非推理）**

- **同一个名字注册了三个 Provider**：路由、POI、天气都叫 `"amap"`，而注册表以名字为键，于是**只有最后一个被保留，另外两个被静默丢弃**。没有异常、没有日志，规划照样"成功"，只是数据变少。同时能力矩阵只读清单声明，因此继续显示"公交/地铁 ✓ 已覆盖"——**声称能坐地铁而没有任何代码能算地铁**。修正：三个 Provider 各有名字；矩阵改为向 `providers_for()` 实际询问
- **酒店搜索永远无法路由到高德**：`CAPABILITY_BY_METHOD["search_hotels"]` 指向的 Protocol 同时要求 `search_room_offers`，而高德只有酒店 POI、没有房价，于是连 `isinstance` 都不过——清单声明了 `search_hotels`，调用却永远到不了。修正：把"找酒店"与"给房价"拆成两个 Protocol，缺房价的源不会因此失去酒店搜索

两个缺陷都属于同一类：**声明、实现、路由三者单独看都正确，只有向注册表提问才会暴露**。因此新增了一条防漂移测试直接问这个问题：清单里每个已实现来源声明的每个能力，`providers_for()` 都必须返回该来源的 Provider。把上述任一处改回去，测试会指名报出 `['amap:search_hotels']` 这样的具体缺口（已用变异探针验证）

**尚未开始**

- 高德之外的第二个真实数据源（抖音/美团/携程/同程均**无公开 API**，已走内容摄取路线）
- 多城市、i18n（zh/en/ja）、地图与知识图谱可视化、偏好记忆
- 导出只做了 Markdown 与日历；**图片 / PDF 导出尚未做**（需要渲染层）
- 作业注册表在进程内：多 worker 部署需要共享存储（已在 `planning-jobs.md` 中写明）
- **房型与实时房价没有任何来源**：高德 Web 服务 API 不提供房价，其余接入的来源都是地理/天气类。
  要补上需要接一个商业酒店渠道，尚未做。矩阵现在如实报为 `NONE` 并说明原因

**已完成的第 3 步：能力矩阵的诚实性修复**

设计细节见 [`docs/architecture/capability-coverage.md`](architecture/capability-coverage.md)。要点：

- 修掉一个真实的多报：Overpass 必须实现 `search_room_offers`（一个 provider 同时服务酒店/餐厅/景点），
  实现方式是**永远返回空**。注册表用 `isinstance` 判断，分不出「真的会给数据」和「永远返回空」，
  于是矩阵报告 `[FULL] search_room_offers providers=['openstreetmap']`——而这个部署里没有任何来源
  能产生一个房价，同一份 manifest 的注意事项还写着「不含房价」
- 一条断言在保护这个 bug：`assert payload["uncovered_capabilities"] == []`。测试通过只是因为
  空桩被算作了覆盖。已改为断言真实状态
- 修法是三层的，因为任何一层单独都可能错：provider 声明 `UNSERVED_CAPABILITIES`；
  注册表拆出 `serving_providers_for()`（报告用）与 `providers_for()`（分发用，桩必须继续被调用，
  它靠返回空来拒绝，而「问过没有」和「没问过」是两件事）；manifest 不再声明拿不出的能力
- 缺口现在**带原因**（`capability_gaps`），而不是只报一个计数——产品前提是「需要什么自己接」，
  所以「缺什么、要接什么」才是可用信息
- 「没人能提供」与「你还没配置」分开：公交地铁属于后者（高德能提供，只差 Key），
  把可解的缺口标成死局等于让用户放弃一个 Key 就能解决的事
- 缺口说明断言了行为（「住宿部分是估算额度并已标注」），所以它被验证：无报价 → `is_estimate=True`
  / 480 元；有真实报价 → `is_estimate=False / status=observed` / 且引用来源。两个方向都钉住，
  因为一个永远为 `True` 的标记不携带信息

**已完成的第 7 步之一：异步任务与进度推送**

设计细节见 [`docs/architecture/planning-jobs.md`](architecture/planning-jobs.md)。要点：

- 实测「杭州 3 天」零密钥全程 **69.8 秒**，其中采集 40.4 秒、算路线 27.8 秒。同步请求在这段时间里
  与「卡死」在用户眼里没有区别
- 进度不是模拟：步骤对应 `build_preview` 里真实的阶段边界，每步带真实开始/结束时间。未完成的步骤
  `duration_ms` 为 `null`（前端显示「进行中」），因为 `0 ms` 是一个测量结论而 `null` 是「还不知道」
- 阶段在**打开时**发布而非完成时——否则用户在最初 40 秒里什么都看不到
- 五种结局，`CANCELLED` 不并入 `FAILED`（一个是用户的决定，一个是问题）；失败回报**原始错误**
  而不是「规划失败」
- SSE 每帧发整个快照，终态自动关闭；前端用 `fetch` 手工解析而不是 `EventSource`，因为后者无法设置
  `Authorization` 头，用它就得把密钥放进查询串（会进日志与历史）
- 顺带补上一个 渐进增强 的洞：此前 Web 上创建行程的唯一入口是需要模型的自然语言对话，
  **零配置的用户无法从界面生成行程**。现在「填写字段」在前、自然语言是需要模型的增强
- **重规划（`refresh`）也接入了同一套作业与进度**（后补）。它做的是同一件事——重新问一遍
  所有来源——所以耗时与首次规划相当（69.8 秒），却曾是唯一没有反馈的操作：浏览器冻结，
  用户分不清「在跑」和「挂了」。现在 `POST /trips/{id}/refresh/jobs` 返回同一个 job 结构，
  只多一个 `kind: "refresh"` 与 `target_trip_id`，客户端因此可以复用同一套轮询/SSE 与同一个进度组件
- 作业在**创建之前**先读行程：不存在或属于别人的行程直接 404，而不是变成一个一分钟后再死的作业。
  这既让调用方立刻得到答案，也让作业列表不能被用来探测行程 id 是否存在
- 同步端点保留：接入作业是更好的默认，不是唯一选项，删掉它只会破坏调用方而换不到任何诚实性

**已完成的第 5 步之一：让「接入高德的理由」真的能用**

设计细节见 [`docs/architecture/amap-contract.md`](architecture/amap-contract.md)。要点：

- **两个只在「有 Key 的用户」身上才出现的缺陷**（无 Key 时高德根本不构建，所以两者都不会显形，也都没有任何测试能碰到）：
  1. **配额被当成钥匙错误**：`10003`/`10004` 同时属于「钥匙问题」和「配额问题」两个集合，而分类函数先查前者——于是「今日访问量已超」被抛成 `AmapKeyError`，`main.py` 把这个异常当作**凭据不可用而丢弃整个 Provider**，用户被告知去改一个完全正常的 Key。修法是让两个集合**互斥**（顺序无法修复重叠，两边都说得通），并加断言交集为空的测试。原来的配额测试只用了 `10044`——唯一一个恰好不重叠的配额码——所以它「验证」了这层区分而两个最常用的码一直是错的
  2. **路线失败被吞掉**：三条路径都 catch `AmapError` 以免一次失败丢掉已得路段，但只 catch 不记录，于是全部请求失败时仍返回 `status="success"`、`errors=[]`、`data=[]`——**与「这个来源不支持该方式」在外部完全一样**。后果是行程对刚填好 Key 的人说「没有可用的数据源……请接入高德 Key」。现记录 `last_attempts`/`last_failures`，有失败即 `degraded` 并带 `errors`，聚合层据此把「没有来源」与「来源已配置但失败」分成两种措辞与两种建议
  3. 这类缺陷的共同点：**Provider 自己把失败处理掉了，所以外部看到的是成功**。已加 `scripts/probe_amap_failure_visibility.py`（用总是失败的传输层复现，无需真实 Key）
- 清单里写着「公交/地铁是 OSRM 完全没有的能力，这是接入它的主要理由」，挂了很久，而**它做不到**：
  选路只有一个入口 `mode_for_pace(pace)`，而 `Pace` 只有 relaxed/balanced/full 三个成员，
  所以它永远返回不了 `TravelMode.TRANSIT`；`_transit_legs` 只能由那一个 `if mode is TRANSIT`
  分支调用。于是实现完整、端点正确、单元测试全过，而**没有任何一次请求能产生一条公交/地铁腿**
- 这是「实现了方法 ≠ 服务了该能力」最纯粹的形态：每个部件都对，缺的是把它们连起来的那根线，
  所以没有任何单元测试能看见它。能力矩阵在配了 Key 时如实报 `transit: covered`，于是整条链上
  唯一的错误信息来自用户——他填了 Key，却发现公交地铁永远不会出现
- 修法是让出行方式可以被要求：新增 `TripProfile.travel_mode`（`None` = 按节奏推导，即所有旧行程的行为），
  `resolve_travel_mode()` 放在 `providers/base.py` 由两个 provider 共用——注册表对同一次聚合同时调用
  两者，判断不一致会让两边的腿基于不同假设却被合并，而载荷里没有东西能对账
- `mode_for_pace` 保留为兜底分支，并有测试专门钉住「它永远返回不了 transit」，防止将来有人直接调用它
  让能力重新变死
- 同一批还修掉两个「按钮是坏的」缺陷：网页表单的「紧凑」发的是 `packed`，而 API 的 `Pace` 枚举是
  `full`，所以点它得到 **HTTP 422** 而不是行程；行程页也只会把 `relaxed` 显示成「轻松节奏」、
  其余一律显示「平衡节奏」，「紧凑」看不出来
- `packed` 这类错误没有任何测试能发现：网页测试对着表单自己的列表断言会通过，Python 测试对着枚举
  断言也会通过，缺陷正好住在两者之间，而两种语言没有共享常量的机制。所以新增
  `tests/unit/test_web_api_contract.py`——用 `.env.example` 那套办法解析 TypeScript 里的取值，
  与枚举比对，**并把表单自己的每个取值真的 POST 到端点**（词汇表对得上，仍可能在请求层被拒）
- 4 个变异全部被抓住（`scripts/mutate_web_api_contract.py`），包括最初那个 `packed`
- **同一类缺口后来被找到八次，都是主动去找的，不是等它报错**：`resolve_places` 这个进度步骤名（我在加入该功能的那次改动里写下的字面量，前端标签表没跟着更新，界面上就冒出一个英文标识符）、`JobStatus`/`JobKind` 两个联合类型、地点定位状态 `PlaceResolutionStatus`，以及随后用脚本把 API 的 18 个 `StrEnum` 与前端源码逐一比对后找出的四个——`ObservationBasis`（**时效性硬约束自己的词汇表**）、行程项类型 `ItineraryItemType`、会话角色 `ConversationRole`、抽取置信度 `ExtractionConfidence`。形状相同：两侧各存一份词汇表、语言不同、无法共享常量，而每一侧的兜底都让缺失看起来像是正常工作（步骤名兜底显示原始名、`isTerminal` 把未知状态当作非终态于是永远轮询、`switch` 的 `default` 给出一句像样的「未知的定位结果」、聊天记录被过滤成**空白面板**让人以为对话丢了、`confidenceLabel` 的 `default` 直接回显原始值）。因此契约测试统一改为**读源码比对**，并覆盖两种 `reporter.step` 调用形状；变异验证包括「删掉标签」「标签改回标识符」「删掉一个状态」「把两个状态写成同一句话」，以及把 `provider_reported` 改名（会让每个真的带时间戳的来源都变成报「数据时间未知」——一个看起来更谨慎、实际把功能删掉的失败）
- 时效性那条规则本身也做了跨语言比对：API 只在 `ObservationBasis.age_is_about_the_data` 里写一次「只有来源自己盖了时间戳才谈得上数据年龄」，前端在 `dataAgeIsKnown` 里另写一次。两边各自自洽，不一致时从任一侧都看不出来，所以直接读前端的实现与 API 的规则比对
- 最后一条最初通过了：`confidenceLabel` 的守卫一开始只改标签文本（`"仅为线索"` → `"low"`），而那只测出了「case 存在」，没测出「值被处理」。改成把 `case "low"` 改名为 `case "weak"` 后才真正抓住——**变异写错会让人误以为守卫有效**，所以每个变异都要确认它确实触发了断言
- 顺带把 `AMAP_MAX_MATRIX_ORIGINS`（声明了但从未被读取的死设置）换成真正生效的
  `AMAP_MAX_ROUTE_POINTS`（默认 25，与 OSRM 预算一致，使「填了 Key」不会让可计算路段变少），
  并把 `/v3/distance` 每次 100 个 origin 的**协议上限**做成结构性钳制的常量而非偏好开关
- 被截断的路线现在如实交代：source reference 同时记录 `points_offered` / `points_routed` /
  `point_budget` / `truncated`，`note` 里也写成一句人话。只有一个 `point_count` 时，客户端会把
  「算了 5 个点」当成「这趟行程就 5 个点」
- 选「公交/地铁」而没有 Key 时，行程里会明确写出**为什么**是估算（`_disclose_unserved_travel_mode`）：
  原来只有一条行程级计数「N 段按距离估算」，它没有把结果和造成它的请求连起来，使用者最自然的
  理解仍然是那些分钟数是实测的。补救办法按方式区分——告诉一个要求「步行」的人「公交地铁需要高德
  Key」是错的补救，而错的补救比没有补救更糟，它会让人去配一个不起作用的凭据
- 真实端到端验证（零密钥、真实免费源、`travel_mode=transit`）：路段 0 条、无任何来源声称测过
  公交路线、行程仍可规划、矩阵报 `transit: covered=False`，并且行程里确实带着上面那条解释
- **「已核实的字段名」与「代码读的字段名」如果同出一个假设，就没有任何测试能发现两者一起错**。
  公交耗时的代价尤其高：读不到时该对不产生路段，于是接入高德的首要理由静默失效，而其余部分看起来全正常。
  因此 `duration` 从 `transits[].duration` 与 `transits[].cost.duration` 两处依次读取（同一个量，
  接受任意一处只可能把「没有路段」变成「AMap 实测的路段」，不可能凭空造数），读到哪一处记进
  `request_context.duration_field`；两处都没有时仍返回 `None`，**不按步行速度推导**——那会给一条真实换乘方案
  配上编造的耗时。`scripts/probe_amap_transit_shape.py` 用真实 Key 打印实际字段路径，是让假设变成事实的唯一办法
  （`scripts/probe_transit_without_key.py`）

### 已完成的第 5 步之二：把「缺失」这一个词拆开

同一类缺陷的第三例：**骑行**。能力矩阵把骑行和公交地铁渲染成同一个词「缺失」，解锁提示甚至写成
「可补：公交/地铁、骑行」——仿佛接一个来源就能补上骑行。补不上：`TravelMode` 没有骑行成员，规划器
不会请求它，所以接入任何来源都不会出现骑行路线。高德确实能算骑行路线，FOSSGIS 也有真实的 `bike`
profile——**正因为数据真的存在，这个错误特别像对的**。矩阵回答的是「这个工具能不能规划它」，不是
「服务器能不能算它」。

- 每个 aspect 现在带 `requestable`（事实：有没有请求能表达它）与 `closable_by`（建议：该接来源还是
  该改工具），全部由服务端判定，客户端不再自己推断
- 界面据此后分开呈现：公交地铁是红色「（缺失）」，骑行是中性色虚边的「（工具不支持）」，并且面板
  标题下那句话把它们分开计数——「另有 N 项这个工具本身还不支持」
- 解锁提示只列 `source` 那一类。「接入后才会提供 X」后面跟一个接不出来的 X 就是假承诺
- 新增 `ASPECT_TRAVEL_MODES`：aspect 词汇（`driving`/`walking`）与 `TravelMode` 成员
  （`drive`/`walk`）是同一件事的两个名字，而**在这张表存在之前代码里没有任何地方说明谁对应谁**，
  所以「某个 aspect 没有生产者」在结构上无法被发现——这正是公交地铁那个缺陷没被发现的原因。注意
  名称相似度检查会失败（`driving` ≠ `drive`），所以对应关系必须声明而不是推断
- `scripts/probe_reachable_aspects.py` 用这张表全面检查（aspect 的两种去向、映射是否指向真实成员、
  每个 `TravelMode` 成员是否都能从某个请求复现），`tests/unit/test_provider_catalog.py` 把不变式
  钉进测试

**已完成的多源交叉验证**

设计细节见 [`docs/architecture/cross-source-validation.md`](architecture/cross-source-validation.md)。要点：

- 修掉一个「无声的表态」：合并多个来源的同一条实体时，原逻辑只处理「某个来源没说话」，
  一旦两个来源**都说了且说的不一样**，它留下最先遍历到的值，却仍把两个来源的引用都挂上——
  页面于是显示「¥60」配两个来源名，读起来像互相印证，而其中一个来源说的是 ¥80。
  这不是显示错误，是把分歧伪装成共识，比报错更糟：报错会被人发现
- 这个 bug 能长期存在，是因为原来的合并**只做加法**：只追加引用，从不比较值，因此没有任何测试能看见它
- 现在每条可比字段有一本完整的账：每个来源的值、观测时间，以及留下的那一条是谁说的、按什么规则选的。
  不做平均、不取中位数、不「综合判断」出一个谁都没说过的值；结果不依赖 provider 注册顺序
- 选择规则写进载荷：**来源实际报告的值优先于系统推算的估算值**（估算不是证据，不参与「谁更新」的比较），
  其次观测时间最新，再次抓取时间，最后来源名（纯粹为了确定性）
- 没有来源引用的说法拿 `_NO_TIME` 而**不是** `datetime.now()`：把缺失时间默认成「现在」会让最无法归因的
  值看起来最新，正好是时效性工作要防的那种倒置
- 明确不承诺：`disagreements == []` 只表示**说过话的来源彼此一致**，不是「数据被核实过」；
  系统比较的是来源之间的一致性，不是与现实的一致性。这条写进了 API 载荷、界面文案和文档
- 一个刻意的边界：`_collect_claims` 在任何成员被修改**之前**读取说法，并跳过已带分歧记录的字段。
  先合并引用再读，主记录的标量值会配上别人的引用，于是错误归因**并且**继承别人的时间戳——
  错误数字戴着自信标签。回归测试 `test_a_nested_merge_does_not_misattribute_the_merged_value`
- 两处断点都有人守：单元测试管合并逻辑，`tests/integration/test_disagreement_persistence.py` 管
  「存库往返」与「HTTP 响应可达」——一个记下来却传不到页面上的分歧，和没有分歧完全一样

**已完成的第 6 步：通用内容摄取**

设计细节见 [`docs/architecture/content-ingestion.md`](architecture/content-ingestion.md)。要点：

- 小红书 / 抖音 / 美团 / 大众点评 / 携程 / 同程**都没有公开 API**。所以走的不是「数据源」而是**摄取**：
  使用者的浏览器书签在他们自己已登录的页面上取正文，或直接粘贴。Traver 从不请求这些域名，
  也不保存任何登录态；脚本不读 cookie / localStorage / token（有测试断言）
- 提取器是**纯规则式**的，这是设计决定：零配置也能用（渐进增强）、每条规则说明自己的依据、
  且不会像语言模型那样在文本没说的地方补出一个看起来很合理的价格
- 五条诚实规则：称呼不是测量（一律 `content_reference`，绝不覆盖报价）、每条事实引用原文片段、
  置信度只描述「读得准不准」而非「是不是真的」、缺失保持缺失、发布时间缺席时如实标为未知
- 进入行程的方式是**只匹配不合并**：笔记贴合到它所谈论的实体上，实体的自有字段一个都不改；
  匹配不上的提及照样列出（那是数据缺口，藏起来会让笔记看起来被完整利用了）
- 排序把闭馆/警告放前面：那会改变能规划什么，提示不会

**已完成的第 6 步之二：把「文中提到的地点」落到坐标上**

设计细节见 [`docs/architecture/content-ingestion.md`](architecture/content-ingestion.md)。要点：

- 之前匹配不上的提及是个死胡同：名字在文中，我们的来源里没有，于是笔记里最具体的那部分
  完全用不上——而「你提供的越多，安排得越详细」正是这里最容易食言的承诺。现在这些地名会走
  新的 `resolve_places` 能力做地理编码
- **它是另一种东西，不是景点**：`ResolvedPlace` 与 `Attraction` 是两个类型，所以「笔记里提到的店」
  不可能在任何消费 `Attraction` 的地方混进去，也就不会凭空获得评分/票价/营业时间。
  有测试断言它既不出现在 `attractions` 里，也不出现在任何一天的 `items` 里——笔记说某人喜欢某处，
  不等于你决定要去，这个决定不由程序替你做
- **同名地点用目的地消歧**：西湖在杭州、惠州、扬州；人民公园在大多数大城市都有。查询本身限定范围
  （高德用 `city`；Nominatim 用 `viewbox` + `bounded=1`，`bounded` 是关键），答案再按到目的地的
  距离核对。超过 80 公里报 `elsewhere` 并给出距离——丢掉会看起来像查询失败，接受会把一站排到另一个省
- **排序是先精度、再距离**，顺序不能反：西湖区的质心比西湖风景名胜区更靠近杭州市中心，只按距离排
  会答错问题；而先按精度在全部候选里排，会让另一个省里更精确的结果顶掉手边可用的近答案。所以距离是
  **过滤器**，精度是过滤器内部的排序键。`PlaceCandidate` 因此把 `confidence`（来源有多确定）与
  `precision`（找到的东西有多具体，或没找到时留空且排序靠后）分成两个字段
- **四种结果，不是两种**：`located` / `elsewhere` / `not_found` / `not_attempted`。后两个必须分开——
  零配置的部署会持续产生 `not_attempted`，把它显示成「没有找到这个地方」就是对地点的一次凭空断言
- 地名索引的重建时间来源不公布，所以引用一律是 `source_silent`，只标注我们何时问的；
  `elsewhere` 与 `not_found` 也带引用，否则「在别处」只是无法核对的断言
- **提取出来的名字原样使用，不做二次修剪**：`_PLACE_PATTERN` 非贪婪匹配到第一个后缀，
  「推荐去灵隐寺看看」到达时已经是「灵隐寺」。写过一个下游修剪器，它的前提是假的，已删除并留下测试；
  提取器真正的毛病在反方向（断桥残雪 → 断桥），那是提取器的限制，两个来源清单都如实写了
- 两个来源（免费地理编码 / 高德）的判断逻辑合并在 `providers/place_matching.py` 一个模块里：
  各自的实现会漂移，而漂移不可见——同一个笔记，一个属主得到坐标，另一个得到拒绝

**已完成的第 4 步：时效性硬约束**

设计细节见 [`docs/architecture/realtime-data.md`](architecture/realtime-data.md)。要点：

- `observed_at` 与 `fetched_at` 严格分开：前者是**数据自身**的产生时间，后者是**我们何时去问**。`is_stale` 只说明「我们的副本过期了吗」，不说明数据有多新
- 新增 `ObservationBasis`：`provider_reported` / `live_computed` / `source_silent` / `unknown`。只有 `provider_reported` 时 `observed_at` 才真的在描述数据，`age_is_about_the_data` 因此只在那一档为真，未分类的来源**不可能**被当成新鲜
- 新增按事实类型区分的**数据年龄上限**（天气 1 小时、路线 1 小时、房态 30 分钟、报价 12 小时、餐厅/酒店/景点 24 小时），与缓存 TTL 分开——后者回答「该再去问了」，前者回答「这个事实开始不成立了」
- `TripPreview.stale_facts`：超限事实进入计划本身并随行程入库，前端有独立的「时效性提示」面板。`too_old`（有依据）与 `age_unknown`（无依据）**分开**，因为把后者报成过期同样是没有依据的断言
- 每条判定带 `evaluated_at` 与绝对 `observed_at`：结论是快照，可能在库里放几天，因此不能写成「N 小时前」——那句话放着不动就会变成假话
- **`age_unknown` 曾经算完就被丢掉**：只有 `too_old` 赋给了 `stale_facts`，于是真实免费源的一次规划里 `stale=0`、前端什么都不渲染——而没有警告等于「都检查过了」。实测那次 9 条引用里有 **5 条**无法判断数据自身多旧（天气、维基、节假日、OSRM 都不给数据生成时间）。现补 `TripPreview.age_unknown_facts` 与独立的中性色面板，按来源归并计数，并写明「这不等于全部数据都被核实过，只等于没有任何一条能被证明过期」。上面那句「分开」由此才成立
- 前端三种状态都有呈现：全部可验证（绿色，明说而非靠沉默暗示）／可疑过期（琥珀色逐条）／无法确认（中性色）。**字段缺失按未知处理**：旧 API 不发该字段，按空数组处理会为一份什么都没检查过的行程打印「全部可验证」——凭一个不存在的 key 造出核实结论（`apps/web/app/components/freshness-coverage.tsx`，11 条测试）

**已完成的第 7 步之二：导出（Markdown + 日历）**

设计细节见 [`docs/architecture/export.md`](architecture/export.md)。要点：

- 导出是**唯一离开应用**的产物，因此是时效性规则最强的用例而不是例外：应用内每个数字旁边都有来源标签与时效性面板，文件里没有。所以两个格式都写明生成时间、写明"数字不会自动更新"，并逐条印出**两个时间**（我们何时抓取 / 数据何时产生）以及后者到底是哪一种含义
- **相对时间永远标注它相对于什么**。"3 分钟前"写进文件就是一句等着变成假话的话；绝对时间同时打印
- 「已过期」与「无法确认」分开呈现，且**结论不取自辅助字段**：最省事的写法是 `年龄无法确认列表为空 → 打印「全部来源都给出了数据自身时间」`，但旧快照里这个字段可能根本不存在，"不存在"与"空列表"在 JSON 上无法区分——于是文档会凭空造出一个从未发生过的核实。改为从 `observed_at_basis` 推导（它是 `age_is_known()` 的纯函数，而且就印在文档自己的来源表里），因此这个结论不可能因为某个字段缺失而被制造出来。这条与 Web 端 `freshness-coverage.tsx` 是同一条不变式，用 25 条单元测试 + 5 条 HTTP 测试钉住
- **iCalendar 折行以字节计但绝不能在字符中间切**。第一版切字节再用 `errors="ignore"` 解码，理由是"下一段补上剩下的字节"——错的，而且静默毁文本：`数据新旧。数据来源` → `数据新旧。数来源`，丢了 `据` 和 `新`。日历仍能解析、事件仍出现。现改为逐字符累积，测试在 1/5/20/60/200 段长度上要求折行—还原**完全等值**
- **时间是浮动时间，不是 UTC**。行程时间是 naive 的"目的地当地 09:30"，加 `Z` 是断言 UTC（每个事件平移几小时），加 `TZID` 是断言一个行程里没有任何东西确立过的时区。浮动时间是 RFC 5545 表达"墙上时钟、时区未指定"的方式，恰好等于已知的东西。代价（客户端按本机时区渲染，东京行程在伦敦设备上显示 09:30 伦敦时间）被写进文件与界面；已知时区时（天气来源带回 `Asia/Shanghai`）会写进描述供解读，但**从不静默换算**——`zoneinfo` 需要 tz 数据库而项目未声明 `tzdata`，同一行程在不同机器上导出成不同时刻比说清自己不知道更糟
- **免责声明写在每个事件上**，不只在日历上：事件会被单独转发，日历级说明不跟着走。`STATUS:TENTATIVE` 同理——行程是计划不是预订
- **一个真实的 500**：`"".join(c if c.isalnum() else "-")` 让**每个中文目的地**返回 500，因为 HTTP 头部的值是 latin-1 而 `"杭".isalnum()` 是 `True`，中文原样通过后抛 `UnicodeEncodeError`。纯函数测试全通过——缺陷住在它们和客户端之间的传输层。修法是 RFC 6266 同时发 `filename`（ASCII 回退）与 `filename*=UTF-8''…`（整段百分号编码，这同时让头部注入不可能）。`scripts/probe_export_documents.py` 现在会读回真实响应检查这些性质
- 顺带修掉一处**语言不匹配**：行程冲突那条计数写的是英文（`N leg(s) use distance-based travel estimates`），于是它夹在一整篇中文之间出现在导出的行程里。新增 `tests/unit/test_user_facing_prose.py` 检查"服务端写给用户看的文本不得含裸露英文标识符"这条**性质**，而不是逐句比对字符串——逐句断言的测试和它断言的字符串互为副本，改一处就一起改，抓不到东西。它当场抓到两处（这条计数、以及补救说明里的 `profile`，后者是 OSRM 的参数名，已改为「路线类型」）。白名单只放真正的专有名词，每条都说明读者为什么能看懂

**这一轮修掉的两个「不吹嘘」缺陷**

- **高德天气把 `observed_at` 写成了我们的抓取时间**，尽管接口**明确返回** `reporttime`。真相就在手上却被一个更方便的默认值盖掉：六小时前发布的预报在结构化字段上读起来像「刚刚观测到」。现 `observed_at` 用 `reporttime`，`fetched_at` 仍是我们请求的时刻，两者分别保留
- **`reporttime` 时区坑**：高德返回的是北京时间且不带时区。按 UTC 解读会把发布时间放到八小时后的未来，`data_age_seconds` 被夹到 0，**旧预报反而显得最新**——唯一会「隐藏过期」的方向。已有专门测试钉住
- **Web 端把未知年龄的数据标成「实时」**：`is_stale` 对不提供时间戳的来源恒为 `false`，于是那个来源一律显示「实时」，等于声明我们并不知道的事。现分三种措辞并把判定逻辑抽到 `apps/web/lib/freshness.ts` 以便直接测试
- **「抓取时间与数据时间分别标注」曾经是假的**：三个分支里只有沉默来源带了抓取时间，`live_computed` 只写「实时计算」、`provider_reported` 只写「数据 X 分钟前」——而这两种恰是最常见的。「实时计算」是对一件过去的事的现在时（三天前生成的行程打开后仍写「实时计算」，读起来像刚刚算的），「数据 30 天前」也回答不了「我看的这份副本放了多久」。缺陷能活下来是因为原来那两条断言各只检查标签的后半截（`toBe("实时计算")`、`toBe("数据 30 天前")`），从没检查前半截。现所有 basis 都带 `抓取于 X`，并有一条测试遍历全部四种 basis 钉住这一点——`fetched_at` 是唯一永远已知的时间（模型里是必填），没有理由省略
- **新鲜度审计的配对列表漏了 `holiday`**：`MAX_AGE_SECONDS` 里节日没有时限，所以漏掉它当时**不改任何输出**，但 `FreshnessService` 的契约是「没有时限」与「有时限却没应用」必须可区分，而两者从外面看完全一样。现补齐配对，并加了一条覆盖测试：它给节日**配上一个时限**再要求出现过期告警——只有真的送进审计的引用才可能被判定，所以漏配对必然失败（已用变异验证：去掉 `hotel` 配对报 `search_hotels`，去掉 `holiday` 配对报节日未进审计）

## 配置卫生（随 Phase 4 完成）

清理了"看起来可配、实际无效"的死配置：

- `FREE_PROVIDER_NOMINATIM_FALLBACK` 之前完全没有接线，设为 `false` 仍会调用 Nominatim；现已传入 `GeoResolver`，并支持显式 `sources` 顺序以便固定来源
- `API_HOST` / `API_PORT` / `LOG_LEVEL` 之前没有任何代码读取；现由 `python -m travel_api` 消费（`docker-compose.yml` 也改用它）
- `REDIS_URL` 与 `redis` 依赖没有任何代码使用，已移除；缓存目前是进程内 TTL（地理编码 24h、POI 1h、天气 15min）。如需共享缓存，应连同真实实现一起加回
- `docker-compose.yml` 之前同时开启免费源与 mock 源，会让合成价格混进真实结果；现改为免费源开启、mock 关闭
- Nominatim 的 User-Agent 规则此前记录有误：实测被拒的是**含邮箱**的 UA 以及 httpx 默认 UA，而 `(+https://...)` 形式是被接受的
- `request_id` 之前只写入 `request.state` 与响应头，从未进入日志上下文，因此日志格式里的 `request_id=%(request_id)s` 恒为 `-`；现已用 `ContextVar` 接线，并补上每请求一条访问日志，日志行与 `x-request-id` 响应头可互相对应
- `logging.basicConfig` 在 root 已有 handler 时会**静默失效**（pytest 等会先装 handler），导致文档中的日志格式可能根本没生效；现已用 `force=True` 使其确定生效
- 本地建表存在两条并行路径（`AUTO_CREATE_SCHEMA` 与 Alembic）。已明确：本地可用前者，生产应设 `AUTO_CREATE_SCHEMA=false` 并只用 `alembic upgrade head`；`db/migrations/README.md` 此前称"迁移尚未添加"，与仓库中已存在的 3 个迁移矛盾，已按实际迁移链重写
- **事实纪律修正**：OSM 的 `stars` 是酒店**星级分类**而非住客评分，此前却被写入 `Hotel.rating`，而 `decision_engine` 以 `(rating or 0) / 5` 计分，等于让"五星级分类"拿到满分、无评分酒店拿到 0 分。现 `rating` 一律为 `None`，星级记录在 `SourceReference.request_context["star_classification"]`
- **数值解析修正**：`_parse_decimal()` 会跳过前导符号，`-1` 被解析为 `1`（畸形星级变合法、负数金额变正数价格），末尾的 `amount >= 0` 因此是死代码；现已保留符号并拒绝负值

随 Phase 4C 又发现并修掉三处：

- **空气质量请求范围错误**：`forecast_days` 沿用了天气的 16 天，而空气质量端点只接受 0–7，返回 400 后**所有 AQI 字段静默为空**。聚合层面的测试完全无法发现（字段本来就是可选的），只有真实请求会暴露。现固定为 `MAX_AIR_QUALITY_DAYS = 7`
- **`request_context["attribution"]` 存了 dataclass**：新 Provider 把 `Attribution` 对象直接放进 `request_context`，而该字段是 **JSON 列**，写入时报 `Object of type Attribution is not JSON serializable`。聚合测试全部通过，只有**持久化测试**会失败。现统一用 `Attribution.as_credit()` 产出字符串，并补上「所有免费来源引用的 `request_context` 必须可 `json.dumps`」的回归测试
- **两个标签同时存在时只用了其中一个**：景点同时带 `wikipedia` 与 `wikidata` 标签时用了 `elif`，导致 QID 没有中文 sitelink 时会**白白丢弃可用的 `wikipedia` 标签**；现已让两条路径都参与解析
- **真实网络测试的运行时预算过小**：`test_live_api_creates_and_persists_a_trip_from_real_data` 把 `FREE_PROVIDER_TIMEOUT_SECONDS` 提到 120 s，却没有提 `PROVIDER_TIMEOUT_SECONDS`（默认 15 s）。实测一次真实 Overpass 查询耗时 **36.5 s**，因此该测试此前一直在靠运气通过；现已同时提高运行时预算
