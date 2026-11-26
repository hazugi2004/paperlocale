"""用自造 PDF 核验 Earth's Future 的刊物证据及旧版面计划兼容。"""
from pathlib import Path
import tempfile
import unittest

import pymupdf as fitz

from paperlocale.journals import identify_journal
from paperlocale.source_layout import extract_layout, load_plan, save_json


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
            old = identify_journal(source, earths_future=False, rules_version=1)
            self.assertEqual(old, {"name": "unknown", "family": "generic",
                                  "evidence": "没有唯一刊名证据", "rules_version": 1})

    def test_other_agu_doi_does_not_become_earths_future(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.make_pdf(Path(directory), pages=("doi:10.1029/2026GL000004",))
            self.assertEqual(identify_journal(source)["name"], "Geophysical Research Letters")

    def test_homepage_acp_markers_take_priority_over_metadata_and_affiliations(self):
        for marker in ('Atmos. Chem. Phys., 21, 15589–15603, 2021', 'doi:10.5194/acp-21-15589-2021'):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as directory:
                source = self.make_pdf(Path(directory), metadata={'subject': 'Atmospheric Research 287 (2023)'},
                                       pages=(marker + '\nLaboratory for Atmospheric Research',))
                journal = identify_journal(source)
                self.assertEqual((journal['name'], journal['family'], journal['page']),
                                 ('Atmospheric Chemistry and Physics', 'copernicus', 1))

    def test_atmospheric_research_requires_publication_evidence(self):
        for text, expected in (
            ('Laboratory for Atmospheric Research', 'unknown'),
            ('Laboratory for\nAtmospheric Research\nWashington State University', 'unknown'),
            ('Atmospheric Research 287 (2023) 106700', 'Atmospheric Research'),
            ('doi:10.1016/j.atmosres.2023.106700', 'Atmospheric Research'),
            ('Global Change Biology Communications', 'Global Change Biology Communications'),
        ):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as directory:
                source = self.make_pdf(Path(directory), pages=(text,))
                self.assertEqual(identify_journal(source)['name'], expected)

    def test_homepage_prose_citation_does_not_supersede_own_masthead(self):
        for text, expected in (
            ('Hydrol. Earth Syst. Sci., 28, 3799-3806, 2024\nRelated study: 10.5194/acp-21-4699-2021',
             'Hydrology and Earth System Sciences'),
            ('An unlisted journal\nRelated study: 10.5194/acp-21-4699-2021', 'unknown'),
            ('https://example.org/references/10.5194/acp-21-4699-2021', 'unknown'),
        ):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as directory:
                source = self.make_pdf(Path(directory), pages=(text,))
                self.assertEqual(identify_journal(source)['name'], expected)

    def test_saved_plan_replays_its_journal_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_pdf(root, pages=(
                'Atmos. Chem. Phys., 21, 15589–15603, 2021\nLaboratory for Atmospheric Research',))
            path = root / 'plan.json'
            for version, expected in ((1, 'Atmospheric Research'), (2, 'Atmospheric Chemistry and Physics')):
                with self.subTest(version=version):
                    plan = extract_layout(source, paragraph=True, journal_rules_version=version)
                    self.assertEqual(plan['journal']['name'], expected)
                    save_json(path, plan)
                    self.assertEqual(load_plan(source, path, paragraph=True)[0], plan)


if __name__ == "__main__":
    unittest.main()
