"""局部编码异常的只读 OCR 辅助；不以识别结果替换科学原文或原始坐标。

优先使用已安装的 Tesseract；macOS 无 Tesseract 时使用系统 Vision。
不安装软件、不联网、不扫描整篇。OCR 候选与置信度仅用于定位，恢复原文
仍须有可核对的源字形证据；失败时保存裁剪图及原因供继续诊断。
"""
from __future__ import annotations

import csv
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pymupdf as fitz


def recognize_crop(image: Path) -> dict:
    """输入局部 PNG 路径，输出文本、引擎及置信度；不改变输入图片。"""
    executable = shutil.which('tesseract')
    if executable:
        result = subprocess.run([executable, str(image), 'stdout', '-l', 'eng', '--psm', '7', 'tsv'],
                                capture_output=True, text=True, timeout=60, check=True)
        words = [r for r in csv.DictReader(io.StringIO(result.stdout), delimiter='\t')
                 if r['text'].strip() and float(r['conf']) >= 0]
        return {'engine': 'tesseract', 'text': ' '.join(r['text'] for r in words),
                'confidence': min((float(r['conf']) / 100 for r in words), default=0)}
    swift = shutil.which('swift') if sys.platform == 'darwin' else None
    if swift:
        result = subprocess.run([swift, str(Path(__file__).with_name('local_ocr.swift')), str(image)],
                                capture_output=True, text=True, timeout=60, check=True)
        return {'engine': 'apple-vision', **json.loads(result.stdout)}
    return {'engine': None, 'text': '', 'confidence': 0, 'error': '未安装本地 OCR 引擎'}


def anomalous_lines(raw: list) -> list[dict]:
    """仅选尚未由绘制记录解决的控制码及 Unicode 替代字符所在行。"""
    return [line for b in raw for line in b.get('lines', [])
            if any(c['c'] == '\ufffd' or not c['c'].isprintable() and not c['c'].isspace()
                   for s in line['spans'] for c in s['chars'])]


def diagnose_lines(page, lines: list[dict], directory: Path) -> list[dict]:
    """把异常行裁为 300 dpi 图像，并保存 OCR 证据；不静默修订提取层。

    单行周围加 2 pt 留出抗锯齿边缘，边界裁到页面内。数学识别和空白判断
    并非 OCR 的可靠保证，所以报告永远不能充当内容合同通过凭据。
    """
    directory.mkdir(parents=True, exist_ok=True)
    records = []
    for index, line in enumerate(lines):
        r = fitz.Rect(line['bbox'])
        clip = fitz.Rect(r.x0-2, r.y0-2, r.x1+2, r.y1+2) & page.rect
        path = directory / f'page-{page.number+1:03d}-line-{index:03d}.png'
        page.get_pixmap(matrix=fitz.Matrix(300/72, 300/72), clip=clip,
                        colorspace=fitz.csRGB, alpha=False).save(path)
        record = {'page': page.number+1, 'rect': list(clip), 'image': str(path.resolve()),
                  'extracted': ''.join(c['c'] for s in line['spans'] for c in s['chars']),
                  'applied': False}
        try:
            record.update(recognize_crop(path))
        except (subprocess.SubprocessError, OSError, ValueError, KeyError) as error:
            # 引擎错误不遮蔽原始提取故障，也不把原论文文字写入命令错误日志。
            record.update(error=type(error).__name__, text='', confidence=0)
        records.append(record)
    from .recovery import _save
    _save(directory / f'page-{page.number+1:03d}.json', {'regions': records})
    return records
