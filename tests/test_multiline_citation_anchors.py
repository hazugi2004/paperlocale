"""Native PDF citations spanning physical lines and paragraph frames."""
from collections import Counter
from pathlib import Path
import re
import tempfile
import unittest

import pymupdf as fitz

from paperlocale.source_layout import extract_layout, units_from_plan, load_plan, save_json
from paperlocale.paragraph_layout import bind_inline_glyphs, fit_paragraph, wrap_citation_anchors


class MultilineCitationTests(unittest.TestCase):
    def _extract(self, lines, **options):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        source = Path(directory.name)/'citations.pdf'
        with fitz.open() as document:
            page = document.new_page(width=650, height=800)
            # Embed the built-in font as Unicode: Base14 insert_text alone
            # substitutes a dot for š and would conceal this real regression.
            page.insert_font(fontname='Original', fontbuffer=fitz.Font('tiro').buffer)
            for x, y, text in lines:
                page.insert_text((x, y), text, fontsize=10, fontname='Original')
            document.save(source)
        return source, extract_layout(source, paragraph=True, **options)

    def _verify_source_characters(self, source, plan):
        with fitz.open(source) as document:
            original = Counter((number, c['c'], tuple(c['origin']))
                for number, page in enumerate(document, 1)
                for b in page.get_text('rawdict')['blocks']
                for line in b.get('lines', []) for span in line['spans']
                for c in span['chars'] if not c['c'].isspace())
        preserved = Counter((part['page'], c['text'], tuple(c['origin']))
            for block in plan['blocks'] for part in block['parts']
            for c in part['chars'] if not c['text'].isspace())
        self.assertEqual(preserved, original)

    def _fixed_text(self, plan):
        return ' '.join(p['text'].strip() for b in plan['blocks']
                        for p in b['parts'] if p['fixed'])

    def test_review_citation_spanning_lines_preserves_authors_and_all_years(self):
        citation = '(Bezak and Mikoš, 2020; Vogel et al., 2021; Bento et al., 2022; Marengo et al., 2022)'
        source, plan = self._extract([
            (40, 100, 'rather report the hotspots as broader regions (Bezak and Mikoš,'),
            (40, 112, '2020; Vogel et al., 2021; Bento et al., 2022; Marengo et al., 2022).'),
            (40, 124, 'The observations in 2020 identify other hotspots.')])
        self.assertEqual(self._fixed_text(plan), citation)
        ordinary = ' '.join(p['text'] for b in plan['blocks'] for p in b['parts'] if not p['fixed'])
        self.assertIn('observations in 2020', ordinary)
        self._verify_source_characters(source, plan)
        units = units_from_plan(plan, paragraph=True)
        bind_inline_glyphs(source, units)
        self.assertTrue(all(a['glyphs'] for unit in units for a in unit['anchors']))
        self.assertTrue(any('š' in a['text'] for unit in units for a in unit['anchors']))
        restored = ' '.join(re.sub(r'\{v(\d+)\}', lambda m: unit['anchors'][int(m[1])]['text'],
                                  unit['source']) for unit in units)
        self.assertEqual(re.sub(r'\s+', '', restored), re.sub(r'\s+', '',
            'rather report the hotspots as broader regions '+citation+'. '
            'The observations in 2020 identify other hotspots.'))

    def test_citation_continuation_across_native_text_frames(self):
        source, plan = self._extract([
            (40, 100, 'Earlier studies report broader hotspots (Bezak and Mikoš,'),
            (40, 155, '2020; Vogel et al., 2021). Observations in 2020 agree.')])
        self.assertGreaterEqual(len(plan['blocks']), 2)
        self.assertEqual(self._fixed_text(plan), '(Bezak and Mikoš, 2020; Vogel et al., 2021)')
        self.assertEqual(len(plan['groups']), 1)
        self._verify_source_characters(source, plan)

    def test_names_in_multiline_parenthetical_prose_are_not_citations(self):
        source, plan = self._extract([
            (40, 100, 'The review notes (Bezak and Mikoš discussed'),
            (40, 112, 'regional hotspots in 2020) and compares other observations.'),
            (40, 124, 'A later study (Smith, 2020) reaches a similar conclusion.')])
        self.assertEqual(self._fixed_text(plan), '(Smith, 2020)')
        ordinary = ' '.join(p['text'] for b in plan['blocks'] for p in b['parts'] if not p['fixed'])
        self.assertIn('Bezak and Mikoš', ordinary)
        self.assertIn('hotspots in 2020', ordinary)
        self._verify_source_characters(source, plan)

    def test_citation_only_middle_frame_keeps_its_original_size_and_glyphs(self):
        source, plan = self._extract([
            (40, 100, 'Earlier studies report broader hotspots (Bezak and Mikoš,'),
            (40, 155, '2020; Vogel et al., 2021;'),
            (40, 210, 'Bento et al., 2022). Other observations in 2020 agree.')])
        self.assertEqual(self._fixed_text(plan),
                         '(Bezak and Mikoš, 2020; Vogel et al., 2021; Bento et al., 2022)')
        units = units_from_plan(plan, paragraph=True)
        self.assertEqual(len(units), 1)
        self.assertEqual(len(units[0]['frames']), 3)
        self.assertEqual(units[0]['frames'][1]['size'], 10)
        bind_inline_glyphs(source, units)
        self.assertTrue(all(a['glyphs'] for a in units[0]['anchors']))
        self._verify_source_characters(source, plan)

    def test_numbered_citation_wrap_preserves_complete_bracket(self):
        source, plan = self._extract([
            (40, 100, 'Earlier studies compare these observations [1,'),
            (40, 112, '2–4] with values measured during 2020.')])
        self.assertEqual(self._fixed_text(plan), '[1, 2–4]')
        self._verify_source_characters(source, plan)

    def test_previous_citation_revision_preserves_saved_plan_identity(self):
        source, old = self._extract([
            (40, 100, 'Earlier studies report broader hotspots (Bezak and Mikoš,'),
            (40, 112, '2020). Other observations in 2020 agree.')], extended_citations=1)
        self.assertEqual(old['citation_revision'], 1)
        self.assertEqual(self._fixed_text(old), '')
        path = source.parent/'plan.json'
        save_json(path, old)
        loaded, _ = load_plan(source, path, paragraph=True)
        self.assertEqual(loaded, old)
        current = extract_layout(source, paragraph=True)
        self.assertEqual(current['citation_revision'], 2)
        self.assertEqual(self._fixed_text(current), '(Bezak and Mikoš, 2020)')

    def test_standalone_wrapped_citation_has_no_translation_unit(self):
        source, plan = self._extract([
            (40, 100, '(Bezak and Mikoš,'),
            (40, 112, '2020; Vogel et al., 2021)')])
        self.assertEqual(self._fixed_text(plan), '(Bezak and Mikoš, 2020; Vogel et al., 2021)')
        self.assertEqual(units_from_plan(plan, paragraph=True), [])
        self.assertTrue(all(b['kind'] == 'formula' for b in plan['blocks']))
        self._verify_source_characters(source, plan)

    def test_wide_continuation_wraps_at_source_semicolons_at_original_size(self):
        source, plan = self._extract([
            (40, 100, 'Earlier studies (Zscheischler and Seneviratne,'),
            (40, 112, '2017; Ridder et al., 2018; Mukherjee and Mishra, 2021).'),
            (40, 124, 'Other observations provide evidence for the comparison.'),
            (40, 136, 'The regional differences support further investigation.')])
        unit = units_from_plan(plan, paragraph=True)[0]
        bind_inline_glyphs(source, [unit])
        self.assertEqual([a['text'] for a in unit['anchors']],
                         ['(Zscheischler and Seneviratne,',
                          '2017; Ridder et al., 2018; Mukherjee and Mishra, 2021)'])
        original = [dict(a) for a in unit['anchors']]
        size = unit['frames'][0]['size']
        target = '已有研究{v0}{v1}。其他观测为比较提供证据。区域差异支持进一步研究。'
        placed = fit_paragraph(unit, target, fitz.Font('china-s'), size, None)
        native = [item['inline_anchor'] for item in placed if 'inline_anchor' in item]
        self.assertEqual(len(native), 4)
        self.assertEqual([g for a in native for g in a['glyphs']],
                         [g for a in original for g in a['glyphs']])
        self.assertEqual(unit['anchors'], original)
        self.assertEqual({p['font_size'] for p in placed if 'inline_anchor' not in p}, {size})

    def test_semicolon_fragment_without_complete_citation_is_not_split(self):
        text = '2017; results in 2020; further observations'
        glyphs = [{'text': c, 'rect': [40+i*4, 100, 44+i*4, 110]}
                  for i, c in enumerate(text)]
        anchor = {'text': text, 'ink': [40, 100, 40+len(text)*4, 110], 'glyphs': glyphs}
        unit = {'source': 'The study reports {v0} as prose.', 'anchors': [anchor],
                'frames': [{'rect': [40, 100, 180, 150]}]}
        anchors, tokens = wrap_citation_anchors(unit, ['{v0}'])
        self.assertEqual(tokens, ['{v0}'])
        self.assertEqual(anchors, [anchor])
