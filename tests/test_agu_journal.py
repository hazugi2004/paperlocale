"""用自造 PDF 核验 Earth's Future 的刊物证据及旧版面计划兼容。"""
from pathlib import Path
import tempfile
import unittest

import pymupdf as fitz

from paperlocale.journals import identify_journal


class EarthsFutureJournalTests(unittest.TestCase):
    def make_pdf(self, root, *, metadata=None, pages=("A synthetic study",)):
        """不使用真实论文；元数据、首页 DOI、后续页眉分别提供明确证据。"""
        source = root / "synthetic.pdf"
        with fitz.open() as document:
            for text in pages:
                page = document.new_page()
                page.insert_text((40, 40), text)
            if metadata:
                document.set_metadata(metadata)
            document.save(source)
        return source

    def test_metadata_journal_and_doi_identify_agu(self):
        for metadata in ({"subject": "Earth's Future"},
                         {"title": "Earth’s Future"},
                         {"subject": "doi:10.1029/2026EF000001"}):
            with self.subTest(metadata=metadata), tempfile.TemporaryDirectory() as directory:
                source = self.make_pdf(Path(directory), metadata=metadata)
                found = identify_journal(source)
                self.assertEqual(found["name"], "Earth's Future")
                self.assertEqual(found["family"], "agu")
                self.assertEqual(found["page"], "metadata")

    def test_first_page_doi_and_second_page_header(self):
        for pages, expected_page in ((("doi:10.1029/2026EF000002",), 1),
                                     (("A synthetic article", "Earth's Future"), 2)):
            with self.subTest(pages=pages), tempfile.TemporaryDirectory() as directory:
                source = self.make_pdf(Path(directory), pages=pages)
                found = identify_journal(source)
                self.assertEqual(found["name"], "Earth's Future")
                self.assertEqual(found["family"], "agu")
                self.assertEqual(found["page"], expected_page)

    def test_conflicting_journal_evidence_stays_unknown(self):
        # 同一证据页出现两个刊物时可能是引用，不按 PROFILES 顺序猜选。
        with tempfile.TemporaryDirectory() as directory:
            source = self.make_pdf(Path(directory), pages=("Earth's Future\nNature Communications",))
            self.assertEqual(identify_journal(source)["family"], "generic")

    def test_old_plan_switch_keeps_previous_unknown_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.make_pdf(Path(directory), pages=("doi:10.1029/2026EF000003",))
            old = identify_journal(source, earths_future=False)
            self.assertEqual(old, {"name": "unknown", "family": "generic",
                                  "evidence": "没有唯一刊名证据", "rules_version": 1})

    def test_other_agu_doi_does_not_become_earths_future(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.make_pdf(Path(directory), pages=("doi:10.1029/2026GL000004",))
            self.assertEqual(identify_journal(source)["name"], "Geophysical Research Letters")


if __name__ == "__main__":
    unittest.main()
