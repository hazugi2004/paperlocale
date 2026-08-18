"""领域包的加载与术语合同。

领域包只包含数据文件，不执行任意 Python 代码。这样贡献者可以增加医学、
生态学等专业术语，而不必理解或修改 PDF 流水线。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


@dataclass(frozen=True)
class GlossaryEntry:
    """一条可验证术语；``required`` 表示命中原文后译文必须包含目标词。"""

    source: str
    target: str
    required: bool
    note: str


@dataclass(frozen=True)
class DomainPack:
    """翻译提示、术语和回归案例的只读集合。"""

    pack_id: str
    version: str
    source_language: str
    target_language: str
    prompt: str
    glossary: tuple[GlossaryEntry, ...]
    eval_cases: tuple[dict[str, str], ...]


def _read_pack(root: Path) -> DomainPack:
    """从一个已解析目录读取领域包，并拒绝缺少关键文件或重复术语。"""

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    prompt = (root / "prompt.txt").read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError(f"领域包提示为空：{root}")

    glossary: list[GlossaryEntry] = []
    seen: set[str] = set()
    with (root / "glossary.tsv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        expected = {"source", "target", "required", "note"}
        if set(reader.fieldnames or ()) != expected:
            raise ValueError(f"术语表字段必须为 {sorted(expected)}：{root}")
        for row in reader:
            source = row["source"].strip()
            target = row["target"].strip()
            if not source or not target:
                raise ValueError(f"术语原文或译文为空：{row!r}")
            key = source.casefold()
            if key in seen:
                raise ValueError(f"领域包包含重复术语：{source}")
            seen.add(key)
            required_text = row["required"].strip().lower()
            if required_text not in {"true", "false"}:
                raise ValueError(f"required 只能是 true/false：{source}")
            glossary.append(
                GlossaryEntry(
                    source=source,
                    target=target,
                    required=required_text == "true",
                    note=row["note"].strip(),
                )
            )

    eval_cases: list[dict[str, str]] = []
    with (root / "eval_cases.jsonl").open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if set(row) != {"source", "target"}:
                raise ValueError(f"{root}/eval_cases.jsonl:{line_number} 字段非法")
            eval_cases.append({"source": str(row["source"]), "target": str(row["target"])})

    return DomainPack(
        pack_id=str(manifest["id"]),
        version=str(manifest["version"]),
        source_language=str(manifest["source_language"]),
        target_language=str(manifest["target_language"]),
        prompt=prompt,
        glossary=tuple(glossary),
        eval_cases=tuple(eval_cases),
    )


def load_domain_pack(identifier: str | Path) -> DomainPack:
    """加载内置包 ID 或外部目录；两者严格使用同一文件合同。"""

    candidate = Path(identifier).expanduser()
    if candidate.is_dir():
        return _read_pack(candidate.resolve())

    package_root = resources.files("paperlocale").joinpath("packs", str(identifier))
    if not package_root.is_dir():
        raise FileNotFoundError(f"未找到领域包：{identifier}")
    with resources.as_file(package_root) as root:
        return _read_pack(root)
