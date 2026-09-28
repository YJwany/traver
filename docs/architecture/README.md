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

## 插件化与自带密钥

Traver 本身不提供数据，也不绑定模型。这两件事都由使用者接入，因此有三块基础设施专门服务于"接入"：

- [provider-system.md](provider-system.md) — Provider 清单与能力矩阵：有哪些来源、解锁了什么、还差什么。
- [credentials.md](credentials.md) — 凭据保险箱：加密可逆、按属主隔离、环境变量作兜底、界面保存免重启。
- [realtime-data.md](realtime-data.md) — 时效性硬约束：面向未来的攻略只能用近期数据。
- [cross-source-validation.md](cross-source-validation.md) — 多源交叉验证：来源说法不一致时如实记录，而不是把分歧伪装成共识。
- [routing.md](routing.md) — 路线：距离属于道路、时长属于交通方式、覆盖范围属于工具，三者分开交代。
- [planning-jobs.md](planning-jobs.md) — 异步任务与进度推送：步骤由服务端上报，未完成的步骤不能报完成。

设计原则是**渐进增强**：0 配置必须可用（免密钥数据源 + 本地确定性规划器），每接入一个源就解锁一批能力。不存在"必须先填 5 个 Key 才有输出"的情况。
