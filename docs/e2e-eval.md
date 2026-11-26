# 端到端评测：抽取 → 检索（T15 改造）

## 1. 为什么改

原评测（`eval/runner.py`）假设 gold fact 已经在库里，只测读路径：抽取错了、漏了、
谓词漂移了，指标一点不动。改造后从**原始对话**出发，一次跑完整写路径再测读路径，
任一环节坏了都会掉分。

```
dialog ──▶ gate ──▶ extract ──▶ confidence ──▶ supersede ──▶ journal
                                                              │
question ──▶ recall(BM25 + 图 + 时效) ──▶ fact ──▶ 语义匹配 gold ┘
```

`eval/runner.py` 保留，用于「gold 已在库」前提下做 Stage 消融（逐层边际增益）；
两者互补，不互相替代。

## 2. 用例集：`eval/datasets/own/e2e.jsonl`（5 条）

| case_id | 类型 | 考什么 |
|---|---|---|
| e2e-01-name | fact | 单跳事实：对话 → 1 条 fact → 召回 |
| e2e-02-tech | fact | 一段对话 3 条事实：抽取完整性 + 多 gold 的 Recall@K |
| e2e-03-weight | temporal | 体重 80→82：supersede 是否触发、旧值是否被过滤 |
| e2e-04-city | temporal | 城市「西安→北京」：高置信旧值被新值覆盖 |
| e2e-05-noise | gate | 纯代码对话：不该记的不能落库（负样本） |

字段：

| 字段 | 含义 |
|---|---|
| `dialog` / `pre_dialog` | 待抽取对话 / 前置对话（先写入旧事实，再写新事实） |
| `pre_date` | 前置事实的 `valid_from`，制造真实的时间窗 |
| `question` | 召回用问题；负样本为空，跳过读路径 |
| `gold_facts[]` | 应为 `{subject, predicate, object, statement}`，**不含 id**（id 由系统生成，无法预知） |
| `must_not_include[]` | 旧事实规格，命中即判时效泄漏；可写多个等价说法 |
| `gold_facts[].subject` | **按系统约定填 `用户`**（抽取器对对话主体一律输出「用户」，见下方匹配规则），人名只出现在 `object` 里 |
| `expect` / `expect_supersede` | `record` / `skip`；是否应发生覆盖 |

## 3. 匹配规则

抽取出的 `id` 是 `F-{日期}-{uuid8}`，模型无权写主键，**不能按 id 比**，只能按语义：

- **strict**：`subject + predicate + object` 归一化后全等 → 写侧 `find_by_key` 能命中；
- **loose**：`subject + object` 相等、predicate 可不同 → 容忍谓词漂移；
- 归一化只做 NFKC、小写、去标点空白、量词同义（`公斤→kg`），保留小数点，不做同义词推理；
- **subject 口径**：抽取器对对话主体一律输出「用户」（真实 vault 6 条事实全为 `subject: 用户`），
  所以 gold 也写 `用户`。首轮评测曾按人名标注，导致 strict/loose 全不匹配、指标假性为 0 —— 这是标注口径错，不是抽取错。

谓词漂移是这套系统最主要的失败模式（`E2E_REPORT.md` 实测过
`用户|在...之间还没定下来|北京和西安`），所以 P/R 按 loose 计，另出
`谓词规范` = strict 命中 / 总命中 —— 语义对但谓词漂移时，检索仍然命中，
但 supersede 不会触发，这一列掉下来就是根因。

## 4. 指标

| 指标 | 定义 | 判分对象 |
|---|---|---|
| gate | 该记的写入了 / 不该记的没写入 | 5/5 |
| 抽取P / 抽取R / F1 | 新写入 fact 与 gold 规格一对一贪心匹配 | 4（负样本无此列） |
| 谓词规范 | strict 命中占比，衡量 subject/predicate 稳定性 | 同上 |
| supersede | 旧事实是否真的被置 `valid_to`（只看 journal，不看检索） | 2 |
| 旧值残留 | 是否两版共存（最严重的写侧故障） | 2 |
| Hit@K / Recall@K / MRR | 召回结果映射回 gold 规格空间后计算 | 4 |
| 时效 | 召回结果中是否出现旧事实（含谓词漂移导致的泄漏） | 2 |

聚合只统计「有定义」的样本（负样本不进检索均值），样本数在表尾列出。
`avg_tokens` / judge 仍未实现：recall 路径不调 LLM，judge 需 `--provider real --judge`。

## 5. 运行

```bash
# 0) 先确认服务可用（LLM 端点 + 配置 + 库体量，输出 JSON；exit 0 = 可用）
.venv/bin/python -m liansu.cli health --json

# 1) 离线冒烟：验证评测链路本身，不联网、不测模型能力
.venv/bin/python -m eval.e2e_runner --provider stub

# 2) 真实评测：走 config.yaml 的 LLM 端点
.venv/bin/python -m eval.e2e_runner --provider real --json /tmp/e2e.json

# 3) 消融：同一条用例集换 stage
.venv/bin/python -m eval.e2e_runner --provider stub --stage 0
```

`--provider stub` 用用例标注喂抽取（`eval/fixture_provider.py`），
所以抽取指标天然接近满分，**只证明链路没写错，不能当模型能力结论**；
真实数字必须 `--provider real`。

## 6. 当前状态

| 项目 | 状态 |
|---|---|
| 用例集 + 用例校验 | 完成（5 条，`tests/test_e2e_eval.py`） |
| 语义匹配层 | 完成（strict/loose、单位归一、假阳性防护） |
| 端到端执行器 | 完成，每用例独立临时 vault，不污染真实记忆 |
| stub 通道冒烟 | **全绿**：gate/supersede/时效/Hit 均 1.00（预期值，仅链路自检） |
| 真实模型跑分 | **已跑（2026-11-26）**：Qwen3-14B @ `192.168.0.101:8080`；gate 口径修复后 5/5 用例全绿，见下表 |
| 负向自检 | 完成：漏抽、多抽、谓词漂移、gate 误放行、stage 缺位 5 种坏情况均有断言 |

真实跑分（`--provider real`，单次）：

```
| 用例          | gate | 抽取P | 抽取R | 谓词规范 | supersede | 旧值残留 | Hit@K | Recall@K | MRR  | 时效 |
| e2e-01-name   | 1    | 1.00  | 1.00  | 1.00     | —         | —        | 1     | 1.00     | 1.00 | —    |
| e2e-02-tech   | 1    | 1.00  | 1.00  | 1.00     | —         | —        | 1     | 1.00     | 1.00 | —    |
| e2e-03-weight | 1    | 1.00  | 1.00  | 1.00     | 1         | 0        | 1     | 1.00     | 1.00 | 1    |
| e2e-04-city   | 1    | 1.00  | 1.00  | 1.00     | 1         | 0        | 1     | 1.00     | 1.00 | 1    |
| e2e-05-noise  | 1    | —     | —     | —        | —         | —        | —     | —        | —    | —    |
| **均值**      | 1.00 | 1.00  | 1.00  | 1.00     | 1.00      | 0.00     | 1.00  | 1.00     | 1.00 | 1.00 |
```

样本数：gate 5｜抽取 4｜supersede 2｜检索 4（top_k=5）｜时效 2｜失败 0。
（修复前同一套用例：gate 0.60｜抽取P/R 0.50｜supersede 0.50｜旧值残留 1 例｜时效 0.50。）

### 6.1 修复前的两处掉分

根因都在写路径而不是评测：

1. **gate 稳定误拒（02、03）**：两条对话各连测 3 次，`worth_remembering` 全为 false。
   02 是「在做连溯 + Python + RAG」的项目/技术事实，03 是「体重 80→82」的状态更新，
   都被判成不值得记。gate prompt 只认「画像、偏好、决策、计划、约束」，事实型更新被系统性漏掉。
2. **误拒的连锁后果（03）**：新值没入库 → `supersede` 从未发生 → 旧值 `80 公斤` 仍 `valid_to=None`
   → 问「现在体重多少」时召回的就是旧值（`leaked_statements` 实为 `我现在体重 80 公斤`）。
   一条 gate 误判会在检索侧表现成「时效正确率 0」，这正是端到端评测才能看到的链路故障。

顺带修掉的真实缺陷：`ApiProvider._build_client` 里 `import httpx` 在本机（openai 3.57 用的是
`httpx2`）静默失败，退回 openai 默认 client 后 `trust_env=True` 读到 `HTTP_PROXY`，
把局域网请求发进代理 → 全部用例 502。改为按 `httpx2 → httpx` 顺序探测并显式传
`http_client(trust_env=False)`，同时补了回归测试。

### 6.2 定位：不是用例问题，是 gate 口径缺口

把同一批事实换说法重跑（`--provider real` 连测 2~3 次，判定稳定）：

| 探针 | 判定 |
|---|---|
| 02 原样（含 MCP / BM25 / 向量 / Python） | ✗ 纯代码改动 |
| 02 去掉技术词「我最近主要在做连溯这个项目」 | ✓ 长期事实 |
| 02 无代码但保留 MCP/向量 | ✗ 纯代码改动（整段一行代码都没有） |
| 03 原样「胖了…得注意饮食了」 | ✗ 闲聊 |
| 03 去情绪词「现在体重 82 公斤」 | ✓ 长期事实 |

两个独立失败模式：**按技术词密度贴「代码改动」标签**、**按情绪词贴「闲聊」标签**——
模型整段贴标签，没有做「段内是否存在持久事实」的判定。用例本身符合真实表述，改的是 prompt。

修法（`providers/api.py`）：

1. gate prompt 补正向清单「客观属性/状态更新」「项目与技术栈事实」，并明确「按内容判定，
   不按语气、用词或技术词密度判定」；`纯代码改动`/`闲聊` 收紧为「整段都无可复用事实」才允许；
   补 4 条 few-shot（含上述两个被误拒的反例）。
2. 抽取 prompt 补覆盖要求（项目/技术栈/客观属性/计划决策不得省略，只采信用户陈述、
   assistant 复述的历史值不抽）、并列多值拆分（「RAG 和多 Agent」→ 两条）与 predicate 固定词表
   （禁止自造「研究领域」「体重变化」），并补 3 条 few-shot。
3. 两条口径都用 `tests/test_providers.py` 的 prompt 内容断言固化，防止后续改 prompt 悄悄回退。

## 7. 局限

1. **5 条样本，一条 = 20 个百分点**，只能做回归门禁（改 prompt / 换模型后不许掉），不能做能力结论。
2. 抽取只判三元组，`statement` 的自由文本表述未评；表述质量需 judge 或人工抽检。
3. 无向量路（仓库未提供 embedder 实现），stage 只到 BM25 + 图 + 时效；接上 embedder 后再补 Stage1。
4. 每条用例重建临时 vault，未覆盖多轮增量、跨月时间推理与并发写入。
5. `--provider real` 的抽取非确定：同一用例跑 3 次取均值±方差才有意义，当前脚本只跑 1 次。
