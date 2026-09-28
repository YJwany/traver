# Decision Engine

预算层支持 `strict`、`balanced`、`comfort`、`time_first`、`food_first` 五种策略。策略改变候选权重和替换顺序，不改变 Provider 事实价格；超预算时 BudgetEngine 生成按体验影响排序的组合调整候选。

## 两阶段决策

1. Hard Constraint Filter：日期、人数、库存、预算、取消政策等不满足即淘汰。
2. Soft Preference Score：价格、位置、交通、评分、行程适配、用户偏好按配置权重评分。

当前酒店评分字段：

\`\`\`text
price + location + transit + rating + itinerary_fit + user_preference
\`\`\`

默认权重配置在 \`DecisionWeights\`，后续可从策略配置表加载。返回 \`CandidateDecision\`，包含总分、分项分数、命中/失败约束和解释数据，供前端和 AI 复用。

## 稳定性

LLM 不参与算分。偏好发生变化时，只改变约束/权重并重新计算，保证同一输入和版本下结果可复现。
