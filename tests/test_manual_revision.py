"""人工译文从错误断点和已验收候选进入同一保版流水线的回归。"""
from pathlib import Path
import tempfile
import unittest

import pymupdf as fitz

from paperlocale.diagnostics import LocatedError, record_error
from paperlocale.domains import load_domain_pack
from paperlocale.manual_revision import manual_revision, revision_errors
from paperlocale.preserved_workflow import run_preserved
from paperlocale.providers import Translation, TranslationProvider
from paperlocale.source_layout import digest
from paperlocale.workflow import accept_run, export_run_pdf, initialize_run, load_manifest


class LocalProvider(TranslationProvider):
    def __init__(self, target: str):
        self.target = target
        self.calls = 0

    def translate(self, segments, context):
        self.calls += 1
        return [Translation(segment.id, self.target if '10.43' in segment.source
                            else '土壤湿度是季节性植物生长的重要预测因子。') for segment in segments]


def prepare(root: Path, *, repeat: bool = False) -> tuple[Path, Path, Path]:
    """重复句用两个独立写入区块；固定色块给真实 QA 提供足够非白像素。"""
    source = root / 'source.pdf'
    with fitz.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.draw_rect((40, 20, 560, 45), fill=(.2, .4, .6))
        text = 'Plant growth continued for a mean duration of 10.43 days.'
        page.insert_text((40, 110), text, fontsize=10)
        page.insert_text((40, 137), text if repeat else
                         'Soil moisture is an important predictor of seasonal plant growth.', fontsize=10)
        doc.save(source)
    font = root / 'cjk.ttf'
    font.write_bytes(fitz.Font('china-s').buffer)
    run = root / 'run'
    initialize_run(source_pdf=source, run_dir=run, source_language='en', target_language='zh-CN')
    return source, font, run


class ManualRevisionTests(unittest.TestCase):
    def setUp(self):
        self.domain = load_domain_pack('atmospheric-science')

    def run_options(self, provider, font):
        return dict(provider=provider, domain=self.domain, plan_path=None, font_file=font, dpi=72,
                    paragraph=True, contract_repair=False)

    def test_diagnostic_adoption_resumes_without_model_and_keeps_spacing(self):
        with tempfile.TemporaryDirectory() as directory:
            source, font, run = prepare(Path(directory))
            source_hash = digest(source)
            provider = LocalProvider('植物生长平均持续了11.43天。')
            options = self.run_options(provider, font)
            with self.assertRaises(LocatedError) as failed:
                run_preserved(run, **options)
            error_id = record_error(failed.exception, run)['error_id']
            version = dict(page=1, error_id=error_id, expected_hash=None)
            block, = manual_revision(run, **version)['items']
            draft = {'id': block['id'], 'before': block['target'], 'target': '植物生长共持续 10.43 天。'}
            before = load_manifest(run)
            self.assertTrue(manual_revision(run, **version, edit=draft, check_only=True)['valid'])
            self.assertEqual(load_manifest(run), before)
            self.assertTrue(manual_revision(run, **version, edit=draft)['valid'])
            self.assertTrue(manual_revision(run, **version, edit=draft)['valid'])
            candidate = run_preserved(run, **options)
            self.assertEqual(provider.calls, 1)
            self.assertEqual(digest(source), source_hash)
            self.assertEqual(candidate['status'], 'qa_generated')
            with fitz.open(candidate['rendered_pdf']) as doc:
                self.assertIn(draft['target'], doc[0].get_text())

    def test_review_edit_binds_one_occurrence_and_rechecks_accepted_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, font, run = prepare(root, repeat=True)
            automatic = '植物生长平均持续了10.43天。'
            provider = LocalProvider(automatic)
            options = self.run_options(provider, font)
            old = run_preserved(run, **options)
            old_hash = old['rendered_sha256']
            old_export = export_run_pdf(run, output_pdf=root / 'accepted.pdf')
            accept_run(run, reviewed_by='旧版复核者', expected_rendered_sha256=old_hash)
            version = dict(page=1, expected_hash=old_hash, error_id=None)
            first, second = manual_revision(run, **version)['items']
            self.assertEqual(first['segment_id'], second['segment_id'])
            self.assertNotEqual(first['id'], second['id'])
            draft = {'id': second['id'], 'before': automatic, 'target': '植物生长平均维持  10.43 天。'}
            invalid = {**draft, 'target': '植物生长平均维持11.43天。'}
            self.assertFalse(manual_revision(run, **version, edit=invalid, check_only=True)['valid'])
            self.assertEqual(load_manifest(run)['status'], 'accepted')
            self.assertTrue(manual_revision(run, **version, edit=draft)['valid'])
            self.assertEqual(load_manifest(run)['status'], 'collected')
            self.assertNotIn('accepted_by', load_manifest(run))
            new = run_preserved(run, **options)
            self.assertEqual(provider.calls, 1)
            self.assertEqual(new['status'], 'qa_generated')
            self.assertNotEqual(new['rendered_sha256'], old_hash)
            self.assertEqual(digest(old_export), old_hash)
            with fitz.open(new['rendered_pdf']) as doc:
                self.assertEqual(doc[0].get_text().splitlines(), [automatic, draft['target']])
            with self.assertRaisesRegex(ValueError, '候选'):
                manual_revision(run, **version)
            with self.assertRaisesRegex(ValueError, '候选'):
                accept_run(run, reviewed_by='新版复核者', expected_rendered_sha256=old_hash)
            accept_run(run, reviewed_by='新版复核者', expected_rendered_sha256=new['rendered_sha256'])
            self.assertEqual(load_manifest(run)['accepted_by'], '新版复核者')

    def test_figure_suffix_requires_original_panel_letters(self):
        source = 'Plant growth lasted 10.43 days {v0}d–i).'
        anchors = {'{v0}': '(Fig. 3'}
        target = '植物持续生长 10.43 天{v0}d–i）。'
        self.assertEqual(revision_errors(source, target, self.domain, anchors, None), [])
        errors = revision_errors(source, target.replace('d–i', 'a–c'), self.domain, anchors, None)
        self.assertTrue(any('固定引用的子图编号' in error for error in errors))


if __name__ == '__main__':
    unittest.main()
