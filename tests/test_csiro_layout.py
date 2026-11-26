"""检验 CSIRO 跨页摘要、偏心双栏和参考文献的原生 PDF 证据。"""
from pathlib import Path
import tempfile
import unittest

import pymupdf as fitz

from paperlocale.source_layout import extract_layout, load_plan, save_json


def _page(doc):
    return doc.new_page(width=595, height=780)


def _line(page, x, y, spans):
    """分别写入同一行的字体和上标，保留真实原生基线与字框。"""
    for text, font, size, rise in spans:
        page.insert_text((x, y-rise), text, fontname=font, fontsize=size)
        x += fitz.get_text_length(text, fontname=font, fontsize=size)


def _sample(path):
    with fitz.open() as doc:
        first = _page(doc)
        first.insert_text((60, 56), 'Australian Journal of Botany, 2021, 69, 110')
        first.insert_text((60, 120), 'Corrigendum')
        article = _page(doc)
        article.insert_text((82, 110), 'An example of disturbance-response protocols', fontname='tibo', fontsize=15)
        article.insert_text((82, 548), 'J. Able, C. Smith, B. Brown, A. Long', fontname='tiit', fontsize=10)
        for number, org in enumerate(('Department of Ecology, Rome', 'School of Science, London', 'University of Paris')):
            _line(article, 82, 570+11*number, [(chr(65+number), 'tiro', 6.3, 3.8), (org, 'tiro', 9, 0)])
        article.insert_text((82, 665), 'Abstract.', fontname='tibo', fontsize=9.5)
        leadin_end = 82 + fitz.get_text_length('Abstract.', fontname='tibo', fontsize=9.5)
        article.insert_text((leadin_end + 9.5, 665),
            'Measuring plant functional traits helps explain patterns in ecosystem structure and function at many scales.',
            fontname='tiro', fontsize=9.5)
        article.insert_text((82, 677), '\n'.join([
            'The handbook emphasises standardized procedures in grasses and woody plants across open environments.',
            'The protocol records disturbance histories and quantifies recovery during the growing season. We',
        ]), fontname='tiro', fontsize=9.5, lineheight=12/9.5)
        continuation = _page(doc)
        continuation.insert_text((56, 85), '\n'.join([
            'describe measurements of recovery in woody plants, grasses, and other species in open environments.',
            'The sampling protocols use the same definitions across study sites for direct comparisons among biomes.',
            'These methods can be adapted to study plant populations across many regions with different local disturbance regimes.',
        ]), fontname='tiro', fontsize=9.5, lineheight=12/9.5)
        continuation.insert_text((56, 130), 'Keywords: grasses, herbivory, recruitment', fontname='tiro', fontsize=9.5)
        continuation.insert_text((56, 156), 'Received 6 May 2020, accepted 8 October 2020, published online 30 November 2020',
                                 fontname='tiro', fontsize=9.5)
        for x, y, end in ((36, 245, 'The disturbance response is different in these'),
                          (293, 230, 'More than one mechanism may affect recovery.')):
            continuation.insert_text((x, y), '\n'.join([
                'The first field protocol describes sampling in grasslands.',
                'We compare samples across sites with consistent measures.',
                'Similar methods are used to compare woody plants.', end,
            ]), fontname='tiro', fontsize=9.5, lineheight=12/9.5)

        references = _page(doc)
        references.insert_text((36, 100), 'Acknowledgements', fontname='tibo', fontsize=9.5)
        references.insert_text((36, 115), 'We thank two technicians for their field support.', fontname='tiro', fontsize=9.5)
        references.insert_text((36, 230), 'References', fontname='tibo', fontsize=9.5)
        for x, top, authors in ((36, 255, ('Able AB', 'Akan CD', 'Alio EF')),
                                (293, 110, ('Auld TD, Denham AJ', 'Bahr LM Jr', 'Baskin CC, Baskin JM'))):
            for n, author in enumerate(authors):
                references.insert_text((x, top+25*n), f'{author} (2005) A study of long-term plant recovery.',
                                       fontname='tiro', fontsize=9.5)
                references.insert_text((x, top+25*n+12), 'Journal of Ecology and Botany 39, 119-120.',
                                       fontname='tiro', fontsize=9.5)
        doc.save(path)

    with fitz.open(path) as doc:
        abstract = next(fitz.Rect(block[:4]) for block in doc[1].get_text('blocks') if block[4].startswith('Abstract.'))
    rect = list(abstract + (-2, -2, 2, 2))
    return [{'page': 2, 'rect': rect, 'kind': kind, 'confidence': confidence}
            for kind, confidence in (('plain text', .65), ('abandon', .45))]


class CsiroLayoutTests(unittest.TestCase):
    def test_cross_page_abstract_and_offset_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'csiro.pdf'
            detections = _sample(source)
            old = extract_layout(source, detections, paragraph=True, publisher_layout=2)
            new = extract_layout(source, detections, paragraph=True, publisher_layout=3)
            self.assertEqual(new['journal']['family'], 'csiro')

            def start(plan, value):
                return next(block for block in plan['blocks'] if block['text'].startswith(value))

            self.assertEqual(start(old, 'Abstract.')['kind'], 'header-footer')
            abstract = start(new, 'Abstract.')
            next_page = start(new, 'describe measurements')
            self.assertEqual(abstract['kind'], 'body')
            self.assertIn([abstract['id'], next_page['id']], new['groups'])
            for value in ('J. Able', 'ADepartment', 'Received 6 May'):
                self.assertEqual(start(new, value)['kind'], 'preserve')
            self.assertEqual(start(old, 'Auld TD')['kind'], 'body')
            self.assertEqual(start(new, 'Auld TD')['kind'], 'reference')
            self.assertEqual(start(new, 'We thank two technicians')['kind'], 'body')
            positions = [block['rect'][0] for block in new['blocks'] if block['page'] == 3
                         and block['text'].startswith('The first field protocol')]
            self.assertEqual(len(positions), 2)
            self.assertLess(*positions)

            for plan in (old, new):
                saved = Path(directory) / 'layout.json'
                save_json(saved, plan)
                self.assertEqual(load_plan(source, saved, detections, paragraph=True)[0], plan)

    def test_equal_visual_confidence_does_not_discard_abandon(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'csiro.pdf'
            detections = _sample(source)
            detections[-1]['confidence'] = detections[0]['confidence']
            plan = extract_layout(source, detections, paragraph=True, publisher_layout=3)
            abstract = next(b for b in plan['blocks'] if b['text'].startswith('Abstract.'))
            self.assertEqual(abstract['kind'], 'header-footer')


if __name__ == '__main__':
    unittest.main()
