"""使用项目自有合成 PDF 检查页数、尺寸、图片对象和逐页渲染。"""

from __future__ import annotations

import hashlib
import logging
import tempfile
import unittest
from pathlib import Path

from PIL import Image
from reportlab.lib.pagesizes import A4, letter
from reportlab.pdfgen import canvas

from paperlocale.qa import _extract_text, inspect_pdf_pair


def _build_pdf(path: Path, *, pagesize: tuple[float, float], label: str, image: Path) -> None:
    """生成含双栏、矢量表格、公式文本和图片对象的一页合成论文。"""

    width, height = pagesize
    document = canvas.Canvas(str(path), pagesize=pagesize)
    document.setFont("Helvetica-Bold", 14)
    document.drawString(40, height - 40, label)
    document.setFont("Helvetica", 8)
    for column in (40, width / 2 + 10):
        for row in range(12):
            document.drawString(column, height - 70 - row * 11, f"Column text {row + 1}: E = mc^2")
    table_x = 40
    table_y = height - 260
    for offset in (0, 50, 100, 150):
        document.line(table_x + offset, table_y, table_x + offset, table_y - 45)
    for offset in (0, 15, 30, 45):
        document.line(table_x, table_y - offset, table_x + 150, table_y - offset)
    document.drawImage(str(image), width - 140, 70, width=90, height=60)
    document.showPage()
    document.save()


class PdfQaTest(unittest.TestCase):
    def test_cmap_warnings_are_counted_without_console_noise(self) -> None:
        """可恢复字体日志应进入报告计数，且调用后恢复原 logger 配置。"""

        logger = logging.getLogger("pypdf._cmap")
        original_handlers = list(logger.handlers)
        original_level = logger.level
        original_propagate = logger.propagate

        class _NoisyPage:
            def extract_text(self) -> str:
                logging.getLogger("pypdf._cmap").warning("recoverable cmap")
                return "译文"

        text, warning_count = _extract_text(_NoisyPage())
        self.assertEqual((text, warning_count), ("译文", 1))
        self.assertEqual(logger.handlers, original_handlers)
        self.assertEqual(logger.level, original_level)
        self.assertEqual(logger.propagate, original_propagate)

    def test_matching_layout_generates_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "figure.png"
            Image.new("RGB", (120, 80), "steelblue").save(image)
            source = root / "source.pdf"
            translated = root / "translated.pdf"
            _build_pdf(source, pagesize=A4, label="Source", image=image)
            _build_pdf(translated, pagesize=A4, label="Translated", image=image)
            report = inspect_pdf_pair(
                source_pdf=source,
                translated_pdf=translated,
                output_dir=root / "qa",
                dpi=72,
            )
            self.assertEqual(report["errors"], [])
            self.assertEqual(report["source_pages"], 1)
            self.assertEqual(
                report["source_sha256"],
                hashlib.sha256(source.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                report["translated_sha256"],
                hashlib.sha256(translated.read_bytes()).hexdigest(),
            )
            self.assertTrue((root / "qa" / "comparisons" / "page-001.png").is_file())
            self.assertTrue(report["visual_inspection_required"])

    def test_page_size_mismatch_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "figure.png"
            Image.new("RGB", (120, 80), "steelblue").save(image)
            source = root / "source.pdf"
            translated = root / "translated.pdf"
            _build_pdf(source, pagesize=A4, label="Source", image=image)
            _build_pdf(translated, pagesize=letter, label="Translated", image=image)
            report = inspect_pdf_pair(
                source_pdf=source,
                translated_pdf=translated,
                output_dir=root / "qa",
                dpi=72,
            )
            self.assertTrue(any("MediaBox" in error for error in report["errors"]))

    def test_lost_image_and_vector_table_are_errors(self) -> None:
        """译文页丢失图片或矢量表格时必须阻止 QA，而不只是发出警告。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "figure.png"
            Image.new("RGB", (120, 80), "steelblue").save(image)
            source = root / "source.pdf"
            translated = root / "translated.pdf"
            _build_pdf(source, pagesize=A4, label="Source", image=image)

            _width, height = A4
            document = canvas.Canvas(str(translated), pagesize=A4)
            document.setFont("Helvetica", 9)
            # 译文保留足够正文以通过空白页检查，但故意不绘制图片和矢量表格。
            for row in range(24):
                document.drawString(
                    40,
                    height - 50 - row * 22,
                    f"Translated paragraph {row + 1}: E = mc^2",
                )
            document.showPage()
            document.save()

            report = inspect_pdf_pair(
                source_pdf=source,
                translated_pdf=translated,
                output_dir=root / "qa",
                dpi=72,
            )
            self.assertTrue(any("图片对象减少" in error for error in report["errors"]))
            self.assertTrue(any("矢量绘图减少" in error for error in report["errors"]))
            page = report["pages"][0]
            self.assertGreater(page["source_vector_drawings"], 0)
            self.assertEqual(page["translated_vector_drawings"], 0)


if __name__ == "__main__":
    unittest.main()
