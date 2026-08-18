"""PDFMathTranslate-next 通用 CLI 翻译器的收集/查表桥接器。

版面引擎会为每个片段启动这个小命令。``collect`` 只记录原文并原样返回；
``lookup`` 只读取已经通过门禁的译文。两阶段分离后，翻译失败不会污染排版，
同一批译文也可以反复重建 PDF 而不再次消耗模型额度。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .contracts import read_jsonl, segment_id, write_jsonl_atomic


def _configure_stdio() -> None:
    """强制 UTF-8，避免中文路径和译文经过子进程管道时乱码。"""

    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name, None)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")


def collect_segment(path: Path, source: str) -> None:
    """按稳定哈希去重保存片段，并把原文返回给版面引擎。"""

    rows = read_jsonl(path) if path.exists() else []
    sid = segment_id(source)
    if sid not in {str(row.get("id")) for row in rows}:
        rows.append({"id": sid, "source": source})
        write_jsonl_atomic(path, rows)
    sys.stdout.write(source)


def lookup_translation(path: Path, source: str) -> None:
    """按稳定 ID 查译文；缺失时非零退出，禁止退回原文伪装成功。"""

    translations = {
        str(row.get("id")): str(row.get("target"))
        for row in read_jsonl(path)
        if row.get("id") and row.get("target") is not None
    }
    sid = segment_id(source)
    if sid not in translations:
        raise KeyError(f"缺少片段译文：{sid}")
    sys.stdout.write(translations[sid])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser("collect")
    collect.add_argument("--segments", type=Path, required=True)
    lookup = subparsers.add_parser("lookup")
    lookup.add_argument("--translations", type=Path, required=True)
    return parser


def main() -> int:
    _configure_stdio()
    args = build_parser().parse_args()
    source = sys.stdin.read()
    if args.command == "collect":
        collect_segment(args.segments.expanduser().resolve(), source)
    else:
        lookup_translation(args.translations.expanduser().resolve(), source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
