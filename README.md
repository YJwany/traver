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
- 204 个离线 pytest 测试 + 8 个可选的真实网络端到端测试
- PostgreSQL/PostGIS 与 API 的 Docker Compose 开发环境

## 数据来源：全部免费、无需密钥

默认数据层不依赖任何付费或需申请密钥的服务。默认开启免费源、关闭 mock 源，避免合成数据混入真实结果。

| 用途 | 服务 | 许可 | 速率约束 |
|---|---|---|---|
| POI（酒店/餐厅/景点） | Overpass API（OpenStreetMap） | ODbL 1.0 | 常规使用 < 100 次/天 |
| 天气 | Open-Meteo | CC BY 4.0 | 免费额度非商用，< 10k 次/天 |
| 空气质量 / 紫外线 | Open-Meteo Air Quality | CC BY 4.0 | 同上；仅覆盖未来 7 天 |
| 路线 | OSRM demo（FOSSGIS） | ODbL 1.0 | 不超过 1 次/秒 |
| 地理编码 | Open-Meteo Geocoding，Nominatim 兜底 | CC BY 4.0 / ODbL 1.0 | Nominatim 不超过 1 次/秒 |
| 景点简介 | 中文维基百科 / Wikidata | CC BY-SA 4.0 / CC0 | 机器人需串行请求 |
| 公共节假日 | `holidays` 库（本地数据） | MIT | 无网络请求 |

上述速率约束是**合规要求**而不是性能调参，已在 `providers/free/http.py` 的按主机限流器中强制执行。署名信息通过 `GET /api/v1/providers/attributions` 暴露，使用方必须展示。

来源选择有一条额外的硬判据：**许可必须允许本项目用途**。调研中技术可用的 `Nager.Date` 因条款只允许私人/非营利使用（商用需赞助）而被否决，改用 MIT 许可的本地 `holidays` 库；后者数据还更完整（含清明节与调休连续假期）。

### 免费数据层的边界

OpenStreetMap 是**地理事实**来源，不含商业事实。因此：

- 房价、评分、人均消费不会被编造。`search_room_offers` 返回空成功，预算中酒店项标记 `is_estimate=true` 并给出可配置的估算额度，而不是让总预算塌缩为 0。
- 公共 OSRM demo 服务器**只提供驾车路径**。实测对 `foot`/`walking`/`bike` 等任意 profile 都静默返回相同的驾车结果（约 40 km/h），因此步行时长由实测道路距离按明确步速推算，并标记为估算值，绝不把驾车时间当作步行时间返回。
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

启动 PostgreSQL、Redis 和 API：

```powershell
docker compose up --build
```

配置项见 `.env.example`，它覆盖了全部可用环境变量及其含义。

## 测试

离线测试不访问网络，使用 `httpx.MockTransport` 回放录制形态的响应：

```powershell
pytest -q
ruff check .
python -m compileall -q apps/api/src

# Alembic（PowerShell）
$env:DATABASE_URL='sqlite:///./travel_alembic.db'
alembic upgrade head
```

真实网络端到端测试默认跳过，需要显式开启。它们会真的请求上述社区服务：

```powershell
$env:TRAVER_LIVE_TESTS='1'
python -m pytest tests/integration/test_free_providers_live.py -q -s
```

这些测试断言真实数据可用，并且**不会出现被编造的事实**：步行速度必须在物理合理区间、不得出现凭空的房价、总预算不得为 0。

## 设计原则

详见 docs/architecture/README.md。真实平台接入必须通过 Provider 实现，不能让平台 API 渗透进 Travel Engine，也不能用未经授权的网页抓取代替正式接口。

免费数据源的实现细节与合规数值见 [docs/providers/free-sources.md](docs/providers/free-sources.md)。
