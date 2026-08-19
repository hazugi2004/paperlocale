"""人工透传映射必须绑定当前源文件、片段字节与逐条确认信息。"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from paperlocale.contracts import segment_id, write_jsonl_atomic
from paperlocale.passthrough import (
    confirm_passthrough_map,
    load_passthrough_map,
    passthrough_segment_ids,
)


class PassthroughMapTest(unittest.TestCase):
    def test_confirmation_is_ordered_auditable_and_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_hash = hashlib.sha256(b"synthetic source").hexdigest()
            formula = "{v1} = {v2} + {v3}"
            authors = "Alice Smith, Bob Jones, Carol White, and David Brown"
            segments = root / "segments.jsonl"
            write_jsonl_atomic(
                segments,
                [
                    {"id": segment_id(formula), "source": formula},
                    {"id": segment_id(authors), "source": authors},
                ],
            )

            mapping = confirm_passthrough_map(
                source_sha256=source_hash,
                segments_path=segments,
                output_dir=root,
                segment_ids=[segment_id(authors), segment_id(formula)],
                reason="纯公式和作者信息没有可翻译正文",
                confirmed_by="reviewer",
            )
            self.assertEqual(
                [entry["id"] for entry in mapping["entries"]],
                [segment_id(formula), segment_id(authors)],
            )
            self.assertEqual(
                passthrough_segment_ids(mapping),
                {segment_id(formula), segment_id(authors)},
            )
            loaded = load_passthrough_map(
                source_sha256=source_hash,
                segments_path=segments,
                map_path=root / "passthrough_map.json",
            )
            self.assertEqual(loaded, mapping)
            before = (root / "passthrough_map.json").read_bytes()
            confirm_passthrough_map(
                source_sha256=source_hash,
                segments_path=segments,
                output_dir=root,
                segment_ids=[segment_id(authors), segment_id(formula)],
                reason="纯公式和作者信息没有可翻译正文",
                confirmed_by="reviewer",
            )
            self.assertEqual((root / "passthrough_map.json").read_bytes(), before)

            write_jsonl_atomic(
                segments,
                [
                    {"id": segment_id(formula), "source": formula},
                    {"id": segment_id(authors), "source": authors},
                    {"id": segment_id("new"), "source": "new"},
                ],
            )
            with self.assertRaisesRegex(ValueError, "segments.jsonl 已变化"):
                load_passthrough_map(
                    source_sha256=source_hash,
                    segments_path=segments,
                    map_path=root / "passthrough_map.json",
                )

    def test_confirmation_rejects_unknown_id_and_audit_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_hash = hashlib.sha256(b"synthetic source").hexdigest()
            source = "Alice Smith, Bob Jones, Carol White, and David Brown"
            sid = segment_id(source)
            segments = root / "segments.jsonl"
            write_jsonl_atomic(segments, [{"id": sid, "source": source}])

            with self.assertRaisesRegex(ValueError, "未知片段"):
                confirm_passthrough_map(
                    source_sha256=source_hash,
                    segments_path=segments,
                    output_dir=root,
                    segment_ids=["missing"],
                    reason="作者信息",
                    confirmed_by="reviewer",
                )
            confirm_passthrough_map(
                source_sha256=source_hash,
                segments_path=segments,
                output_dir=root,
                segment_ids=[sid],
                reason="作者信息",
                confirmed_by="reviewer",
            )
            with self.assertRaisesRegex(ValueError, "不同审计信息"):
                confirm_passthrough_map(
                    source_sha256=source_hash,
                    segments_path=segments,
                    output_dir=root,
                    segment_ids=[sid],
                    reason="换一个原因",
                    confirmed_by="reviewer",
                )


if __name__ == "__main__":
    unittest.main()
