# 宿主接线（T13）

把连溯接入 Codex 与 WorkBuddy。所有改动均已备份在原配置文件同级目录，回滚见文末。

## 0. 前置：安装为可执行命令

```bash
cd /Users/liangminghan/work/code/liansu
python -m venv .venv          # 已存在可跳过
.venv/bin/pip install -e . --no-deps
.venv/bin/liansu capture --help   # 应打印用法
```

## 1. Codex：Stop hook 自动捕获

文件：`~/.codex/hooks.json`

在 `hooks.Stop` 数组追加一组（不动原有 `auto_review.py`）：

```json
{
  "hooks": [
    {
      "type": "command",
      "command": "/Users/liangminghan/work/code/liansu/.venv/bin/liansu capture --stdin",
      "timeout": 20,
      "statusMessage": "Capturing memories to liansu"
    }
  ]
}
```

**实测结论**（见 `docs/hook-probe.md`）：Stop hook 的 stdin 是 JSON，含 `cwd`/`stop_hook_active`，**不含对话文本**。因此 `liansu capture --stdin` 的兜底路径是读 Codex 最新会话文件（`~/.codex/sessions/**/*.jsonl` 与 `archived_sessions/*.jsonl.zst`），而非依赖 stdin 里的对话。

## 2. WorkBuddy：注册本地 MCP server

文件：`~/.workbuddy/mcp.json`

```json
"liansu": {
  "command": "/Users/liangminghan/work/code/liansu/.venv/bin/python",
  "args": ["-m", "liansu.mcp_server"],
  "env": { "PYTHONPATH": "/Users/liangminghan/work/code/liansu/src" },
  "disabled": false
}
```

改完需在 WorkBuddy **连接器管理页右上角「自定义连接器」里对 liansu 点「信任」**，否则不会激活。激活后工具列表应出现 `record / recall / expand / brief / rebuild`。

## 3. 规则注入（按需检索的触发开关）

规则文本见仓库根 `AGENTS.md`（项目级，Codex 自动读取）。若希望跨项目生效，把 `AGENTS.md` 里「recall/expand/brief」三条抄进用户级 `~/.agents/AGENTS.md` 或记忆中枢 `MEMORY.md` 的合适分区——**这一步涉及个人长期记忆文件，请在确认后手动执行**，连溯不自动改写。

## 4. dsh

dsh 无原生 Stop hook，仅支持手动兜底：

```bash
.venv/bin/liansu capture --source ~/.codex/sessions/2026/.../rollout-xxx.jsonl
```

或用 MCP 手动调 `record`。

## 验证清单

| 检查 | 命令/操作 | 预期 |
|---|---|---|
| CLI 可用 | `.venv/bin/liansu capture --help` | 打印子命令用法 |
| MCP 工具 | WorkBuddy 连接器页看 liansu | 5 个工具 |
| Codex hook | 结束一次 Codex 会话后看 `journal/` | 新增事实或明确日志 |
| 按需召回 | 问「我的求职目标城市」 | agent 主动调 recall 命中北京 |

## 回滚

```bash
cp .liansu/backup/hooks.json.bak ~/.codex/hooks.json
cp .liansu/backup/mcp.json.bak   ~/.workbuddy/mcp.json
```

原始备份创建于接线前（2026-11-22）。
