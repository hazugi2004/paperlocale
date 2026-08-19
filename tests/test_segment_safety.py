"""片段安全审查只使用自有合成 PDF 的精确可见文本证据。"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from reportlab.pdfgen import canvas

from paperlocale.contracts import read_jsonl, segment_id, write_jsonl_atomic
from paperlocale.segment_safety import (
    load_segment_safety_summary,
    prepare_segment_safety_review,
)


class SegmentSafetyReviewTest(unittest.TestCase):
    def test_split_word_and_unlocated_short_text_require_passthrough(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "paper.pdf"
            document = canvas.Canvas(str(source_pdf))
            document.drawString(
                40,
                780,
                "Figure 5 Assessments of agricultural drought for the period",
            )
            normal = "Soil moisture was 10 mm."
            document.drawString(40, 740, normal)
            document.save()

            split = "re 5 Assessments of agricultural drought for the perio"
            hidden = "Hello"
            placeholder = "{v1}"
            segments = root / "segments.jsonl"
            write_jsonl_atomic(
                segments,
                [
                    {"id": segment_id(split), "source": split},
                    {"id": segment_id(hidden), "source": hidden},
                    {"id": segment_id(normal), "source": normal},
                    {"id": segment_id(placeholder), "source": placeholder},
                ],
            )
            source_hash = hashlib.sha256(source_pdf.read_bytes()).hexdigest()

            summary = prepare_segment_safety_review(
                source_pdf=source_pdf,
                source_sha256=source_hash,
                segments_path=segments,
                output_dir=root,
            )
            self.assertEqual(summary["split_token_candidate_count"], 1)
            self.assertEqual(summary["unlocated_short_text_count"], 1)
            self.assertEqual(
                set(summary["required_passthrough_segment_ids"]),
                {segment_id(split), segment_id(hidden)},
            )

            rows = {row["id"]: row for row in read_jsonl(root / "segment_safety_review.jsonl")}
            occurrence = rows[segment_id(split)]["split_occurrences"][0]
            self.assertEqual(occurrence["page"], 1)
            self.assertEqual(occurrence["literal_prefix"], "Figu")
            self.assertEqual(occurrence["literal_suffix"], "d")
            self.assertEqual(
                rows[segment_id(hidden)]["kind"],
                "unlocated-short-ascii-text",
            )
            self.assertNotIn(segment_id(normal), rows)
            self.assertNotIn(segment_id(placeholder), rows)

            loaded = load_segment_safety_summary(
                source_sha256=source_hash,
                segments_path=segments,
                summary_path=root / "segment_safety_summary.json",
            )
            self.assertEqual(loaded, summary)
            review_path = root / "segment_safety_review.jsonl"
            review_path.write_text(
                review_path.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "JSONL 在生成后发生变化"):
                load_segment_safety_summary(
                    source_sha256=source_hash,
                    segments_path=segments,
                    summary_path=root / "segment_safety_summary.json",
                )


if __name__ == "__main__":
    unittest.main()
