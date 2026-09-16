"""源版面工作流：全部译文通过且预排版闭合后，才原子产生完整候选。

旧版 BabelDOC 工作流继续处理原断点；此模式直接修改源 PDF 的明确正文字符，
保留其余对象，不依赖先重排后恢复。科学合同与 QA/accept 复用公共实现。
"""
from __future__ import annotations

import json
import math
from dataclasses import replace
from statistics import median
from pathlib import Path

import pymupdf as fitz

from .contracts import read_jsonl, write_jsonl_atomic, validate_translation
from .providers import Segment, TranslationContext
from .pipeline import translate_segment_file
from .safe_text import (safe_erase_rectangles, verify_text_erased, restore_changed_isolated_regions,
                        page_pixels, region_pixels, fixed_text_rectangles)
from .source_layout import (digest, extract_layout, fit_unit, load_plan, save_json,
                            verify_unchanged, anchor_errors)
from .workflow import (load_manifest, save_manifest, _verify_source_pdf,
                       _verify_rendered_pdf, _provider_resume_identity,
                       _subset_repair_font, qa_run, _missing_source_vector_drawings,
                       _replay_vector_drawing)


def run_preserved(root: Path, *, provider, domain, plan_path: Path | None,
                  font_file: Path | None, min_font_size: float | None = None,
                  contract_repair: bool = True, dpi: int = 144,
                  pdftoppm_bin=None, max_segments: int = 200,
                  max_characters: int = 30000) -> dict:
    """逻辑段落翻译→全量预排版→源页写入→保护区域验证→机器 QA。

    一个逻辑段落允许跨任意多个页面/图片间隙。模型只收到带固定锚点的全文，
    固定锚点原文只作为上下文，不允许翻译或改写。无法容纳时不产出部分中文候选，交给外层等待。
    """
    manifest = load_manifest(root)
    source = _verify_source_pdf(manifest)
    if manifest.get('pages'):
        raise ValueError('源版面模式要求完整文档；页码筛选请使用 legacy 模式')
    if manifest['status'] in {'qa_generated', 'accepted'}:
        _verify_rendered_pdf(manifest)
        return manifest
    if manifest['status'] == 'rendered':
        qa_run(root, dpi=dpi, pdftoppm_bin=pdftoppm_bin, restore_vectors=False)
        return load_manifest(root)
    if font_file is None:
        from babeldoc.assets.assets import get_font_and_metadata
        try:
            # 新安装也能自动取得同一校验字体，不要求用户事先运行旧版翻译。
            font_file, _ = get_font_and_metadata('SourceHanSerifCN-Regular.ttf')
        except SystemExit as error:
            # 上游下载器用 exit(1) 表示资源不可用，仅在这个已知边界转为
            # 可等待错误；用户取消/操作系统终止仍可正常结束进程。
            raise RuntimeError('自动获取中文字体失败，等待资源恢复') from error
    font_file = Path(font_file).expanduser().resolve()
    if not font_file.is_file():
        raise FileNotFoundError('缺少中文字体，请通过 --font-file 指定具有所需字形的字体')
    plan_path = (plan_path or root / 'layout_plan.json').expanduser().resolve()
    from .layout_detection import detect_regions
    detections = detect_regions(source, root / 'layout_detection.json')
    if not plan_path.exists():
        save_json(plan_path, extract_layout(source, detections))
    plan, units = load_plan(source, plan_path, detections)
    editable = [part for unit in units for slot in unit['slots'] for part in slot]
    protected = list(plan['protected_regions'])
    protected.extend({'page': b['page'], 'rect': b['rect']} for b in plan['blocks'] if b['kind'] != 'body')
    protected.extend(part for unit in units for part in unit['anchors'])
    # 删除范围与排字范围分开：先证明可逐字删除，再请求翻译，避免已知
    # 源几何缺陷导致重复模型调用。固定字框虽可重叠，实际删除框不能触及它。
    erase_regions = safe_erase_rectangles(editable, protected, source)
    plan_hash = digest(plan_path)
    if manifest.get('layout_plan_sha256') not in (None, plan_hash):
        raise ValueError('翻译开始后的版面计划已改变，请使用新的运行目录避免混用缓存')
    if provider is None:
        raise ValueError('源版面翻译需要提供原 Provider、模型和推理档位')
    identity = _provider_resume_identity(provider.provenance())
    if manifest.get('translation_provider') is not None and _provider_resume_identity(manifest['translation_provider']) != identity:
        raise ValueError('恢复不能更换 Provider、模型或推理档位')
    if manifest.get('domain_sha256') not in (None, domain.content_sha256):
        raise ValueError('恢复时领域包发生变化')
    manifest.update(layout_mode='preserved', layout_plan=str(plan_path),
                    layout_plan_sha256=plan_hash, translation_provider=provider.provenance(),
                    domain_sha256=domain.content_sha256, status='collected')
    save_manifest(root, manifest)
    # 相同全文可复用翻译，物理位置仍逐组保存，不能按源哈希丢掉重复出现位置。
    unique = {unit['id']: {'id': unit['id'], 'source': unit['source']} for unit in units}
    segments, translations = root / 'segments.jsonl', root / 'translations.jsonl'
    write_jsonl_atomic(segments, list(unique.values()))
    anchor_text = {u['id']: {'{v' + str(i) + '}': a['text'] for i, a in enumerate(u['anchors'])}
                   for u in units if u['anchors']}
    translate_segment_file(segments_path=segments, translations_path=translations,
                           provider=provider, domain=domain, contract_repair=contract_repair,
                           max_segments=max_segments, max_characters=max_characters, anchor_text=anchor_text)
    targets = {row['id']: row['target'] for row in read_jsonl(translations)}
    if set(targets) != set(unique):
        raise ValueError('正文译文仍有缺口，不能创建候选 PDF')
    # 普通翻译可能在固定公式/引用之间过长。仅对溢出的逻辑段落自动请求
    # 同一模型进行一次等义精炼，仍验证科学合同和真实字体宽度，不缩短科学事实。
    full_font = fitz.Font(fontfile=str(font_file))
    bold_file = None
    bold_font = None
    if any(p.get('bold') for unit in units for slot in unit['slots'] for p in slot):
        from babeldoc.assets.assets import get_font_and_metadata
        try:
            bold_file, _ = get_font_and_metadata('SourceHanSerifCN-Bold.ttf')
        except SystemExit as error:
            raise RuntimeError('自动获取中文粗体字体失败，等待资源恢复') from error
        bold_font = fitz.Font(fontfile=str(bold_file))
    failed = {}
    for unit in units:
        try:
            fit_unit(unit, targets[unit['id']], full_font, min_font_size, bold_font)
        except ValueError as error:
            failed[unit['id']] = (unit, str(error))
    if failed and not contract_repair:
        raise ValueError('完整译文在原区域内溢出，已禁用自动等义精炼')
    refinement_path = root / 'layout_refinements.json'
    refinements = json.loads(refinement_path.read_text(encoding='utf-8')) if refinement_path.exists() else {}
    for sid, (unit, reason) in failed.items():
        size = min_font_size or median(p['size'] for slot in unit['slots'] for p in slot)
        budgets = [math.floor(sum(fitz.Rect(p.get('writing_rect', p['rect'])).width for p in slot) / size)
                   for slot in unit['slots']]
        feedback = (reason, '保持全部科学事实、数字、术语和占位符顺序，使用更简练的中文同义表达。'
                    '按 {vN} 分隔的各段宽度预算（汉字约1，ASCII字符约0.5）依次为：' + str(budgets) +
                    '。零宽段不得插入非空白字符；不能删去信息、合并数值或改动引用归属。')
        context = TranslationContext(source_language=domain.source_language, target_language=domain.target_language,
                                     domain=domain, repair_feedback={sid: (targets[sid], feedback)}, anchor_text=anchor_text)
        previous = refinements.get(sid)
        if previous is not None:
            # 已取得的模型答复即使未放得下也保存。恢复时先用同一答复重测
            # 修正后的几何，不为仍未解决的版面缺陷反复消耗模型额度。
            repaired_target = previous.get('repair', {}).get('after')
            if targets[sid] not in (previous['before'], previous['after'], repaired_target):
                raise ValueError('版面精炼断点与当前译文不一致')
            refined = repaired_target if repaired_target is not None else previous['after']
        else:
            results = provider.translate([Segment(sid, unit['source'])], context)
            if len(results) != 1 or results[0].id != sid:
                raise ValueError('版面精炼返回了错误的段落 ID')
            refined = results[0].target
            refinements[sid] = {'before': targets[sid], 'after': refined, 'width_budgets': budgets,
                                'anchor_text': anchor_text.get(sid, {})}
            save_json(refinement_path, refinements)
        errors = validate_translation(unit['source'], refined, domain) + anchor_errors(unit, refined)
        if errors and 'repair' not in refinements[sid]:
            # 实测等义精炼会漏掉 ERA5，或重复锚点里原有的右括号。将明确
            # 门禁错误反馈给同一 Provider 一次。原答复和纠正答复分别落盘；
            # 第二次仍失败就等待，恢复不会不断请求或丢弃失败证据。
            repair_context = replace(context, repair_feedback={sid: (refined, tuple(errors) + feedback)})
            results = provider.translate([Segment(sid, unit['source'])], repair_context)
            if len(results) != 1 or results[0].id != sid:
                raise ValueError('版面精炼纠正返回了错误的段落 ID')
            refinements[sid]['repair'] = {'before': refined, 'after': results[0].target, 'errors': errors}
            refined = results[0].target
            save_json(refinement_path, refinements)
            errors = validate_translation(unit['source'], refined, domain) + anchor_errors(unit, refined)
        if errors:
            raise ValueError('自动版面精炼未通过科学内容校验：' + str(errors))
        for occurrence in units:
            if occurrence['id'] == sid:
                fit_unit(occurrence, refined, full_font, min_font_size, bold_font)
        targets[sid] = refined
        write_jsonl_atomic(translations, [{**row, 'target': targets[row['id']]} for row in read_jsonl(translations)])
    # 预排版使用最终嵌入字体的同一个子集，避免测量原字体、写入子集产生差异。
    font_bytes, font_evidence = _subset_repair_font(font_file, ''.join(targets.values()))
    font = fitz.Font(fontbuffer=font_bytes)
    bold_bytes = None
    if bold_file is not None:
        # 粗体也使用最终嵌入的字形子集测量，不能用细体宽度预检后再加粗。
        bold_bytes, bold_evidence = _subset_repair_font(Path(bold_file), ''.join(targets.values()))
        bold_font = fitz.Font(fontbuffer=bold_bytes)
        font_evidence = {'regular': font_evidence, 'bold': bold_evidence}
    placements = [placement for unit in units
                  for placement in fit_unit(unit, targets[unit['id']], font, min_font_size, bold_font)]
    # 写入和完整检查只针对临时文件。任何异常均不触碰已有候选，也不改变验收。
    temporary = root / '.preserved.tmp.pdf'
    try:
        with fitz.open(source) as document:
            for number, page in enumerate(document, 1):
                edits = [p for p in editable if p['page'] == number]
                if not edits:
                    continue
                for part in [r for r in erase_regions if r['page'] == number]:
                    page.add_redact_annot(fitz.Rect(part['rect']), fill=False, cross_out=False)
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                      graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                      text=fitz.PDF_REDACT_TEXT_REMOVE)
                verify_text_erased(page, edits, [p for p in protected if p['page'] == number])
                page.insert_font(fontname='PLPreserved', fontbuffer=font_bytes)
                if bold_bytes is not None:
                    page.insert_font(fontname='PLPreservedBold', fontbuffer=bold_bytes)
                for item in placements:
                    if item['page'] != number:
                        continue
                    rect, size = fitz.Rect(item['rect']), item['font_size']
                    page.insert_text((item.get('origin_x', rect.x0), item.get('baseline', rect.y0 + font.ascender * size)), item['target'],
                                     fontname='PLPreservedBold' if item.get('bold') else 'PLPreserved', fontsize=size)
            document.save(temporary, garbage=0, deflate=True)
        # 以序列化后重新打开的 PDF 为核验对象。MuPDF 编辑中间态与落盘后
        # 的图片插值可能不同，不能对中间态的差异过早重放整张图。
        with fitz.open(temporary) as document, fitz.open(source) as original:
            restored = False
            vector_restorations = []
            annotation_restorations = []
            for number, page in enumerate(document, 1):
                restored |= restore_changed_isolated_regions(page, original[number - 1],
                              [p for p in editable if p['page'] == number],
                              [p for p in protected if p['page'] == number])
                # 删除引擎还可能清理面积为零的填充路径。复用现有源矢量身份
                # 比较和原生重放函数，在候选发布前补回；不能靠画面相同忽略对象减少。
                missing = _missing_source_vector_drawings(original[number - 1], page)
                for drawing in missing:
                    _replay_vector_drawing(page, drawing)
                if missing:
                    vector_restorations.append({'page': number, 'count': len(missing)})
                    restored = True
                # MuPDF 会因删除框碰到链接外框而从 Annots 列表移除链接，
                # 即使引用字形本身完全保留。实测原注释对象仍未改变；本流程
                # 始终 garbage=0，不重编号或清理源对象，因此恢复源 Annots
                # 引用即可保留原坐标、目标与注释属性，不重新拼装链接字典。
                original_annots = original.xref_get_key(original[number - 1].xref, 'Annots')
                if document.xref_get_key(page.xref, 'Annots') != original_annots:
                    document.xref_set_key(page.xref, 'Annots', original_annots[1])
                    annotation_restorations.append(number)
                    restored = True
            repaired = document.tobytes(garbage=0, deflate=True) if restored else None
        if repaired is not None:
            temporary.write_bytes(repaired)
        evidence = verify_unchanged(source, temporary, editable)
        with fitz.open(source) as original, fitz.open(temporary) as written:
            # 保护区域不享有正文掩膜边缘的抗锯齿容差，包括上标、公式与表格文字。
            pixels = {}
            for region in protected:
                index = region['page'] - 1
                if index not in pixels:
                    pixels[index] = page_pixels(original[index]), page_pixels(written[index])
                a, b = pixels[index]
                equal = region_pixels(a, region['rect']) == region_pixels(b, region['rect'])
                if region.get('fixed'):
                    # 字符锚点使用精确浮点裁剪后再光栅化：向外取整的截图可能
                    # 混入框外正文的抗锯齿像素（实测末尾 g 的三个边缘像素），
                    # 它们不是引用本身。图像区域仍使用整页取样以固定插值相位。
                    equal = True
                    for clip in fixed_text_rectangles(region):
                        x = original[index].get_pixmap(clip=clip, matrix=fitz.Matrix(4, 4), alpha=False)
                        y = written[index].get_pixmap(clip=clip, matrix=fitz.Matrix(4, 4), alpha=False)
                        equal &= (x.width, x.height, x.samples) == (y.width, y.height, y.samples)
                if not equal:
                    raise ValueError(f'第{index + 1}页保护区域像素发生变化：{region["rect"]}, {region.get("text", region.get("kind", ""))!r}')
            page_chars = {}
            for item in placements:
                # get_textbox 的边界容差会把紧贴正文的原始右括号也抽入；
                # 按字形中心读取精确正文框，仍要求每个写入字符完整回读。
                rectangle = fitz.Rect(item['rect'])
                if item['page'] not in page_chars:
                    # 每页仅抽取一次字符；逐行回读若反复附带图片字节，会让
                    # 图文复杂的整篇论文产生大量无用解码及内存占用。
                    raw = written[item['page'] - 1].get_text('rawdict',
                          flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES)
                    page_chars[item['page']] = [c for block in raw['blocks'] for line in block.get('lines', [])
                                               for span in line['spans'] for c in span['chars']]
                extracted = ''.join(c['c'] for c in page_chars[item['page']] if abs(c['origin'][1] - item['baseline']) < .01 and fitz.Point((c['bbox'][0] + c['bbox'][2]) / 2,
                                                                       (c['bbox'][1] + c['bbox'][3]) / 2) in rectangle)
                if ''.join(extracted.split()) != ''.join(item['target'].split()):
                    raise ValueError(f'回读译文与排版内容不同：page={item["page"]}, expected={item["target"]!r}, actual={extracted!r}')
        evidence.update(source_sha256=digest(source), translated_sha256=digest(temporary),
                        layout_plan_sha256=plan_hash, font=font_evidence,
                        protected_region_count=len(protected), logical_group_count=len(units),
                        body_writing_regions=[{'page': p['page'], 'rect': p.get('writing_rect', p['rect'])}
                                              for p in editable],
                        anchor_dpi=288,
                        vector_restorations=vector_restorations,
                        annotation_restorations=annotation_restorations,
                        all_translations_present=True)
        candidate = root / 'render_output' / 'translated.pdf'
        candidate.parent.mkdir(exist_ok=True)
        temporary.replace(candidate)
        save_json(root / 'preservation_report.json', evidence)
        manifest.update(status='rendered', rendered_pdf=str(candidate.resolve()),
                        rendered_sha256=digest(candidate), preservation_report=str(root / 'preservation_report.json'))
        save_manifest(root, manifest)
    finally:
        temporary.unlink(missing_ok=True)
    qa_run(root, dpi=dpi, pdftoppm_bin=pdftoppm_bin, restore_vectors=False)
    return load_manifest(root)
