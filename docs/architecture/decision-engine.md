# Decision Engine

预算层支持 `strict`、`balanced`、`comfort`、`time_first`、`food_first` 五种策略。

**策略改变的是「估算额度」，不是价格，也不改变替换顺序。** 这一句此前写的是「策略改变候选权重和替换顺序」，而它是假的，两个分句都假：

- 四个权重（`hotel_weight` / `food_weight` / `transport_weight` / `comfort_penalty`）被算出来之后**没有任何地方读过**。`calculate` 建好 `BudgetStrategy` 只用了 `name` 一个字段，把它存进 breakdown 当标签。实测同一份输入，五种策略给出**完全相同**的总价与逐项金额（`3760` 与 `['1600','1600','360','200','0']`），而策略是用户可选的（`planning_jobs.py`、`routes.py`）。五个名字是同一个行为的五个名字；
- 「替换顺序」由 `decision_engine` 的 `DecisionWeights` 决定，不在这里，`reallocate` 也没有生产调用方。

现在策略作用于**没有来源报价时的兜底额度**，并且只能作用于它：

| 允许 | 不允许 |
| --- | --- |
| 缩放 `PriceFallback` 的每类额度（酒店/餐饮/交通） | 缩放任何来源报出的价格——那会把一个观测值乘上一个偏好，再挂上供应商的名字 |
| 让 `strict` 对住处的**估算**高于 `comfort` | 改变排序（排序不在这一层） |
| 在每一类都有报价时**完全无效** | 把带权重的估算提升为 `OBSERVED`——带权重的猜测仍然是猜测 |

最后一行是这条规则最实用的表述：**估算没了，就没东西可加权**，所以一份全部有报价的预算不因策略而变。`comfort_penalty` 刻意不做第四个乘数：它的含义是「这个策略有多不愿意砍东西」，那是替换顺序的事，把它乘到各类额度上会让 `comfort` 在每一类上都最贵，正好和名字相反。

超预算时 BudgetEngine 生成按体验影响排序的组合调整候选。

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
