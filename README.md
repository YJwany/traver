# AI Travel Information Aggregation & Planning System

事实驱动的旅游信息聚合与智能规划系统。系统把外部 Provider 的事实数据统一标准化、去重、校验时效，再交给规则/算法进行筛选、评分、预算和路线计算；AI 只负责理解需求、解释结果和编排工具调用。

## 当前阶段

Phase 0-4 的可运行底座已经落地：

- FastAPI API 与版本化路由
- Pydantic 领域模型与 SQLAlchemy 基础表
- 可插拔 Provider Protocol、能力路由、注册中心、健康检查
- **免费免密钥数据层**：OpenStreetMap/Overpass（POI）、Open-Meteo（天气与空气质量）、OSRM（路线）、Nominatim（地理编码兜底）、维基百科（景点简介）、`holidays` 库（公共节假日）
- Chengdu Mock/Fixture Provider，用于离线与确定性测试
- 标准化、来源引用、时效判断、实体去重
- 硬约束过滤、可配置酒店评分、程序化预算计算
- PostgreSQL/SQLite 持久化、Alembic、Repository、Unit of Work、Snapshot 和 Change History
- Route Optimizer、Budget Strategy、Trip Revision 和结构化 Tool Registry Contract
- Provider timeout/retry/circuit breaker/health metrics，按能力隔离熔断
- RequirementAgent、OpenAI-compatible Adapter、结构化 Tool Calling 和本地 deterministic fallback
- Trip Conversation、AI Tool Call/Run 日志、会话上下文裁剪
- Travel Workspace：创建旅行、旅行列表、行程、地图坐标视图、酒店/美食/景点、预算、来源、AI Chat 和修改 diff
- 异步规划作业与实时进度推送（SSE），重规划走同一套
- 导出：Markdown 完整记录（`GET /api/v1/trips/{id}/export.md`）与日历（`.../export.ics`），
  两种都写明生成时间、逐条标注「抓取时间 / 数据自身时间」与无法判断年龄的部分；
  Markdown 另附**你自己交进来的内容**（原文片段、识别把握、原文发布时间与你保存它的时间），
  因为一篇可能明天就被删掉的帖子与一个随时能重查的 POI 不是一回事
- **笔记里提到的地点会落到坐标上**（`resolve_places` 能力）：数据源没有的名字由地理编码补齐，
  结果单独列出、标明四种结果（已定位 / 在别处 / 没查到 / 没查过），**不会变成景点、也不会被排进行程**
- **1020 个离线 pytest 测试 + 8 个可选的真实网络端到端测试**；前端 232 个 vitest 测试
- PostgreSQL/PostGIS 与 API 的 Docker Compose 开发环境

## 这是一套"汇总工具"：数据源与模型都由你接入

Traver 自己**不提供数据，也不绑定任何模型**。它是一套把外部来源汇总成行程的工具，需要什么就接什么。因此有两件基础设施专门服务于"接入"：

- **Provider 清单与能力矩阵**（`GET /api/v1/providers/catalog`）——有哪些来源、各自解锁什么能力、还差哪些凭据。清单里 `planned` 的源**不贡献任何能力覆盖**：列出来不等于能用。`needs_credentials` 的源在**你没有填凭据时**同样不贡献覆盖——别人填了 Key 不算你的能力。
- **凭据保险箱**——你填入自己的 AI 模型与平台 Key，AES-256-GCM 加密后存库（可逆，因为要重放给上游），按属主隔离，环境变量作兜底，界面保存**不需要重启**。

设计原则是**渐进增强**：

| 你接入了什么 | 得到什么 |
|---|---|
| 什么都不填 | 免密钥数据源 + 本地确定性规划器，行程**照样完整可用** |
| 加一个 AI 模型 | 需求理解与工具编排更灵活（本地模型如 Ollama **不需要 Key**） |
| 加高德 Web 服务 Key | **公交/地铁规划**、国内 POI 精度、高德自己的驾车与步行耗时——公交地铁是免费源完全没有的能力（OSRM 不含班次数据，任何公共部署都没有 transit profile） |
| 加一个酒店供应 Key（道旅 / RollingGo） | **房型与实时房价**——同样是免费源完全没有的能力，预算里的住宿因此从「估算额度」变成真实报价。报价只贴到该来源自己发出的酒店上：别的来源找到的酒店不会被按名称匹配，因为贴错酒店的报价与贴对的在外部看起来一样 |
| 把小红书 / 抖音 / 美团 / 大众点评 / 携程 / 同程的内容交进来 | 这些平台**没有公开 API**。Traver 不抓取它们：用一个浏览器书签在你自己已登录的页面上取值，或直接粘贴正文。提取出的价格、时长、闭馆日、提醒与地名会作为**提示与估算**贴到对应地点上，并附上原文出处。其中地名（「推荐去灵隐寺」）会按目的地做地理编码——它们是**笔记里提到的**地点，不是查到的景点，因此没有评分/票价/营业时间，也不会被排进任何一天 |

同一次部署可以多人使用各自的模型与 Key，互不影响：数据源按属主解析，某人的 Key 只会解锁他自己的能力。

威胁模型与边界写在 [docs/architecture/credentials.md](docs/architecture/credentials.md)：它防数据库泄露、防篡改、防跨租户搬移，**不防**能读取整个文件系统或能在进程内执行代码的攻击者。这些说清楚比笼统说"已加密"更有用。

**先说清楚导出的东西是什么。** 一份行程可以导出成 Markdown 或日历，但导出的文件是**快照**：它的数字不会自动更新，文件里写明了生成时间，并且逐条标注每个来源的「抓取时间」（我们何时去问，永远已知）与「数据自身时间」（数据何时产生，不少来源并不提供）。时间按**目的地当地时间**写入日历，因为行程里没有记录时区——所以日历客户端会按设备时区显示它，跨时区时显示出来的时刻与当地时刻不同。这一点在文件和界面上都写着，而不是留给你自己发现。

## 数据来源：默认全部免费、无需密钥

默认数据层不依赖任何付费或需申请密钥的服务。默认开启免费源、关闭 mock 源，避免合成数据混入真实结果。

| 用途 | 服务 | 许可 | 速率约束 |
|---|---|---|---|
| POI（酒店/餐厅/景点） | Overpass API（OpenStreetMap） | ODbL 1.0 | 常规使用 < 100 次/天 |
| 天气 | Open-Meteo | CC BY 4.0 | 免费额度非商用，< 10k 次/天 |
| 空气质量 / 紫外线 | Open-Meteo Air Quality | CC BY 4.0 | 同上；仅覆盖未来 7 天 |
| 路线 | OSRM @ FOSSGIS（默认，含真实步行/骑行 profile） | ODbL 1.0 | 不超过 1 次/秒 |
| 地理编码 | Open-Meteo Geocoding，Nominatim 兜底（结果不可靠时也触发） | CC BY 4.0 / ODbL 1.0 | Nominatim 不超过 1 次/秒 |
| 景点简介 | 中文维基百科 / Wikidata | CC BY-SA 4.0 / CC0 | 机器人需串行请求 |
| 公共节假日 | `holidays` 库（本地数据） | MIT | 无网络请求 |

上述速率约束是**合规要求**而不是性能调参，已在 `providers/free/http.py` 的按主机限流器中强制执行。署名信息通过 `GET /api/v1/providers/attributions` 暴露，使用方必须展示。

来源选择有一条额外的硬判据：**许可必须允许本项目用途**。调研中技术可用的 `Nager.Date` 因条款只允许私人/非营利使用（商用需赞助）而被否决，改用 MIT 许可的本地 `holidays` 库；后者数据还更完整（含清明节与调休连续假期）。

### 免费数据层的边界

OpenStreetMap 是**地理事实**来源，不含商业事实。因此：

- 房价、评分、人均消费不会被编造。`search_room_offers` 返回空成功，预算中酒店项标记 `is_estimate=true` 并给出可配置的估算额度，而不是让总预算塌缩为 0。
- **路线默认走 FOSSGIS**（`routing.openstreetmap.de`，由 FOSSGIS e.V. 运营），它有真实的全球步行、骑行与驾车 profile。实测同一段路，`foot` 给 8386 m / 4.5 km/h，`driving` 给 9479 m / 48.3 km/h——**距离不同正说明 profile 是真的**：行人走的是另一套路网。步行时长因此是**实测值**（`is_estimate=false`），不是推算。
- **服务器不会因为做不到而报错，所以不能盲信。** 公共 OSRM demo 对 `foot`/`bike` 等任意 profile 都返回 HTTP 200 和与本车完全一致的驾车结果（约 40 km/h）。因此 Traver 对每个部署登记它真正支持的 profile，**未登记的主机一律按「只有驾车」处理**；在那种部署上步行只借**路网距离**（路网的属性），时长按明确步速推算并标 `is_estimate=true`（时长的模式属性绝不外借），约 40 km/h 的驾车数字不会渗进去。公交/地铁没有这种代理关系（公交既不按驾车路线走、距离也不同），所以**直接拒绝**而不是拿驾车数据顶上——这正是高德不可替代之处。
- 每条腿的来源里写明 `osrm_server`、`osrm_profile`、`duration_source`、`duration_is_measured`，以及路网图的新鲜度（FOSSGIS 约每两天重建一次）。单看一条腿就能判断它是观测还是推算，不需要知道部署指向了哪台服务器。
- 餐厅与景点之间的距离使用真实矩阵；当缺少路线时按坐标距离推算，并在行程 `conflicts` 中披露有多少段是推算值。
- 景点的维基简介**只跟随 OSM 显式 `wikipedia`/`wikidata` 标签**。按名称匹配实测经常出错（`火烈鸟` 会匹配到物种条目 `紅鸛科`，`人民公园` 会匹配到讲其他城市的消歧义页），因此覆盖率约 8% 但精度 100%——空描述是诚实的，错误描述是编造。
- 空气质量与节假日都只表达**已知事实**：空气质量端点只覆盖 7 天，超出范围的日期字段留空而不是填默认值；节假日不派生任何拥挤度评分，因为来源只说「哪天是假期」，不说「某个景点有多挤」。

商业数据请通过实现相同 Provider 协议接入，不要用未经授权的网页抓取代替正式接口。

## 本地运行

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python -m travel_api
```

`python -m travel_api` 从配置读取 `API_HOST`、`API_PORT` 和 `LOG_LEVEL`（开发环境下自动开启 reload）。也可以直接用 uvicorn：

```powershell
uvicorn travel_api.main:app --app-dir apps/api/src --reload
```

打开 http://127.0.0.1:8000/docs，健康检查为 http://127.0.0.1:8000/api/v1/health。

前端单独运行：

```powershell
cd apps/web
npm install
npm run dev
```

打开 http://127.0.0.1:3000。

没有 AI Key 时，Web 仍使用本地规则规划完整 Demo；接入 OpenAI-compatible 服务时，在 `.env` 配置 `AI_BASE_URL`、`AI_API_KEY` 和 `AI_MODEL`。业务层不会直接依赖具体厂商 SDK。

## 认证与行程隔离

默认 `AUTH_ENABLED=false`，本机开发无需任何密钥；`/api/v1/health` 会报告当前认证模式，未启用的部署不会被误认为已加固。

要对外暴露时先启用认证——没有认证时，任何能访问该端口的客户端都可以读写全部行程：

```powershell
# 1. 生成第一把密钥（只显示一次）
$env:DATABASE_URL='sqlite:///./travel.db'
python -m travel_api.cli create-key --owner alice --label laptop

# 2. 启动时启用认证
$env:AUTH_ENABLED='true'
python -m travel_api
```

密钥以 `trv_` 开头，服务端只保存 SHA-256 摘要与一个 12 字符的显示前缀，明文不落库、不可再取回。行程按属主隔离，一把密钥读不到另一把密钥创建的行程。

不止 `/trips`：**工具端点也要求凭据**。`POST /api/v1/tools/{name}` 曾经没有认证依赖，`?confirm_write=true` 只是确认而不是权限，而工具 handler 按对外发布的工具契约不收 owner，于是在仓储层以「不过滤」查行程——无凭据的调用方可以按 id 读到、并改掉别人的行程。现在它按工具自身的读/写类别校验作用域，并在调用期间绑定调用者属主；`GET /trips/{id}` 与经工具读同一个 id 会得到同一个答案。

公开路由只有四条（`/health`、`/providers/attributions`、`/providers/health`、只返回工具定义的 `GET /tools`），每条都写明理由。这份名单不是靠人维护的测试清单：`tests/integration/test_route_protection.py` 从应用自己的 OpenAPI 文档里读出全部路由逐条以无凭据身份请求，只有被明确声明为公开的才允许不是 401——新增一条忘记带认证依赖的路由会被立刻抓住。

`AUTH_ENABLED=false` 时的属主不是「没有」，而是 `AUTH_ANONYMOUS_OWNER`（默认 `local-dev`）：关闭认证意味着**谁都能访问**，不意味着数据没有归属。写出与读取必须用同一个答案，否则同一次请求里保存的东西下一次查不到。

```powershell
python -m travel_api.cli list-keys
python -m travel_api.cli revoke-key <key-id>
```

Web 端在 http://127.0.0.1:3000/settings 粘贴密钥。密钥只保存在该浏览器的 `localStorage`，不会进入前端构建产物：`NEXT_PUBLIC_*` 会被内联进 JavaScript 包，任何人打开页面都能读到，因此这里刻意不使用它。CI 会在构建后扫描产物，一旦出现 `NEXT_PUBLIC_API_KEY` 或字面量密钥就失败。

注意 `localStorage` 对同源脚本可读，一次 XSS 即可窃取，因此该密钥不应被视为高价值凭据。面向多人或公网部署时，应改为由后端签发 httpOnly Cookie 会话。

## 部署

生产镜像不挂载源码、不以 root 运行、从 `requirements.lock` 安装，并带就绪探针（探针会真正往返数据库，而不是只判断进程存活）：

```powershell
$env:POSTGRES_PASSWORD = "<一串足够长的口令>"
docker compose --profile prod up --build
```

开发栈（挂载源码、开启 reload）：

```powershell
docker compose up --build
```

`POSTGRES_PASSWORD` 没有默认值，未设置时 compose 会直接拒绝启动，避免把可用口令写进仓库。

配置项见 `.env.example`，它覆盖了全部可用环境变量及其含义；`tests/unit/test_env_example.py` 会在两个方向上校验它与 `Settings` 保持同步。

## 测试

离线测试不访问网络，使用 `httpx.MockTransport` 回放录制形态的响应：

```powershell
pytest -q
ruff check .
ruff format --check .
python -m compileall -q apps/api/src

# Alembic（PowerShell）
$env:DATABASE_URL='sqlite:///./travel_alembic.db'
alembic upgrade head
```

前端：

```powershell
cd apps/web
npm run typecheck
npm run lint
npm test
npm run build
```

真实网络端到端测试默认跳过，需要显式开启。它们会真的请求上述社区服务：

```powershell
$env:TRAVER_LIVE_TESTS='1'
python -m pytest tests/integration/test_free_providers_live.py -q -s
```

这些测试断言真实数据可用，并且**不会出现被编造的事实**：步行速度必须在物理合理区间、不得出现凭空的房价、总预算不得为 0。

## CI

`.github/workflows/ci.yml` 分三个作业：

| 作业 | 内容 |
|---|---|
| API | `ruff check`、`ruff format --check`、`pytest` |
| Migrations | `alembic upgrade head` → `downgrade base` → `upgrade head`，再用 autogenerate 断言模型与迁移无漂移 |
| Web | `tsc --noEmit`、`next lint`、`vitest`、`next build`，并扫描产物确认没有密钥被内联 |

依赖从 `requirements.lock` 与 `apps/web/package-lock.json` 安装，因此 CI 与本地解析到同一组版本。

## 设计原则

详见 docs/architecture/README.md。真实平台接入必须通过 Provider 实现，不能让平台 API 渗透进 Travel Engine，也不能用未经授权的网页抓取代替正式接口。

免费数据源的实现细节与合规数值见 [docs/providers/free-sources.md](docs/providers/free-sources.md)。
