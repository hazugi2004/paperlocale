"""移动 URI 后必须以保存重开的语义和区域验收，不能只检查插入没有异常。"""

import unittest

import pymupdf as fitz

from paperlocale.paragraph_layout import links_equal, relocate_links


def _source_with_uri(uri, *, indirect_annots=False):
    """自造合法输入；typed 字符串写入避免让上游插入缺陷污染测试种子。"""
    with fitz.open() as document:
        for _ in range(3):
            document.new_page(width=400, height=500)
        # 两页使用不同的非零裁剪原点，强制迁移代码按目标页的逆矩阵写 /Rect。
        document[0].set_cropbox(fitz.Rect(20, 30, 380, 470))
        document[1].set_cropbox(fitz.Rect(40, 60, 380, 470))
        document[2].set_cropbox(fitz.Rect(10, 20, 390, 480))
        document.xref_set_key(document.pdf_catalog(), 'Dests',
            f'<< /keep [{document.page_xref(1)} 0 R /XYZ 0 400 0] >>')
        source = document[0]
        source.insert_link({'kind': fitz.LINK_URI, 'from': fitz.Rect(30, 40, 150, 50),
                            'uri': 'https://seed.example/move'})
        source.insert_link({'kind': fitz.LINK_NAMED, 'from': fitz.Rect(200, 20, 230, 30),
                            'name': 'keep'})
        source.insert_link({'kind': fitz.LINK_URI, 'from': fitz.Rect(200, 60, 250, 70),
                            'uri': 'https://example.org/keep-source'})
        document[1].insert_link({'kind': fitz.LINK_URI,
            'from': fitz.Rect(200, 80, 250, 90), 'uri': 'https://example.org/keep-target'})
        data = document.tobytes()
    with fitz.open(stream=data, filetype='pdf') as document:
        first = document[0].get_links()[0]
        document.xref_set_key(first['xref'], 'A/URI', fitz.get_pdf_str(uri))
        if indirect_annots:
            # PDF 允许 Annots 为间接数组；不能拼接引用字符串，也不能丢掉旧注释。
            for page in (document[0], document[1]):
                kind, values = document.xref_get_key(page.xref, 'Annots')
                assert kind == 'array'
                xref = document.get_new_xref()
                document.update_object(xref, values)
                document.xref_set_key(page.xref, 'Annots', f'{xref} 0 R')
        return document.tobytes()


def _placement(target_page, rectangle, shift):
    # 一个片段代表换行后仍使用原字形的点击区域；原链接覆盖全部源片段。
    return {'page': target_page, 'shift': shift, 'inline_anchor': {
        'page': 1, 'ink': rectangle, 'glyphs': [{'rect': rectangle}]}}


class UriLinkRoundtripTests(unittest.TestCase):
    def test_literal_delimiters_backslashes_unicode_and_plain_uri_roundtrip(self):
        urls = (
            'https://example.org/plain',
            'https://BioRender.com/w9aozbu)',
            'https://example.org/open(',
            r'https://example.org/one\two(three)\)',
            'https://example.org/论文?主题=复合干热&词=(干旱)',
        )
        for uri in urls:
            with self.subTest(uri=uri):
                data = _source_with_uri(uri)
                with fitz.open(stream=data, filetype='pdf') as original, \
                        fitz.open(stream=data, filetype='pdf') as output:
                    self.assertEqual(original[0].get_links()[0]['uri'], uri)
                    # 第3页最初没有 Annots，覆盖向空目标页追加链接的边界。
                    self.assertEqual(output.xref_get_key(output[2].xref, 'Annots')[0], 'null')
                    expected = relocate_links(output, original,
                        [_placement(3, [30, 40, 150, 50], [15, 75])])
                    result = output.tobytes(garbage=0)
                with fitz.open(stream=result, filetype='pdf') as written:
                    for number, page in enumerate(written, 1):
                        self.assertTrue(links_equal(expected[number], page.get_links()))
                    self.assertEqual(len(written[0].get_links()), 2)
                    moved = written[2].get_links()
                    self.assertEqual(len(moved), 1)
                    self.assertEqual(moved[0]['uri'], uri)
                    self.assertEqual(moved[0]['from'], fitz.Rect(45, 115, 165, 125))

    def test_split_cross_page_regions_preserve_named_uri_and_indirect_arrays(self):
        uri = r'https://example.org/分段\url(one)'
        data = _source_with_uri(uri, indirect_annots=True)
        placements = [
            _placement(1, [30, 40, 70, 50], [5, 80]),
            _placement(2, [70, 40, 110, 50], [10, 100]),
            _placement(3, [110, 40, 150, 50], [-40, 130]),
        ]
        with fitz.open(stream=data, filetype='pdf') as original, \
                fitz.open(stream=data, filetype='pdf') as output:
            self.assertEqual(output.xref_get_key(output[0].xref, 'Annots')[0], 'xref')
            self.assertEqual(output.xref_get_key(output[1].xref, 'Annots')[0], 'xref')
            keep = {link['xref']: original.xref_object(link['xref'])
                    for page in original for link in page.get_links()
                    if link.get('uri') != uri}
            expected = relocate_links(output, original, placements)
            result = output.tobytes(garbage=0)
        with fitz.open(stream=result, filetype='pdf') as written:
            moved = []
            for number, page in enumerate(written, 1):
                self.assertTrue(links_equal(expected[number], page.get_links()))
                moved += [(number, link['from']) for link in page.get_links()
                          if link.get('uri') == uri]
            self.assertEqual(moved, [(1, fitz.Rect(35, 120, 75, 130)),
                                     (2, fitz.Rect(80, 140, 120, 150)),
                                     (3, fitz.Rect(70, 170, 110, 180))])
            # 未迁移的原命名目标/URI 对象仍为原引用和原内容，不只保留数量。
            for xref, annotation in keep.items():
                self.assertEqual(written.xref_object(xref), annotation)
            named = [link for link in written[0].get_links()
                     if link['kind'] == fitz.LINK_NAMED]
            self.assertEqual(len(named), 1)
            self.assertEqual(named[0]['nameddest'], 'keep')


if __name__ == '__main__':
    unittest.main()
