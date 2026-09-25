"""长上标后的回退下标必须随变量整体流动，真正下一行不能被误并。"""
import tempfile
import unittest
from pathlib import Path
import pymupdf as fitz
from paperlocale.source_layout import extract_layout, units_from_plan
from paperlocale.paragraph_layout import bind_inline_glyphs, fit_paragraph


class InlineSubscriptTests(unittest.TestCase):
    def test_probability_variable_is_one_movable_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "probability.pdf"
            with fitz.open() as doc:
                page = doc.new_page()
                page.insert_text((40, 100), "The conditional probability is", fontsize=10)
                # 顺序来自本次 PDF：先 p 和长上标，再回退到 p 右侧的 r 下标，
                # 最后接等号及正文。下标不能成为原位保护区或独立换行词元。
                page.insert_text((40, 112), "p", fontsize=10, fontname="Times-Italic")
                page.insert_text((45, 108), "brown", fontsize=7, fontname="Times-Italic")
                page.insert_text((45, 113.5), "r", fontsize=7, fontname="Times-Italic")
                page.insert_text((65, 112), "= Pr(TAC > 0), with the following interpretation.", fontsize=10)
                page.insert_text((40, 127), "A separate physical line remains below the equation.", fontsize=10)
                doc.save(source)
            plan = extract_layout(source, paragraph=True)
            units = units_from_plan(plan, paragraph=True)
            self.assertEqual(len(units), 1)
            unit = units[0]
            anchor = next(a for a in unit["anchors"] if "brown" in a["text"])
            self.assertEqual(anchor["text"], "pbrownr")
            self.assertFalse(any(b["kind"] == "formula" and b["text"] == "r" for b in plan["blocks"]))
            self.assertIn("A separate physical line", unit["source"])
            bind_inline_glyphs(source, units)
            placed = fit_paragraph(unit, unit["source"], fitz.Font("china-s"), None, None)
            moving = [p for p in placed if p.get("inline_anchor") is anchor]
            self.assertEqual(len(moving), 1)
            self.assertEqual(len(moving[0]["inline_anchor"]["glyphs"]), 7)
