"""矢量匹配回归：零面积曲线简化不能触发叠画，实际路径缺失仍要报告。"""
import unittest
from unittest.mock import Mock, patch
from pathlib import Path
import pymupdf as fitz
from paperlocale.workflow import _missing_source_vector_drawings
from paperlocale.preserved_workflow import run_preserved


class VectorRoundingTests(unittest.TestCase):
    def drawing(self, *, degenerate=False, kind='f'):
        items = [('l', fitz.Point(10, 10), fitz.Point(20, 20))]
        if degenerate:
            items.append(('c', *(fitz.Point(20, 20) for _ in range(4))))
        return {'items': items, 'type': kind, 'fill': (0, 0, 0)}

    def test_zero_area_curve_does_not_replay_existing_fill(self):
        source = self.drawing(degenerate=True)
        rewritten = self.drawing()
        self.assertEqual(_missing_source_vector_drawings(
            Mock(get_drawings=lambda: [source]), Mock(get_drawings=lambda: [rewritten])), [])

    def test_stroked_curve_and_missing_duplicate_are_not_hidden(self):
        source = self.drawing(degenerate=True, kind='s')
        self.assertEqual(_missing_source_vector_drawings(
            Mock(get_drawings=lambda: [source]),
            Mock(get_drawings=lambda: [self.drawing(kind='s')])), [source])
        source = self.drawing(degenerate=True)
        self.assertEqual(_missing_source_vector_drawings(
            Mock(get_drawings=lambda: [source, source]),
            Mock(get_drawings=lambda: [self.drawing()])), [source])

    def test_rendered_resume_can_explicitly_omit_post_render_qa(self):
        # 用户选择不运行输出后QA时，状态保持rendered，不伪造通过报告。
        manifest = {'status': 'rendered'}
        with patch('paperlocale.preserved_workflow.load_manifest', return_value=manifest), \
             patch('paperlocale.preserved_workflow._verify_source_pdf'), \
             patch('paperlocale.preserved_workflow._verify_rendered_pdf'), \
             patch('paperlocale.preserved_workflow.qa_run') as qa:
            result = run_preserved(Path('/unused'), provider=None, domain=None,
                                   plan_path=None, font_file=None, generate_qa=False)
        qa.assert_not_called()
        self.assertEqual(result['status'], 'rendered')
