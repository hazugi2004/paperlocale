"""将正文删除范围和排字范围分离，保留原始引用/公式字符。"""
import pymupdf as fitz
from PIL import Image


def page_pixels(page):
    """统一在整页 144 dpi 渲染后取样，避免逐块重渲染改变图片插值相位。"""
    pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    return Image.frombytes('RGB', (pixmap.width, pixmap.height), pixmap.samples)


def region_pixels(image, rect):
    box = (fitz.Rect(rect) * 2).irect
    return image.crop(tuple(box)).tobytes()


def fixed_text_rectangles(part):
    """按原字符基线划分固定文本核验区，避免把上标下方正文算成引用。

    网址与紧接的上标可属于同一个固定锚点，但它们的联合外框包含上标
    下方的空白。真实论文中后续正文句点的抗锯齿恰落在该空白内。逐基线
    使用全部原字符外框仍完整覆盖固定文字，同时不保护并不存在的文字。
    """
    lines = {}
    for char in part.get('chars', []):
        # 空白字符没有可见墨迹，其字体外框可能伸入下一行；不能让空格
        # 的空框将待翻译正文误算成公式。非空字形仍保留完整原字框。
        if 'text' in char and not char['text'].strip():
            continue
        baseline = round(char['origin'][1], 3)
        lines[baseline] = lines.get(baseline, fitz.Rect()) | fitz.Rect(char['rect'])
    return list(lines.values()) or [fitz.Rect(part['rect'])]


def subtract_rectangles(rect, obstacles):
    """从矩形扣除障碍物，返回互不相交的剩余矩形，不舍弃可见细缝。"""
    pieces = [fitz.Rect(rect)]
    for obstacle in obstacles:
        remaining = []
        for piece in pieces:
            cut = piece & fitz.Rect(obstacle)
            if cut.is_empty:
                remaining.append(piece)
                continue
            remaining.extend(r for r in (
                fitz.Rect(piece.x0, piece.y0, piece.x1, cut.y0),
                fitz.Rect(piece.x0, cut.y1, piece.x1, piece.y1),
                fitz.Rect(piece.x0, cut.y0, cut.x0, cut.y1),
                fitz.Rect(cut.x1, cut.y0, piece.x1, cut.y1)) if not r.is_empty)
        pieces = remaining
    return pieces


def writing_rectangles(rect, obstacles):
    """枚举障碍物边缘确定的可写矩形，允许候选之间重叠。

    删除用的不相交小片不能直接当排字区域：侧边上标会把右侧整行人为切成
    上下两片。此处按候选 x 区间合并可用 y 范围，找回完整行高；最终每个
    原文字行只选一个候选，不能利用相互重叠的候选重复填字。
    """
    outer = fitz.Rect(rect)
    cuts = [outer & fitz.Rect(o) for o in obstacles if outer.intersects(fitz.Rect(o))]
    xs = sorted({outer.x0, outer.x1, *(x for c in cuts for x in (c.x0, c.x1))})
    result = []
    for i, x0 in enumerate(xs):
        for x1 in xs[i + 1:]:
            spans = [(outer.y0, outer.y1)]
            for cut in cuts:
                if cut.x0 >= x1 or cut.x1 <= x0:
                    continue
                spans = [(a, b) for lo, hi in spans
                         for a, b in ((lo, min(hi, cut.y0)), (max(lo, cut.y1), hi)) if b > a]
            result.extend(fitz.Rect(x0, lo, x1, hi) for lo, hi in spans)
    return result


def safe_erase_rectangles(editable, protected, source=None):
    """每个删除框仅触及正文；每个待译非空字符必须至少被一个框触及。

    MuPDF 会删除与框相交的整字，所以无需覆盖整字外框。相邻引用的字框
    可以与正文重叠，只要正文还有不触碰引用的局部区域即可。无法证明安全
    时明确失败，不把未删除原文留在译文下面。这里不移动或重绘任何固定字符。
    """
    result, ligatures = [], {}
    if source is not None:
        with fitz.open(source) as document:
            for number, page in enumerate(document, 1):
                parts = []
                for span in page.get_texttrace():
                    leader = None
                    for item in span['chars']:
                        if item[1] >= 0:
                            leader = item
                        elif leader is not None:
                            # MuPDF 用 glyph_id=-1 明确表示合字的续字符。
                            # rawdict 的续字符位于主字形右边界，且没有独立宽度；
                            # 删除主字形即可删除整个合字，不能把零宽字当漏译跳过。
                            parts.append((chr(item[0]), leader[3][2], item[2][1], leader[3]))
                ligatures[number] = parts
    for part in editable:
        obstacles = [rect for p in protected if p['page'] == part['page']
                     for rect in fixed_text_rectangles(p)]
        pieces = subtract_rectangles(part['rect'], obstacles)
        # 内缩只为避开 PDF 浮点边界；完整触字证明使用实际内缩结果。
        patches = [fitz.Rect(r.x0 + .01, r.y0 + .01, r.x1 - .01, r.y1 - .01)
                   for r in pieces if r.width > .02 and r.height > .02]
        for char in part['chars']:
            footprint = fitz.Rect(char['rect'])
            if footprint.is_empty:
                matches = [box for text, x, y, box in ligatures.get(part['page'], [])
                           if text == char['text'] and abs(x - char['origin'][0]) < .001
                           and abs(y - char['origin'][1]) < .001]
                if len(matches) == 1:
                    footprint = fitz.Rect(matches[0])
            if char['text'].strip() and not any(footprint.intersects(r) for r in patches):
                raise ValueError(f'第{part["page"]}页正文字符没有安全删除范围：{char["text"]!r}')
        result.extend({'page': part['page'], 'rect': list(r)} for r in patches)
    return result


def verify_text_erased(page, editable, protected):
    """插入中文前核验：原正文已删除，固定字符仍位于原坐标。

    坐标容差仅覆盖 PDF 数值序列化误差；最终另有原保护区域严格像素核验。
    此检查也覆盖没有分配到中文的原文行，避免短译文掩盖残留英文。
    """
    chars = [c for block in page.get_text('rawdict')['blocks'] for line in block.get('lines', [])
             for span in line['spans'] for c in span['chars']]
    def present(char):
        return any(c['c'] == char['text'] and
                   max(abs(a - b) for a, b in zip(c['origin'], char['origin'])) < .001 for c in chars)
    if any(present(c) for part in editable for c in part['chars'] if c['text'].strip()):
        raise ValueError(f'第{page.number + 1}页仍有未删除的待译原文')
    if any(not present(c) for part in protected for c in part.get('chars', []) if c['text'].strip()):
        raise ValueError(f'第{page.number + 1}页固定字符缺失或位置改变')


def restore_changed_isolated_regions(page, source_page, editable, protected):
    """修复 MuPDF 删除时重编码无关文字产生的字距变化，使用原 PDF 原生片段。

    实测出版社页眉在删除远处正文后也会出现字距变化。只处理与所有正文框
    完全分离的保护区；重放源向量/文字，绝不重新排字或用截图替代。原区域
    的旧文字先删除，避免白底覆盖后仍能提取到重复文字。最终全页像素检查
    仍负责验证修复及其边界，引用/正文交叠区域不能用此方法绕过保护。
    """
    regions = []
    before, after = page_pixels(source_page), page_pixels(page)
    writing = [fitz.Rect(e.get('writing_rect', e['rect'])) for e in editable]
    for part in protected:
        rect = fitz.Rect(part['rect'])
        if any(rect.intersects(r) for r in writing):
            continue
        if region_pixels(before, rect) != region_pixels(after, rect):
            # 透明组的浮点裁剪边界会改变 MuPDF 离屏合成的像素起点，
            # 即使变换矩阵严格为单位矩阵也会出现插值色差。与整页核验
            # 使用同一 144 dpi 网格向外对齐，且不得碰到任何译文可写区。
            # 这改变重放裁剪范围，不改变源对象位置或核验容差。
            aligned = (fitz.Rect((rect * 2).irect) / 2) & page.rect
            if not any(aligned.intersects(r) for r in writing):
                rect = aligned
            if not any(r.contains(rect) for r in regions):
                regions = [r for r in regions if not rect.contains(r)] + [rect]
    if not regions:
        return False
    for rect in regions:
        page.add_redact_annot(rect, fill=False, cross_out=False)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                          graphics=fitz.PDF_REDACT_LINE_ART_NONE, text=fitz.PDF_REDACT_TEXT_REMOVE)
    for rect in regions:
        page.draw_rect(rect, color=None, fill=(1, 1, 1), width=0)
        form = page.show_pdf_page(rect, source_page.parent, source_page.number, clip=rect, keep_proportion=False)
        group_type, group = source_page.parent.xref_get_key(source_page.xref, 'Group')
        if group_type != 'null':
            # 真实透明插图在普通 Form 中重放会把底色参与混合两次，产生
            # 细小但真实的色值变化。沿用源页的色彩空间并隔离透明组，使
            # 原片段先独立合成，再覆盖白底。调用方始终保留源 xref 编号
            # （garbage=0），所以原 Group 内的色彩空间引用仍指向原对象。
            if group_type == 'xref':
                group = source_page.parent.xref_object(int(group.split()[0]))
            page.parent.xref_set_key(form, 'Group', group)
            page.parent.xref_set_key(form, 'Group/I', 'true')
    return True
