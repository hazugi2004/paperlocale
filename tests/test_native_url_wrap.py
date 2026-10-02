"""Wrapped native URLs keep their glyphs, hyperlink and original font size."""
from pathlib import Path
import tempfile
import unittest

import pymupdf as fitz

from paperlocale.source_layout import extract_layout, units_from_plan
from paperlocale.paragraph_layout import (bind_inline_glyphs, fit_paragraph,
                                         relocate_links, wrap_citation_anchors)


class NativeUrlWrapTests(unittest.TestCase):
    def test_parenthesized_link_keeps_query_continuation_in_visual_reading_order(self):
        first = 'https://scholar.google.com/scholar?hl=en&as_sdt=2005&'
        middle = 'sciodt=0%2C5&cites=18403910731188548420&scipsc='
        last = '1&q=drought+AND+%28heat+OR+heatwave%29&btnG='
        uri = first+middle+last
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'parenthesized-link.pdf'
            with fitz.open() as doc:
                page = doc.new_page(width=360, height=300)
                for y, text in [(90, 'Citing articles are available at:'),
                                (102, '('+first), (114, middle), (126, last),
                                (138, 'More information follows in this paragraph.')]:
                    page.insert_text((40, y), text, fontsize=9, fontname='tiro')
                for text in (first, middle, last):
                    page.insert_link({'kind': fitz.LINK_URI, 'from': page.search_for(text)[0], 'uri': uri})
                doc.save(source)
            unit = units_from_plan(extract_layout(source, paragraph=True), paragraph=True)[0]
            bind_inline_glyphs(source, [unit])
            first_anchor = unit['anchors'][0]
            self.assertEqual(first_anchor['text'], '('+first)
            self.assertEqual(first_anchor['source_url'], uri)
            self.assertEqual(first_anchor['baseline']-first_anchor['body_baseline'], 12)
            original_glyphs = [g for a in unit['anchors'] for g in a['glyphs']]
            placed = fit_paragraph(unit, '引用文章网址：{v0} '+middle+' {v1}。更多说明如下。',
                                   fitz.Font('china-s'), unit['frames'][0]['size'], None)
            baselines = []
            for item in placed:
                anchor = item.get('inline_anchor')
                actual = anchor['glyphs'][0]['origin'][1]+item['shift'][1] if anchor else item['baseline']
                self.assertAlmostEqual(actual, item['baseline'])
                baselines.append(actual)
            self.assertEqual(baselines, sorted(baselines))
            native = [p['inline_anchor'] for p in placed if 'inline_anchor' in p]
            self.assertEqual([g for a in native for g in a['glyphs']], original_glyphs)
            self.assertEqual(native[0]['text'], '(https://')
            with fitz.open(source) as original, fitz.open(source) as output:
                links = relocate_links(output, original, placed)
                self.assertEqual({link['uri'] for link in links[1]}, {uri})

    def test_linked_url_continuation_uses_its_own_line_and_original_path_breaks(self):
        first = 'https://www.frontiersin.org/articles/10.3389/feart.2022.'
        last = '914437/full#supplementary-material'
        uri = first+last
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'supplement.pdf'
            with fitz.open() as doc:
                page = doc.new_page(width=360, height=300)
                font = fitz.Font('tiro')
                page.insert_font(fontname='Original', fontbuffer=font.buffer)
                for origin, text in [((40, 100), 'The Supplementary Material for this article can be found'),
                                     ((40, 112), 'online at: '),
                                     ((40+font.text_length('online at: ', fontsize=9), 112), first),
                                     ((40, 124), last)]:
                    page.insert_text(origin, text, fontsize=9, fontname='Original')
                for text in (first, last):
                    page.insert_link({'kind': fitz.LINK_URI, 'from': page.search_for(text)[0], 'uri': uri})
                doc.save(source)
            plan = extract_layout(source, paragraph=True)
            unit = units_from_plan(plan, paragraph=True)[0]
            bind_inline_glyphs(source, [unit])
            self.assertEqual([a['text'] for a in unit['anchors']], [first, last])
            self.assertEqual([a['source_url'] for a in unit['anchors']], [uri, uri])
            self.assertGreater(unit['anchors'][1]['baseline']-unit['anchors'][1]['body_baseline'], 10)
            source_glyphs = [g for a in unit['anchors'] for g in a['glyphs']]
            size = unit['frames'][0]['size']
            for target in ('本文的补充材料可在线获取：{v0} {v1}', '本文补充材料网址：{v0} {v1}'):
                placed = fit_paragraph(unit, target, fitz.Font('china-s'), size, None)
                native = [p['inline_anchor'] for p in placed if 'inline_anchor' in p]
                self.assertEqual([g for a in native for g in a['glyphs']], source_glyphs)
                self.assertEqual(native[0]['text'], 'https://')
                self.assertEqual(native[-1]['body_baseline'], unit['anchors'][1]['baseline'])
                self.assertEqual({p['font_size'] for p in placed}, {size})
                with fitz.open(source) as original, fitz.open(source) as output:
                    links = relocate_links(output, original, placed)
                    self.assertGreater(len(links[1]), 2)
                    self.assertEqual({link['uri'] for link in links[1]}, {uri})

    def test_formula_separators_are_not_url_or_baseline_evidence(self):
        text = 'P/Q#R&S'
        glyphs = [{'text': c, 'rect': [40+i*10, 100, 50+i*10, 110], 'origin': [40+i*10, 110]}
                  for i, c in enumerate(text)]
        anchor = {'text': text, 'ink': [40, 100, 110, 110], 'glyphs': glyphs,
                  'baseline': 110, 'body_baseline': 105, 'size': 10}
        unit = {'source': 'The formula is {v0}.', 'anchors': [anchor],
                'frames': [{'rect': [40, 100, 100, 150]}]}
        anchors, tokens = wrap_citation_anchors(unit, ['{v0}'])
        self.assertEqual(anchors, [anchor])
        self.assertEqual(tokens, ['{v0}'])
