# Hook 实验报告：Stop hook 能否拿到当前会话

> 实验日期：2026-11-22 · 对应任务：T3（写路径最脆弱点）
> 结论先行：**Stop hook 的 stdin 只给会话元数据（至少 `cwd` / `stop_hook_active`），不给对话文本**；当前会话不在 `~/.codex/sessions/` 顶层，而是落在按日期分层的 `sessions/YYYY/MM/DD/rollout-*.jsonl`，历史会话在 `archived_sessions/*.jsonl.zst`。因此 `capture` 采用「stdin 优先 → 显式 `--source` → 最新归档/活跃文件降级」三层策略，见 `src/liansu/capture/hook.py`。

---

## 1. 实验方法：dump Stop hook 的 stdin

目标是把 Codex 触发 Stop 时喂给 hook 的 stdin 原样落盘，确认字段结构。脚本（本次未改动用户全局 `hooks.json`，仅给出可复现方法）：

```sh
# 1) 备份
cp ~/.codex/hooks.json ~/.codex/hooks.json.bak.liansu-probe

# 2) 在 Stop[0].hooks 数组最前面插入一个 dump hook（占位可在实验后删掉）
python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home() / ".codex" / "hooks.json"
cfg = json.loads(p.read_text())
cfg["hooks"]["Stop"][0]["hooks"].insert(0, {
    "type": "command",
    "command": "cat > /tmp/liansu-stop-stdin.json",
    "timeout": 5,
})
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
PY

# 3) 跑一轮真实 Codex 会话，正常结束（触发 Stop）

# 4) 查看落盘结果与字段
cat /tmp/liansu-stop-stdin.json | python3 -m json.tool

# 5) 恢复（务必执行）
mv ~/.codex/hooks.json.bak.liansu-probe ~/.codex/hooks.json
```

> 现有 Stop hook 是 `~/.codex/hooks/auto_review.py`，同一机制（见 `hooks.json` 的 `hooks.Stop[0].hooks`）。它可以复用，不必另起一套。

---

## 2. 本次实际观察（基于 ls / 解压 / 读取，非推测）

### 2.1 活跃会话目录 `~/.codex/sessions/`

```
sessions/
└── 2026/
    ├── 11/
    │   ├── 18/  rollout-2026-11-18T*.jsonl        （未压缩，9 个）
    │   ├── 17/  rollout-2026-11-17T*.jsonl
    │   ├── 16/  rollout-2026-11-16T*.jsonl
    │   └── 10/  rollout-2026-11-10T*.jsonl.zst    （部分已压缩）
    └── 05/ ...
```

- **方案文档 §7 写「sessions/ 为空」已过时**：`sessions/2026/11/18/` 实际有未压缩 `.jsonl`，最大约 809 KB / 198 行。
- 分析结果（最大那份）：行 `type` 分布为 `event_msg` 97、`response_item` 68、`token_usage_record` 22、`turn_context` 9、`developer/user/assistant` 消息消息角色分布 `developer=5 / user=10 / assistant=14`，另有 `session_meta`、`world_state` 各 1。
- **结论**：活跃会话落盘在 `sessions/YYYY/MM/DD/`，不是 `sessions/` 顶层；且同时存在 `.jsonl` 与 `.jsonl.zst` 两种后缀。

### 2.2 归档 `~/.codex/archived_sessions/`

- 共 **392** 个 `rollout-*.jsonl.zst`（zstd 压缩的 jsonl）。
- 单文件从几十 KB 到数 MB 不等；样本 `rollout-2026-06-08T11-21-41-*.jsonl.zst`（26 KB）共 30 行，`type` 分布 `response_item=19 / event_msg=9 / session_meta=1 / turn_context=1`。

### 2.3 jsonl 行结构（真实样本）

每行是一条事件，消息藏在 `payload` 里：

```jsonc
// 会话元数据
{"type":"session_meta","payload":{"session_id":"...","cwd":"...","cli_version":"..."}}

// 消息（user / assistant / developer）
{"type":"response_item","payload":{
  "type":"message","role":"assistant",
  "content":[{"type":"output_text","text":"我先看一下当前代码..."}],
  "phase":"commentary"
}}

// user 文本在 input_text 里；首条常是系统注入
{"type":"response_item","payload":{
  "type":"message","role":"user",
  "content":[{"type":"input_text","text":"<environment_context>...</environment_context>"}]
}}

// 事件行（无对话文本，需跳过）
{"type":"event_msg","payload":{"type":"task_started" | "item_completed" | "token_count" | "task_complete", ...}}
```

要点：
- `content` 是 list，元素 `type` 为 `input_text`（user/developer）或 `output_text`（assistant），文本在 `text` 字段；
- user 首条常为 `<environment_context>` 注入，活跃文件里还见 `# AGENTS.md instructions for ...` 注入——解析时需过滤，否则会把系统提示当用户输入；
- `event_msg` / `turn_context` / `token_usage_record` / `world_state` 等非消息行应跳过。

### 2.4 Stop hook stdin（来自现有 `auto_review.py` 与 `hooks.json`）

- `auto_review.py` 对 stdin 执行 `json.load(sys.stdin)`，并读取 `event.get("cwd")`、`event.get("stop_hook_active")`。
- **实测确认**：stdin 是 JSON 对象，至少含 `cwd`、`stop_hook_active`；`auto_review.py` 未使用任何对话文本字段。
- **未实测确认**：是否附带 `transcript_path` 之类指向当前会话文件的字段——本次实验未真实 dump 到该 payload（`hooks.json` 属用户全局配置，未擅自改动）。`hook.py` 已做尽力支持：若 stdin JSON 带 `transcript_path` / `transcript` / `session_file` / `path`，会读取该文件。

---

## 3. 结论：能否拿到当前会话？

| 问题 | 判断 |
|---|---|
| Stop hook stdin 直接含对话文本吗？ | **否**（至少从 `auto_review.py` 的使用方式看，payload 只有元数据） |
| 当前会话已落盘可读吗？ | **部分可以**：活跃文件在 `sessions/YYYY/MM/DD/rollout-*.jsonl`，但 Stop 触发瞬间是否写完最后 assistant 轮次未验证 |
| 归档有内容吗？ | **有**：392 个 `.jsonl.zst`，可稳定解析出多轮对话 |
| 写路径能否全自动？ | **可以，但必须有兜底**：不能只依赖 stdin |

**明确结论**：不能假设 Stop hook stdin 会给出当前会话全文。全自动写路径必须以文件为准；stdin 只作快速通道与人工/调试注入。

---

## 4. 兜底策略（已落地到代码）

`src/liansu/capture/hook.py::capture()` 的优先级：

1. **显式 `--source`**：CLI 指定文件时直接解析，最高优先；
2. **stdin 优先**：
   - 是 jsonl 文本 → 逐行解析；
   - 是 JSON 且含 `turns/messages/dialog/conversation` → 取内嵌对话；
   - 是 JSON 且含 `transcript_path` 等 → 读该文件；
   - 是纯文本 → 当作一条 user 发言（调试/手动兜底）；
   - 是 hook 元数据 JSON（无对话字段）→ 不伪造，转降级；
3. **最新归档降级**：递归扫描 `archived_sessions/*.jsonl.zst` 与 `sessions/**/*.jsonl(.zst)`，按 mtime 倒序尝试前 3 个，跳过损坏/空解析文件；
4. **失败返回结构化错误码**：`NO_ROLLOUT` / `PARSE_ERROR` / `EMPTY_DIALOG`，不抛裸异常，写 error 日志。

会话边界以 hook 触发为准；短空闲兜底与手动 `record` 作为 T13/T4 的补充手段。

---

## 5. 复现解析的命令

```sh
cd /Users/liangminghan/work/code/liansu

# 解析一个归档（路径可替换为任意 rollout-*.jsonl.zst）
PYTHONPATH=src .venv/bin/python -c "
from liansu.capture.parse import parse_zst
import glob, os
f = sorted(glob.glob(os.path.expanduser('~/.codex/archived_sessions/*.jsonl.zst')), key=os.path.getsize)[5]
for t in parse_zst(f): print(t['role'], ':', t['content'][:60])
"

# CLI 入口
PYTHONPATH=src .venv/bin/python -m liansu.cli capture --source <某个 rollout 文件>
```
