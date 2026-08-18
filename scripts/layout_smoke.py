"""生成自有合成论文并跑通 PaperLocale 版面闭环。

本脚本不调用真实模型。它用确定性 Provider 验证 PDFMathTranslate-next 桥接、
科学信息门禁、PDF 重建和逐页 QA，适合发布前或上游升级后的本地兼容性检查。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
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
    initialize_run,
    run_to_qa,
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


def _scaled_default_label(text: str, scale: int = 3) -> Image.Image:
    """用 Pillow 自带字体生成跨平台标题，避免依赖操作系统字体文件。

    默认位图字体尺寸较小，因此先按原始像素绘制，再用最近邻插值整数倍放大。
    这种做法在 macOS、Linux 和 CI 中得到相同结果，也不会为一个演示图引入字体依赖。
    """

    font = ImageFont.load_default()
    left, top, right, bottom = font.getbbox(text)
    label = Image.new("RGBA", (right - left + 2, bottom - top + 2), (0, 0, 0, 0))
    ImageDraw.Draw(label).text((-left + 1, -top + 1), text, font=font, fill="#10243e")
    return label.resize(
        (label.width * scale, label.height * scale),
        Image.Resampling.NEAREST,
    )


def _demo_frame(content: Image.Image, title: str, accent: str) -> Image.Image:
    """把一张 QA 页面放入固定尺寸画布，保证 GIF 各帧尺寸完全一致。"""

    canvas = Image.new("RGB", (960, 720), "#f5f7fb")
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((20, 18, 940, 702), radius=18, fill="white", outline="#d8deea")
    draw.rounded_rectangle((36, 32, 48, 66), radius=6, fill=accent)
    label = _scaled_default_label(title)
    canvas.paste(label, (62, 38), label)

    # 只在副本上缩放，调用方传入的源图不会被修改；缩略图保留原始宽高比。
    fitted = content.convert("RGB")
    fitted.thumbnail((888, 610), Image.Resampling.LANCZOS)
    x = (canvas.width - fitted.width) // 2
    y = 78 + (610 - fitted.height) // 2
    canvas.paste(fitted, (x, y))
    return canvas


def build_demo_gif(comparison_path: Path, output_path: Path) -> Path:
    """把真实逐页 QA 对照图转换为可嵌入 README 的三帧演示 GIF。

    第一帧展示原文页，第二帧展示译文页，第三帧回到完整并排对照。
    输入必须是 PaperLocale 生成的左右等宽对照图；奇数像素会归入右半页。
    """

    comparison_file = comparison_path.expanduser().resolve()
    if not comparison_file.is_file():
        raise FileNotFoundError(f"QA 对照图不存在：{comparison_file}")
    with Image.open(comparison_file) as opened:
        comparison = opened.convert("RGB")
    if comparison.width < 2 or comparison.height < 2:
        raise ValueError("QA 对照图尺寸过小，无法拆分原文页与译文页")

    midpoint = comparison.width // 2
    source = comparison.crop((0, 0, midpoint, comparison.height))
    translated = comparison.crop((midpoint, 0, comparison.width, comparison.height))
    frames = [
        _demo_frame(source, "1  SOURCE PDF", "#2f6feb"),
        _demo_frame(translated, "2  TRANSLATED PDF", "#2da44e"),
        _demo_frame(comparison, "3  ALL-PAGE QA COMPARISON", "#8250df"),
    ]

    destination = output_path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        destination,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=[1500, 1500, 2400],
        loop=0,
        optimize=True,
        disposal=2,
    )
    return destination


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
    parser.add_argument(
        "--demo-gif",
        type=Path,
        help="可选：把首张逐页 QA 对照图导出为 README 演示 GIF",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = args.output.expanduser().resolve()
    if root.exists():
        raise FileExistsError(f"输出目录已存在，请更换 --output，避免覆盖证据：{root}")
    root.mkdir(parents=True)

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
    manifest = run_to_qa(
        run_dir=run_dir,
        provider=SmokeProvider(),
        domain=domain,
        pdf2zh_bin=args.pdf2zh_bin,
        dpi=96,
        pdftoppm_bin=args.pdftoppm_bin,
    )
    translated_pdf = Path(str(manifest["rendered_pdf"]))
    report = json.loads(
        Path(str(manifest["qa_report"])).read_text(encoding="utf-8")
    )
    comparison = Path(str(report["pages"][0]["comparison"]))
    print(f"版面冒烟测试通过：{translated_pdf}")
    print(f"仍需人工查看逐页对照：{comparison}")
    if args.demo_gif:
        print(f"演示 GIF 已生成：{build_demo_gif(comparison, args.demo_gif)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
