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

- 自建 OSRM 实例以支持真实步行/骑行路径（公共 demo 只提供驾车路径）
- 高德/天气与正式授权商业 Provider（房价、库存、评分）
- 面向出站调用的商业来源凭据轮换、配额与数据用途审查
  （入站认证与限流见 Phase 6；这里指的是**调用第三方**时的凭据与合规治理）

## Phase 6 — Production hardening（部分完成）

### 已完成

**写入门禁修复（先于功能，因为它是正确性漏洞）**

- `POST /tools/{name}` 曾无条件传入 `allow_write=True`，使 AI 工具路径上的写意图检查可被任何直接调用该端点的客户端完全绕过；现由请求本身推导，并补齐 HTTP 层回归测试
- 写意图正则此前会把否定句判为写入请求（"不要预订"被读成预订）；现在先检测否定

**认证与租户隔离**

- Bearer API Key，服务端只保存 SHA-256 摘要与 12 字符显示前缀，明文不落库
- 行程按属主隔离：一把密钥读不到另一把密钥创建的行程
- `python -m travel_api.cli` 提供 create-key / list-keys / revoke-key
- `AUTH_ENABLED` 默认 `false` 以便本机开发，且 `/api/v1/health` 会报告当前认证模式——未加固的部署不会被误认为已加固

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

- 自动渲染的设置页与能力矩阵 UI（服务端接口已完成，Web 侧对 `needs_credentials` 状态与"
  公交/地铁"缺口的呈现待补）
- 高德之外的第二个真实数据源（抖音/美团/携程/同程均**无公开 API**，需走内容摄取路线）
- 通用内容摄取（浏览器小工具 + 粘贴入口，用使用者自己的登录态覆盖小红书/抖音/美团/携程/同程）
- 时效性硬约束的落地：所有数据带抓取时间、过期标记、近期优先（高德路径已带抓取时间与短过期，全局策略待统一）
- 异步任务与进度推送（实测单次 Overpass 查询 36.5 s）、多城市、i18n、可视化、导出、多源交叉验证

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
