"""保护同行完整的科研版本名，并按原版面计划重放旧断点。"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pymupdf as fitz

from paperlocale.diagnostics import unit_location
from paperlocale.source_layout import extract_layout, load_plan, save_json, units_from_plan


class HyphenatedScientificNamesTests(unittest.TestCase):
    def test_new_plans_keep_model_names_atomic_without_reclassifying_negative_values(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.pdf'
            with fitz.open() as document:
                page = document.new_page(width=600, height=800)
                page.insert_text((40, 110), 'The ERA-5 and MERRA-2 datasets were evaluated.')
                page.insert_text((40, 135), 'We used SSP5-8.5 and ERA-5-Land to evaluate -4 degrees.')
                document.save(source)

            for revised in (False, True):
                with self.subTest(revised=revised):
                    plan = extract_layout(source, paragraph=True, scientific_identifiers=revised)
                    path = root / 'layout_plan.json'
                    save_json(path, plan)
                    self.assertEqual(load_plan(source, path, paragraph=True)[0], plan)
                    units = units_from_plan(plan, paragraph=True)
                    anchors = [anchor for unit in units for anchor in unit_location(unit)['anchors'].values()]
                    if revised:
                        self.assertEqual(plan['scientific_identifier_revision'], 1)
                        self.assertEqual(anchors, ['ERA-5', 'MERRA-2', 'SSP5-8.5', 'ERA-5-Land', ' -4'])
                        self.assertNotIn('ERA{', ' '.join(unit['source'] for unit in units))
                    else:
                        self.assertNotIn('scientific_identifier_revision', plan)
                        self.assertEqual(anchors, ['-5', '-2', '-8.5', '-5', ' -4'])


if __name__ == '__main__':
    unittest.main()
