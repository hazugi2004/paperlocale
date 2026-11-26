"""源版面工作流：全部译文通过且预排版闭合后，才原子产生完整候选。

旧版 BabelDOC 工作流继续处理原断点；此模式直接修改源 PDF 的明确正文字符，
保留其余对象，不依赖先重排后恢复。科学合同与 QA/accept 复用公共实现。
"""
from __future__ import annotations

from .font_geometry import open_source_pdf

import json
import math
import re
from dataclasses import replace
from statistics import median
from pathlib import Path

import pymupdf as fitz


from .contracts import CJK_RE, read_jsonl, write_jsonl_atomic, validate_translation
from .providers import Segment, TranslationContext
from .pipeline import translate_segment_file, restored_content_errors
from .manual_revision import occurrence_id, revision_errors
from .safe_text import (safe_erase_rectangles, prepare_source_layer, restore_changed_isolated_regions,
                        page_pixels, region_pixels, fixed_text_rectangles, fixed_characters,
                        restore_default_color_spaces, verify_fixed_ink)
from .source_layout import (digest, extract_layout, fit_unit, load_plan, save_json,
                            verify_unchanged, anchor_errors)
from .workflow import (load_manifest, save_manifest, _verify_source_pdf,
                       _verify_rendered_pdf, _provider_resume_identity,
                       _verify_domain_languages, _domain_provenance,
                       _subset_repair_font, qa_run, _missing_source_vector_drawings,
                       _replay_vector_drawing)


def _render_model_target(text: str) -> str:
    """中文段落自然断行，清除模型在中文字符间插入的不可见断词符。"""
    # 字体不必为中文断词提示补字。只投影自动译文用于排字的副本，
    # 保留原模型响应、原 PDF 锚点、人工修订及其他 Unicode 格式字符。
    return re.sub(r'(?<=[\u3400-\u9fff，。；：！？（）《》])\u200b+(?=[\u3400-\u9fff，。；：！？（）《》])', '', text)


def _heading_without_source_echo(unit, block, target, domain, *, outlined=False):
    """仅为独立标题提出去掉整句重复原文的候选，经合同和真实排版复验后才可写入。"""
    source = unit['source']
    if block['kind'] != 'body' or block.get('list_start') or unit['anchors']:
        return None
    # 粗体或全大写都可能是正文。只接纳版面标记或与原 PDF 书签逐字匹配的标题。
    if not (block.get('heading') or outlined):
        return None
    echo = re.search(r'(?:\(([^()（）]+)\)|（([^()（）]+)）)\s*$', target)
    if echo is None or (echo[1] or echo[2]).split() != source.split():
        return None
    compact = target[:echo.start()].rstrip()
    if not CJK_RE.search(compact) or validate_translation(source, compact, domain, anchor_text={}):
        return None
    if restored_content_errors(source, compact, {}):
        return None
    return compact


def run_preserved(root: Path, *, provider, domain, plan_path: Path | None,
                  font_file: Path | None, min_font_size: float | None = None,
                  contract_repair: bool = True, dpi: int = 144,
                  pdftoppm_bin=None, max_segments: int = 200,
                  max_characters: int = 30000, paragraph: bool = False,
                  import_cache_from: Path | None = None, generate_qa: bool = True,
                  preflight_only: bool = False) -> dict:
    """逻辑段落翻译→全量预排版→源页写入→保护区域验证→机器 QA。

    一个逻辑段落允许跨任意多个页面/图片间隙。模型只收到带固定锚点的全文，
    固定锚点原文只作为上下文，不允许翻译或改写。无法容纳时不产出部分中文候选，交给外层等待。
    preflight_only=True 只验证源版面和真实删除，不请求模型、不产生译文候选。
    generate_qa=False 仅省略输出后的机器QA，返回真实的 rendered 状态；
    不跳过写入前完整性检查，也不伪造 qa_generated 或人工验收记录。
    """
    from contextlib import ExitStack
    with ExitStack() as stack:
        return _run_preserved(root, provider=provider, domain=domain, plan_path=plan_path,
                              font_file=font_file, min_font_size=min_font_size,
                              contract_repair=contract_repair, dpi=dpi, pdftoppm_bin=pdftoppm_bin,
                              max_segments=max_segments, max_characters=max_characters,
                              paragraph=paragraph, import_cache_from=import_cache_from,
                              generate_qa=generate_qa, preflight_only=preflight_only, stack=stack)


def _run_preserved(root, *, provider, domain, plan_path, font_file, min_font_size,
                   contract_repair, dpi, pdftoppm_bin, max_segments, max_characters,
                   paragraph, import_cache_from, generate_qa, preflight_only, stack):
    from .progress import emit_progress
    from .diagnostics import LocatedError, unit_location
    from .repair_choices import read_choices
    emit_progress("读取断点与资源", .02)
    manifest = load_manifest(root)
    source = _verify_source_pdf(manifest)
    if manifest.get('pages'):
        raise ValueError('源版面模式要求完整文档；页码筛选请使用 legacy 模式')
    if not preflight_only and manifest['status'] in {'qa_generated', 'accepted'}:
        _verify_rendered_pdf(manifest)
        return manifest
    if not preflight_only and manifest['status'] == 'rendered':
        _verify_rendered_pdf(manifest)
        if generate_qa:
            qa_run(root, dpi=dpi, pdftoppm_bin=pdftoppm_bin, restore_vectors=False)
        return load_manifest(root)
    default_font = font_file is None
    if default_font:
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
    # 新尝试开始即撤销旧预检结果；失败不能留下看似仍通过的报告。
    (root / 'preflight_report.json').unlink(missing_ok=True)
    # 新段落计划使用归一化的源页。先对照未经处理的原文件，防止
    # 提取、删除两道检查共用有缺陷的中间层而同时漏掉正文丢失。
    if paragraph:
        from .font_geometry import verify_canonical_source_pixels
        emit_progress("核对原 PDF 像素与字体", .04)
        verify_canonical_source_pixels(source)
    def page_progress(stage, begin, end):
        # 页码表示当前处理的页，比例按已处理的页数增长。后端仍使用同一份版面计划。
        return lambda page, total: emit_progress(f"{stage} · 第 {page}/{total} 页",
                                                  begin + (end - begin) * (page - 1) / total)
    emit_progress("加载页面版式识别", .06)
    detections = detect_regions(source, root / 'layout_detection.json',
                                page_progress=page_progress("识别页面版式", .06, .07))
    original_source = source
    caption_space = None
    # 旧计划按原坐标严格重放。有旋转证据的新计划必须从原 PDF 及原检测结果重建。
    if paragraph and (not plan_path.exists() or
                      json.loads(plan_path.read_text(encoding='utf-8')).get('rotated_captions')):
        from tempfile import TemporaryDirectory
        from .rotated_caption import prepare_caption_space
        scratch = Path(stack.enter_context(TemporaryDirectory(prefix='paperlocale-caption-')))
        caption_space = prepare_caption_space(source, detections, scratch)
        if caption_space is not None:
            stack.enter_context(caption_space.display_coordinates())
    if not plan_path.exists():
        emit_progress("识别期刊与正文结构", .07)
        save_json(plan_path, extract_layout(source, detections, paragraph=paragraph, ocr_dir=root / "local_ocr",
                                          caption_space=caption_space,
                                          page_progress=page_progress("提取正文与保护区", .07, .085)))
    choices = read_choices(root, manifest['source_sha256'])
    emit_progress("核对原文版面计划", .085)
    plan, units = load_plan(source, plan_path, detections, paragraph=paragraph,
                           caption_space=caption_space,
                           skip_block_ids=set(choices.get('skip_blocks', [])),
                           page_progress=page_progress("核对原文版面计划", .085, .10))
    if caption_space is not None:
        source = caption_space.path

    def original_item(item):
        return caption_space.original_item(item) if caption_space is not None else item

    def original_region(region):
        return caption_space.original_region(region) if caption_space is not None else region

    if plan.get('journal'):
        print('期刊：'+plan['journal']['name']+'；识别依据：'+plan['journal']['evidence'], flush=True)
    locations = []
    revisions = manifest.get('manual_revisions', {})
    remaining_revisions = set(revisions)
    skipped_ids = set(choices['skip'])
    for unit in units:
        location = unit_location(unit)
        revision_id = occurrence_id(original_item(location))
        if revision_id in revisions:
            revision = revisions[revision_id]
            if revision['segment_id'] != unit['id'] or revision_id not in remaining_revisions:
                raise ValueError('人工修订的译文区块在当前版面中不唯一。')
            remaining_revisions.remove(revision_id)
            # 明确保留原文时仍保留修订供回退追溯，但不校验或写入已跳过的译文。
            if unit['id'] not in skipped_ids:
                errors = revision_errors(unit['source'], revision['target'], domain, location['anchors'],
                                         revision.get('confirmed_denominator'))
                if errors:
                    raise LocatedError('人工修订须重新确认。', [{**location, 'target': revision['target'],
                                         'manual_revision': revision_id, 'validation_source': unit['source'],
                                         'errors': errors}], 'translation')
                unit['_manual_revision'] = revision_id
        default_floor = min_font_size if min_font_size is not None else location['font_size']
        unit['_repair_font_floor'] = choices['font_sizes'].get(unit['id'], default_floor)
        locations.append(original_item({**location, 'font_size': unit['_repair_font_floor']}))
    if remaining_revisions:
        raise ValueError('人工修订的区块已无法在当前版面定位，请恢复修订前的断点。')
    save_json(root / 'source_locations.json', locations)
    skipped_units = [unit for unit in units if unit['id'] in skipped_ids]
    all_ids = {u['id'] for u in units}
    if set(choices['skip']) - all_ids or set(choices['font_sizes']) - all_ids:
        raise ValueError('修复选择包含当前版面没有的片段')
    units = [unit for unit in units if unit['id'] not in skipped_ids]
    def fit_selected(unit, target, font, minimum, bold):
        try:
            fitted = fit_unit(unit, target, font, choices['font_sizes'].get(unit['id'], minimum), bold)
            if '_manual_revision' in unit:
                for placement in fitted:
                    placement['manual_revision'] = unit['_manual_revision']
                    placement['paragraph_id'] = unit['id']
            return fitted
        except ValueError as error:
            raise LocatedError(str(error), [{**unit_location(unit), 'validation_source': unit['source'],
                              'target': target, 'manual_revision': unit.get('_manual_revision'),
                              'errors': [str(error)]}], 'layout') from error
    if import_cache_from is not None:
        if not paragraph:
            raise ValueError('缓存导入只支持段落模式')
        from .cache_handoff import import_cache
        import_cache(import_cache_from, root, plan, units, domain)
    editable = [part for unit in units for slot in unit['slots'] for part in slot]
    protected = list(plan['protected_regions'])
    # 显式跳过保留原字符及原坐标，不用中文字体重新排英文，也不留下空洞。
    protected.extend(p for u in skipped_units for slot in u['slots'] for p in slot)
    protected.extend(p for u in skipped_units for p in u['anchors'])
    translated_kinds = {'body', 'caption'} if paragraph else {'body'}
    for block in plan['blocks']:
        if block['kind'] in translated_kinds:
            continue
        if block.get('preserve_reason') == '用户选择跳过，保留原文':
            # 按既有文本 run 分别保护：URL 内未知字符不得使同块普通正文
            # 丧失原字形核验。未知 run 仍按完整字框逐像素比较，不丢弃字符。
            protected.extend({**part, 'id': block['id'], 'kind': 'preserve',
                              'preserve_reason': block['preserve_reason']} for part in block['parts'])
        else:
            protected.append(block)
    if paragraph:
        from .safe_text import separate_caption_edges
        protected = separate_caption_edges(source, protected, plan['blocks'],
                                           journal_family=(plan.get('journal') or {}).get('family'))
        from .paragraph_layout import bind_inline_glyphs
        bind_inline_glyphs(source, units)
        # 逻辑网址可跨多行，只删除各原字符片段所在区域，不清空跨行外接框。
        editable.extend(fragment for unit in units for part in unit['anchors']
                        for fragment in part.get('url_fragments', [part]))
        # 段落框可包含原文绕排的独立公式，连续框合并后仍须避让其真实
        # 墨迹。只增加运行时障碍，不改变源计划、分段ID或已有译文缓存。
        from .font_geometry import source_anchor_ink_rectangles
        obstacles, font_cache = {}, {}
        with open_source_pdf(source) as original:
            for region in protected:
                chars = fixed_characters(region)
                boxes = source_anchor_ink_rectangles(original[region['page']-1], chars, font_cache) if chars else None
                boxes = boxes or fixed_text_rectangles(region)
                obstacles.setdefault(region['page'], []).extend(
                    list(fitz.Rect(b.x0-.5, b.y0-.5, b.x1+.5, b.y1+.5)) for b in map(fitz.Rect, boxes))
        for unit in units:
            for frame in unit['frames']:
                frame['obstacles'] = [b for b in obstacles.get(frame['page'], [])
                                      if fitz.Rect(b).intersects(fitz.Rect(frame['rect']))]
    else:
        protected.extend(part for unit in units for part in unit['anchors'])
    # 删除范围与排字范围分开：先证明可逐字删除，再请求翻译，避免已知
    # 源几何缺陷导致重复模型调用。固定字框虽可重叠，实际删除框不能触及它。
    erase_regions = safe_erase_rectangles(editable, protected, source)
    plan_hash = digest(plan_path)
    if manifest.get('layout_plan_sha256') not in (None, plan_hash):
        raise ValueError('翻译开始后的版面计划已改变，请使用新的运行目录避免混用缓存')
    if not preflight_only:
        _verify_domain_languages(manifest, domain)
        if provider is None:
            raise ValueError('源版面翻译需要提供原 Provider、模型和推理档位')
        identity = _provider_resume_identity(provider.provenance())
        if manifest.get('translation_provider') is not None and _provider_resume_identity(manifest['translation_provider']) != identity:
            raise ValueError('恢复不能更换 Provider、模型或推理档位')
        if manifest.get('domain_sha256') not in (None, domain.content_sha256):
            raise ValueError('恢复时领域包发生变化')
    emit_progress("预检正文删除与公式保留", .10)
    reference_bytes = prepare_source_layer(source, editable, protected, erase_regions)
    moving_math = [anchor for unit in units for anchor in unit['anchors'] if anchor.get('strokes')]
    stroke_removal = []
    if moving_math:
        from .inline_vectors import remove_strokes
        reference_bytes, stroke_removal = remove_strokes(reference_bytes, moving_math)
    preflight = {'status': 'preflight_passed', 'source_sha256': manifest['source_sha256'],
                 'layout_plan_sha256': plan_hash, 'logical_group_count': len(units),
                 'checks': ['source_identity', 'layout_identity', 'inline_font_binding',
                            'safe_erase_geometry', 'actual_erasure_and_fixed_characters'],
                 'translation_checked': False, 'final_pixels_checked': False}
    if paragraph:
        preflight['checks'].append('canonical_source_pixels')
    if caption_space is not None:
        preflight['checks'].append('rotated_caption_original_pixels')
        preflight['rotated_captions'] = caption_space.records()
    if stroke_removal:
        preflight['checks'].append('declared_inline_stroke_removal')
        preflight['inline_strokes'] = stroke_removal
    save_json(root / 'preflight_report.json', preflight)
    if preflight_only:
        return preflight
    manifest.update(layout_mode='paragraph' if paragraph else 'preserved', layout_plan=str(plan_path),
                    layout_plan_sha256=plan_hash, translation_provider=provider.provenance(),
                    domain_pack=_domain_provenance(domain),
                    domain_sha256=domain.content_sha256, status='collected')
    save_manifest(root, manifest)
    # 相同全文可复用翻译，物理位置仍逐组保存，不能按源哈希丢掉重复出现位置。
    unique = {unit['id']: {'id': unit['id'], 'source': unit['source']} for unit in units}
    segments, translations = root / 'segments.jsonl', root / 'translations.jsonl'
    write_jsonl_atomic(segments, list(unique.values()))
    if translations.exists() and skipped_ids:
        write_jsonl_atomic(translations, [r for r in read_jsonl(translations) if r['id'] not in skipped_ids])
    anchor_text = {u['id']: {'{v' + str(i) + '}': a['text'] for i, a in enumerate(u['anchors'])}
                   for u in units if u['anchors']}
    # 修复台使用与实际流水线相同的领域合同和锚点，不从错误文案猜校验配置。
    save_json(root / 'repair_context.json', {'source_sha256': manifest['source_sha256'],
              'layout_plan_sha256': plan_hash, 'domain': domain.pack_id,
              'domain_sha256': domain.content_sha256, 'anchors': anchor_text})
    model_ids = {unit['id'] for unit in units if '_manual_revision' not in unit}
    translate_segment_file(segments_path=segments, translations_path=translations,
                           provider=provider, domain=domain, contract_repair=contract_repair,
                           max_segments=max_segments, max_characters=max_characters, anchor_text=anchor_text,
                           manually_covered=frozenset(set(unique) - model_ids),
                           progress=lambda done, total: emit_progress("翻译片段", .15+.55*done/max(1,total), done, total))
    targets = {row['id']: row['target'] for row in read_jsonl(translations)} if translations.exists() else {}
    if model_ids - targets.keys() or targets.keys() - unique.keys():
        raise ValueError('正文译文仍有缺口，不能创建候选 PDF')

    def target_for(unit):
        if revision_id := unit.get('_manual_revision'):
            return revisions[revision_id]['target']
        if '_heading_without_echo' in unit:
            return unit['_heading_without_echo']
        return _render_model_target(targets[unit['id']])
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
    regular_data = bold_data = None

    def extend_default_fonts(text):
        nonlocal full_font, bold_font, regular_data, bold_data
        if not default_font:
            return
        from .unicode_fonts import supplement_default_font
        if data := supplement_default_font(full_font, text):
            regular_data = data
            full_font = fitz.Font(fontbuffer=data)
        if bold_font is not None and (data := supplement_default_font(bold_font, text, bold=True)):
            bold_data = data
            bold_font = fitz.Font(fontbuffer=data)

    # 预排版与最终子集使用同一份补充字体程序，精炼新增的字形也必须先核对。
    extend_default_fonts(''.join(target_for(unit) for unit in units))
    emit_progress("验证译文与预排版", .72)
    blocks = {block['id']: block for block in plan['blocks']}
    headings = set()
    if contract_repair and paragraph:
        with fitz.open(original_source) as document:
            headings = {(page, tuple(title.split())) for _, title, page in document.get_toc()}

    def prefit(unit, target):
        unit.pop('_heading_without_echo', None)
        try:
            fit_selected(unit, target, full_font, min_font_size, bold_font)
        except ValueError as error:
            if not contract_repair or not paragraph or '_manual_revision' in unit or len(unit['blocks']) != 1:
                raise
            block = blocks[unit['blocks'][0]]
            outlined = (block['page'], tuple(unit['source'].split())) in headings
            compact = _heading_without_source_echo(unit, block, target, domain, outlined=outlined)
            if compact is None:
                raise
            try:
                fit_selected(unit, compact, full_font, min_font_size, bold_font)
            except ValueError:
                raise error from None
            # 片段 ID 可能同时出现于标题和正文。只修改当前排字实例，模型答复保留供回放。
            unit['_heading_without_echo'] = compact

    failed = {}
    for unit in units:
        try:
            prefit(unit, target_for(unit))
        except ValueError as error:
            # 用户确认的文字只能由用户再修订，自动等义精炼不处理它。
            if '_manual_revision' in unit:
                raise
            failed[unit['id']] = (unit, str(error))
    if failed and not contract_repair:
        # fit_unit 同时检查锚点合同和排版，不能把所有校验错误都称为溢出。
        save_json(root / 'layout_failures.json', {sid: reason for sid, (_, reason) in failed.items()})
        raise LocatedError('译文预排版或锚点校验失败，已禁用自动修复：' +
                         '; '.join(f'{sid[:12]}: {reason}' for sid, (_, reason) in failed.items()),
                           [{**unit_location(u), 'validation_source': u['source'], 'target': targets[u['id']],
                             'errors': [reason]} for u, reason in failed.values()], 'layout')
    refinement_path = root / 'layout_refinements.json'
    refinements = json.loads(refinement_path.read_text(encoding='utf-8')) if refinement_path.exists() else {}
    for index, (sid, (unit, reason)) in enumerate(failed.items(), 1):
        emit_progress("精炼失败段落并验证排版", .72+.08*(index-1)/len(failed), index-1, len(failed))
        size = min_font_size or median(p['size'] for slot in unit['slots'] for p in slot)
        budgets = [math.floor(sum(fitz.Rect(p.get('writing_rect', p['rect'])).width for p in slot) / size)
                   for slot in unit['slots']]
        feedback = (reason, '保持全部科学事实、数字、术语和占位符顺序，使用更简练的中文同义表达。'
                    '按 {vN} 分隔的各段宽度预算（汉字约1，ASCII字符约0.5）依次为：' + str(budgets) +
                    '。零宽段不得插入非空白字符；不能删去信息、合并数值或改动引用归属。')
        if anchor_errors(unit, targets[sid]):
            # 括号合同失败并不意味着空间不足；只要求修正锚点归属，不以
            # 错误的宽度诊断诱导模型删减文字。修正后仍须实际预排版。
            feedback = (reason, '完整翻译原文并保留全部科学事实、数字、术语和占位符顺序。'
                        '固定锚点已经包含的括号不得重复补写，保持代回锚点后的括号结构。')
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
            try:
                results = provider.translate([Segment(sid, unit['source'])], context)
            except Exception as error:
                raise LocatedError(str(error), [unit_location(unit)], 'provider') from error
            if len(results) != 1 or results[0].id != sid:
                raise ValueError('版面精炼返回了错误的段落 ID')
            refined = results[0].target
            refinements[sid] = {'before': targets[sid], 'after': refined, 'width_budgets': budgets,
                                'anchor_text': anchor_text.get(sid, {}), 'provider': provider.provenance()}
            save_json(refinement_path, refinements)
        anchors = anchor_text.get(sid, {})
        errors = validate_translation(unit['source'], refined, domain, anchor_text=anchors)
        errors += restored_content_errors(unit['source'], refined, anchors)
        if errors and 'repair' not in refinements[sid]:
            # 实测等义精炼会漏掉 ERA5，或重复锚点里原有的右括号。将明确
            # 门禁错误反馈给同一 Provider 一次。原答复和纠正答复分别落盘；
            # 第二次仍失败就等待，恢复不会不断请求或丢弃失败证据。
            repair_context = replace(context, repair_feedback={sid: (refined, tuple(errors) + feedback)})
            try:
                results = provider.translate([Segment(sid, unit['source'])], repair_context)
            except Exception as error:
                raise LocatedError(str(error), [unit_location(unit)], 'provider') from error
            if len(results) != 1 or results[0].id != sid:
                raise ValueError('版面精炼纠正返回了错误的段落 ID')
            refinements[sid]['repair'] = {'before': refined, 'after': results[0].target, 'errors': errors}
            refined = results[0].target
            save_json(refinement_path, refinements)
            errors = validate_translation(unit['source'], refined, domain, anchor_text=anchors)
            errors += restored_content_errors(unit['source'], refined, anchors)
        if errors:
            raise LocatedError('自动版面精炼未通过科学内容校验：' + str(errors), [{**unit_location(unit), 'validation_source': unit['source'],
                               'target': refined, 'errors': errors}], 'translation')
        render_refined = _render_model_target(refined)
        extend_default_fonts(render_refined)
        for occurrence in units:
            if occurrence['id'] == sid and '_manual_revision' not in occurrence:
                prefit(occurrence, render_refined)
        targets[sid] = refined
        write_jsonl_atomic(translations, [{**row, 'target': targets[row['id']],
            **({'provider': provider.provenance(), 'previous_translation': row} if row['id'] == sid else {})}
            for row in read_jsonl(translations)])
    # 覆盖报告说明自动翻译范围；机器合同不能证明语义忠实或视觉可接受。
    save_json(root / 'translation_coverage.json', {
        'user_skipped_segments': [original_item(unit_location(u)) for u in skipped_units],
        'user_skipped_blocks': [b['id'] for b in plan['blocks'] if b['id'] in choices.get('skip_blocks', [])],
        'translation_complete': not skipped_units and not choices.get('skip_blocks'),
        'translated_blocks': sorted({p['source_block'] for u in units for slot in u['slots'] for p in slot if 'source_block' in p}),
        'preserved_blocks': [{'id': b['id'], 'page': b['page'], 'kind': b['kind'],
                              'text': b['text'], 'reason': b.get('preserve_reason') or b['kind'],
                              'regions': [original_region({'page': b['page'], 'rect': b['rect']})]}
                             for b in plan['blocks'] if b['kind'] not in translated_kinds],
        'manual_revision_count': sum('_manual_revision' in unit for unit in units),
        'deduplicated_heading_count': sum('_heading_without_echo' in unit for unit in units),
        'validated_segments': len(unique), 'semantic_review': 'pending', 'visual_review': 'pending'})
    # 预排版使用最终嵌入字体的同一个子集，避免测量原字体、写入子集产生差异。
    font_text = ''.join(target_for(unit) for unit in units) or ' '
    font_bytes, font_evidence = _subset_repair_font(font_file, font_text, font_data=regular_data)
    font = fitz.Font(fontbuffer=font_bytes)
    from .unicode_fonts import insert_text_exact, repair_default_tounicode, unicode_glyph_aliases
    font_aliases = unicode_glyph_aliases(font_bytes)
    bold_aliases = {}
    bold_bytes = None
    if bold_file is not None:
        # 粗体也使用最终嵌入的字形子集测量，不能用细体宽度预检后再加粗。
        bold_bytes, bold_evidence = _subset_repair_font(Path(bold_file), font_text, font_data=bold_data)
        bold_font = fitz.Font(fontbuffer=bold_bytes)
        bold_aliases = unicode_glyph_aliases(bold_bytes)
        font_evidence = {'regular': font_evidence, 'bold': bold_evidence}
    placements = [placement for unit in units
                  for placement in fit_selected(unit, target_for(unit), font, min_font_size, bold_font)]
    # 写入和完整检查只针对临时文件。任何异常均不触碰已有候选，也不改变验收。
    emit_progress('写入完整候选并核对保护区', .82)
    temporary = root / '.preserved.tmp.pdf'
    try:
        with fitz.open(stream=reference_bytes, filetype='pdf') as document:
            inline_fonts = {}
            unicode_fonts = {}
            checked_unicode_maps = set()
            for number, page in enumerate(document, 1):
                if not any(p['page'] == number for p in editable):
                    continue
                font_xref = page.insert_font(fontname='PLPreserved', fontbuffer=font_bytes)
                default_fonts = [(font_xref, font_aliases)]
                if bold_bytes is not None:
                    bold_xref = page.insert_font(fontname='PLPreservedBold', fontbuffer=bold_bytes)
                    default_fonts.append((bold_xref, bold_aliases))
                for reference, aliases in default_fonts:
                    if reference not in checked_unicode_maps:
                        repair_default_tounicode(document, reference, aliases)
                        checked_unicode_maps.add(reference)
                for item in placements:
                    if item['page'] != number:
                        continue
                    if 'inline_anchor' in item:
                        from .paragraph_layout import write_inline
                        write_inline(page, item, inline_fonts)
                        continue
                    rect, size = fitz.Rect(item['rect']), item['font_size']
                    bold = item.get('bold')
                    insert_text_exact(page,
                        (item.get('origin_x', rect.x0), item.get('baseline', rect.y0 + font.ascender * size)),
                        item['target'], fontname='PLPreservedBold' if bold else 'PLPreserved',
                        font_xref=bold_xref if bold else font_xref,
                        font=bold_font if bold else font, fontsize=size,
                        aliases=bold_aliases if bold else font_aliases, font_refs=unicode_fonts)
            document.save(temporary, garbage=0, deflate=True)
        # 以序列化后重新打开的 PDF 为核验对象。MuPDF 编辑中间态与落盘后
        # 的图片插值可能不同，不能对中间态的差异过早重放整张图。
        with fitz.open(temporary) as document, open_source_pdf(source) as original:
            restored = False
            vector_restorations = []
            annotation_restorations = []
            color_space_restorations = []
            for number, page in enumerate(document, 1):
                if restore_default_color_spaces(page, original[number - 1]):
                    page = document.reload_page(page)
                    color_space_restorations.append(number)
                    restored = True
                restored |= restore_changed_isolated_regions(page, original[number - 1],
                              [p for p in editable if p['page'] == number],
                              [p for p in protected if p['page'] == number])
                # 删除引擎还可能清理面积为零的填充路径。复用现有源矢量身份
                # 比较和原生重放函数，在候选发布前补回；不能靠画面相同忽略对象减少。
                missing = _missing_source_vector_drawings(original[number - 1], page)
                if moving_math:
                    from .inline_vectors import exclude_moved_drawings, verify_strokes
                    # 从旧位置删去的笔画只在已证实重放的候选中排除恢复，避免重复绘制。
                    for item in placements:
                        strokes = item.get('inline_anchor', {}).get('strokes')
                        if strokes and item['page'] == number:
                            verify_strokes(page, strokes, item['shift'])
                    missing = exclude_moved_drawings(missing, [a for a in moving_math if a['page'] == number])
                for drawing in missing:
                    _replay_vector_drawing(page, drawing)
                if missing:
                    vector_restorations.append({'page': number, 'count': len(missing)})
                    restored = True
                # MuPDF 会因删除框碰到链接外框而从 Annots 列表移除链接，
                # 即使引用字形本身完全保留。实测原注释对象仍未改变；本流程
                # 恢复和搬移链接前使用 garbage=0，不清理源对象，因此恢复源 Annots
                # 引用即可保留原坐标、目标与注释属性，不重新拼装链接字典。
                original_annots = original.xref_get_key(original[number - 1].xref, 'Annots')
                if document.xref_get_key(page.xref, 'Annots') != original_annots:
                    document.xref_set_key(page.xref, 'Annots', original_annots[1])
                    annotation_restorations.append(number)
                    restored = True
            # 源对象恢复完毕后可清理无引用对象。段落模式要等链接搬移完成。
            repaired = (document.tobytes(garbage=0 if paragraph else 1, deflate=True)
                        if restored or not paragraph else None)
        if repaired is not None:
            temporary.write_bytes(repaired)
        expected_links = None
        if paragraph:
            from .paragraph_layout import relocate_links
            with open_source_pdf(source) as original, fitz.open(temporary) as written:
                expected_links = relocate_links(written, original, placements)
                linked = written.tobytes(garbage=1, deflate=True)
            temporary.write_bytes(linked)
        # 完成当前坐标系的 MuPDF 写入后恢复精度；竖排图注还原后再按原页核验。
        from .vector_precision import restore_vector_precision
        precision_evidence = restore_vector_precision(source, temporary,
            protected_regions=plan['protected_regions'])
        writing_regions = editable + ([frame for u in units for frame in u['frames']] if paragraph else [])
        evidence = (verify_unchanged(source, temporary, writing_regions, expected_links=expected_links)
                    if caption_space is None else {})
        from .source_watermarks import verify_backgrounds
        with open_source_pdf(source) as original, fitz.open(temporary) as written:
            evidence['watermarks'] = verify_backgrounds(original, written)
        if paragraph:
            from .paragraph_layout import verify_inline
            evidence['inline_checks'] = verify_inline(temporary, placements)
        checked_ink, ink_evidence = verify_fixed_ink(source, temporary, reference_bytes, protected, editable)
        with open_source_pdf(source) as original, fitz.open(temporary) as written:
            # 保护区域不享有正文掩膜边缘的抗锯齿容差，包括上标、公式与表格文字。
            pixels = {}
            for region_number, region in enumerate(protected):
                if region_number in checked_ink:
                    continue
                index = region['page'] - 1
                if index not in pixels:
                    pixels[index] = page_pixels(original[index]), page_pixels(written[index])
                a, b = pixels[index]
                equal = region_pixels(a, region['rect']) == region_pixels(b, region['rect'])
                if region.get('fixed') or fixed_characters(region):
                    # 字符锚点及用户明确保留的文本块都按原字符并集核验；
                    # 保留块的整段外框可能罩住同一行的独立小标题。
                    # 使用精确浮点裁剪后再光栅化：向外取整的截图可能
                    # 混入框外正文的抗锯齿像素（实测末尾 g 的三个边缘像素），
                    # 它们不是引用本身。图像区域仍使用整页取样以固定插值相位。
                    equal = True
                    for clip in fixed_text_rectangles(region):
                        x = original[index].get_pixmap(clip=clip, matrix=fitz.Matrix(4, 4), alpha=False)
                        y = written[index].get_pixmap(clip=clip, matrix=fitz.Matrix(4, 4), alpha=False)
                        equal &= (x.width, x.height, x.samples) == (y.width, y.height, y.samples)
                if not equal:
                    # 原文与坐标放入结构化证据，供 CLI/App 单独展示，避免整段
                    # repr（含软连字符转义）挤进错误标题。此类检查不提供跳过门禁。
                    raise LocatedError(f'第{index + 1}页保护区域像素发生变化', [{
                        'id': region.get('id'), 'source': region.get('text', ''),
                        'pages': [index + 1], 'kind': region.get('kind', ''),
                        'regions': [{'page': index + 1, 'rect': region['rect']}],
                    }], 'preservation')
            page_chars = {}
            for item in placements:
                if 'inline_anchor' in item:
                    continue
                # get_textbox 的边界容差会把紧贴正文的原始右括号也抽入；
                # 按字形中心读取精确正文框，仍要求每个写入字符完整回读。
                rectangle = fitz.Rect(item['rect'])
                if item['page'] not in page_chars:
                    # 每页仅抽取一次字符；逐行回读若反复附带图片字节，会让
                    # 图文复杂的整篇论文产生大量无用解码及内存占用。
                    raw = written[item['page'] - 1].get_text('rawdict',
                          flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES)
                    page_chars[item['page']] = [{**c, 'font': span['font']} for block in raw['blocks'] for line in block.get('lines', [])
                                               for span in line['spans'] for c in span['chars']]
                if paragraph:
                    # 句号等墨迹很小，其字体字框中心可能落在墨迹框外。
                    # 按写入基线和字形原点范围回读完整行，不能漏掉标点；
                    # 右端严格开区间，避免吸入紧接的行内锚点。
                    face = bold_font if item.get('bold') else font
                    end = item['origin_x'] + face.text_length(item['target'], fontsize=item['font_size'])
                    # 原始行内字形以墨迹边界定位，其字形原点可能略伸入
                    # 中文的进宽区间；用实际写入字体区分原字形与译文。
                    face_name = ''.join(c for c in face.name if c.isalnum()).lower()
                    extracted = ''.join(c['c'] for c in page_chars[item['page']]
                        if abs(c['origin'][1] - item['baseline']) < .01
                        and ''.join(v for v in c['font'] if v.isalnum()).lower() == face_name
                        and item['origin_x'] - .01 <= c['origin'][0] < end - .01)
                else:
                    extracted = ''.join(c['c'] for c in page_chars[item['page']] if abs(c['origin'][1] - item['baseline']) < .01 and fitz.Point((c['bbox'][0] + c['bbox'][2]) / 2,
                                                                           (c['bbox'][1] + c['bbox'][3]) / 2) in rectangle)
                if ''.join(extracted.split()) != ''.join(item['target'].split()):
                    unit = next((u for u in units if u['id'] == item.get('paragraph_id') and
                                 u.get('_manual_revision') == item.get('manual_revision')), None)
                    raise LocatedError(f'第{item["page"]}页回读译文与排版内容不同', [{
                        'id': item.get('paragraph_id'), 'source': unit['source'] if unit else '',
                        'target': target_for(unit) if unit else item['target'],
                        'pages': [item['page']], 'regions': [{'page':item['page'], 'rect':item['rect']}],
                        'errors': [f'preservation.readback 应写入 {item["target"]!r}；实际回读 {extracted!r}；请核对重叠、缺字或错位。'],
                    }], 'preservation')
        if caption_space is not None:
            from .rotated_caption import restore_caption_pages, verify_caption_pages
            expected = restore_caption_pages(temporary, caption_space)
            precision_evidence = restore_vector_precision(original_source, temporary,
                protected_regions=[original_region(p) for p in plan['protected_regions']])
            edited_pages = {p['page'] for p in placements}
            evidence['rotated_captions'] = verify_caption_pages(
                original_source, temporary, caption_space, expected, edited_pages)
            # 只有独立图注带替代排字掩膜。普通页保留原字符，校验字形越过字符框的实际墨迹。
            writing_regions = [p for p in writing_regions if p['page'] not in caption_space.pages]
            writing_regions.extend({'page': number, 'rect': list(page.original_rect)}
                                   for number, page in caption_space.pages.items() if number in edited_pages)
            try:
                evidence.update(verify_unchanged(original_source, temporary, writing_regions,
                                                 expected_links=expected_links))
            except LocatedError as error:
                error.original_page_coordinates = True
                raise
        evidence['vector_precision'] = precision_evidence
        evidence.update(source_sha256=digest(original_source), translated_sha256=digest(temporary),
                        layout_plan_sha256=plan_hash, font=font_evidence,
                        protected_region_count=len(protected), logical_group_count=len(units),
                        body_writing_regions=[{'page': p['page'], 'rect': p.get('writing_rect', p['rect'])}
                                              for p in writing_regions],
                        anchor_dpi=288,
                        vector_restorations=vector_restorations,
                        annotation_restorations=annotation_restorations,
                        color_space_restorations=color_space_restorations,
                        fixed_ink_checks=ink_evidence,
                        all_translations_present=not skipped_units and not choices.get('skip_blocks'),
                        user_skipped_segments=sorted(skipped_ids), user_skipped_blocks=choices.get('skip_blocks', []))
        candidate = root / 'render_output' / 'translated.pdf'
        candidate.parent.mkdir(exist_ok=True)
        temporary.replace(candidate)
        save_json(root / 'preservation_report.json', evidence)
        manifest.update(status='rendered', rendered_pdf=str(candidate.resolve()),
                        rendered_sha256=digest(candidate), preservation_report=str(root / 'preservation_report.json'))
        save_manifest(root, manifest)
    finally:
        temporary.unlink(missing_ok=True)
    if generate_qa:
        emit_progress("机器 QA", .94)
        qa_run(root, dpi=dpi, pdftoppm_bin=pdftoppm_bin, restore_vectors=False)
    return load_manifest(root)
