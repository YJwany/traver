# Providers

本目录记录 Provider 层的实现细节与接入要求。

- [free-sources.md](free-sources.md) — 免费、免密钥数据源（Overpass/Open-Meteo/OSRM/Nominatim）的端点、许可、合规速率，以及实测发现的限制与应对方式
- [free-api-catalog.md](free-api-catalog.md) — 已实测的免费 API 候选清单（含空气质量、节假日、维基内容、汇率、公共交通），以及返回 200 但实际不可用的假阳性
- [../architecture/hotel-supply-integration.md](../architecture/hotel-supply-integration.md) — 商业酒店供应源（道旅 / RollingGo，MCP 形态，自带 Key）：房型与实时房价，以及为什么接它需要先把 `search_room_offers` 的入参从 id 改成实体
- [../architecture/provider-system.md](../architecture/provider-system.md) — 能力路由、注册中心、运行时保障与真实接入要求

## 接入清单

新增 Provider 前请确认：

1. **访问合法性** — 官方或授权接口，不用未经授权的网页抓取冒充正式接口。
2. **能力声明** — 只声明真正提供的能力 Protocol，不必实现完整接口。
3. **认证** — 凭据经配置注入，不硬编码。
4. **超时与重试** — 尊重基线预算，需要时用自身 `timeout_seconds` 覆盖；区分瞬时与永久错误。
5. **速率限制** — 把来源的使用条款写成代码里的最小请求间隔，而不是只写在文档里。
6. **标准输出** — 返回领域模型，不暴露平台特有字段。
7. **来源引用** — 每个事实携带 `SourceReference`，含 `observed_at`/`fetched_at`/`expires_at`、`DataDomain` 与署名。
8. **事实纪律** — 缺失的商业事实留空并标记 `is_estimate`，绝不填充猜测值；空结果集是成功而非错误。
9. **时效 TTL** — 与 `FreshnessService` 的策略一致。
10. **健康与测试** — 实现 `health()`；离线测试用 `httpx.MockTransport` 回放，必要时补可选的真实网络测试。
