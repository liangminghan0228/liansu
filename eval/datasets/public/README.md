# 公开数据集评测

两块东西：**转换器**把各家原始格式变成统一 gold JSONL；**runner** 把 gold 事实灌进
隔离 journal 再跑与自建集同一套 Stage 消融。原始数据不入库（体积与许可），
仓库只提交转换器、fixture 样本与转换后的最小必要字段。

## 为什么公开集要单独一层

`eval.runner` 的前提是「fact 已在 vault 里」。公开集的事实只存在于数据集本身，
不先落进 journal 就没有东西可召回。所以 `eval.public_runner` 按隔离粒度建临时
journal（`--isolate auto|case|sample|file`），灌 `gold_facts` + `stale_facts`，跑完即删，
不碰用户真实 vault。默认 `auto`：有 `<dataset>.haystack.jsonl` 的数据集按整场会话隔离，
否则逐题隔离。

## 三步跑通

```bash
# 1. 把原始数据放到 raw/（自行下载，见下表）
#    raw/longmemeval_s.json  raw/locomo10.json  raw/hotpot_dev_distractor_v1.json  raw/freshqa_v2.json

# 2. 转换 + 校验（一条命令出四个 *.gold.jsonl）
.venv/bin/python -m eval.datasets.public.convert --dataset all --input-dir raw/

# 3. 跑消融
.venv/bin/python -m eval.public_runner --public-dir eval/datasets/public
```

不联网也想验证链路：仓库自带 `fixtures/*.sample.json`（按各家真实字段结构手写的小样本），

```bash
for ds in longmemeval locomo hotpotqa freshqa; do
  .venv/bin/python -m eval.datasets.public.convert --dataset $ds \
    --input eval/datasets/public/fixtures/$ds.sample.json \
    --output /tmp/$ds.gold.jsonl
done
.venv/bin/python -m eval.public_runner --public-dir /tmp
```

## 下载来源

| 数据集 | 下载来源 | 用它的什么 | 转换要点 |
|---|---|---|---|
| **LongMemEval**（首选） | `xiaowu0162/longmem-eval` 的 `data/longmemeval_s.json` | 长程记忆、知识更新 | `answer_session_ids` → 证据会话，会话内用户陈述即 gold；`knowledge-update` 用数值冲突启发式补 stale |
| **LoCoMo** | `snap-research/locomo` 的 `data/locomo10.json` | 多会话、跨会话多跳、对抗样本 | `evidence` 的 dia_id（`D1:3`）直接映射到会话轮次；`category == 5` → `expect=abstain` |
| **HotpotQA** | `hotpotqa.github.io` 的 `hotpot_dev_distractor_v1.json` | 多跳、BM25/图召回基线 | `supporting_facts` 的 (title, sent_id) 定位支撑句；无时间维度，`valid_from` 用 `2000-01-01` 占位并在 `meta` 标注 |
| **FreshQA** | `freshqa` 官方仓库的 `freshqa_v2.json` | 时效变化、假前提 | 无证据原文，只评时效（`scope=temporal`）；`outdated_answers` → stale，`false_premise` → 对抗样本 |

约定文件名见 `convert.DEFAULT_INPUT_NAMES`，也可以 `--input` 直接指定。

## 统一格式（在 docs/evalset.md §3 上向后兼容扩展）

必填仍是 `question` / `gold_facts` / `answer` / `source`；新增四个可选字段：

| 字段 | 含义 |
|---|---|
| `must_not_include` | 旧 fact id，仅用于时效正确率判分 |
| `stale_facts` | 旧 fact 的完整对象，**`valid_to` 必填**；灌库后由 Stage3 过滤 |
| `expect` | `answer`（默认）｜`abstain`（对抗样本，不计入检索指标） |
| `scope` | `retrieval`（默认）｜`temporal`（无证据原文，只评时效） |
| `meta` | qid / category / 证据 id / 日期来源，仅溯源不判分 |

三条口径，别踩：

1. **公开集只评「检索 + 时效」，不评抽取**。原始数据没有 `(subject, predicate, object)`
   标注，机械三元组会让抽取指标变成噪声，所以 `predicate` 填数据集类型标签、`object` 填答案，仅供人读。
2. **statement 必须放证据原文**，索引只吃这段字。FreshQA 没有证据，只能拿答案当 statement，
   所以标 `scope=temporal`，绝不拿问题原文当证据 —— 那等于让查询命中自己。
3. **时效靠 `stale_facts` 显式给出**：LongMemEval 知识更新取更新日期，FreshQA 取统一截断日
   `2025-01-01`。覆盖不到的类型（改城市、换公司）宁可不标，误标会污染时效分母。

## 指标与现状

`eval.public_runner` 复用 `eval.runner` 的 `to_markdown_table`，输出 Stage0 仅 BM25 →
Stage1 +向量 → Stage2 +图 → Stage3 +时效 的 HitRate / Recall@k / MRR / 时效正确率 / 延迟。

两个必须说明的读数限制：

- 仓库当前**没有 embedder**（bge-small-zh 未接入），Stage1 与 Stage0 等价；
  BM25 + jieba 是纯词面召回，在英文公开集上会系统性偏低 —— 这是仓库现状，不是方法结论；
- 默认 `auto`：有 haystack 时按场次（`sample`）检索，否则逐题（`case`）；`--isolate sample`
  在同场全部轮次里检索，`--isolate file` 把整个数据集灌进一个索引，更接近真实长期积累，
  但跨题事实会互相挤占 top-k，只作对照。

## 自检

```bash
.venv/bin/python -m pytest tests/test_public_eval.py -q     # 24 条：转换/校验/隔离/时效消融/CLI
.venv/bin/python -m eval.datasets.public.convert --check-only eval/datasets/public/locomo.gold.jsonl
```
