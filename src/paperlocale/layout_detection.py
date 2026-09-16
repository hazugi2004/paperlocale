"""自动视觉版面识别。使用已安装的 BabelDOC 模型，不要求用户画框或改计划。

识别图表、图注、独立公式及页眉页脚；结果绑定源文件和模型摘要保存，
网络/模型资源不可用时由外层等待恢复，不静默降级为纯文字猜测。
"""
from pathlib import Path

from .source_layout import digest, save_json


def detect_regions(source: Path, output: Path) -> list[dict]:
    import json
    if output.exists():
        record = json.loads(output.read_text(encoding='utf-8'))
        if record.get('source_sha256') != digest(source):
            raise ValueError('版面检测缓存不属于当前源文件')
        return record['regions']
    import numpy as np
    import pymupdf as fitz
    from babeldoc.docvision.doclayout import OnnxModel

    try:
        model = OnnxModel.from_pretrained()
    except SystemExit as error:
        # 上游资源下载失败可能直接 exit(1)，必须转回工作流的等待式错误。
        raise RuntimeError('自动版面模型资源不可用，等待资源恢复') from error
    regions = []
    with fitz.open(source) as document:
        for number, page in enumerate(document, 1):
            pixmap = page.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
            pixels = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, 3)
            result = model.predict(pixels)[0]
            for box in result.boxes:
                # 源 PDF 可能有非零 CropBox；将像素坐标转换回 PyMuPDF 页面坐标。
                coordinates = [float(x) for x in box.xyxy]
                rect = fitz.Rect(coordinates) & page.rect
                if not rect.is_empty:
                    regions.append({'page': number, 'rect': list(rect),
                                    'kind': result.names[int(box.cls)], 'confidence': float(box.conf)})
    save_json(output, {'source_sha256': digest(source), 'model_sha256': digest(Path(model.model_path)),
                       'regions': regions})
    return regions
