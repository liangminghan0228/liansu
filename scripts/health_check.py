"""连溯健康自检：不依赖 WorkBuddy，直接看数据层真实状态。

用法：
    .venv/bin/python scripts/health_check.py

退出码 0 = 数据层健康可召回；1 = 尚未就绪（会给出具体原因和处置建议）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from liansu.config import load_config           # noqa: E402
from liansu.index.bm25 import BM25Index          # noqa: E402
from liansu.index.fuse import FuseIndex          # noqa: E402
from liansu.vault.journal import Journal         # noqa: E402

OK, BAD, WARN = "✓", "✗", "!"


def _line(mark: str, text: str) -> None:
    print(f"  {mark} {text}")


def check_llm(cfg: dict) -> bool:
    base = (cfg.get("llm") or {}).get("base_url", "").rstrip("/")
    if base.endswith("/v1"):          # /health 在根路径，不属于 OpenAI 兼容前缀
        base = base[:-3]
    if not base:
        _line(WARN, "config 未配置 llm.base_url")
        return False
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"{base}/health", timeout=5) as r:
            ok = r.status == 200
        _line(OK if ok else BAD, f"LLM 端点 {base}/health → {r.status}")
        return ok
    except Exception as e:
        _line(BAD, f"LLM 端点不可达: {type(e).__name__}（IP 可能变了，或代理拦截）")
        return False


def check_host_wiring() -> bool:
    """检查宿主配置 + 信任审批，返回是否已写入信任。"""
    print("\n[5] 宿主接线（配置 + 信任审批）")
    wb = Path.home() / ".workbuddy"
    mcp_json = wb / "mcp.json"
    approved = False
    if mcp_json.exists():
        entry = (json.loads(mcp_json.read_text(encoding="utf-8")).get("mcpServers") or {}).get("liansu")
        if entry:
            state = "已配置（disabled=true）" if entry.get("disabled") else "已配置（enabled）"
            _line(OK if not entry.get("disabled") else WARN, f"~/.workbuddy/mcp.json {state}")
        else:
            _line(BAD, "~/.workbuddy/mcp.json 无 liansu 条目")
    else:
        _line(BAD, "未找到 ~/.workbuddy/mcp.json")

    approvals = wb / "mcp-approvals.json"
    if approvals.exists():
        keys = json.loads(approvals.read_text(encoding="utf-8"))
        hits = [k for k in keys if k.endswith("::liansu")]
        approved = bool(hits)
        _line(OK if approved else BAD,
              f"信任审批 {'已写入' if approved else '缺失'}（{len(hits)} 条 liansu 记录）")
        if not approved:
            _line(WARN, "处置：连接器页对 liansu 点「信任」，或重跑授权流程")
    else:
        _line(BAD, "未找到 mcp-approvals.json（尚无信任记录）")
    _line(WARN if approved else BAD,
          "审批已写入 ≠ 当前会话已加载：必须完全退出并重启 WorkBuddy 后，工具才会出现")
    return approved


def main() -> int:
    cfg = load_config()
    journal_dir = ROOT / (cfg.get("vault") or {}).get("journal_dir", "journal")
    print("=" * 62)
    print("连溯健康自检")
    print("=" * 62)

    print("\n[1] LLM 端点")
    llm_ok = check_llm(cfg)

    print("\n[2] 真源 journal")
    if not journal_dir.exists():
        _line(BAD, f"{journal_dir} 不存在 —— 从未写入过任何记忆")
        _line(WARN, "处置：让宿主动 record，或跑 scripts/e2e_real.py 验证链路")
        check_host_wiring()
        print("\n结论：数据层为空，recall 必然返回空。先灌数据再验证记忆效果。")
        return 1

    files = sorted(journal_dir.glob("*.md"))
    journal = Journal(journal_dir)
    all_facts = list(journal.replay())
    valid = list(journal.iter_facts(valid_only=True))
    dead = len(all_facts) - len(valid)
    _line(OK if files else BAD, f"{len(files)} 个落盘文件，共 {len(all_facts)} 条 fact（有效 {len(valid)}，已失效 {dead}）")
    if files:
        _line(WARN, f"最近写入: {files[-1].name}")
    if not all_facts:
        _line(BAD, "文件存在但解析不出 fact —— 真源格式可能损坏")
        return 1
    if not valid:
        _line(BAD, "全部 fact 均已失效（valid_to 已过）—— 时效逻辑异常")
        return 1

    print("\n[3] 检索可用性（BM25 快检，不加载向量模型）")
    bm25 = BM25Index()
    bm25.rebuild(all_facts)
    fuse = FuseIndex(journal, {"bm25": bm25}, config=cfg)
    probe = valid[0].statement[:12]
    hits = fuse.recall(probe, top_k=3, stage=3)
    _line(OK if hits else BAD, f"用「{probe}」探针召回 {len(hits)} 条")
    for h in hits[:3]:
        _line(WARN, f"  {h.statement[:40]}  (score={h.score})")

    print("\n[4] 派生视图")
    views = ROOT / "vault_views"
    if views.exists():
        ents = list((views / "entities").glob("*.md")) if (views / "entities").exists() else []
        brief = views / "brief.md"
        _line(OK, f"entities {len(ents)} 个，brief {brief.stat().st_size if brief.exists() else 0} bytes")
    else:
        _line(WARN, "vault_views 未生成（首次 record 或 rebuild 后自动出现，不影响检索）")

    check_host_wiring()

    print("\n" + "=" * 62)
    print(f"结论：数据层健康，{len(valid)} 条有效记忆可召回。")
    print("下一步：在 WorkBuddy 新对话里问一个只有 vault 里才有的细节，能答对即记忆生效。")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
