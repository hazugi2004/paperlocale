"""Wrapped native URLs keep their glyphs, hyperlink and original font size."""
from pathlib import Path
import tempfile
import unittest

import pymupdf as fitz

from paperlocale.source_layout import extract_layout, units_from_plan
from paperlocale.paragraph_layout import (bind_inline_glyphs, fit_paragraph, links_equal,
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
            anchor, = unit['anchors']
            self.assertEqual(anchor['text'], '('+uri)
            first_anchor, middle_anchor, last_anchor = anchor['url_fragments']
            self.assertEqual([a['text'] for a in anchor['url_fragments']], ['('+first, middle, last])
            self.assertEqual([a['source_url'] for a in anchor['url_fragments']], [uri] * 3)
            self.assertNotIn(middle, unit['source'])
            self.assertEqual(first_anchor['baseline']-first_anchor['body_baseline'], 12)
            original_glyphs = [g for a in (first_anchor, middle_anchor, last_anchor) for g in a['glyphs']]
            placed = fit_paragraph(unit, '引用文章网址：{v0}。更多说明如下。',
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
                for item in placed:
                    if not (moved := item.get('inline_anchor')):
                        continue
                    shift = fitz.Point(item['shift'])
                    centers = [(box.tl + box.br) * .5 + shift for glyph in moved['glyphs']
                               if glyph['text'] != '(' for box in [fitz.Rect(glyph['rect'])]]
                    self.assertTrue(any(all(center in link['from'] for center in centers)
                                        for link in links[1]))
                with fitz.open(stream=output.tobytes(), filetype='pdf') as saved:
                    self.assertTrue(links_equal(links[1], saved[0].get_links()))

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
            anchor, = unit['anchors']
            self.assertEqual(anchor['text'], uri)
            fragments = anchor['url_fragments']
            self.assertEqual([a['text'] for a in fragments], [first, last])
            self.assertEqual([a['source_url'] for a in fragments], [uri, uri])
            self.assertGreater(fragments[1]['baseline']-fragments[1]['body_baseline'], 10)
            source_glyphs = [g for a in fragments for g in a['glyphs']]
            size = unit['frames'][0]['size']
            for target in ('本文的补充材料可在线获取：{v0}', '本文补充材料网址：{v0}'):
                placed = fit_paragraph(unit, target, fitz.Font('china-s'), size, None)
                native = [p['inline_anchor'] for p in placed if 'inline_anchor' in p]
                self.assertEqual([g for a in native for g in a['glyphs']], source_glyphs)
                self.assertEqual(native[0]['text'], 'https://')
                self.assertEqual(native[-1]['body_baseline'], fragments[1]['baseline'])
                self.assertEqual({p['font_size'] for p in placed}, {size})
                with fitz.open(source) as original, fitz.open(source) as output:
                    links = relocate_links(output, original, placed)
                    self.assertGreater(len(links[1]), 2)
                    self.assertEqual({link['uri'] for link in links[1]}, {uri})

    def test_non_drawing_soft_hyphen_keeps_visible_hyphen_and_url_destination(self):
        cases = [('https://github.com/icos-', 'etc/', 'https://github.com/icos-etc/FFP-utils'),
                 ('S6HM-', 'CP8Q)', 'https://doi.org/10.18160/S6HM-CP8Q')]
        for prefix, suffix, uri in cases:
            with self.subTest(uri=uri), tempfile.TemporaryDirectory() as directory:
                source = Path(directory)/'non-drawing-url-break.pdf'
                next_line = uri[len(prefix+suffix):] if uri.startswith(prefix+suffix) else ''
                with fitz.open() as doc:
                    page = doc.new_page(width=360, height=300)
                    font = fitz.Font('tiro')
                    page.insert_font(fontname='Original', fontbuffer=font.buffer)
                    page.insert_text((40, 88), 'This paper uses the source code available online at:',
                                     fontsize=9, fontname='Original')
                    page.insert_text((40, 100), prefix, fontsize=9, fontname='Original')
                    x = 40+font.text_length(prefix, fontsize=9)
                    # 排印层在同一原点叠画两个空格，提取层将其识别为 U+00AD。
                    for _ in range(2):
                        page.insert_text((x, 100), ' ', fontsize=9, fontname='Original')
                        ref = page.get_contents()[-1]
                        doc.update_stream(ref, b'/Span << /ActualText <FEFF00AD> >> BDC\n'
                                          +doc.xref_stream(ref)+b'\nEMC')
                    page.insert_text((x, 100), suffix, fontsize=9, fontname='Original')
                    if next_line:
                        page.insert_text((40, 112), next_line, fontsize=9, fontname='Original',
                                         color=(0, 0, 1))
                        page.insert_link({'kind': fitz.LINK_URI, 'from': page.search_for(next_line)[0],
                                          'uri': uri})
                    page.insert_text((40+font.text_length(next_line, fontsize=9), 112),
                                     ' Further information is available from the authors.',
                                     fontsize=9, fontname='Original')
                    page.insert_link({'kind': fitz.LINK_URI, 'from': fitz.Rect(
                        39, 89, x+font.text_length(suffix.rstrip(')'), fontsize=9), 103), 'uri': uri})
                    doc.save(source)
                plan = extract_layout(source, paragraph=True, url_groups=False)
                if suffix.endswith(')'):
                    # 此样例只有跨行 DOI 的末段。把已识别的固定片段传给绑定。
                    part = next(p for b in plan['blocks'] for p in b['parts']
                                if p['text'] == prefix+'\u00ad'+suffix)
                    part['fixed'] = True
                if next_line:
                    # 复现已缓存计划将跨行网址尾段归入可翻译正文的情况。
                    part = next(p for b in plan['blocks'] for p in b['parts']
                                if p['text'].startswith(next_line))
                    part['fixed'] = False
                unit = units_from_plan(plan, paragraph=True)[0]
                bind_inline_glyphs(source, [unit])
                anchor, = unit['anchors']
                self.assertEqual(anchor['text'], prefix+'\u00ad'+suffix)
                self.assertEqual(anchor['source_url'], uri)
                self.assertEqual(''.join(g['text'] for g in anchor['glyphs']), prefix+suffix)
                self.assertEqual([g['glyph_id'] for g in anchor['glyphs'] if g['text'] == '-'],
                                 [font.has_glyph(ord('-'))])
                target = ('本文的'+next_line+'源代码可从{v0}'+next_line+'获取，作者可提供更多信息。'
                          if next_line else '本文的源代码可从{v0}获取，作者可提供更多信息。')
                placed = fit_paragraph(unit, target,
                                       fitz.Font('china-s'), unit['frames'][0]['size'], None)
                with fitz.open(source) as original, fitz.open(source) as output:
                    links = relocate_links(output, original, placed)
                    self.assertEqual({link['uri'] for link in links[1]}, {uri})
                    if next_line:
                        self.assertIn('{v0} '+next_line, unit['source'])
                        self.assertEqual(anchor['linked_continuation']['text'], next_line)
                        self.assertIn(next_line, ''.join(p['target'] for p in placed
                                                        if 'inline_anchor' not in p))
                        moved, = (p for p in placed if p.get('inline_anchor', {}).get('text') == next_line)
                        self.assertTrue(all(g['color'] == (0, 0, 1)
                                            for g in moved['inline_anchor']['glyphs']))
                        source_link = fitz.Rect(moved['inline_anchor']['source_link_rect'])
                        self.assertNotIn(source_link, [link['from'] for link in links[1]])
                        centers = [(r.tl+r.br)*.5+fitz.Point(moved['shift'])
                                   for g in moved['inline_anchor']['glyphs']
                                   for r in (fitz.Rect(g['rect']),)]
                        self.assertTrue(any(all(center in link['from'] for center in centers)
                                            for link in links[1]))
                    if suffix.endswith(')'):
                        moved, = (p for p in placed if p.get('inline_anchor'))
                        link, = links[1]
                        centers = [(rect.tl+rect.br)*.5+fitz.Point(moved['shift']) for rect in
                                   (fitz.Rect(g['rect']) for g in moved['inline_anchor']['glyphs'])]
                        self.assertTrue(all(center in link['from'] for center in centers[:-1]))
                        self.assertNotIn(centers[-1], link['from'])

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
