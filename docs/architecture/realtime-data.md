# Realtime Data

Phase 2 已实现 `hotel_offer_snapshots` 和 `route_snapshots`。Offer 刷新追加历史，不覆盖旧值；`GET /api/v1/trips/{id}/snapshots` 返回 `current`、`history` 和相邻观察的 `diff`。快照通过 SourceReference 绑定 Provider、source id 和观察时间。

## Freshness 分级

| 数据 | 策略 |
| --- | --- |
| 房态、Offer 价格、票库存 | 短 TTL，默认刷新 |
| 餐厅评分、营业时间、POI | 中 TTL |
| 城市介绍、攻略描述 | 长 TTL |

每一条事实由 \`observed_at\`、\`fetched_at\`、\`expires_at\` 控制；前端必须在过期时显示“数据已过期，请刷新”，而不是继续装作实时。

## 快照

下一阶段将增加 \`hotel_offer_snapshots\`、\`restaurant_price_snapshots\`、\`weather_snapshots\`、\`route_snapshots\`，记录价格/库存/路线的变化，用于刷新后的差异解释和回滚。

## 故障降级

Provider 单独隔离。一个数据源超时不会让整次聚合返回 500；结果保留可用 Provider、失败状态和 request id，前端显示“本次结果基于哪些来源，哪些来源暂不可用”。
