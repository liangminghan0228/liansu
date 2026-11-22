"""端到端真实链路测试（非 mock）。

从 MCP 协议层进入，走完整生产装配：
  真实 ApiProvider(gate+extract) → journal 落盘 → 三路索引(BM25/Vector-bge/Graph)
  → RRF 消融检索 → expand 多跳 → 派生视图 → rebuild 一致性校验

用法：
    .venv/bin/python scripts/e2e_real.py
退出码 0 = 全部断言通过，1 = 有失败。

依赖外部条件（脚本会先自检，不满足直接报错退出，不静默跳过）：
  - LLM 服务 http://192.168.0.100:8080/health 可达
  - bge-small-zh-v1.5 模型可加载
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from urllib.request import build_opener, ProxyHandler  # noqa: E402
from fastmcp import Client  # noqa: E402

import liansu.mcp_server as ms  # noqa: E402
from liansu.config import load_config  # noqa: E402

LLM_HEALTH = "http://192.168.0.100:8080/health"

# ---- 测试对话：三段，第三段与第二段给同一属性不同取值，用于验证 supersede ----
DIALOGS = [
    ("e2e://d1-identity",
     "我叫梁明晗，南开大学软件学院软件工程硕士毕业，2024年7月入职理想汽车做软件开发工程师。"),
    ("e2e://d2-intent",
     "我准备转 Agent 开发工程师方向，求职目标城市先定西安。"),
    ("e2e://d3-decision",
     "想清楚了，求职目标城市改成北京，西安不考虑了。"),
]

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    _results.append((name, bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return bool(cond)


def preflight() -> tuple[bool, str]:
    try:
        opener = build_opener(ProxyHandler({}))      # 局域网直连，绕过 HTTP_PROXY
        with opener.open(LLM_HEALTH, timeout=5) as r:
            if r.status != 200:
                return False, f"LLM health {r.status}"
    except Exception as e:
        return False, f"LLM 不可达: {e}"
    try:
        from liansu.providers.embedder import SentenceTransformerEmbedder
        e = SentenceTransformerEmbedder()
        v = e.embed("自检")
        return True, f"LLM 200 + embedder dim={len(v)}"
    except Exception as exc:
        return False, f"embedder 加载失败: {exc}"


def main() -> int:
    print("=" * 68)
    print("连溯 · 端到端真实链路测试")
    print("=" * 68)

    print("\n[0] 前置自检")
    ok, detail = preflight()
    check("外部依赖就绪", ok, detail)
    if not ok:
        print("\n前置条件不满足，终止（不静默跳过）。")
        return 1

    root = Path(tempfile.mkdtemp(prefix="liansu-e2e-"))
    print(f"\n临时 vault: {root}")
    t_all = time.time()

    try:
        # ---------- 1. 生产装配 ----------
        print("\n[1] 生产装配 build_context()")
        cfg = load_config()
        ctx = ms.build_context(config=cfg, root=root)
        ms._CTX = ctx                      # 让 MCP 工具层用临时 vault
        check("组件装配", all(k in ctx for k in
              ("journal", "pipeline", "indexes", "fuse", "graph", "views")),
              f"indexes={list(ctx['indexes'])}")
        check("真实 provider", type(ctx["pipeline"].provider).__name__ == "ApiProvider",
              type(ctx["pipeline"].provider).__name__)

        async def run_mcp():
            out = {}
            async with Client(ms.mcp) as c:
                out["tools"] = sorted(t.name for t in await c.list_tools())

                # ---------- 2. record：真实 LLM gate + extract ----------
                print("\n[2] record：真实 LLM 抽取（3 段对话）")
                written_all = []
                for src, text in DIALOGS:
                    t0 = time.time()
                    res = (await c.call_tool("record", {"dialog": text, "source": src})).data
                    el = round(time.time() - t0, 1)
                    n = res.get("count", 0) if isinstance(res, dict) else 0
                    print(f"    {src}  → {n} 条 fact, {el}s")
                    for f in (res.get("written") or []) if isinstance(res, dict) else []:
                        print(f"      · {f.get('subject')} | {f.get('predicate')} | {f.get('object')} | conf={f.get('confidence')}")
                    written_all.append(res)
                out["written_all"] = written_all

                total = sum((r.get("count", 0) or 0) for r in written_all if isinstance(r, dict))
                check("三段对话共抽出事实", total >= 2, f"共 {total} 条")
                check("抽取链路无错误", all(r.get("ok") for r in written_all if isinstance(r, dict)),
                      str([r.get("error") for r in written_all if isinstance(r, dict) and not r.get("ok")]))

                # ---------- 3. 真源状态 ----------
                print("\n[3] journal 真源状态")
                all_facts = list(ctx["journal"].replay())
                valid = list(ctx["journal"].iter_facts(valid_only=True))
                superseded = [f for f in all_facts if f.valid_to is not None]
                print(f"    总 fact={len(all_facts)} 有效={len(valid)} 已失效={len(superseded)}")
                check("真源已落盘", len(all_facts) >= 2, f"{len(all_facts)} 条")
                check("存在 supersede 替换（时效链路）", len(superseded) >= 1,
                      f"{len(superseded)} 条已置 valid_to: {[f.id for f in superseded]}")
                out["facts"] = all_facts
                dead_ids = {f.id for f in superseded}

                # ---------- 4. recall：三路融合 + 消融 ----------
                print("\n[4] recall：RRF 融合与 Stage 消融")
                for stage in (0, 1, 2, 3):
                    res = (await c.call_tool("recall", {"query": "我的求职目标城市是哪里", "top_k": 3, "stage": stage})).data
                    ids = [r["fact_id"] for r in res.get("results", [])]
                    print(f"    stage={stage} 命中 {ids}")
                    check(f"stage={stage} 可返回结果", res.get("ok") and len(ids) >= 1,
                          f"{len(ids)} 条")

                # 时效性：问当前城市，答案应指向北京；已失效的旧事实不得出现
                res = (await c.call_tool("recall", {"query": "我现在目标城市定的是哪里", "top_k": 5, "stage": 3})).data
                rows = res.get("results", [])
                texts = " ".join(str(r.get("statement", "")) for r in rows)
                hit_ids = {r.get("fact_id") for r in rows}
                print(f"    时效查询召回: {texts[:100]}")
                check("当前城市召回含北京", "北京" in texts, texts[:80])
                check("已失效事实未出现在结果中", not (dead_ids & hit_ids),
                      f"泄漏: {sorted(dead_ids & hit_ids)}")
                # 消融对照：不过滤时效的 stage=2 允许召回旧事实，stage=3 必须干净
                res2 = (await c.call_tool("recall", {"query": "我现在目标城市定的是哪里", "top_k": 5, "stage": 2})).data
                print(f"    （对照）stage=2 不过滤时效召回 {res2.get('count')} 条")

                # ---------- 5. expand 多跳 ----------
                print("\n[5] expand 多跳关联")
                seed = None
                for f in all_facts:
                    if "梁明晗" in f.subject or "梁明晗" in f.statement:
                        seed = f
                        break
                if seed is None and all_facts:
                    seed = all_facts[0]
                check("存在可展开的种子事实", seed is not None)
                if seed is not None:
                    for hops in (1, 2):
                        res = (await c.call_tool("expand", {"fact_id": seed.id, "hops": hops, "max_results": 5})).data
                        got = res.get("facts") or res.get("results") or []
                        print(f"    hops={hops} 关联 {len(got)} 条: {[g.get('id') for g in got]}")
                        check(f"expand hops={hops} 链路可用", res.get("ok") is True,
                              f"{len(got)} 条关联")

                # ---------- 6. brief 派生视图 ----------
                print("\n[6] brief 派生视图")
                res = (await c.call_tool("brief", {"max_tokens": 500})).data
                brief_path = root / "vault_views" / "brief.md"
                check("brief 工具成功", res.get("ok") is True, str(res.get("error", "")))
                check("brief.md 已生成", brief_path.exists() and brief_path.stat().st_size > 0,
                      f"{brief_path.stat().st_size if brief_path.exists() else 0} bytes")
                ents = list((root / "vault_views" / "entities").glob("*.md"))
                check("entities 视图已生成", len(ents) >= 1, f"{len(ents)} 个实体文件")

                # ---------- 7. rebuild 一致性 ----------
                print("\n[7] rebuild 全量重建 + 一致性校验")
                t0 = time.time()
                res = (await c.call_tool("rebuild", {"verify": True})).data
                rep = res.get("report") or {}
                print(f"    耗时 {round(time.time()-t0,1)}s → {json.dumps({k: rep.get(k) for k in ('facts_total','facts_valid','verified','issues')}, ensure_ascii=False)}")
                check("rebuild 成功", res.get("ok") is True, str(res.get("error", "")))
                check("重建后事实数一致", rep.get("facts_total") == len(all_facts),
                      f"{rep.get('facts_total')} vs {len(all_facts)}")
                check("一致性校验通过 verified", rep.get("verified") is True, str(rep.get("issues")))

                # ---------- 8. 幂等 ----------
                print("\n[8] 幂等：重复记录同一对话")
                res = (await c.call_tool("record", {"dialog": DIALOGS[0][1], "source": DIALOGS[0][0]})).data
                check("重复对话不产生新事实", res.get("count") == 0, f"count={res.get('count')}")

            return out

        out = asyncio.run(run_mcp())

        # ---------- 9. 重建后检索仍可用 ----------
        print("\n[9] 重建后检索回归")
        res = ms.recall_impl(ctx, "我的学历和专业", top_k=3, stage=2)
        check("rebuild 后 recall 仍命中", res.get("ok") and res.get("count", 0) >= 1,
              f"{res.get('count')} 条")

        # ---------- 10. stdio 真实子进程（宿主实际接入方式）----------
        print("\n[10] MCP stdio 子进程（WorkBuddy 实际接入路径）")
        try:
            mcp_json = Path.home() / ".workbuddy" / "mcp.json"
            entry = json.loads(mcp_json.read_text(encoding="utf-8"))["mcpServers"]["liansu"]

            async def _stdio_probe():
                from fastmcp.client.transports import StdioTransport
                t = StdioTransport(command=entry["command"], args=entry["args"],
                                   env=entry.get("env"))
                async with Client(t) as cl:
                    return sorted(x.name for x in await cl.list_tools())

            names = asyncio.run(_stdio_probe())
            check("stdio 子进程启动并握手", set(names) == {"record", "recall", "expand", "brief", "rebuild"},
                  str(names))
        except FileNotFoundError:
            check("stdio 子进程启动并握手", False, f"未找到 {mcp_json}")
        except Exception as exc:
            check("stdio 子进程启动并握手", False, f"{type(exc).__name__}: {exc}")

    except Exception:
        print("\n[EXCEPTION]")
        traceback.print_exc()
        _results.append(("无异常完成", False, "见上方堆栈"))
    finally:
        elapsed = round(time.time() - t_all, 1)

        # ---------- 汇总 ----------
        passed = sum(1 for _, ok, _ in _results if ok)
        failed = [(n, d) for n, ok, d in _results if not ok]
        print("\n" + "=" * 68)
        print(f"结果：{passed}/{len(_results)} 通过，耗时 {elapsed}s")
        if failed:
            print("失败项：")
            for n, d in failed:
                print(f"  ✗ {n}" + (f" — {d}" if d else ""))
        print(f"临时 vault 保留在 {root}（便于人工抽查），如需清理手动删除。")
        print("=" * 68)

        # 落一份机器可读结果，供报告引用
        report = root / "e2e_result.json"
        try:
            report.write_text(json.dumps({
                "passed": passed, "total": len(_results), "elapsed_s": elapsed,
                "results": [{"name": n, "ok": ok, "detail": d} for n, ok, d in _results],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"详细结果：{report}")
        except Exception:
            pass

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
