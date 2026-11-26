import unittest

from paperlocale.paragraph_layout import wrap_citation_anchors


class NativeCitationWrapTests(unittest.TestCase):
    def test_author_year_citation_missing_extracted_spaces_wraps_native_fragments(self):
        cases = [
            ('(Mauder ', 'et al.2024; Ramtvedt and Pirk2022; Sánchez et al.2010)'),
            ('(McGloin etal. 2018; Meyers ', 'and Hollinger 2004; Oliphant etal. 2004; Twine etal.2000)'),
            ('(Anderson ', 'and Wang2014; Grachev et al.2020; Sánchez et al.2010)'),
        ]
        for first, second in cases:
            with self.subTest(first=first, second=second):
                anchors = []
                for text, baseline in ((first, 100), (second, 112)):
                    glyphs = [{'text': c, 'rect': [i*4, baseline-6, i*4+3, baseline],
                               'origin': [i*4, baseline]} for i, c in enumerate(text)]
                    anchors.append({'text': text, 'glyphs': glyphs,
                                    'ink': [0, baseline-6, len(text)*4-1, baseline],
                                    'size': 9, 'baseline': baseline, 'body_baseline': 100})
                unit = {'source': 'Similar results were reported {v0} {v1}.',
                        'anchors': anchors, 'frames': [{'rect': [0, 90, 245, 150]}]}
                wrapped, tokens = wrap_citation_anchors(unit, ['{v0}', '{v1}', '。'])
                self.assertEqual(tokens, ['{v0}', '{v2}', '{v3}', '{v4}', '。'])
                self.assertEqual(wrapped[0]['glyphs'], anchors[0]['glyphs'])
                self.assertEqual([g for a in wrapped[2:] for g in a['glyphs']], anchors[1]['glyphs'])
                self.assertEqual([a['body_baseline'] for a in wrapped[2:]], [112, 112, 112])

    def test_missing_space_does_not_confirm_parenthetical_prose_as_citation(self):
        text = '(Wang2014; heat flux)'
        glyphs = [{'text': c, 'rect': [i*4, 94, i*4+3, 100], 'origin': [i*4, 100]}
                  for i, c in enumerate(text)]
        anchor = {'text': text, 'glyphs': glyphs, 'ink': [0, 94, len(text)*4-1, 100]}
        unit = {'source': 'The term was {v0}.', 'anchors': [anchor],
                'frames': [{'rect': [0, 90, 90, 120]}]}
        anchors, tokens = wrap_citation_anchors(unit, ['{v0}'])
        self.assertEqual(anchors, [anchor])
        self.assertEqual(tokens, ['{v0}'])
