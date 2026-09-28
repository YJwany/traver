# Architecture Overview

## 目标

系统是“事实聚合 + 标准化 + 实体解析 + 决策 + 规划 + AI 解释”的旅行工作区，不是让 LLM 自由生成旅游事实的聊天机器人，也不是把平台预订能力当成核心域。

## 分层

```text
Provider / Fact Layer
        ↓
Normalization Layer
        ↓
Entity Resolution Layer
        ↓
Candidate & Decision Layer
        ↓
Route / Budget Engines
        ↓
AI Planning Layer (tool calling, explanation)
        ↓
API / Web Presentation Layer
```

## 一条请求的生命周期

1. 用户提交 \`TripProfile\`，硬约束与软偏好分开保存。
2. Provider Registry 并发调用可用 Provider；每个 Provider 独立隔离失败。
3. 外部结果进入标准化和实体去重，保留来源与匹配置信度。
4. Decision Engine 先过滤硬约束，再按配置权重评分。
5. Budget Engine 以程序计算商业金额，不把金额计算交给 LLM。
6. Route Optimizer 使用地点、时间窗、停留时长和交通数据生成日程。
7. AI 通过结构化工具读取候选、解释计算结果、修改 Trip Object，不直接成为事实数据库。

## Phase 2 持久化闭环

当前已实现：

- Alembic 初始迁移，支持 upgrade/downgrade 和 SQLite/PostgreSQL
- Trip、Profile、Offer、Snapshot、Budget、Itinerary、Decision、SourceReference、Change History 持久化
- Repository + Unit of Work 事务边界；失败自动 rollback
- FixtureLoader 与 Mock Generated Provider 二选一，核心 Aggregation 不感知数据来源形态
- Provider timeout、指数退避重试、熔断和健康指标
- FreshnessService 统一处理 TTL、过期和剩余有效期
- Route Optimizer 的 opening hours、travel time、visit duration 和 pace buffer
- BudgetStrategy、预算状态（planned/estimated/observed/confirmed）和预算重分配候选
- TripRevisionService 的预算、酒店、节奏、步行/景点局部修订与 change history
- ToolRegistry 和 AI Adapter Contract；模型尚未接入，工具返回结构化 JSON

持久化采用“Trip 聚合状态 JSON + 可查询事实/快照子表”组合：聚合回读保证 API 一致性，子表为价格历史、来源追溯、路线和决策分析提供查询基础。
