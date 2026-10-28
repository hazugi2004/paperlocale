"""自造字体/原生行核验高括号脚本的整组保护及旧计划兼容，不保存论文字体。"""

from collections import Counter
import copy
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf as fitz
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.t2CharStringPen import T2CharStringPen

from paperlocale.font_geometry import _source_anchor_glyphs
from paperlocale.paragraph_layout import merge_inline_parts
from paperlocale.source_layout import (_parts, _protect_enclosed_scripts, extract_layout,
                                       load_plan, save_json)


def _span(text, x, baseline=100, size=10, *, native=None, tall=False):
    width = 4 if size == 10 else 3
    chars = []
    for index, value in enumerate(text):
        box = [x+index*width, 80 if tall else baseline-size,
               x+(index+1)*width, 110 if tall else baseline+1]
        chars.append({'c': value, 'bbox': box, 'origin': [box[0], baseline],
                      **({'native_text': native} if native is not None else {})})
    return {'font': 'FixtureRoman', 'flags': 0, 'size': size,
            'origin': [x, baseline], 'chars': chars}


def _block():
    # 故意先输出普通基字和脚本，后输出两只高括号，模拟提取器的非视觉顺序。
    # 高括号原点在正文基线上方一整行，几何位置由其独特轮廓决定。
    spans = [_span('Observed', 5), _span('C(k)', 54), _span('x', 70, 103, 7),
             _span(',', 74), _span('C(k)', 78), _span('y', 94, 103, 7),
             _span('(', 49, 85, native='ð', tall=True),
             _span(')', 98, 85, native='Þ', tall=True), _span('changes', 110)]
    return {'lines': [{'spans': [span]} for span in spans]}


def _identity(chars):
    return Counter((c['text'], tuple(c['rect']), tuple(c['origin'])) for c in chars)


def _bracket_program(right=False):
    """测试专有的粗括号轮廓；不复制出版社字体，1000em下高于普通字框。"""
    builder = FontBuilder(1000, isTTF=False)
    builder.setupGlyphOrder(['.notdef', 'FixtureBracket'])
    builder.setupCharacterMap({80: 'FixtureBracket'})
    glyphs = {}
    for name in ('.notdef', 'FixtureBracket'):
        pen = T2CharStringPen(500, None)
        if name == 'FixtureBracket':
            points = [(450, 1200), (100, 1200), (100, -1500), (450, -1500),
                      (450, -1400), (200, -1400), (200, 1100), (450, 1100)]
            if right:
                points = [(550-x, y) for x, y in points]
            pen.moveTo(points[0])
            for point in points[1:]:
                pen.lineTo(point)
            pen.closePath()
        glyphs[name] = pen.getCharString()
    family = 'FixtureEnclosedRight' if right else 'FixtureEnclosedLeft'
    builder.setupCFF(family, {'FullName': family,
        'FamilyName': family, 'Weight': 'Regular',
        'FontMatrix': [.001, 0, 0, .001, 0, 0]}, glyphs, {})
    data = BytesIO()
    builder.font['CFF '].cff.compile(data, builder.font)
    return data.getvalue()


def _pdf(path):
    with fitz.open() as document:
        page = document.new_page(width=360, height=300)
        page.insert_text((40, 60), 'Migration follows connected event coordinates.', fontsize=10)
        for point, value, size in [((131, 120), 'C(k)', 10), ((149, 123), 'x', 7),
                ((154, 120), ',', 10), ((158, 120), 'C(k)', 10), ((176, 123), 'y', 7)]:
            # 明确不同字重使原生提取器保留独立的下标run，避免fixture依赖
            # 特定版本合并同字体的策略；正体基字、低位字号和原坐标均真实存在。
            page.insert_text(point, value, fontsize=size, fontname='hebo' if size == 7 else 'helv')

        def obj(value, stream=None):
            xref = document.get_new_xref()
            document.update_object(xref, value)
            if stream is not None:
                document.update_stream(xref, stream)
            return xref

        for name, native, right in [('Left', '00F0', False), ('Right', '00DE', True)]:
            family = 'FixtureEnclosed'+name
            program = obj('<< /Subtype /Type1C >>', _bracket_program(right))
            descriptor = obj(f'<< /Type /FontDescriptor /FontName /{family} '
                '/Flags 4 /FontBBox [0 -1500 550 1200] /Ascent 1200 /Descent -1500 '
                f'/CapHeight 300 /StemV 80 /ItalicAngle 0 /FontFile3 {program} 0 R >>')
            cmap = obj('<<>>', ('begincmap /CMapType 2 def 1 begincodespacerange '
                '<00> <ff> endcodespacerange 1 beginbfchar <50> <'+native+'> '
                'endbfchar endcmap').encode())
            font = obj(f'<< /Type /Font /Subtype /Type1 /BaseFont /{family} '
                '/FirstChar 80 /LastChar 80 /Widths [500] '
                '/Encoding << /Differences [80 /FixtureBracket] >> '
                f'/FontDescriptor {descriptor} 0 R /ToUnicode {cmap} 0 R >>')
            resources = int(document.xref_get_key(page.xref, 'Resources')[1].split()[0])
            document.xref_set_key(resources, 'Font/'+name, f'{font} 0 R')
        # 高括号最后写入；这里与基字同原点，抬高原点的情形由raw输入用例覆盖。
        contents = page.read_contents() + (b'BT /Left 10 Tf 1 0 0 1 125 180 Tm <50> Tj ET\n'
                                           b'BT /Right 10 Tf 1 0 0 1 181 180 Tm <50> Tj ET')
        page.set_contents(obj('<<>>', contents))
        document.save(path)
    reviewed = {}
    with fitz.open(path) as document:
        page, cache = document[0], {}
        for block in page.get_text('rawdict')['blocks']:
            for line in block.get('lines', []):
                for span in line['spans']:
                    for char in span['chars']:
                        if char['c'] not in ('ð', 'Þ'):
                            continue
                        glyph = _source_anchor_glyphs(page, [{'text': char['c'],
                            'rect': char['bbox'], 'origin': char['origin']}], cache)[0]
                        _, outlines, scale, kind = cache[glyph['xref']]
                        pen = RecordingPen()
                        outlines[glyph['name']].draw(pen)
                        reviewed[(kind, 1/scale, sha256(repr(pen.value).encode()).hexdigest())] = (
                            '(' if char['c'] == 'ð' else ')')
    assert len(reviewed) == 2
    return reviewed


class EnclosedScriptFragmentsTests(unittest.TestCase):
    def test_raw_parts_form_one_anchor_keep_native_coordinates_and_logical_baseline(self):
        block = _block()
        old = _parts(copy.deepcopy(block), 1, [], paragraph=True, script_geometry=3)
        current = _parts(copy.deepcopy(block), 1, [], paragraph=True, script_geometry=4)
        merged = merge_inline_parts(current)
        anchor = next(p for p in merged if p['text'] == '(C(k)x,C(k)y)')
        self.assertTrue(anchor['fixed'])
        self.assertEqual(anchor['baseline'], 100)
        self.assertEqual(anchor['chars'][0]['origin'][1], 85)
        self.assertEqual(anchor['chars'][-1]['origin'][1], 85)
        self.assertEqual(_identity([c for p in old for c in p['chars']]),
                         _identity([c for p in current for c in p['chars']]))
        self.assertEqual([p['text'] for p in merged if not p['fixed']], ['Observed', 'changes'])
        self.assertFalse(any(p['fixed'] and p['text'] == 'C(k)' for p in old))

    def test_ordinary_unknown_long_word_and_no_lower_small_script_do_not_expand(self):
        for case in ('ordinary_brackets', 'unknown_brackets', 'long_word',
                     'normal_size_script', 'no_lowering'):
            with self.subTest(case=case):
                block = _block()
                spans = [line['spans'][0] for line in block['lines']]
                if case == 'ordinary_brackets':
                    for span in spans[6:8]:
                        for char in span['chars']:
                            char.pop('native_text')
                elif case == 'unknown_brackets':
                    spans[6]['chars'][0]['c'] = 'ð'
                    spans[6]['chars'][0].pop('native_text')
                elif case == 'long_word':
                    block['lines'][1]['spans'][0] = _span('Climate', 54)
                elif case == 'normal_size_script':
                    for index in (2, 5):
                        spans[index]['size'] = 10
                else:
                    for index in (2, 5):
                        spans[index]['chars'][0]['origin'][1] = 100
                old = _parts(copy.deepcopy(block), 1, [], paragraph=True, script_geometry=3)
                current = _parts(copy.deepcopy(block), 1, [], paragraph=True, script_geometry=4)
                self.assertEqual(current, old)

    def test_large_character_gaps_and_different_pages_cannot_pair_brackets(self):
        old = _parts(_block(), 1, [], paragraph=True, script_geometry=3)
        # 不仅测试跨run空白，也把大空白藏在一个run内部，禁止以总外框替代逐字间隙。
        for mode in ('between_runs', 'within_run', 'different_pages'):
            with self.subTest(mode=mode):
                parts = copy.deepcopy(old)
                if mode == 'different_pages':
                    next(p for p in parts if p['text'] == ')')['page'] = 2
                else:
                    for part in parts:
                        if part['rect'][0] >= 78:
                            part['rect'][0] += 220
                            part['rect'][2] += 220
                            for char in part['chars']:
                                char['rect'][0] += 220
                                char['rect'][2] += 220
                                char['origin'][0] += 220
                    if mode == 'within_run':
                        a = next(p for p in parts if p['text'] == 'C(k)')
                        b = next(p for p in parts if p['text'] == 'C(k)' and p is not a)
                        a['chars'] += b['chars']
                        a['text'] += b['text']
                        a['rect'][2] = b['rect'][2]
                        parts.remove(b)
                before = copy.deepcopy(parts)
                self.assertEqual(_protect_enclosed_scripts(parts), before)

    def test_self_made_pdf_extract_and_load_preserve_old_revision_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'enclosed.pdf'
            reviewed = _pdf(source)
            with patch('paperlocale.source_symbols.REVIEWED_BRACKET_OUTLINES', reviewed):
                modern = extract_layout(source, paragraph=True)
                self.assertEqual(modern['script_geometry_revision'], 4)
                # 独立公式块会直接原样保留，段落块才在产品内合并锚点；这里
                # 对同一块调用真实merge，核验两种消费路径得到相同完整字序。
                parts = [p for b in modern['blocks'] for p in merge_inline_parts(b['parts'])]
                anchors = [p for p in parts if p['text'] == '(C(k)x,C(k)y)' and p['fixed']]
                self.assertEqual(len(anchors), 1)
                anchor = anchors[0]
                self.assertEqual(anchor['baseline'], 120)
                self.assertEqual(anchor['chars'][0]['origin'][1], 120)
                old = extract_layout(source, paragraph=True, script_geometry=3)
                self.assertEqual(_identity([c for b in modern['blocks'] for p in b['parts']
                                            for c in p['chars']]),
                                 _identity([c for b in old['blocks'] for p in b['parts']
                                            for c in p['chars']]))
                for revision in range(5):
                    with self.subTest(revision=revision):
                        plan = extract_layout(source, paragraph=True, script_geometry=revision)
                        path = root/f'plan-{revision}.json'
                        save_json(path, plan)
                        before = path.read_bytes()
                        checked, _ = load_plan(source, path, paragraph=True)
                        self.assertEqual(checked, plan)
                        self.assertEqual(path.read_bytes(), before)
                        if revision < 4:
                            self.assertFalse(any(p['text'] == '(C(k)x,C(k)y)'
                                for b in plan['blocks'] for p in b['parts']))


if __name__ == '__main__':
    unittest.main()
