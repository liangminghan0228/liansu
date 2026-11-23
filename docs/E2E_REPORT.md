# 端到端测试报告（真实链路）

> 执行时间：2026-11-22 11:22 GMT+8
> 执行方式：`.venv/bin/python scripts/e2e_real.py`
> 结果：**25/25 断言通过，耗时 31.1s**
> 环境：macOS + Python 3.13；LLM `http://192.168.0.101:8080/v1`（真实 Qwen3-14B，2026-11-23 实测；此前为 `.100`，DHCP 漂移后失效）；embedder `BAAI/bge-small-zh-v1.5`（512 维）

## 1. 测的是什么

与单元测试（135 个，全 mock）的区别：本测试**不用任何 mock**，从 MCP 协议层进入，走完整生产装配路径。

```
MCP Client ──stdio/协议层──▶ record/recall/expand/brief/rebuild
                                │
                                ├─ ApiProvider  → 真实 LLM（gate + 结构化抽取）
                                ├─ Journal      → 文件真源（append-only + supersede）
                                ├─ BM25Index    → jieba 分词
                                ├─ VectorIndex  → 真实 bge 模型 + sqlite-vec
                                └─ GraphIndex   → networkx 双时态图
```

装配走 `build_context()`（生产同一路径），数据落在临时 vault，不污染真实记忆。

## 2. 覆盖的 10 个环节

| # | 环节 | 断言数 | 结论 |
|---|---|---|---|
| 0 | 前置自检（LLM 可达 + embedder 可加载） | 1 | LLM 200，embedder 512 维 |
| 1 | 生产装配 `build_context()` | 2 | 3 路索引 + 真实 ApiProvider |
| 2 | record：真实 LLM 抽取 3 段对话 | 2 | 10 条 fact，无错误 |
| 3 | journal 真源落盘 + supersede | 2 | 10 条总量，1 条被置 `valid_to` |
| 4 | recall：RRF 融合 + Stage 0/1/2/3 消融 | 6 | 四档均可返回，时效过滤生效 |
| 5 | expand 多跳（1/2 跳） | 3 | 1 跳 5 条关联 |
| 6 | brief / entities 派生视图 | 3 | brief 480 bytes，1 个实体文件 |
| 7 | rebuild 全量重建 + 一致性校验 | 3 | `verified=true`，10 vs 10 一致 |
| 8 | 幂等（重复对话不重复写） | 1 | `count=0` |
| 9 | rebuild 后检索回归 | 1 | 仍命中 |
| 10 | MCP stdio 子进程（宿主真实接入路径） | 1 | 5 工具握手成功 |

## 3. 测试过程中发现并修复的 5 个真实缺陷

这些缺陷**在 135 个 mock 单测里全部通过**，只有接真实 LLM + 真实线程模型才暴露。

### 缺陷 1（致命）：fact id 跨批重复，主键冲突丢数据

- 现象：`journal/2023-10.md` 与 `journal/2024-07.md` 同时存在 `fact_001` / `fact_002`。
- 根因：`_fill_fact` 只在 id 为空时才生成，模型自造的 `fact_001` 被原样保留 → replay 时互相覆盖。
- 修复：**id 无条件由系统生成**（`F-{日期}-{uuid8}`），模型无权重写主键。

### 缺陷 2（致命）：模型编造日期，新事实一写入就「已过期」

- 现象：当天对话抽出的事实被标 `valid_from: 2023-10-01`、`valid_to: 2025-12-31`（当天是 2026-11-22），直接落入失效状态，`stage=3` 检索全空。
- 根因：直接采信模型给的 `valid_from` / `valid_to`。
- 修复：
  - `valid_to` 对新建事实**一律清空**（新陈述默认当前有效）；
  - `valid_from` 只在**原文真提到该年份**时才采信，否则回退当天（`_trusted_valid_from`）。保留「2024年7月入职」这类有据可依的历史时间。

### 缺陷 3（致命）：`valid_to: "null"` 字符串打穿 schema

- 现象：`1 validation error for Fact — valid_to: Input should be a valid date, input is too short, input_value='null'`，整批抽取失败并重试一次仍失败。
- 修复：`_clean_scalar` 统一把 `'' / 'null' / 'none' / 'n/a' / 'undefined' / '-'` 归一为 `None`。

### 缺陷 4（致命）：SQLite 跨线程，MCP 生产环境必踩

- 现象：`SQLite objects created in a thread can only be used in that same thread`，向量索引 add / rebuild / count 全失败。
- 根因：`VectorIndex` 在构造线程建连接，MCP 工具在事件循环的线程池执行。
- 修复：`check_same_thread=False` + `threading.RLock`，并把耗时的 embed 计算放到锁外。

### 缺陷 5（功能）：模型覆盖系统字段 `source`

- 现象：调用方传 `source="e2e://d1-identity"`，落盘却是 `source: 用户对话`。
- 修复：`source`、`created_at` 改为系统无条件覆盖。

### 附带改进：抽取 prompt 的 schema 治理

首轮端到端抽出的 subject / predicate 极度漂移：

| 轮次 | d2 抽出 | d3 抽出 | 后果 |
|---|---|---|---|
| 修复前 | `用户 \| 在...之间还没定下来 \| 北京和西安` | `目标城市 \| 确定为 \| 北京` | subject/predicate 全不同 → supersede 永不触发 → 矛盾事实共存 |
| 修复后 | `用户 \| 求职目标城市 \| 西安` | `用户 \| 求职目标城市 \| 北京` | 正确触发 supersede |

prompt 增加了硬约束：subject 指本人统一写「用户」、predicate 用 2~6 字稳定属性名、禁止句式片段、同一属性前后写法必须一致，并给了一条 few-shot 示例。

## 4. 已知限制（未在本轮修复）

1. **检索排序质量不足**：查询「我现在目标城市定的是哪里」，正确答案（北京）排第 3，第 1 位是「职业规划」。RRF 融合目前只用排名，未引入 confidence / recency 加权。需 T15 评测集量化后再调参，避免拍脑袋。
2. **抽取召回不稳定**：同一段 d1 对话，两次运行分别抽出 3 条与 7 条（漏掉「学位」「职位」）。LLM 抽取的 recall 波动需靠评测框架量化，不宜用单次结果下结论。
3. **predicate 归一化只靠 prompt**：无代码层同义词兜底。若模型换措辞（「目标城市」vs「求职目标城市」），supersede 仍可能漏触发。
4. **stage=0/1/2 不做时效过滤**：这是消融设计（需要旧事实作对照），生产必须用 `stage=3`（MCP `recall` 默认值即 3）。
5. **不适合放进默认 CI**：单轮约 30s 且依赖局域网 LLM，需按需手动执行。

## 5. 如何复跑

```bash
cd /Users/liangminghan/work/code/liansu

# 前置条件自检（脚本会自动做，不满足直接退出，不静默跳过）
curl -s --noproxy '*' -m 4 -o /dev/null -w "%{http_code}\n" http://192.168.0.101:8080/health

# 跑端到端（真实 LLM + 真实向量模型）
.venv/bin/python scripts/e2e_real.py

# 单元测试（全 mock，快）
.venv/bin/python -m pytest tests/ -q
```

退出码 0 = 全部通过，1 = 有失败项。每次运行会在临时目录留一份 `e2e_result.json`（含逐项断言明细）便于抽查。
