"""连溯命令行入口。

capture：Stop hook 调用，捕获一次会话；
record / recall / expand / brief / rebuild：与 MCP 工具同一套实现，便于调试与手动兜底；
metrics：聚合遥测事件流，输出单个 JSON 对象（DSH 记忆插件的数据源）；
health：服务可用性自检（必需项 / 可选项），输出单个 JSON 对象，不可用时退出码 1。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Sequence


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="liansu", description="连溯 — 本地长期记忆系统")
    sub = parser.add_subparsers(dest="command")

    cap = sub.add_parser("capture", help="从 Stop hook stdin 捕获一次会话对话")
    cap.add_argument("--stdin", action="store_true", help="从标准输入读取 hook payload")
    cap.add_argument("--source", help="直接指定会话文件路径（优先于 stdin）")
    cap.add_argument("--out", help="把捕获结果 JSON 写入该文件（默认 stdout）")

    rec = sub.add_parser("record", help="写入一段对话（自动 gate→抽取→落盘）")
    rec.add_argument("dialog", help="对话 JSON 或纯文本")
    rec.add_argument("--source", default="", help="来源标识")

    rcl = sub.add_parser("recall", help="检索记忆")
    rcl.add_argument("query", help="查询语句")
    rcl.add_argument("--top-k", type=int, default=5)
    rcl.add_argument("--stage", type=int, default=3, help="消融开关 0-3，默认 3 含时效")

    exp = sub.add_parser("expand", help="从一条事实做多跳关联")
    exp.add_argument("fact_id")
    exp.add_argument("--hops", type=int, default=1)

    brf = sub.add_parser("brief", help="全局画像摘要")
    brf.add_argument("--max-tokens", type=int, default=500)

    rb = sub.add_parser("rebuild", help="从 journal 全量重建索引与视图")
    rb.add_argument("--verify", action="store_true")

    met = sub.add_parser("metrics", help="聚合遥测指标，输出单个 JSON 对象")
    met.add_argument("--json", action="store_true", help="JSON 输出（当前唯一格式）")
    met.add_argument("--session", help="只看该 session 的事件与摘要")
    met.add_argument("--days", type=int, default=7, help="统计窗口天数（含今天），默认 7")

    hea = sub.add_parser("health", help="服务可用性自检，输出单个 JSON 对象")
    hea.add_argument("--json", action="store_true", help="JSON 输出（当前唯一格式）")
    hea.add_argument("--timeout", type=float, default=1.5, help="LLM 探测超时（秒）")
    hea.add_argument("--no-llm", action="store_true", help="跳过 LLM 网络探测")

    ep = sub.add_parser("endpoint", help="显示当前解析出的 LLM 端点（Deskflow 动态 IP）")
    ep.add_argument("--refresh", action="store_true", help="强制重读 Deskflow 配置")
    ep.add_argument("--no-probe", action="store_true", help="只解析地址，不请求 /health")

    return parser


def _out(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


def _cmd_capture(args: argparse.Namespace) -> int:
    from .capture.hook import capture

    stdin_text = sys.stdin.read() if args.stdin else None
    result = capture(stdin_text=stdin_text, source_path=args.source)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
        return 0 if result["ok"] else 1
    print(payload)
    return 0 if result["ok"] else 1


def _cmd_via_mcp(name: str, args: argparse.Namespace) -> int:
    from . import mcp_server as ms

    ctx = ms.get_context()
    if name == "record":
        return _out(ms.record_impl(ctx, args.dialog, args.source))
    if name == "recall":
        return _out(ms.recall_impl(ctx, args.query, args.top_k, args.stage))
    if name == "expand":
        return _out(ms.expand_impl_tool(ctx, args.fact_id, args.hops))
    if name == "brief":
        return _out(ms.brief_impl(ctx, args.max_tokens))
    if name == "rebuild":
        return _out(ms.rebuild_impl(ctx, args.verify))
    return 1


def _cmd_metrics(args: argparse.Namespace) -> int:
    """聚合遥测并打印单个 JSON 对象；stdout 只有这一个对象，日志一律走 stderr。"""
    from .metrics import build_metrics

    payload = build_metrics(session=args.session, days=args.days)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    """打印服务可用性自检；stdout 只有 JSON，日志走 stderr。"""
    from .health import check_service

    payload = check_service(timeout=args.timeout, probe_llm=not args.no_llm)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["available"] else 1


def _cmd_endpoint(args: argparse.Namespace) -> int:
    from .config import load_config
    from .deskflow import (
        DeskflowEndpointResolver,
        DeskflowResolveError,
        health_url,
        probe_health,
        read_live_peers,
    )

    llm = load_config().get("llm") or {}
    resolver = DeskflowEndpointResolver(llm)
    try:
        base_url = resolver.current_base_url(force=args.refresh)
    except DeskflowResolveError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1

    url = health_url(base_url, resolver.health_path)
    payload: dict[str, Any] = {
        "ok": True,
        "dynamic": resolver.dynamic,
        "template": llm.get("base_url"),
        "base_url": base_url,
        "source": resolver.last_source,
        "live_peers": read_live_peers(resolver.port),
        "health_url": url,
    }
    if not args.no_probe:
        payload["health_ok"] = probe_health(url, resolver.probe_timeout)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="[liansu] %(levelname)s %(message)s")
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "capture":
        return _cmd_capture(args)
    if args.command == "metrics":
        return _cmd_metrics(args)
    if args.command == "health":
        return _cmd_health(args)
    if args.command == "endpoint":
        return _cmd_endpoint(args)
    if args.command in ("record", "recall", "expand", "brief", "rebuild"):
        return _cmd_via_mcp(args.command, args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
