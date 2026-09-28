# AI System

当前已实现 RequirementAgent、OpenAICompatibleAdapter、ToolOrchestrator 和 TravelAIService。无 AI Key 时使用确定性的本地规划路径，因此 Mock/Fixture 环境仍可完整演示；配置 AI_BASE_URL 与 AI_API_KEY 后，模型只通过 ToolRegistry 获取事实和执行结构化修改。

## AI 边界

AI 负责需求理解、偏好解释、候选比较、行程编排、自然语言修改和方案总结。事实仍由 Provider、数据库和计算引擎提供。

当前内部服务：

\`\`\`text
RequirementAgent
ConversationContextBuilder
ToolOrchestrator
TravelAIService
TripService / Decision / Budget / Route
\`\`\`

第一阶段使用内部 service/tool interface，不急于 MCP 化。

## Tool Calling 合同

工具返回 JSON 结构，不返回大段自然语言。ToolDefinition 同时声明 input schema、output schema、permission 和 read/write kind。Read 工具默认可调用，Write 工具必须由服务层根据用户明确意图传入 allow_write=True。单轮最多 20 次工具调用、同一工具最多 5 次。

AI 执行顺序是：读取当前 Trip 与近期会话 → RequirementAgent 补齐需求 → Read tools → Decision/Budget/Route → 必要时 Write tool → 重新从数据库读取最终 Trip → 生成解释。模型生成的 JSON 不是事实层，也不能直接写入数据库。

正式适配器是 `OpenAICompatibleAdapter`，业务层只依赖 `AIProvider`。它记录 model、token usage、latency、estimated cost 和 success/error；API Key 永远不进入会话内容或数据库。

## Policy

`TravelAIService` 强制执行以下规则：只使用用户输入、当前 Trip 和工具结果；金钱字段必须带观察/估算状态；实时事实过期先 refresh，刷新失败则明确标记最近一次记录；预算、路线和候选评分由程序计算；没有用户明确修改意图时不调用 Write tool；未知字段保持 unknown 并向用户询问。

## 会话持久化

一个 Trip 对应一个 `TripConversation`，消息、工具调用和 AI run 分别落在 `conversation_messages`、`ai_tool_calls`、`ai_runs`。`ConversationContextBuilder` 只发送当前 Trip、最近 N 条消息和相关来源，不把整个历史数据库塞给模型。`GET /api/v1/trips/{id}/conversation` 可回读会话。

## 引用

AI 生成的每个关键事实应能回指 SourceReference 或 CalculationReference。解释采用“当前配置下更偏向”的权衡语言，不输出无来源的绝对结论。
