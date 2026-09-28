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
- Web 端凭据仍是 `localStorage`：对同源脚本可读，一次 XSS 即可窃取。面向多人或公网部署时应改为后端签发 httpOnly Cookie 会话
- 会话级权限细分（当前只有 `trip:read` / `trip:write` 两个 scope）
- 国内 POI 深度仍弱于高德：OSM 在省会级城市充足（实测 6/6 打满限额），但县级/古镇类目的地依赖当地测绘贡献。**未接入高德**（会打破"零密钥"前提），如接入应做成"有 Key 优先、无 Key 回退 OSM"的可选 Provider

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
