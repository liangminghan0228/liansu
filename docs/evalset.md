# 连溯评测集说明（T14）

## 1. 目的

评测集用于 K15/T15 的检索与生成消融：公开集证明方法有效，自建集调真实个人记忆场景（TECH_DESIGN §5.9 方案 C）。两套集共用同一 JSONL 格式，便于统一 runner 消费。

## 2. 目录结构

```
evalset/
├── own/
│   └── gold.jsonl        # 自建集（核心产出，24 条 question）
└── public/
    └── README.md         # 公开集下载来源与转换说明（不实际下载）
```

## 3. 统一格式

每行一个 JSON 对象：

```json
{"question": "...", "gold_facts": [ {fact}, ... ], "answer": "...", "source": "自建", "must_not_include": ["F-..."] }
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `question` | string | 自然语言提问，全部中文 |
| `gold_facts` | array⟨Fact⟩ | 应命中的事实列表（**对象数组**，非 id 数组） |
| `answer` | string | 参考答案，中文 |
| `source` | string | 数据集来源：自建集恒为 `自建`，公开集填数据集名 |
| `must_not_include` | array⟨string⟩ | **仅时效性样本有**：不应被召回的旧 fact id 列表 |

### 3.1 gold fact 最小字段

每条 gold fact 至少包含以下字段，与 `liansu.schema.Fact` 的必填字段对齐，可直接 `Fact.from_dict()` 校验：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | fact 唯一 id，形如 `F-YYYYMMDD-NNN` |
| `subject` | string | 主体，如 `梁明晗` |
| `predicate` | string | 谓词，如 `求职目标城市` |
| `object` | string | 客体，如 `北京` |
| `statement` | string | 原始自然语言证据，供 LLM 读 |
| `valid_from` | string | 生效日期，`YYYY-MM-DD` |
| `confidence` | number | [0,1] |
| `topics` | array⟨string⟩ | 主题锚点 |
| `source` | string | 来源，约定前缀 `vault://journal/...` |

> **TODO（待真源落地后校验）**：当前 vault 尚无数据，所有 gold fact 的 `source` 使用约定前缀 `vault://journal/YYYY-MM-DD.md`，代表"将来应在该 journal 行找到对应 fact"。`tests/test_evalset.py` 只校验格式自洽与 `vault://` 前缀，**不校验文件真实存在**。待 T3 抓取链路产出真实 journal 后，需补一步"每条 gold fact 都能在 vault 命中"的存在性校验，并把占位 source 替换为真实行定位。

## 4. 自建集设计

24 条 question，围绕用户画像展开：

- **身份/学历**：姓名梁明晗（强调"晗"非"汉"）、南开大学软件学院本科；
- **工作/求职**：理想汽车 2024.07–2026.05、Agent 开发工程师、目标城市北京、薪资底线 20k×12、目标公司类型；
- **身体/籍贯/偏好**：188cm、82kg、陕西西安、本地优先工具；
- **技术栈**：RAG / 多Agent / 评测（含多跳聚合题）；
- **个人项目**：连溯 Liansu 本地记忆系统、连阅 Pass 评测项目。

其中 **10 条为时效性样本**（详见 §5）。

## 5. 时效性样本设计

时效性样本专门检验"已 supersede 的旧事实不应被召回"。共 10 条，统一形态：

- `question` 问**当前状态**；
- `gold_facts` **只含新事实**；
- `must_not_include` 标注应被排除的旧 fact id；
- `answer` 用中文点明旧说法已作废/变更。

| # | 当前问题 | 新事实（gold） | 被排除的旧事实（must_not_include） |
|---|---|---|---|
| 1 | 目标城市定了吗 | 定为北京 F-20261121-006 | 北京/西安未定 F-20261001-007 |
| 2 | 连阅 Pass 通过率 85.7%？ | 成绩作废 F-20261122-020 | 85.7% F-20261015-002 |
| 3 | 还在理想汽车工作吗 | 2026.05 离职 F-20261121-016 | 在职 F-20260601-003 |
| 4 | 现在体重多少 | 82kg F-20261121-011 | 80kg F-20261101-004 |
| 5 | 最低薪资多少 | 20k×12 F-20261121-005 | 18k×12 F-20261101-005 |
| 6 | 现在找什么岗位 | Agent开发工程师 F-20261121-007 | 后端开发工程师 F-20261020-006 |
| 7 | 主要技术方向 | RAG/多Agent/评测 F-2026...-008/009/010 | 传统后端CRUD F-20261010-007 |
| 8 | 主要编程语言 | Python F-20261121-013 | Java F-20260901-008 |
| 9 | 现在住哪个城市 | 北京 F-20261121-014 | 天津 F-20261005-009 |
| 10 | 倾向什么类型公司 | AI创业公司 F-20261121-015 | 外企 F-20261015-010 |

这与 TECH_DESIGN §3 的 supersede 语义一致：旧事实不删行，只置 `valid_to`，新事实的 `supersedes` 指向它。评测时若检索器返回了 `must_not_include` 中的 id，时效正确率记失败。

## 6. 如何扩充

1. 在 `evalset/own/gold.jsonl` 追加一行，字段照 §3 填写；question 必须非空、gold_facts 非空。
2. 新增 fact 沿用 `F-YYYYMMDD-NNN` id 规则；`confidence` 落在 [0,1]；`source` 用 `vault://journal/...`。
3. 涉及"当前状态"且存在旧说法的，务必加 `must_not_include` 指向旧 id，并让 gold_facts 只含新事实。
4. 跑 `./.venv/bin/python -m pytest tests/test_evalset.py -q` 校验格式。
5. 真源落地后，同步补齐 vault 中对应 journal 行，移除 §3.1 的 TODO。

## 7. 公开集

见 `evalset/public/README.md`：列出 LongMemEval（首选，含时序/知识更新）、LoCoMo、HotpotQA、FreshQA 的下载来源与转换为统一格式的方法。本仓库不实际下载原始数据。

## 8. 校验

```bash
./.venv/bin/python -m pytest tests/test_evalset.py -q
```

覆盖：JSON 可解析、四字段齐全、question/answer/gold_facts 非空、fact 最小 schema 字段与类型、confidence 范围、`vault://` 前缀、时效性样本 ≥10、旧 id 不出现在 gold_facts。
