"""新增已核验括号映射不能改变旧版段落计划及其续跑身份。"""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf as fitz
from fontTools.pens.recordingPen import RecordingPen

from paperlocale.font_geometry import _source_anchor_glyphs
from paperlocale.source_layout import extract_layout, load_plan, save_json
from test_corpus_encoding import fixture


class SymbolRevision5Tests(unittest.TestCase):
    def test_old_and_new_plans_replay_their_own_symbol_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.pdf'
            fixture(source)
            with fitz.open(source) as document:
                page = document[0]
                reference = page.get_fonts()[0][0]
                cmap = int(document.xref_get_key(reference, 'ToUnicode')[1].split()[0])
                document.update_stream(cmap, document.xref_stream(cmap).replace(b'E001', b'00F0'))
                page.insert_text((40, 60), 'This paragraph explains water and vegetation.', fontsize=10)
                raw = page.get_text('rawdict')['blocks']
                char = next(c for block in raw for line in block.get('lines', [])
                            for span in line['spans'] for c in span['chars'] if c['c'] == 'ð')
                cache = {}
                glyph = _source_anchor_glyphs(page, [{'text': char['c'], 'rect': char['bbox'],
                                                    'origin': char['origin']}], cache)[0]
                pen = RecordingPen()
                cache[glyph['xref']][1][glyph['name']].draw(pen)
                identity = ('cff', 1000, hashlib.sha256(repr(pen.value).encode()).hexdigest())
                data = document.tobytes()
            source.write_bytes(data)
            # 用自造字体模拟已审核轮廓，实际extract/load流程不能被mock替代。
            # 同一个源文件在旧计划中保留原语义，新计划显式采用新修订。
            with patch('paperlocale.source_symbols.REVIEWED_BRACKET_OUTLINES', {identity: '('}):
                old = extract_layout(source, paragraph=True, symbol_revision=4)
                modern = extract_layout(source, paragraph=True)
                self.assertEqual(modern['symbol_revision'], 6)
                self.assertIn('ð', ' '.join(block['text'] for block in old['blocks']))
                self.assertNotIn('ð', ' '.join(block['text'] for block in modern['blocks']))
                self.assertNotEqual(old, modern)
                for name, plan in [('old', old), ('modern', modern)]:
                    path = root / f'{name}.json'
                    save_json(path, plan)
                    checked, _ = load_plan(source, path, paragraph=True)
                    self.assertEqual(checked, plan)
