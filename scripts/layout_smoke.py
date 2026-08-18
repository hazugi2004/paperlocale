"""生成自有合成论文并跑通 PaperLocale 版面闭环。

本脚本不调用真实模型。它用确定性 Provider 验证 PDFMathTranslate-next 桥接、
科学信息门禁、PDF 重建和逐页 QA，适合发布前或上游升级后的本地兼容性检查。
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from paperlocale.domains import load_domain_pack
from paperlocale.providers import (
    Segment,
    Translation,
    TranslationContext,
    TranslationProvider,
)
from paperlocale.workflow import (
    collect_run,
    initialize_run,
    qa_run,
    render_run,
    translate_run,
    validate_run,
)


class SmokeProvider(TranslationProvider):
    """只替换已知合成文本，保证测试结果不依赖网络和模型随机性。"""

    def translate(
        self,
        segments: list[Segment],
        context: TranslationContext,
    ) -> list[Translation]:
        translations: list[Translation] = []
        for segment in segments:
            target = segment.source
            for source, translated in (
                ("Compound dry-hot events", "复合干热事件"),
                ("Compound dry-hot event", "复合干热事件"),
                ("soil moisture", "土壤湿度"),
                ("Source scientific paper", "源科学论文"),
                ("Column text", "栏文本"),
                ("Hello", "你好"),
            ):
                target = target.replace(source, translated)
            if target == segment.source:
                # 未知的版面引擎探测片段仍保留原文，并加入中文以通过正文语言门禁。
                target = f"{segment.source} 测试译文"
            translations.append(Translation(id=segment.id, target=target))
        return translations


def build_synthetic_pdf(path: Path, figure_path: Path) -> None:
    """生成包含双栏、公式、矢量表格和图片对象的一页 A4 测试论文。"""

    width, height = A4
    document = canvas.Canvas(str(path), pagesize=A4)
    document.setTitle("PaperLocale layout smoke test")
    document.setFont("Helvetica-Bold", 14)
    document.drawString(40, height - 40, "Compound dry-hot events")
    document.setFont("Helvetica", 8)
    line = "Column text {n}: Compound dry-hot event; soil moisture 10 mm; E = mc^2"
    for column in (40, width / 2 + 10):
        for row in range(8):
            document.drawString(column, height - 70 - row * 13, line.format(n=row + 1))

    # 矢量表格故意不用图片绘制，以同时验证 PDF 图元与图片对象两条路径。
    table_x = 40
    table_y = height - 250
    for offset in (0, 50, 100, 150):
        document.line(table_x + offset, table_y, table_x + offset, table_y - 45)
    for offset in (0, 15, 30, 45):
        document.line(table_x, table_y - offset, table_x + 150, table_y - offset)
    document.drawImage(str(figure_path), width - 140, 70, width=90, height=60)
    document.showPage()
    document.save()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("tmp/layout-smoke"))
    parser.add_argument("--pdf2zh-bin")
    parser.add_argument("--pdftoppm-bin")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = args.output.expanduser().resolve()
    if root.exists():
        raise FileExistsError(f"输出目录已存在，请更换 --output，避免覆盖证据：{root}")
    root.mkdir(parents=True)

    pdf2zh = args.pdf2zh_bin or shutil.which("pdf2zh_next")
    if not pdf2zh:
        raise FileNotFoundError("未找到 pdf2zh_next；请安装项目的 layout 可选依赖")

    figure = root / "figure.png"
    Image.new("RGB", (240, 160), "steelblue").save(figure)
    source = root / "source.pdf"
    build_synthetic_pdf(source, figure)

    run_dir = root / "run"
    domain = load_domain_pack("atmospheric-science")
    initialize_run(
        source_pdf=source,
        run_dir=run_dir,
        source_language="en",
        target_language="zh-CN",
    )
    collect_run(run_dir, pdf2zh)
    translate_run(run_dir=run_dir, provider=SmokeProvider(), domain=domain)
    validate_run(run_dir, domain)
    translated_pdf = render_run(run_dir, pdf2zh)
    report = qa_run(
        run_dir,
        dpi=96,
        pdftoppm_bin=args.pdftoppm_bin,
    )
    comparison = Path(str(report["pages"][0]["comparison"]))
    print(f"版面冒烟测试通过：{translated_pdf}")
    print(f"仍需人工查看逐页对照：{comparison}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
