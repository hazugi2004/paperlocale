"""删除预检的真实 PDF 回归：临时字框扩大、定位证据和模型调用顺序。"""
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pymupdf as fitz
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen

from paperlocale.diagnostics import LocatedError
from paperlocale.domains import load_domain_pack
from paperlocale.font_geometry import open_source_for_editing
from paperlocale.preserved_workflow import run_preserved
from paperlocale.safe_text import prepare_source_layer, safe_erase_rectangles, verify_text_erased
from paperlocale.workflow import initialize_run, load_manifest


def expanded_math_fixture(path):
    """自造下伸数学字形；CFF 与 PDF 描述符指标不一致，模拟出版社字体。

    P 是测试符号，墨迹延伸到基线下 8 pt；正文的墨迹与它分离。
    临时 CFF 指标归一化会扩大原提取字框，旧算法仍避让小框就会误删 P。
    所有轮廓均自造，不复制论文或商业字体。
    """
    builder = FontBuilder(1000, isTTF=False)
    builder.setupGlyphOrder(['.notdef', 'P'])
    builder.setupCharacterMap({80: 'P'})
    charstrings = {}
    for name in ['.notdef', 'P']:
        pen = T2CharStringPen(500, None)
        if name == 'P':
            pen.moveTo((50, -1000)); pen.lineTo((450, -1000))
            pen.lineTo((450, 0)); pen.lineTo((50, 0)); pen.closePath()
        charstrings[name] = pen.getCharString()
    builder.setupCFF('FixtureMath', {'FullName': 'FixtureMath', 'FamilyName': 'FixtureMath',
                                   'Weight': 'Regular'}, charstrings, {})
    builder.setupHorizontalMetrics({name: (500, 0) for name in charstrings})
    builder.setupHorizontalHeader(ascent=770, descent=-2958)
    builder.setupNameTable({'familyName': 'FixtureMath', 'styleName': 'Regular'})
    builder.setupOS2(sTypoAscender=770, sTypoDescender=-2958, usWinAscent=770, usWinDescent=2958)
    builder.setupPost()
    builder.font['CFF '].cff[0].FontBBox = [-20, -2958, 1447, 770]
    builder.font.recalcBBoxes = False
    buffer = BytesIO()
    builder.font['CFF '].cff.compile(buffer, builder.font)
    with fitz.open() as document:
        page = document.new_page()
        font = page.insert_font(fontname='FixtureMath', fontbuffer=buffer.getvalue())
        page.insert_text((100, 100), 'P', fontname='FixtureMath', fontsize=8)
        page.insert_text((100, 115), 'abc', fontsize=8)
        import re
        descendant = int(re.search(r'\d+', document.xref_get_key(font, 'DescendantFonts')[1]).group())
        descriptor = int(document.xref_get_key(descendant, 'FontDescriptor')[1].split()[0])
        stream = int(document.xref_get_key(descriptor, 'FontFile3')[1].split()[0])
        document.xref_set_key(stream, 'Subtype', '/Type1C')
        for key, value in {'Ascent': '720', 'Descent': '-180', 'FontBBox': '[-50 -180 1000 720]'}.items():
            document.xref_set_key(descriptor, key, value)
        document.update_object(font, f'<< /Type /Font /Subtype /Type1 /BaseFont /FixtureMath '
            f'/FirstChar 80 /LastChar 80 /Widths [500] /Encoding /WinAnsiEncoding '
            f'/FontDescriptor {descriptor} 0 R >>')
        first = page.get_contents()[0]
        document.update_stream(first, document.xref_stream(first).replace(b'<0001>', b'<50>'))
        document.save(path)


class ErasurePreflightTests(unittest.TestCase):
    def test_expanded_cff_box_keeps_math_and_erases_next_line(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.pdf'
            expanded_math_fixture(source)
            with fitz.open(source) as original:
                parts = []
                for block in original[0].get_text('rawdict')['blocks']:
                    for line in block.get('lines', []):
                        for span in line['spans']:
                            parts.append({'page': 1, 'rect': span['bbox'],
                                          'text': ''.join(c['c'] for c in span['chars']),
                                          'chars': [{'text': c['c'], 'origin': c['origin'], 'rect': c['bbox']}
                                                    for c in span['chars']]})
                protected = [{**parts[0], 'fixed': True}]
                editable = parts[1:]
                working, _ = open_source_for_editing(source)
                with working:
                    self.assertGreater(working[0].search_for('P')[0].y1, parts[0]['rect'][3])
                patches = safe_erase_rectangles(editable, protected, source)
                result = prepare_source_layer(source, editable, protected, patches)
                with fitz.open(stream=result, filetype='pdf') as output:
                    self.assertEqual(output[0].get_text().strip(), 'P')
                    # 既检查字符身份，也比较真实符号墨迹；不能靠保留隐藏文字过关。
                    clip = fitz.Rect(100, 100, 104, 108)
                    self.assertEqual(original[0].get_pixmap(clip=clip, matrix=fitz.Matrix(4, 4)).samples,
                                     output[0].get_pixmap(clip=clip, matrix=fitz.Matrix(4, 4)).samples)

    def test_missing_fixed_character_has_exact_location(self):
        with fitz.open() as document:
            page = document.new_page()
            region = {'page': 1, 'text': 'sum formula', 'fixed': True,
                      'chars': [{'text': 'P', 'origin': [100, 100], 'rect': [100, 94, 104, 108]}]}
            with self.assertRaises(LocatedError) as caught:
                verify_text_erased(page, [], [region])
            item = caught.exception.items[0]
            self.assertEqual((item['source'], item['pages'], item['character']), ('sum formula', [1], 'P'))
            self.assertEqual(item['origin'], [100, 100])
            self.assertEqual(caught.exception.category, 'preservation')

    def test_coordinate_index_keeps_original_tolerance(self):
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text((100, 100), 'P')
            char = {'text': 'P', 'origin': [99.9995, 100.0005], 'rect': [99, 90, 110, 105]}
            # 相邻网格内仍须找到原字；超过原容差或换成别的字符必须失败。
            verify_text_erased(page, [], [{'fixed': True, 'chars': [char]}])
            for change in ({'origin': [99.998, 100]}, {'text': 'Q'}):
                with self.subTest(change=change), self.assertRaises(LocatedError):
                    verify_text_erased(page, [], [{'fixed': True, 'chars': [{**char, **change}]}])

    def test_preflight_does_not_call_provider_or_create_candidate(self):
        self.check_order(fail=False)

    def test_actual_erasure_failure_stops_before_translation(self):
        self.check_order(fail=True)

    def check_order(self, fail):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / 'source.pdf'; run = root / 'run'
            with fitz.open() as document:
                document.new_page().insert_text((50, 100), 'This paragraph explains water and vegetation.')
                document.save(source)
            font = root / 'font.ttf'; font.write_bytes(fitz.Font('china-s').buffer)
            initialize_run(source_pdf=source, run_dir=run, source_language='en', target_language='zh-CN')
            provider = Mock(); provider.provenance.return_value = {'provider': 'fixture'}
            with patch('paperlocale.layout_detection.detect_regions', return_value=[]), \
                 patch('paperlocale.preserved_workflow.prepare_source_layer', wraps=prepare_source_layer) as prepare:
                if fail:
                    prepare.side_effect = LocatedError('固定字符缺失', [], 'preservation')
                    with self.assertRaises(LocatedError):
                        run_preserved(run, provider=provider, domain=load_domain_pack('ecology'),
                                      plan_path=None, font_file=font, paragraph=True)
                else:
                    report = run_preserved(run, provider=None, domain=load_domain_pack('ecology'),
                                           plan_path=None, font_file=font, paragraph=True, preflight_only=True)
                    self.assertEqual(report['status'], 'preflight_passed')
                    self.assertFalse(report['translation_checked'])
                prepare.assert_called_once()
            provider.translate.assert_not_called()
            self.assertFalse((run / 'translations.jsonl').exists())
            self.assertFalse((run / 'render_output').exists())
            self.assertEqual(load_manifest(run)['status'], 'initialized')
