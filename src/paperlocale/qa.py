"""源 PDF 与译文 PDF 的结构检查、全页渲染和对照图生成。"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import shutil
import subprocess
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw
from pypdf import PdfReader
from .pdf_streams import content_parsing_budget, selected_content_operations

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


def _canonical_page_geometry(source: Path, target: Path) -> list[dict]:
    """Use the preserved workflow's source coordinates, with pixel proof of rotation."""
    from .font_geometry import open_source_pdf

    result = []
    with fitz.open(source) as raw, open_source_pdf(source) as before, fitz.open(target) as after:
        for index, (a, b) in enumerate(zip(before, after)):
            normalized = raw[index].rotation != a.rotation
            if normalized:
                x = raw[index].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                y = a.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                if (x.width, x.height, x.samples) != (y.width, y.height, y.samples):
                    raise ValueError(f'第{index + 1}页源页归一化改变了原文像素')
            result.append({'source_media_box': tuple(a.mediabox), 'translated_media_box': tuple(b.mediabox),
                           'source_crop_box': tuple(a.cropbox), 'translated_crop_box': tuple(b.cropbox),
                           'source_rotation': a.rotation, 'translated_rotation': b.rotation,
                           'source_effective_rect': tuple(a.rect), 'translated_effective_rect': tuple(b.rect),
                           'raw_source_rotation': raw[index].rotation,
                           'source_rotation_pixels_verified': normalized})
    return result


def _image_counts(pdf_path: Path) -> list[int]:
    """统计内容流实际调用的图片（含嵌套 Form 与同图多次放置）。

    资源表中的软蒙版和未调用图片不是独立页面插图，不能按 page.images
    数量判定缺图；否则清理冗余资源就会误报。仍检查实际放置次数减少。
    """

    # MuPDF会把sh渐变绘制展开成临时小位图；它们不是PDF图片调用，
    # 相同渐变在编辑前后可能展开成不同数量，不能据此判定缺图。
    # 直接计数内容流中的Image/内联图片，并逐次展开Form调用；渐变
    # 的可见完整性仍由源版面像素核验负责，真实图片减少仍报错。
    reader = PdfReader(pdf_path)

    def count(stream, resources, active):
        if stream is None:
            return 0
        total = 0
        for operands, operator in selected_content_operations(stream, reader, {b'Do', b'INLINE IMAGE'}):
            if operator == b'INLINE IMAGE':
                total += 1
            elif operator == b'Do':
                reference = resources.get('/XObject', {})[operands[0]]
                obj = reference.get_object()
                if obj.get('/Subtype') == '/Image':
                    total += 1
                elif obj.get('/Subtype') == '/Form':
                    identity = id(obj)
                    if identity in active:
                        raise ValueError('PDF Form资源存在循环引用，不能完成图片核验')
                    total += count(obj, obj.get('/Resources', resources), active | {identity})
        return total

    return [count(page.get_contents(), page.get('/Resources', {}), set())
            for page in reader.pages]


def _nonempty_image_calls(pdf_path: Path, index: int) -> list[tuple]:
    """只读记录非空裁切中的原生图片调用；不按图片包围框猜裁切结果。

    MuPDF sanitize 在裁切交集为空时不转发图片。使用同一原生解释器的
    image_filter 回调记录其余调用，no_update 禁止写回，文档副本用后关闭。
    仅支持页内 Image XObject；Form/内联图片缺少这里所需的独立资源身份，
    明确拒绝本证明，调用者继续按原缺图错误处理，不作宽松降级。
    """
    with fitz.open(pdf_path) as document:
        page = document[index]
        if page.get_xobjects():
            raise ValueError('含 Form 的页面不能使用空裁切图片证明')
        names = {item[7] for item in page.get_images(full=True)}

        class ImageCalls(fitz.mupdf.PdfSanitizeFilterOptions2):
            def __init__(self):
                super().__init__()
                self.use_virtual_image_filter()
                # 重叠图片的后绘制内容会覆盖先绘制内容；相同调用集合及次数
                # 不能证明顺序相同。按原生解释器的回调顺序保留每次调用。
                self.calls = []

            def image_filter(self, context, matrix, name, image, clip):
                if not name or name not in names:
                    raise ValueError('内联或未知图片无法建立源资源身份')
                geometry = (matrix.a, matrix.b, matrix.c, matrix.d, matrix.e, matrix.f,
                            clip.x0, clip.y0, clip.x1, clip.y1)
                if not all(math.isfinite(value) for value in geometry):
                    raise ValueError('图片调用包含非有限坐标')
                self.calls.append((name, *geometry))
                return image

        observations = ImageCalls()
        options = fitz._make_PdfFilterOptions(instance_forms=0, recurse=0, no_update=1,
                                             sanitize=1, sopts=observations)
        native = page._pdf_page()
        fitz.mupdf.pdf_filter_page_contents(native.doc(), native, options)
        return observations.calls


def _empty_clip_image_evidence(source_path, target_path, index, source_page, target_page,
                               source_count, target_count):
    """建立非空调用顺序、几何及图片资源相同的局部证据。

    clip 回调只给裁切包围框，不包含完整路径或透明度状态，所以本函数
    不能独自免除缺图错误。调用者还必须持有当前源/译文件的成功版面
    保护报告，联合其真实非正文及保护区域像素核验后才接纳空裁切差。
    """
    # 空 clip 中的内联图片会被 sanitize 在 image_filter 之前丢弃，不能
    # 依靠回调里的未知名字检查覆盖它。先由原生 PDF 语法完整解析两条流，
    # 明确拒绝任何内联图片；仅在 raw 数减少的诊断分支支付这次解析开销。
    for page in (source_page, target_page):
        if selected_content_operations(page.get_contents(), page.pdf, {b'INLINE IMAGE'}):
            raise ValueError('含内联图片的页面不能使用空裁切图片证明')
    before = _nonempty_image_calls(source_path, index)
    after = _nonempty_image_calls(target_path, index)
    if before != after:
        return None
    # 沿用精度核验的完整资源语义比较：解析后的图像数据、软蒙版、颜色空间
    # 等必须一致；只允许压缩方式和 float32 表示差，不以 xref 编号当作身份。
    from .vector_precision import _same_resource
    source_objects = source_page.get('/Resources', {}).get('/XObject', {})
    target_objects = target_page.get('/Resources', {}).get('/XObject', {})
    used = {name for name, *_ in before}
    for name in used:
        key = '/' + name
        if key not in source_objects or key not in target_objects:
            return None
        if not _same_resource(source_objects[key], target_objects[key]):
            return None
    effective = len(before)
    if effective > min(source_count, target_count):
        return None
    return {'method': 'native_nonempty_clip_calls_and_source_image_resources',
            'source_nonempty_calls': effective, 'translated_nonempty_calls': effective,
            'source_empty_clip_calls': source_count - effective,
            'translated_empty_clip_calls': target_count - effective,
            'nonempty_call_geometry_and_multiplicity_equal': True,
            'nonempty_call_order_equal': True,
            'used_image_resources_equal': True, 'used_image_resource_count': len(used)}


def _bound_preservation_report(report, source_sha256, translated_sha256, page_count):
    """校验既有成功报告的文件绑定和完整结构，不把任意 truthy 对象当凭据。

    run_preserved 只在每页非正文、全部保护区及固定墨迹核验均通过后写出
    此报告。protected_region_count 与 fixed_ink_checks 是已成功核验的
    工作量；body_overlap_pixels 是使用专门基准核验的重叠量，不是失败数。
    这里复用该生成契约，不把局部图片调用签名升级成全部图形状态证明。
    """
    if (not isinstance(report, dict)
            or report.get('source_sha256') != source_sha256
            or report.get('translated_sha256') != translated_sha256
            or type(report.get('dpi')) is not int or report['dpi'] < 144
            or type(report.get('protected_region_count')) is not int
            or report['protected_region_count'] < 0):
        return False
    pages = report.get('pages')
    if (not isinstance(pages, list) or len(pages) != page_count
            or any(not isinstance(item, dict) or type(item.get('page')) is not int
                   or item['page'] != number or item.get('outside_pixels_equal') is not True
                   for number, item in enumerate(pages, 1))):
        return False
    checks = report.get('fixed_ink_checks')
    if not isinstance(checks, list):
        return False
    checked_pages = set()
    for item in checks:
        if (not isinstance(item, dict) or type(item.get('page')) is not int
                or not 1 <= item['page'] <= page_count or item['page'] in checked_pages
                or any(type(item.get(key)) is not int or item[key] < 0
                       for key in ('fixed_ink_pixels', 'body_overlap_pixels'))):
            return False
        checked_pages.add(item['page'])
    return True


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
        contents = page.get_contents()  # type: ignore[attr-defined]
        # 文本提取内部也会解析整页；使用相同局部有限预算，继续用独立
        # pypdf回读文字，不因复杂图形页改换提取器或隐藏CMap异常。
        with content_parsing_budget(contents.get_data() if contents is not None else b''):
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
    canonical_source_geometry: bool = False,
    preservation_report: dict | None = None,
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
    source_sha256, translated_sha256 = _sha256(source_path), _sha256(target_path)
    preservation_bound = (len(source_reader.pages) == len(target_reader.pages)
        and _bound_preservation_report(preservation_report, source_sha256,
                                       translated_sha256, len(source_reader.pages)))
    geometry = _canonical_page_geometry(source_path, target_path) if canonical_source_geometry else []
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

    try:
        source_image_counts = _image_counts(source_path)
        target_image_counts = _image_counts(target_path)
    except Exception as exc:  # noqa: BLE001  # 特殊内容流的解析异常类型不固定。
        source_image_counts = [-1] * len(source_reader.pages)
        target_image_counts = [-1] * len(target_reader.pages)
        warnings.append(f"图片实际放置无法枚举：{exc}")

    checked_pages = min(len(source_reader.pages), len(target_reader.pages))
    for index in range(checked_pages):
        source_page = source_reader.pages[index]
        target_page = target_reader.pages[index]
        source_media = _box(source_page, "mediabox")
        target_media = _box(target_page, "mediabox")
        source_crop = _box(source_page, "cropbox")
        target_crop = _box(target_page, "cropbox")
        raw_source_media, raw_source_crop = source_media, source_crop
        if canonical_source_geometry:
            record = geometry[index]
            source_media, target_media = record['source_media_box'], record['translated_media_box']
            source_crop, target_crop = record['source_crop_box'], record['translated_crop_box']
            if record['source_rotation'] != record['translated_rotation']:
                errors.append(f"第{index + 1}页旋转方向不一致")
            if record['source_effective_rect'] != record['translated_effective_rect']:
                errors.append(f"第{index + 1}页实际尺寸不一致")
        if (source_media != target_media if canonical_source_geometry else
                any(abs(left - right) > 0.1 for left, right in zip(source_media, target_media))):
            errors.append(f"第{index + 1}页 MediaBox 不一致")
        if (source_crop != target_crop if canonical_source_geometry else
                any(abs(left - right) > 0.1 for left, right in zip(source_crop, target_crop))):
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
        source_images = source_image_counts[index]
        target_images = target_image_counts[index]
        image_evidence = None
        if source_images >= 0 and target_images < source_images and preservation_bound:
            try:
                image_evidence = _empty_clip_image_evidence(source_path, target_path, index,
                    source_page, target_page, source_images, target_images)
            except Exception as exc:  # noqa: BLE001  # 原生不支持的结构仍保留缺图错误。
                warnings.append(f'第{index + 1}页空裁切图片证明不可用：{exc}')
            if image_evidence is not None:
                image_evidence['bound_preservation_pixels_equal'] = True
                image_evidence['preservation_dpi'] = preservation_report['dpi']
        if source_images >= 0 and target_images < source_images and image_evidence is None:
            errors.append(
                f"第{index + 1}页图片对象减少：source={source_images}, translated={target_images}"
            )
        try:
            # 优先统计实际绘制对象，包含 Form XObject 内的图形。仅统计顶层
            # 内容流会漏掉嵌套图表，也会把背景修复的操作符误当作缺图已恢复。
            source_vectors = (len(source_vector_objects[index])
                              if index < len(source_vector_objects) else _vector_paint_count(source_page))
            target_vectors = (len(target_vector_objects[index])
                              if index < len(target_vector_objects) else _vector_paint_count(target_page))
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
        if canonical_source_geometry:
            page_record.update(geometry[index], raw_source_media_box=raw_source_media,
                               raw_source_crop_box=raw_source_crop)
        if image_evidence is not None:
            page_record['image_preservation_evidence'] = image_evidence
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

    final_source_sha256, final_translated_sha256 = _sha256(source_path), _sha256(target_path)
    if (final_source_sha256, final_translated_sha256) != (source_sha256, translated_sha256):
        errors.append('源文或译文 PDF 在 QA 读取期间发生变化，版面保护证据失效')
    report: dict[str, object] = {
        "source_pdf": str(source_path),
        "translated_pdf": str(target_path),
        "source_sha256": final_source_sha256,
        "translated_sha256": final_translated_sha256,
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
