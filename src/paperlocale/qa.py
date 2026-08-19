"""源 PDF 与译文 PDF 的结构检查、全页渲染和对照图生成。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

import fitz
from PIL import Image, ImageDraw
from pypdf import PdfReader

PLACEHOLDER_RE = re.compile(r"\{v\d+\}|<style\s+id=|</style>", re.IGNORECASE)
VECTOR_PAINT_OPERATORS = {
    b"S",
    b"s",
    b"f",
    b"F",
    b"f*",
    b"B",
    b"B*",
    b"b",
    b"b*",
}


def _sha256(path: Path) -> str:
    """把 QA 报告绑定到实际检查的两个 PDF 字节。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _box(page: object, name: str) -> tuple[float, float, float, float]:
    """把 pypdf RectangleObject 转成可序列化浮点元组。"""

    rectangle = getattr(page, name)
    return tuple(float(value) for value in rectangle)  # type: ignore[return-value]


def _image_count(page: object) -> int:
    """统计页面可枚举图片；解析失败时交给调用方记录警告。"""

    return len(list(page.images))  # type: ignore[attr-defined]


def _vector_paint_count(page: object) -> int:
    """统计页面内容流中实际描边或填充的路径，用于发现矢量表格/图形丢失。"""

    contents = page.get_contents()  # type: ignore[attr-defined]
    if contents is None:
        return 0
    return sum(
        operator in VECTOR_PAINT_OPERATORS
        for _operands, operator in contents.operations
    )


def _vector_objects(pdf_path: Path) -> list[list[dict[str, object]]]:
    """读取每页矢量绘图的边界框与面积，用于定位数量减少的具体对象。"""

    document = fitz.open(pdf_path)
    pages: list[list[dict[str, object]]] = []
    try:
        for page in document:
            objects: list[dict[str, object]] = []
            for drawing in page.get_drawings():
                rectangle = fitz.Rect(drawing["rect"])
                objects.append(
                    {
                        "bbox": [round(float(value), 3) for value in rectangle],
                        "area": round(float(rectangle.get_area()), 3),
                    }
                )
            pages.append(objects)
    finally:
        document.close()
    return pages


def _missing_vector_objects(
    source: list[dict[str, object]],
    translated: list[dict[str, object]],
) -> list[dict[str, object]]:
    """按 0.01 PDF 点坐标匹配对象，返回只在源页出现的矢量绘图。"""

    translated_counts: dict[tuple[float, ...], int] = {}
    for item in translated:
        key = tuple(round(float(value), 2) for value in item["bbox"])
        translated_counts[key] = translated_counts.get(key, 0) + 1

    missing: list[dict[str, object]] = []
    for item in source:
        key = tuple(round(float(value), 2) for value in item["bbox"])
        if translated_counts.get(key, 0):
            translated_counts[key] -= 1
        else:
            missing.append(item)
    return missing


def _extract_text(page: object) -> tuple[str, int]:
    """提取文字并把 pypdf 可恢复的 CMap 噪声压缩为计数。

    BabelDOC 生成的部分复合字体映射可以被 pypdf 容错读取，但一次页面提取可能
    输出数百行警告。这里仅在单次调用期间接管对应 logger，随后完整恢复全局
    配置；调用方仍会把警告数量写入 QA 报告，避免静默隐藏解析异常。
    """

    logger = logging.getLogger("pypdf._cmap")
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collector(level=logging.WARNING)
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate
    logger.handlers = [handler]
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        text = page.extract_text() or ""  # type: ignore[attr-defined]
    finally:
        logger.handlers = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate
    return str(text), len(records)


def _render(
    pdf_path: Path,
    output_dir: Path,
    prefix: str,
    *,
    dpi: int,
    pdftoppm_bin: str | Path | None,
) -> list[Path]:
    """使用 Poppler 渲染全部页面，并要求进程成功和输出非空。"""

    executable = str(pdftoppm_bin) if pdftoppm_bin else shutil.which("pdftoppm")
    if not executable:
        raise FileNotFoundError("未找到 pdftoppm；无法执行全页视觉 QA")
    output_prefix = output_dir / prefix
    completed = subprocess.run(
        [
            executable,
            "-png",
            "-r",
            str(dpi),
            str(pdf_path),
            str(output_prefix),
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"pdftoppm 渲染失败，exit={completed.returncode}：{completed.stderr[-2000:]}"
        )
    images = sorted(output_dir.glob(f"{prefix}-*.png"))
    if not images:
        raise RuntimeError(f"pdftoppm 没有生成页面图：{pdf_path}")
    return images


def _nonwhite_ratio(image: Image.Image) -> float:
    """估算页面非白像素比例，用于发现意外空白页。"""

    grayscale = image.convert("L")
    histogram = grayscale.histogram()
    white_like = sum(histogram[248:])
    return 1.0 - white_like / (image.width * image.height)


def _draw_missing_vector_boxes(
    draw: ImageDraw.ImageDraw,
    *,
    objects: list[dict[str, object]],
    image_size: tuple[int, int],
    page_size: tuple[float, float],
    offset: tuple[int, int],
) -> None:
    """把 PDF 点坐标映射到渲染图，在缺失位置绘制醒目的红框。"""

    if not objects or page_size[0] <= 0 or page_size[1] <= 0:
        return
    scale_x = image_size[0] / page_size[0]
    scale_y = image_size[1] / page_size[1]
    for item in objects:
        left, top, right, bottom = (float(value) for value in item["bbox"])
        box = (
            offset[0] + left * scale_x - 4,
            offset[1] + top * scale_y - 4,
            offset[0] + right * scale_x + 4,
            offset[1] + bottom * scale_y + 4,
        )
        draw.rectangle(box, outline="red", width=3)


def _comparison(
    source: Image.Image,
    target: Image.Image,
    page_number: int,
    *,
    missing_vectors: list[dict[str, object]] | None = None,
    page_size: tuple[float, float] | None = None,
) -> Image.Image:
    """生成一张明确标注左右来源的逐页复核图。"""

    header = 42
    gap = 18
    canvas = Image.new(
        "RGB",
        (source.width + target.width + gap, max(source.height, target.height) + header),
        "white",
    )
    canvas.paste(source, (0, header))
    canvas.paste(target, (source.width + gap, header))
    draw = ImageDraw.Draw(canvas)
    draw.text((12, 12), f"Page {page_number} - SOURCE", fill="black")
    draw.text((source.width + gap + 12, 12), f"Page {page_number} - TRANSLATED", fill="black")
    if missing_vectors and page_size:
        # 源图框出实际对象，译文图框出其应出现的位置，方便人工并排定位。
        _draw_missing_vector_boxes(
            draw,
            objects=missing_vectors,
            image_size=source.size,
            page_size=page_size,
            offset=(0, header),
        )
        _draw_missing_vector_boxes(
            draw,
            objects=missing_vectors,
            image_size=target.size,
            page_size=page_size,
            offset=(source.width + gap, header),
        )
    return canvas


def inspect_pdf_pair(
    *,
    source_pdf: Path,
    translated_pdf: Path,
    output_dir: Path,
    dpi: int = 144,
    pdftoppm_bin: str | Path | None = None,
) -> dict[str, object]:
    """生成机器报告和全部页面对照图；不以自动检查替代人工视觉确认。"""

    source_path = source_pdf.expanduser().resolve()
    target_path = translated_pdf.expanduser().resolve()
    root = output_dir.expanduser().resolve()
    render_dir = root / "rendered"
    comparison_dir = root / "comparisons"
    render_dir.mkdir(parents=True, exist_ok=True)
    comparison_dir.mkdir(parents=True, exist_ok=True)

    source_reader = PdfReader(source_path)
    target_reader = PdfReader(target_path)
    errors: list[str] = []
    warnings: list[str] = []
    pages: list[dict[str, object]] = []
    try:
        source_vector_objects = _vector_objects(source_path)
        target_vector_objects = _vector_objects(target_path)
    except Exception as exc:  # noqa: BLE001  # PyMuPDF 对异常 PDF 的错误类型不固定。
        source_vector_objects = target_vector_objects = []
        warnings.append(f"矢量边界框无法提取：{exc}")
    if len(source_reader.pages) != len(target_reader.pages):
        errors.append(
            f"页数不一致：source={len(source_reader.pages)}, translated={len(target_reader.pages)}"
        )

    checked_pages = min(len(source_reader.pages), len(target_reader.pages))
    for index in range(checked_pages):
        source_page = source_reader.pages[index]
        target_page = target_reader.pages[index]
        source_media = _box(source_page, "mediabox")
        target_media = _box(target_page, "mediabox")
        source_crop = _box(source_page, "cropbox")
        target_crop = _box(target_page, "cropbox")
        if any(abs(left - right) > 0.1 for left, right in zip(source_media, target_media)):
            errors.append(f"第{index + 1}页 MediaBox 不一致")
        if any(abs(left - right) > 0.1 for left, right in zip(source_crop, target_crop)):
            errors.append(f"第{index + 1}页 CropBox 不一致")

        target_text, cmap_warning_count = _extract_text(target_page)
        if cmap_warning_count:
            warnings.append(
                f"第{index + 1}页字体 CMap 由 pypdf 容错读取："
                f"{cmap_warning_count} 条解析警告"
            )
        placeholders = PLACEHOLDER_RE.findall(target_text)
        if placeholders:
            errors.append(f"第{index + 1}页仍有内部占位符：{placeholders[:10]!r}")
        try:
            source_images = _image_count(source_page)
            target_images = _image_count(target_page)
        except Exception as exc:  # noqa: BLE001  # pypdf 可能抛出多类解析异常。
            source_images = target_images = -1
            warnings.append(f"第{index + 1}页图片对象无法枚举：{exc}")
        if source_images >= 0 and target_images < source_images:
            errors.append(
                f"第{index + 1}页图片对象减少：source={source_images}, translated={target_images}"
            )
        try:
            source_vectors = _vector_paint_count(source_page)
            target_vectors = _vector_paint_count(target_page)
        except Exception as exc:  # noqa: BLE001  # 特殊内容流异常类型不固定。
            source_vectors = target_vectors = -1
            warnings.append(f"第{index + 1}页矢量绘图无法枚举：{exc}")
        if source_vectors >= 0 and target_vectors < source_vectors:
            errors.append(
                f"第{index + 1}页矢量绘图减少："
                f"source={source_vectors}, translated={target_vectors}"
            )
        page_record: dict[str, object] = {
                "page": index + 1,
                "source_media_box": source_media,
                "translated_media_box": target_media,
                "source_crop_box": source_crop,
                "translated_crop_box": target_crop,
                "source_images": source_images,
                "translated_images": target_images,
                "source_vector_drawings": source_vectors,
                "translated_vector_drawings": target_vectors,
                "translated_text_characters": len(target_text),
            }
        if (
            target_vectors < source_vectors
            and index < len(source_vector_objects)
            and index < len(target_vector_objects)
        ):
            page_record["missing_vector_drawings"] = _missing_vector_objects(
                source_vector_objects[index],
                target_vector_objects[index],
            )
        pages.append(page_record)

    source_images = _render(
        source_path,
        render_dir,
        "source",
        dpi=dpi,
        pdftoppm_bin=pdftoppm_bin,
    )
    target_images = _render(
        target_path,
        render_dir,
        "translated",
        dpi=dpi,
        pdftoppm_bin=pdftoppm_bin,
    )
    if len(source_images) != len(source_reader.pages):
        errors.append("源 PDF 渲染页数与逻辑页数不一致")
    if len(target_images) != len(target_reader.pages):
        errors.append("译文 PDF 渲染页数与逻辑页数不一致")

    for index, (source_image_path, target_image_path) in enumerate(
        zip(source_images, target_images),
        1,
    ):
        with Image.open(source_image_path) as source_image, Image.open(target_image_path) as target_image:
            ratio = _nonwhite_ratio(target_image)
            if ratio < 0.01:
                errors.append(f"第{index}页疑似空白，非白像素比例={ratio:.4f}")
            page_record = pages[index - 1] if index <= len(pages) else {}
            source_media = page_record.get("source_media_box")
            page_size = None
            if isinstance(source_media, tuple):
                page_size = (
                    float(source_media[2]) - float(source_media[0]),
                    float(source_media[3]) - float(source_media[1]),
                )
            comparison = _comparison(
                source_image.convert("RGB"),
                target_image.convert("RGB"),
                index,
                missing_vectors=page_record.get("missing_vector_drawings"),
                page_size=page_size,
            )
            comparison_path = comparison_dir / f"page-{index:03d}.png"
            comparison.save(comparison_path)
            if index <= len(pages):
                pages[index - 1]["translated_nonwhite_ratio"] = round(ratio, 6)
                pages[index - 1]["comparison"] = str(comparison_path)

    report: dict[str, object] = {
        "source_pdf": str(source_path),
        "translated_pdf": str(target_path),
        "source_sha256": _sha256(source_path),
        "translated_sha256": _sha256(target_path),
        "source_pages": len(source_reader.pages),
        "translated_pages": len(target_reader.pages),
        "dpi": dpi,
        "errors": errors,
        "warnings": warnings,
        "pages": pages,
        "visual_inspection_required": True,
        "visual_accepted": False,
    }
    report_path = root / "qa_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
