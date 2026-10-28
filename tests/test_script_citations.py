"""自造 PDF 区分普通词后的上标引文与数学脚本，不使用出版论文或字体。"""
from collections import Counter
from pathlib import Path
import tempfile
import unittest

import pymupdf as fitz
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

from paperlocale.source_layout import _parts, extract_layout, load_plan, save_json, units_from_plan


def script_font(path):
    """生成仅供语义/坐标回归的小字体；方块轮廓不承担视觉排版验收。

    预制 Unicode 上标必须真实写入 PDF 的 cmap，不能把普通数字抬高后
    冒充 U+2075。字体由测试自行构造，避免依赖系统字体或下载字体资源。
    """
    mapping = {0x2075: 'five.sup', 0x00B2: 'two.sup', 0x207B: 'minus.sup',
               0x2013: 'endash', 0xFB01: 'fi'}
    builder = FontBuilder(1000, isTTF=True)
    order = ['.notdef', *mapping.values()]
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap(mapping)
    glyphs = {}
    for name in order:
        pen = TTGlyphPen(None)
        if name != '.notdef':
            low, high = (350, 700) if name.endswith('.sup') else (0, 700)
            pen.moveTo((20, low)); pen.lineTo((380, low))
            pen.lineTo((380, high)); pen.lineTo((20, high)); pen.closePath()
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (500, 0) for name in order})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({'familyName': 'FixtureScriptGeometry', 'styleName': 'Regular',
                           'uniqueFontIdentifier': 'FixtureScriptGeometry',
                           'fullName': 'FixtureScriptGeometry-Regular',
                           'psName': 'FixtureScriptGeometry-Regular'})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    builder.setupMaxp()
    builder.save(path)


class ScriptCitationTests(unittest.TestCase):
    def make_source(self, root, *, word='anomalies', script='14', script_size=6,
                    shift=-3, italic=False, unicode_text=None, ligature=False,
                    prefix='Observed '):
        source, font = root/'source.pdf', root/'scripts.ttf'
        script_font(font)
        with fitz.open() as document:
            page = document.new_page()
            page.insert_font(fontname='FixtureScript', fontfile=font)
            page.insert_text((40, 100), prefix, fontname='tiro', fontsize=10)
            x = 40 + fitz.get_text_length(prefix, fontname='tiro', fontsize=10)
            if unicode_text is not None:
                # 同字号、同基线的预制 Unicode 上标没有普通上标 span 的几何信号。
                for char in unicode_text:
                    face = 'FixtureScript' if char in '⁵²⁻–ﬁ' else 'tiit' if italic else 'tiro'
                    page.insert_text((x, 100), char, fontname=face, fontsize=10)
                    x += 5 if face == 'FixtureScript' else fitz.get_text_length(char, fontname=face, fontsize=10)
            else:
                face = 'tiit' if italic else 'tiro'
                if ligature:
                    page.insert_text((x, 100), 'ﬁ', fontname='FixtureScript', fontsize=10)
                    x += 5
                    word = word.removeprefix('fi')
                page.insert_text((x, 100), word, fontname=face, fontsize=10)
                x += fitz.get_text_length(word, fontname=face, fontsize=10)
                for char in script:
                    face = 'FixtureScript' if char == '–' else 'tiro'
                    page.insert_text((x, 100+shift), char, fontname=face, fontsize=script_size)
                    x += .5*script_size if face == 'FixtureScript' else fitz.get_text_length(char, fontname=face, fontsize=script_size)
            page.insert_text((x, 100), ' in the record.', fontname='tiro', fontsize=10)
            document.save(source)
        return source

    def parts(self, plan):
        return [part for block in plan['blocks'] for part in block['parts']]

    def fixed_text(self, plan):
        return ''.join(p['text'] for p in self.parts(plan) if p['fixed'])

    def source_characters(self, source):
        with fitz.open(source) as document:
            return Counter((c['c'], tuple(c['origin']), tuple(c['bbox']))
                for page in document for block in page.get_text('rawdict')['blocks']
                for line in block.get('lines', []) for span in line['spans'] for c in span['chars'])

    def test_raised_numeric_citations_do_not_freeze_their_prose_word(self):
        for word, script in [('anomalies', '14'), ('events', '1,18'),
                             ('scenarios', '6'), ('regions', '19–21')]:
            with self.subTest(word=word), tempfile.TemporaryDirectory() as directory:
                source = self.make_source(Path(directory), word=word, script=script)
                plan = extract_layout(source, paragraph=True, script_geometry=3)
                self.assertEqual(plan['script_geometry_revision'], 3)
                self.assertTrue(any(not p['fixed'] and word in p['text'] for p in self.parts(plan)))
                self.assertNotIn(word, self.fixed_text(plan))
                self.assertEqual(self.fixed_text(plan), script)
                old = extract_layout(source, paragraph=True, script_geometry=2)
                self.assertIn(word+script, self.fixed_text(old))

    def test_precomposed_unicode_citation_keeps_original_codepoint_and_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.make_source(Path(directory), unicode_text='risks⁵')
            before = self.source_characters(source)
            plan = extract_layout(source, paragraph=True, script_geometry=3)
            chars = Counter((c['text'], tuple(c['origin']), tuple(c['rect']))
                for p in self.parts(plan) if p['fixed'] for c in p['chars'])
            expected = Counter({k: count for k, count in before.items() if k[0] == '⁵'})
            self.assertTrue(expected)
            self.assertEqual(chars, expected)
            unit = next(u for u in units_from_plan(plan, paragraph=True) if 'risks' in u['source'])
            self.assertIn('risks{v0}', unit['source'])
            self.assertEqual(unit['anchors'][0]['text'], '⁵')

    def test_precomposed_quantity_exponent_protects_the_complete_value_and_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            for text in ('10⁵ km²', '10 m⁻²'):
                with self.subTest(text=text):
                    source = self.make_source(Path(directory), unicode_text=text)
                    plan = extract_layout(source, paragraph=True, script_geometry=3)
                    self.assertEqual(self.fixed_text(plan), text)
                    self.assertTrue(any(not p['fixed'] and 'Observed' in p['text'] for p in self.parts(plan)))

    def test_math_and_non_numeric_scripts_keep_the_previous_protection(self):
        # 小号低位下标、斜体/短变量、全大写常量、非数字脚本以及不缩小的
        # 抬高数字均不满足“普通长词+独立编号”的豁免，不能释放数学基字。
        examples = [dict(word='PEI', script='30', shift=2),
                    dict(word='N', script='2', italic=True),
                    dict(word='x', script='2'), dict(word='ABCD', script='2'),
                    dict(word='events', script='abc'),
                    dict(word='anomalies', script='14', script_size=10)]
        for example in examples:
            with self.subTest(example=example), tempfile.TemporaryDirectory() as directory:
                source = self.make_source(Path(directory), **example)
                old = extract_layout(source, paragraph=True, script_geometry=2)
                plan = extract_layout(source, paragraph=True, script_geometry=3)
                self.assertEqual([(p['text'], p['fixed']) for p in self.parts(plan)],
                                 [(p['text'], p['fixed']) for p in self.parts(old)])
                if example.get('script_size', 6) == 10:
                    # 同字号抬高数字在旧版也只保护数字；本例验证没有改变既有行为，
                    # 不误称旧版会因未缩小的数字而固定整个普通英文词。
                    self.assertEqual(self.fixed_text(plan), example['script'])
                else:
                    self.assertIn(example['word']+example['script'], self.fixed_text(plan))
        with tempfile.TemporaryDirectory() as directory:
            source = self.make_source(Path(directory), unicode_text='N²', italic=True)
            self.assertEqual(self.fixed_text(extract_layout(source, paragraph=True, script_geometry=3)), 'N²')

    def test_script_revisions_replay_saved_plan_without_mutating_its_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_source(root, word='anomalies', script='14')
            for revision in (0, 1, 2, 3):
                with self.subTest(revision=revision):
                    plan = extract_layout(source, paragraph=True, script_geometry=revision)
                    path = root/f'plan-{revision}.json'
                    save_json(path, plan); before = path.read_bytes()
                    self.assertEqual(load_plan(source, path, paragraph=True)[0], plan)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(plan.get('script_geometry_revision', 0), revision)

    def test_ligature_prefix_before_citation_stays_translatable(self):
        for word, script in [('fields', '43'), ('fires', '50')]:
            with self.subTest(word=word), tempfile.TemporaryDirectory() as directory:
                source = self.make_source(Path(directory), word=word, script=script, ligature=True)
                before = self.source_characters(source)
                plan = extract_layout(source, paragraph=True, script_geometry=3)
                # NFKD 可供判断完整词长；原 ﬁ 的编码、字形坐标仍须保持，不写回 fi。
                actual = Counter((c['text'], tuple(c['origin']), tuple(c['rect']))
                    for p in self.parts(plan) for c in p['chars'] if c['text'] == 'ﬁ')
                expected = Counter({k: count for k, count in before.items() if k[0] == 'ﬁ'})
                self.assertTrue(expected)
                self.assertEqual(actual, expected)
                units = units_from_plan(plan, paragraph=True)
                self.assertTrue(any(word in u['source'] for u in units))
                self.assertEqual(self.fixed_text(plan), script)

    def test_short_tokens_do_not_borrow_a_long_word_citation_exemption(self):
        for word, script, ligature in [('res', '50', False), ('fix', '2', True)]:
            with self.subTest(word=word), tempfile.TemporaryDirectory() as directory:
                source = self.make_source(Path(directory), word=word, script=script, ligature=ligature)
                plan = extract_layout(source, paragraph=True, script_geometry=3)
                old = extract_layout(source, paragraph=True, script_geometry=2)
                # ﬁx 的语义长度仍只有3，故新豁免不能释放旧版固定的 x2。
                # 原规则未把前面的 ﬁ 扩进锚点；本修复也不另行扩大数学保护范围。
                self.assertEqual([(p['text'], p['fixed']) for p in self.parts(plan)],
                                 [(p['text'], p['fixed']) for p in self.parts(old)])
                self.assertIn(('x' if ligature else word)+script, self.fixed_text(plan))

    def test_bibliography_link_disambiguates_events_citation_from_unit_power(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.make_source(Path(directory), word='events', script='38',
                                      prefix='sustaining CDHW ')
            with fitz.open(source) as document:
                block = document[0].get_text('rawdict')['blocks'][0]
            numbers = [c for line in block['lines'] for span in line['spans']
                       if span['size'] < 8 for c in span['chars'] if c['c'].isdigit()]
            self.assertEqual(''.join(c['c'] for c in numbers), '38')
            rectangle = fitz.Rect(numbers[0]['bbox'])
            for char in numbers[1:]:
                rectangle |= fitz.Rect(char['bbox'])
            # 直接给真实 rawdict 注入明确的书目链接证据，避免为测试额外
            # 构造 PDF 名称树；链接框仅覆盖数字，不借宽链接框保护正文词。
            bibliography = {'kind': 4, 'nameddest': 'bm_CR38', 'page': 1, 'from': rectangle}
            parts = _parts(block, 1, [bibliography], paragraph=True, script_geometry=3)
            self.assertEqual(''.join(p['text'] for p in parts if p['fixed']), '38')
            self.assertTrue(any(not p['fixed'] and 'events' in p['text'] for p in parts))
            # 无链接、错编号或普通外链均不能解除既有单位幂保护；events 是
            # 已有单位别名，单凭“长单词+小号数字”不能覆盖单位解析证据。
            for label, links in [('none', []),
                                 ('wrong_number', [{**bibliography, 'nameddest': 'bm_CR39'}]),
                                 ('external', [{'kind': fitz.LINK_URI, 'page': -1,
                                                'uri': 'https://example.org/reference/38',
                                                'from': rectangle}])]:
                with self.subTest(link=label):
                    parts = _parts(block, 1, links, paragraph=True, script_geometry=3)
                    self.assertEqual(''.join(p['text'] for p in parts if p['fixed']), 'events38')

    def test_event_and_seconds_powers_without_bibliography_evidence_stay_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_source(root, word='events', script='38', prefix='Observed 10 ')
            plan = extract_layout(source, paragraph=True, script_geometry=3)
            self.assertIn('10 events38', self.fixed_text(plan))
            source = self.make_source(root, unicode_text='seconds²')
            plan = extract_layout(source, paragraph=True, script_geometry=3)
            self.assertEqual(self.fixed_text(plan), 'seconds²')


if __name__ == '__main__':
    unittest.main()
