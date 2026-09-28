# Data Model

## 核心实体

\`\`\`text
Trip
 ├─ TripProfile
 ├─ Hotel / HotelRoomType / HotelRatePlan / HotelOffer
 ├─ Restaurant / Attraction / Location
 ├─ RouteLeg / RouteSnapshot
 ├─ BudgetBreakdown / BudgetItem
 ├─ SourceReference
 └─ ChangeHistory
\`\`\`

酒店价格只存在于 \`HotelOffer\`，不直接挂在 \`Hotel\` 上。Offer 包含入住日期、人数、房型、Rate Plan、税费、库存、取消/支付策略和数据来源。

## 来源是一等实体

所有外部事实携带：\`source\`、\`source_type\`、\`source_id\`、\`observed_at\`、\`fetched_at\`、\`expires_at\`、\`request_context\`、\`data_version\`、\`confidence\`、\`domain\`。商业事实和系统估算通过 \`is_estimate\` / \`DataDomain\` 区分。

## 持久化第一版

Alembic 当前迁移已覆盖：

- `trips`、`trip_profiles`、`trip_changes`
- `hotels`、`hotel_room_types`、`hotel_rate_plans`、`hotel_offers`、`hotel_offer_snapshots`
- `restaurants`、`attractions`、`route_snapshots`
- `trip_budgets`、`trip_budget_items`、`trip_itinerary_days`、`trip_itinerary_items`
- `decision_scores`、`source_references`、`provider_requests`、`provider_responses`

Trip 使用“可回读的聚合 state_json + 可查询子表”组合：state_json 保证 API 重建出的 Trip Object 完整一致，子表负责快照历史、来源审计、预算、路线和决策查询。生产环境将关闭 `AUTO_CREATE_SCHEMA`，只通过 Alembic 管理结构变更。
# Phase 3 additions

AI 交互状态与 Trip 一起持久化，但不进入事实层：

```text
trips
 └── trip_conversations (one-to-one)
      ├── conversation_messages
      ├── ai_tool_calls
      └── ai_runs
```

`conversation_messages` 保存角色、文本、工具名、参数摘要、结果摘要、模型与延迟；`ai_tool_calls` 保存一次工具执行的成功状态和结构化结果摘要；`ai_runs` 保存 request id、模型、token usage、估算成本和错误。API Key、完整敏感 prompt 与 provider secrets 不进入这些表。
