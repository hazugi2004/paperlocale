"""新入口的真实边界回归：边栏不是正文、旧计划兼容、失败后原子回退及原文保留。"""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
import pymupdf as fitz
from paperlocale.source_layout import extract_layout, save_json, load_plan, digest
from paperlocale.journals import identify_journal
from paperlocale.diagnostics import LocatedError, record_error, unit_location
from paperlocale.repair_choices import apply_choice, interactive_run
from paperlocale.workflow import initialize_run, load_manifest
from paperlocale.preserved_workflow import run_preserved
from paperlocale.providers import TranslationProvider, Translation
from paperlocale.domains import load_domain_pack


class JournalRecoveryTests(unittest.TestCase):
    def make_source(self, root, journal=True):
        source = root/'source.pdf'
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=800)
            if journal:
                page.insert_text((40,30), 'J. Hydrol. Eng., 2021, 26(7): 04021022', fontsize=8)
            page.insert_text((6.4 if journal else 15,700), 'Downloaded from ascelibrary.org. For personal use only.', fontsize=7, rotate=90)
            page.insert_text((40,100), 'The experiment describes the water response.', fontsize=10)
            page.insert_text((40,200), 'Independent observations confirm this result.', fontsize=11)
            doc.save(source)
        return source

    def test_asce_margin_protected_and_rotated_body_still_requires_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = self.make_source(root)
            self.assertEqual(identify_journal(source)['family'], 'asce')
            plan = extract_layout(source, paragraph=True)
            margin = next(b for b in plan['blocks'] if 'Downloaded' in b['text'])
            self.assertEqual(margin['kind'], 'preserve')
            self.assertIn('asce', margin['preserve_reason'])
            self.assertEqual(margin['rect'][0], 0)
            save_json(root/'plan.json', plan)
            load_plan(source, root/'plan.json', paragraph=True)
            self.assertTrue(any(b['kind']=='body' and 'experiment' in b['text'] for b in plan['blocks']))
            with fitz.open(source) as doc:
                doc[0].insert_text((290,700), 'A rotated scientific paragraph must not be skipped.', fontsize=8, rotate=90)
                doc.save(root/'rotated.pdf')
            plan = extract_layout(root/'rotated.pdf', paragraph=True)
            self.assertTrue(any(b['kind']=='review' and 'rotated' in b['text'] for b in plan['blocks']))

    def test_unknown_does_not_guess_and_old_plan_remains_loadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=self.make_source(root, journal=False)
            self.assertEqual(identify_journal(source)['name'], 'unknown')
            plan=extract_layout(source, paragraph=True)
            self.assertTrue(any(b['kind']=='review' for b in plan['blocks']))
            # 无旋转辅助文字的旧计划仍按旧版规则复核，不自动套用新期刊策略。
            with fitz.open() as doc:
                p=doc.new_page();p.insert_text((40,100),'Independent observations confirm this result.')
                doc.save(root/'old.pdf')
            plan=extract_layout(root/'old.pdf',paragraph=True,journal_adapt=False)
            save_json(root/'plan.json', plan)
            self.assertNotIn('journal', load_plan(root/'old.pdf',root/'plan.json',paragraph=True)[0])

    def test_failed_attempt_can_restore_exact_checkpoint_and_stale_menu_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=self.make_source(root); run=root/'run'
            initialize_run(source_pdf=source,run_dir=run,source_language='en',target_language='zh-CN')
            manifest_before=(run/'run_manifest.json').read_bytes()
            error=LocatedError('溢出',[{'id':'a','source':'Actual source sentence.', 'pages':[2], 'font_size':8}], 'layout')
            report=record_error(error,run)
            with self.assertRaises(ValueError):apply_choice(run,'s','old-error')
            apply_choice(run,'f',report['error_id'])
            self.assertEqual(json.loads((run/'repair_choices.json').read_text())['font_sizes']['a'],7.2)
            (run/'failed-attempt.txt').write_text('new')
            again=record_error(error,run)
            apply_choice(run,'b',again['error_id'])
            self.assertFalse((run/'failed-attempt.txt').exists())
            self.assertFalse((run/'repair_choices.json').exists())
            self.assertEqual((run/'run_manifest.json').read_bytes(),manifest_before)
            self.assertEqual(digest(source),load_manifest(run)['source_sha256'])

    def test_skip_keeps_original_pdf_text_and_records_partial_translation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=self.make_source(root);run=root/'run'
            # 加入原生图形，使测试页具有论文页面的可见墨迹；QA 空白页门禁照常运行。
            with fitz.open(source) as doc:
                doc[0].draw_rect(fitz.Rect(40,350,280,500),color=(.4,.4,.4),fill=(.8,.8,.8))
                doc.save(root/'graphic.pdf')
            (root/'graphic.pdf').replace(source)
            initialize_run(source_pdf=source,run_dir=run,source_language='en',target_language='zh-CN')
            plan=extract_layout(source,paragraph=True);save_json(run/'layout_plan.json',plan)
            _, units=load_plan(source,run/'layout_plan.json',paragraph=True)
            unit=next(u for u in units if 'experiment' in u['source'])
            report=record_error(LocatedError('内容校验失败',[unit_location(unit)],'translation'),run)
            apply_choice(run,'s',report['error_id'])
            font=root/'cjk.ttf';font.write_bytes(fitz.Font('china-s').buffer)
            class Provider(TranslationProvider):
                sources=[]
                def translate(self, segments, context):
                    self.sources.extend(s.source for s in segments)
                    return [Translation(s.id,'独立观测证实了此结果。') for s in segments]
            provider=Provider()
            with patch('paperlocale.layout_detection.detect_regions',return_value=[]):
                result=run_preserved(run,provider=provider,domain=load_domain_pack('ecology'),
                                     plan_path=None,font_file=font,paragraph=True,generate_qa=True)
            self.assertEqual(result['status'],'qa_generated')
            self.assertFalse(any('experiment' in s for s in provider.sources))
            with fitz.open(result['rendered_pdf']) as doc,fitz.open(source) as original:
                text=doc[0].get_text()
                self.assertIn('The experiment describes the water response.',text)
                self.assertIn('独立观测',text)
                self.assertEqual(doc[0].search_for('The experiment'),original[0].search_for('The experiment'))
            coverage=json.loads((run/'translation_coverage.json').read_text())
            self.assertFalse(coverage['translation_complete'])
            self.assertEqual(coverage['user_skipped_segments'][0]['id'],unit['id'])

    def test_cli_letter_menu_retry_once_and_quit_has_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=self.make_source(root);run=root/'run'
            initialize_run(source_pdf=source,run_dir=run,source_language='en',target_language='zh-CN')
            calls=[]
            def operation():
                calls.append(1)
                if len(calls)==1: raise ValueError('test failure')
                return 0
            with patch('builtins.input',return_value='r'):
                self.assertEqual(interactive_run(operation,run),0)
            self.assertEqual(len(calls),2)
            with patch('builtins.input',return_value='q'):
                self.assertEqual(interactive_run(lambda: (_ for _ in ()).throw(ValueError('stop')),run),1)

    def test_extraction_skip_preserves_review_block_without_editing_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=self.make_source(root,journal=False);run=root/'run'
            initialize_run(source_pdf=source,run_dir=run,source_language='en',target_language='zh-CN')
            plan=extract_layout(source,paragraph=True);save_json(run/'layout_plan.json',plan)
            before=(run/'layout_plan.json').read_bytes()
            with self.assertRaises(LocatedError) as caught:
                load_plan(source,run/'layout_plan.json',paragraph=True)
            report=record_error(caught.exception,run)
            self.assertEqual(report['items'][0]['pages'],[1])
            apply_choice(run,'s',report['error_id'])
            choices=json.loads((run/'repair_choices.json').read_text())
            # 未识别刊物仍先询问用户；明确选择后只改变运行时分类，不伪造自动计划。
            updated,units=load_plan(source,run/'layout_plan.json',paragraph=True,
                                   skip_block_ids=set(choices['skip_blocks']))
            self.assertTrue(units)
            self.assertTrue(any(b['preserve_reason']=='用户选择跳过，保留原文' for b in updated['blocks']))
            self.assertEqual(before,(run/'layout_plan.json').read_bytes())

    def test_corrupt_backup_refuses_rollback_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=self.make_source(root);run=root/'run'
            initialize_run(source_pdf=source,run_dir=run,source_language='en',target_language='zh-CN')
            report=record_error(ValueError('failed'),run);apply_choice(run,'r',report['error_id'])
            undo=json.loads((run/'recovery_undo.json').read_text())
            (run/'recovery_backups'/undo['backup']/'run_manifest.json').write_text('{}')
            current=(run/'run_manifest.json').read_bytes()
            report=record_error(ValueError('failed again'),run)
            with self.assertRaisesRegex(ValueError,'备份内容已变化'):
                apply_choice(run,'b',report['error_id'])
            self.assertEqual(current,(run/'run_manifest.json').read_bytes())

    def test_repeated_original_figure_marks_are_not_missing_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'figure.pdf'
            with fitz.open() as doc:
                page=doc.new_page()
                page.insert_text((40,100),'Independent observations confirm this result.')
                # 中间插入另一绘制位置，复现散点图的不同原生块回到同一坐标。
                page.insert_text((100,300),'G',fontsize=10)
                page.insert_text((160,330),'K',fontsize=10)
                page.insert_text((100,300),'G',fontsize=10)
                doc.save(source)
            detections=[{'page':1,'rect':[80,275,140,315],'kind':'figure'}]
            plan=extract_layout(source,detections,paragraph=True)
            marks=[b for b in plan['blocks'] if b['text']=='G']
            self.assertEqual(len(marks),2)
            self.assertEqual(marks[0]['id'],marks[1]['id'])
            self.assertTrue(all(b['kind']=='figure' for b in marks))
            save_json(root/'plan.json',plan)
            _,units=load_plan(source,root/'plan.json',detections,paragraph=True)
            self.assertTrue(units)
