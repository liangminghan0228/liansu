"""连溯命令行入口。

当前实现 capture（Stop hook 调用）；record/recall/rebuild 为后续 task 占位，
只打印 not implemented，不 import 尚未存在的模块。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Sequence


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="liansu", description="连溯 — 本地长期记忆系统")
    sub = parser.add_subparsers(dest="command")

    cap = sub.add_parser("capture", help="从 Stop hook stdin 捕获一次会话对话")
    cap.add_argument("--stdin", action="store_true", help="从标准输入读取 hook payload")
    cap.add_argument("--source", help="直接指定会话文件路径（优先于 stdin）")
    cap.add_argument("--out", help="把捕获结果 JSON 写入该文件（默认 stdout）")

    for name in ("record", "recall", "rebuild"):
        placeholder = sub.add_parser(name, help=f"{name}（尚未实现）")
        placeholder.add_argument("rest", nargs="*", help=argparse.SUPPRESS)

    return parser


def _cmd_capture(args: argparse.Namespace) -> int:
    from .capture.hook import capture  # 延迟导入：占位子命令不依赖捕获模块

    stdin_text = sys.stdin.read() if args.stdin else None
    result = capture(stdin_text=stdin_text, source_path=args.source)

    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
    else:
        print(payload)
    return 0 if result["ok"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="[liansu] %(levelname)s %(message)s")
    args = _build_parser().parse_args(argv)

    if args.command == "capture":
        return _cmd_capture(args)
    if args.command in ("record", "recall", "rebuild"):
        print(f"{args.command}: not implemented")
        return 0

    _build_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
