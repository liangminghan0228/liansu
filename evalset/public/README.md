# 公开评测集

本目录用于存放**公开数据集**的转换结果。当前仓库**不实际下载**原始数据（避免把大文件与许可纠葛带进 git），只约定下载来源、统一格式与转换方法。需要跑 T15 评测框架时，按下方步骤自行下载并放置到本目录。

## 统一格式

转换后每个数据集产出一个 JSONL 文件，每行一条，字段与自建集 `evalset/own/gold.jsonl` 完全一致：

```json
{"question": "...", "gold_facts": [{"id": "...", "subject": "...", "predicate": "...", "object": "...", "statement": "...", "valid_from": "YYYY-MM-DD", "confidence": 0.9, "topics": ["..."], "source": "..."}], "answer": "...", "source": "LongMemEval"}
```

字段说明见 `docs/evalset.md`。顶层 `source` 填数据集名称（如 `LongMemEval`），以区别于自建集的 `自建`。

## 下载来源与转换方法

| 数据集 | 下载来源 | 特点 | 转换要点 |
|---|---|---|---|
| **LongMemEval**（首选） | GitHub：`xiaowu0162/longmem-eval`，仓库 `data/` 目录 | 长程记忆、含知识更新/时序问题 | 取 `question`；将 `answer` 作为答案；把支撑证据（含 `session_date`）映射为 gold facts，日期写进 `valid_from`；知识更新类问题用新事实作 gold，旧事实不写入 |
| **LoCoMo** | GitHub：`microsoft/LoCoMo` 或 `facebookresearch/LoCoMo` | 多会话对话记忆、指代与多跳 | 每条多跳问题把跨会话的支撑句拆成多条 fact；`valid_from` 用对话日期 |
| **HotpotQA** | 官网 `hotpotqa.github.io`（`hotpot_train_v1.1.json` 等） | 维基多跳问答，BM25/图召回基线 | 用 `question`/`answer`；`context` 中两条支撑段落转成两条 fact；无真实日期时 `valid_from` 用统一占位日（如 `2000-01-01`）并在文档注明 |
| **FreshQA** | GitHub：`freshqa`(Future-Land/Google) 官方仓库 | 含时效变化与世界知识更新 | 已有 `questions.json`；`answer` 与 `latest_answer` 取最新值；旧答案对应的实体状态转成被 supersede 的旧事实，标注 `must_not_include` |

> 通用转换脚本骨架建议放在 `evalset/public/convert.py`（本任务不要求实现）：读原始 JSON → 抽取 question/answer/evidence → 用 `liansu.schema.Fact` 校验每条 gold fact → 写 JSONL。`confidence` 无原始标注时统一填 `0.9`。

## 许可与注意事项

- 各数据集遵循各自 LICENSE，下载后按原许可使用；本仓库只提交转换后的最小必要字段与出处，不提交原始大数据文件。
- 保持 `gold_facts` 为**对象数组**而非 id 数组，便于 T15 直接比对 statement 与时效字段。
- 公开集为英文、非个人场景，用于证明方法有效；真实场景调参以自建集为主（见 TECH_DESIGN §5.9 方案 C）。
