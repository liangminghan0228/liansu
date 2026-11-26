# 连溯评测报告（T15 · 消融与指标）

> 状态：**框架已跑通，指标数字待真实 vault 数据 + 真实 embedder 到位后填充**。
> 本文不含任何未经实测的数字；表格中的 `TBD` 需在真实环境跑完后替换。

## 1. 目的

回答两个问题，而不是堆指标：

1. **每一层检索是否真的有用**：BM25 → 向量 → 图 → 时效过滤，逐级看边际增益，避免无效复杂度。
2. **时效正确性是否成立**：问「现在住哪 / 还在不在职」时，返回的必须是当前事实，不能混入已被 supersede 的旧事实。

评测口径对齐 `docs/TECH_DESIGN.md` §4.3 与 §5.9：自建集调真实场景，公开集（LongMemEval / LoCoMo / HotpotQA / FreshQA）证明方法（公开集接入不在本次范围内）。

## 2. 评测集

- 路径：`eval/datasets/own/gold.jsonl`，当前 24 条，来源自建。
- 字段：
  - `question`：查询
  - `gold_facts[]`：完整 fact dict，评测只取其中的 `id` 作为 gold id
  - `answer`：参考答案文本，供 judge 使用
  - `must_not_include[]`（部分行）：已被 supersede 的旧 fact id，命中即判时效错误
- 覆盖：身份/教育/工作经历/身体状况/技术栈/项目/求职意向/偏好，其中 10 条是「旧值陷阱」问题（居住地、体重、薪资底线、岗位、编程语言、公司类型、在职状态、项目成绩有效性、技术方向、求职目标城市），带 `must_not_include`。

## 3. 指标定义

实现见 `eval/metrics.py`，全部为纯函数，输入统一是 `list[fact_id]` + `set[fact_id]`。

| 指标 | 定义 | 取值 |
|---|---|---|
| HitRate | TopK 结果是否命中任一 gold（`gold ∩ results ≠ ∅`） | 0 / 1，跨 query 平均 |
| Recall@K | `|top_k ∩ gold| / |gold|` | 0~1，跨 query 平均 |
| MRR | 第一个命中 gold 的结果的倒数排名 `1/rank`，未命中为 0 | 0~1，跨 query 平均 |
| 时效正确率 | 结果中**不含** `must_not_include` 的任何 id 记为 1，否则 0 | 0 / 1，只对带 `must_not_include` 的 query 平均 |

约定与边界：

- gold 为空的样本按 0 分处理，不把无标注样本算成满分。
- 时效正确率只统计「有旧事实陷阱」的 query（`n_temporal` 记录条数）；无陷阱样本返回真空 1.0 但不参与平均，避免稀释。
- 平均延迟为 `fuse.recall` 单次调用耗时（毫秒），不含索引构建。
- `avg_tokens` 当前为占位（`None`）：纯检索路径不调 LLM，token 成本要在接入生成/重排后统计。
- Judge（Faithfulness / Answer Relevancy）复用 `LLMProvider.judge`，**可选**：不传 provider 时整列不出现，评测不依赖 LLM。

## 4. 消融 Stage 定义

`FuseIndex.recall(query, top_k, stage, when)` 的 stage 开关：

| Stage | 组成的召回/过滤 | 预期增益来源 |
|---|---|---|
| 0 | 仅 BM25（jieba 分词） | 基线：精确词面命中 |
| 1 | + 向量（bge-small-zh + sqlite-vec） | 同义/近义改写，词面不重合 |
| 2 | + 图（networkx 双时态多跳） | 关联实体、间接事实 |
| 3 | + 时效过滤（`is_valid_at(when)`） | 排除被 supersede 的旧事实 |

`Stage 4 · +LLM 重排`（TASKS.md 提到的最后一层）**尚未接入**：`FuseIndex.recall` 目前没有 rerank 钩子，runner 不自造重排逻辑以免评测与线上路径不一致。接入时在 fuse 内加钩子（或注入 reranker），runner 的 `stages` 参数无需改动即可纳入 `4`。

## 5. 运行方法

在项目根目录（已存在 `.venv`）：

```bash
# 全部 stage，top_k=5
.venv/bin/python -m eval.runner --gold eval/datasets/own/gold.jsonl --stages 0,1,2,3 --top-k 5

# 只看关键两级，并把指标落成 JSON
.venv/bin/python -m eval.runner --stages 0,3 --json /tmp/eval.json

# 指定 journal 目录（默认项目根 journal/）
.venv/bin/python -m eval.runner --vault /path/to/vault

# 单测（纯函数 + StubFuse，不依赖真实 journal / 网络）
.venv/bin/python -m pytest tests/test_eval.py -q
```

`eval/runner.py` 自带 `src/` 路径引导，无需先 `pip install -e .`，也无需手设 `PYTHONPATH`。

### 输出示例（表结构）

```
| Stage | HitRate | Recall@5 | MRR | 时效正确率 | 平均延迟(ms) |
|---|---|---|---|---|---|
| 0 · BM25 | ... | ... | ... | ... | ... |
| 1 · +向量 | ... | ... | ... | ... | ... |
| 2 · +图 | ... | ... | ... | ... | ... |
| 3 · +时效 | ... | ... | ... | ... | ... |
```

传入 provider 后会多出一列 `Judge`。

## 6. 当前实测状态

| 项目 | 状态 |
|---|---|
| 指标函数 + 单测 | 完成（20 条用例，覆盖命中/未命中/空结果/多 gold/时效陷阱） |
| 消融执行器 + 对比表 | 完成，`StubFuse` 单测覆盖 stage 传递、top_k 截断、judge 可选/失败降级 |
| 端到端链路 | 已用「gold_facts 合成临时 vault（21 条事实）」跑通全流程，确认 CLI/索引/聚合无阻塞；该次数字仅用于链路自检，**不作结论** |
| 真实 vault 跑分 | **待跑**：仓库当前无 `journal/` 数据，直接跑为全 0 |
| 向量路（Stage1） | **待接入**：仓库尚无 `Embedder` 实现，故 Stage1 与 Stage0 结果相同，仅占位 |
| 图路（Stage2） | 已接入 networkx GraphIndex；增益依赖真实数据中的实体关联密度 |
| 时效（Stage3） | 逻辑已接入；**区分度前提是库内真实存在被 supersede 的旧事实**，否则全为 1.0 无信息量 |
| token 成本 | 占位（`None`），待生成/重排接入 |
| 抽取 precision | 本报告未覆盖（gold 集不含抽取错误样本）；已另建端到端入口 `eval/e2e_runner.py`，见 `docs/e2e-eval.md` |
| 公开集 | 未接入 |

### 待填充指标表

| Stage | HitRate | Recall@5 | MRR | 时效正确率 | 平均延迟(ms) |
|---|---|---|---|---|---|
| 0 · BM25 | TBD | TBD | TBD | TBD | TBD |
| 1 · +向量 | TBD | TBD | TBD | TBD | TBD |
| 2 · +图 | TBD | TBD | TBD | TBD | TBD |
| 3 · +时效 | TBD | TBD | TBD | TBD | TBD |

填充前提（缺一不可）：

1. 真实 vault：`journal/` 内有真实事实，且包含被 supersede 的旧事实（否则时效列无区分度）。
2. 真实 embedder：实现 `Embedder`（bge-small-zh），在 `eval/runner.py:build_fuse()` 里挂上 `VectorIndex`，Stage1/2 才有向量增益可言。
3. 一次完整的「抽取 → 落盘 → 建索引」流程跑过，保证 gold id 与 journal 中的 fact id 对得上（对不上会全 0）。

## 7. 如何用结果调参（闭环）

TASKS.md 要求「评测 → 诊断 → 调参 → 回归」：

| 观察到的现象 | 调整项 | 位置 |
|---|---|---|
| Stage1 相对 Stage0 增益小 | 提高向量权重 / 换 embedding 模型 | `config.yaml: rrf.weights.vector`、`embedder.model` |
| Stage2 拖慢延迟但增益为负 | 降图权重或关图路 | `config.yaml: rrf.weights.graph` |
| Recall@K 卡在 K 上限附近 | 提高 `top_k` 再复测 | `config.yaml: top_k`、`--top-k` |
| 时效列低于 1.0 | 检查 supersede 事件是否完整、`when` 语义（左闭右开） | `vault/journal.py`、`Fact.is_valid_at` |
| 抽取噪声多、gold 命中但答案错 | 提高阈值后复测 HitRate/Recall 是否掉太多 | `config.yaml: confidence_threshold` |

每次调参后固定用同一条命令回归：`--gold eval/datasets/own/gold.jsonl --stages 0,1,2,3 --top-k 5`。

## 8. 已知局限

1. 自建集 24 条，规模小，单条样本对均值影响大（1 条 = 4 个百分点），结论需结合置信区间谨慎解读。
2. 一个 query 可能对应多条 gold（如「有哪些个人项目」），Recall@K 对这类样本更敏感，HitRate 会饱和。
3. 延迟为进程内单次调用，未含冷启动（jieba 词典加载、模型加载）。
4. judge 若使用本地模型，分数波动大，仅作趋势参考，不与检索指标混为一谈。
5. 未覆盖多轮对话、跨月份时间推理（gold 集以单跳事实查询为主）。

## 9. 复现清单

- [x] `eval/metrics.py` — 4 个纯函数指标
- [x] `eval/runner.py` — gold 加载 / 分 Stage 消融 / 对比表 / CLI / 可选 judge
- [x] `tests/test_eval.py` — 20 条用例，`pytest tests/test_eval.py -q` 全绿
- [ ] 真实 vault + 真实 embedder 跑分，替换 §6 的 TBD
- [ ] Stage4 LLM 重排接入后补一组对比
- [ ] 抽检抽取 precision，补一列
