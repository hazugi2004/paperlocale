"""验证复杂PDF内容流的局部、有限预算，不改变其他解析安全设置。"""
from dataclasses import asdict
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pypdf
from pypdf.generic import ContentStream, DecodedStreamObject

try:
    from pypdf.errors import LimitReachedError
except ImportError:
    # 项目仍支持没有局部配置API的早期pypdf 6；那时无需执行新版
    # 原生预算验收，不能仅因测试导入新异常类型就破坏旧环境。
    LimitReachedError = None

from paperlocale import pdf_streams


def _stream(data):
    """使用真实pypdf原生内容流，避免mock掉预算实际生效的解析过程。"""
    stream = DecodedStreamObject()
    stream.set_data(data)
    return stream


@unittest.skipUnless(LimitReachedError is not None
                     and hasattr(pypdf, 'apply_configuration')
                     and hasattr(pypdf, 'get_configuration'),
                     '当前pypdf没有新版局部解析预算API')
class PdfStreamBudgetTests(unittest.TestCase):
    def test_budget_uses_length_and_preserves_larger_existing_setting(self):
        for current, data in [(5, b''), (5, b'q'), (5, b'q\n' * 20), (200, b'q\n')]:
            with self.subTest(current=current, length=len(data)):
                with pypdf.apply_configuration(stream_parsing_work_maximum_count=current):
                    original = pypdf.get_configuration()
                    with pdf_streams.content_parsing_budget(data):
                        self.assertEqual(pypdf.get_configuration().stream_parsing_work_maximum_count,
                                         max(current, min(2 * len(data) + 1, 16_000_000)))
                    self.assertIs(pypdf.get_configuration(), original)
        # 调用者主动设置更高上限时不得擅自降低；1600万只限制本模块新增的额度。
        with pypdf.apply_configuration(stream_parsing_work_maximum_count=16_000_001):
            with pdf_streams.content_parsing_budget(b'q'):
                self.assertEqual(pypdf.get_configuration().stream_parsing_work_maximum_count, 16_000_001)

    def test_default_and_all_other_configuration_fields_stay_unchanged(self):
        original = pypdf.get_configuration()
        original_values = asdict(original)
        # 小流不抬高默认额度，退出后也不留下新的配置对象。
        with pdf_streams.content_parsing_budget(b'q\nQ\n'):
            self.assertEqual(asdict(pypdf.get_configuration()), original_values)
        self.assertIs(pypdf.get_configuration(), original)
        # 在非默认调用者配置中，只允许改变目标token预算这一项。
        with pypdf.apply_configuration(stream_parsing_work_maximum_count=5,
                                       array_maximum_element_count=137,
                                       stream_decoding_work_maximum_length=123456):
            configured = pypdf.get_configuration()
            expected = asdict(configured)
            with pdf_streams.content_parsing_budget(b'0 0 m\n' * 20):
                actual = asdict(pypdf.get_configuration())
                self.assertGreater(actual.pop('stream_parsing_work_maximum_count'), 5)
                expected.pop('stream_parsing_work_maximum_count')
                self.assertEqual(actual, expected)
            self.assertIs(pypdf.get_configuration(), configured)
        self.assertIs(pypdf.get_configuration(), original)

    def test_context_restores_configuration_when_body_raises(self):
        with pypdf.apply_configuration(stream_parsing_work_maximum_count=5):
            original = pypdf.get_configuration()
            with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
                with pdf_streams.content_parsing_budget(b'0 0 m\n' * 20):
                    self.assertGreater(pypdf.get_configuration().stream_parsing_work_maximum_count, 5)
                    raise RuntimeError('fixture failure')
            self.assertIs(pypdf.get_configuration(), original)

    def test_real_parser_crosses_low_budget_and_eager_result_survives_restoration(self):
        data = b'0 0 m\n' * 20
        with pypdf.apply_configuration(stream_parsing_work_maximum_count=5):
            original = pypdf.get_configuration()
            with self.assertRaises(LimitReachedError):
                ContentStream(_stream(data), None).operations
            parsed = pdf_streams.parsed_content_stream(_stream(data), None)
            self.assertIs(pypdf.get_configuration(), original)
            # 当前配置已恢复为5；若函数没有在局部上下文内真正解析，这里
            # 会再次触发上限。保留原生操作数与操作符，不能正则切割绕过语法。
            self.assertEqual(len(parsed.operations), 20)
            self.assertTrue(all(operator == b'm' and list(operands) == [0, 0]
                                for operands, operator in parsed.operations))
            self.assertEqual(parsed.get_data(), data)

    def test_finite_cap_still_rejects_excess_work_and_restores_configuration(self):
        self.assertEqual(pdf_streams.MAX_CONTENT_TOKENS, 16_000_000)
        # 把上限缩小，只减少测试资源消耗；解析器仍真实消费token并抛出
        # LimitReachedError，不能用mock成功或仅检查一个常量来代替验收。
        with pypdf.apply_configuration(stream_parsing_work_maximum_count=5):
            original = pypdf.get_configuration()
            with patch.object(pdf_streams, 'MAX_CONTENT_TOKENS', 40):
                data = b'0 0 m\n' * 100
                with pdf_streams.content_parsing_budget(data):
                    self.assertEqual(pypdf.get_configuration().stream_parsing_work_maximum_count, 40)
                with self.assertRaises(LimitReachedError):
                    pdf_streams.parsed_content_stream(_stream(data), None)
            self.assertIs(pypdf.get_configuration(), original)

    def test_selected_operations_match_native_syntax_and_empty_result_does_not_reparse(self):
        # 假Do分别放入普通字符串、十六进制字符串、注释及内联图像数据；
        # 正则搜索会误计，原生语法解析必须只返回真正的绘图操作。
        data = (b'BT /F1 10 Tf (fake /Shadow Do \\(q\\)) Tj ET\n'
                b'% /Comment Do and q Q\n'
                b'0 0 m 10 10 l S\nq /TrueOne Do Q\n'
                b'BT [(a Do) 0 <446F>] TJ ET\n/TrueTwo Do\n'
                b'BI /W 2 /H 1 /BPC 8 /CS /G ID Do EI\n')
        operators = {b'Do', b'INLINE IMAGE'}
        native = ContentStream(_stream(data), None).operations
        expected = [item for item in native if item[1] in operators]
        self.assertEqual([op for _, op in expected], [b'Do', b'Do', b'INLINE IMAGE'])
        with pypdf.apply_configuration(stream_parsing_work_maximum_count=5):
            original = pypdf.get_configuration()
            with self.assertRaises(LimitReachedError):
                ContentStream(_stream(data), None).operations
            selected = pdf_streams.selected_content_operations(_stream(data), None, operators)
            self.assertEqual(selected, expected)
            self.assertIs(pypdf.get_configuration(), original)
            # 包装而不替代原生parser，只计真正解析次数。空匹配返回列表，
            # 之后读取该列表不能因空_operations被当成未解析而再读一次。
            calls = []
            native_parse = ContentStream._parse_content_stream

            def track_parse(instance, stream):
                calls.append(True)
                return native_parse(instance, stream)

            with patch.object(ContentStream, '_parse_content_stream', track_parse):
                empty = pdf_streams.selected_content_operations(_stream(data), None, {b'NoSuchOperator'})
                self.assertIsInstance(empty, list)
                self.assertEqual(list(empty), [])
                self.assertEqual(len(calls), 1)
            self.assertIs(pypdf.get_configuration(), original)

    def test_legacy_api_fallback_is_local_and_does_not_mutate_pypdf(self):
        original = pypdf.get_configuration()
        for available in [None, 'apply_configuration', 'get_configuration']:
            with self.subTest(available=available):
                api = Mock(side_effect=AssertionError('partial API must not be used'))
                legacy = SimpleNamespace(**({available: api} if available else {}))
                # 只替换被测模块持有的引用，不删改全局pypdf的API。真实
                # ContentStream继续使用其原解析器，兼容路径不提高其上限。
                with patch.object(pdf_streams, 'pypdf', legacy):
                    with pdf_streams.content_parsing_budget(b'q\n'):
                        self.assertIs(pypdf.get_configuration(), original)
                    parsed = pdf_streams.parsed_content_stream(_stream(b'q\nQ\n'), None)
                    self.assertEqual([op for _, op in parsed.operations], [b'q', b'Q'])
                api.assert_not_called()
        self.assertIs(pypdf.get_configuration(), original)
