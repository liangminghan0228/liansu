# 连溯（liansu）使用规则

本仓库接入本地长期记忆 MCP。按需调用，不要每轮都调：

- **recall**：用户提到个人画像、历史决策、偏好、过往项目/经历，或问题依赖「他之前说过什么」时，先调 `liansu.recall(query)`。
- **expand**：需要顺着一条事实做关联/多跳推理（同实体、同主题扩散）时，调 `liansu.expand(fact_id, hops)`。
- **brief**：仅长任务开局要全局画像、或用户显式要求整体摘要时调用，平时别用（费 token 且可能 stale）。
- **record**：会话结束或用户明确说「记住」时，调 `liansu.record(dialog, source)`；正常由 Stop hook 自动触发。
- **rebuild**：索引/视图损坏或换了 embedding 模型时，调 `liansu.rebuild(verify=true)`。

事实源以 vault `journal/` 为准，召回结果里过期事实（`valid_to` 非空）已被时效过滤，可直接采信。
