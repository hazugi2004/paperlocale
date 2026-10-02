"""App/CLI 人工修订的实际边界：坏译文不落盘、好译文可继续、其他缓存不动、可回退。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from paperlocale.contracts import read_jsonl, segment_id, write_jsonl_atomic
from paperlocale.diagnostics import LocatedError, record_error
from paperlocale.domains import load_domain_pack
from paperlocale.repair_choices import apply_choice
from paperlocale.repair_editor import check_edits
from paperlocale.source_layout import digest


class RepairEditorTests(unittest.TestCase):
    def prepare(self, root):
        source_pdf = root/'source.pdf'; source_pdf.write_bytes(b'%PDF-source')
        plan = root/'layout_plan.json'; plan.write_text('{}')
        source = 'CDHE is shown in {v0}.'; sid = segment_id(source)
        domain = load_domain_pack('atmospheric-science')
        manifest = {'source_pdf':str(source_pdf), 'source_sha256':digest(source_pdf),
                    'layout_plan':str(plan), 'status':'collected', 'translation_provider':{'provider':'test'}}
        (root/'run_manifest.json').write_text(json.dumps(manifest))
        (root/'repair_context.json').write_text(json.dumps({'source_sha256':digest(source_pdf),
            'layout_plan_sha256':digest(plan), 'domain':domain.pack_id,
            'domain_sha256':domain.content_sha256, 'anchors':{sid:{'{v0}':'Fig. 1'}}}))
        write_jsonl_atomic(root/'segments.jsonl', [{'id':sid,'source':source}])
        write_jsonl_atomic(root/'translations.jsonl', [{'id':'unaffected','source':'Keep','target':'保留'}])
        report = record_error(LocatedError('内容校验失败', [{'id':sid,'source':source,'target':'见{v0}。',
            'pages':[2],'errors':['abbreviation 标记缺失：CDHE']}], 'translation'),root)
        return sid, report

    def test_invalid_edit_and_preview_leave_cache_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);sid,report=self.prepare(root)
            before=(root/'translations.jsonl').read_bytes()
            self.assertTrue(check_edits(root,report['error_id'],{sid:'见{v0}。'})[0]['errors'])
            with self.assertRaisesRegex(ValueError,'尚未通过'):
                apply_choice(root,'e',report['error_id'],{sid:'见{v0}。'})
            self.assertEqual((root/'translations.jsonl').read_bytes(),before)
            self.assertFalse((root/'recovery_undo.json').exists())
            checked=check_edits(root,report['error_id'],{sid:'CDHE见{v0}。'})
            self.assertEqual(checked[0]['errors'],[])
            self.assertEqual((root/'translations.jsonl').read_bytes(),before)

    def test_valid_revision_preserves_other_cache_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);sid,report=self.prepare(root)
            before=(root/'translations.jsonl').read_bytes()
            apply_choice(root,'e',report['error_id'],{sid:'CDHE见{v0}。'})
            rows=read_jsonl(root/'translations.jsonl')
            self.assertEqual(rows[0],{'id':'unaffected','source':'Keep','target':'保留'})
            self.assertEqual(rows[1]['target'],'CDHE见{v0}。')
            self.assertEqual(rows[1]['manual_revision']['error_id'],report['error_id'])
            new=record_error(LocatedError('后续排版失败',[{'id':sid,'source':'CDHE','pages':[2]}],'layout'),root)
            apply_choice(root,'b',new['error_id'])
            self.assertEqual((root/'translations.jsonl').read_bytes(),before)

    def test_stale_or_unknown_segment_and_changed_source_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);sid,report=self.prepare(root)
            for error_id, edits in [('old',{sid:'CDHE'}),(report['error_id'],{'other':'CDHE'})]:
                with self.assertRaises(ValueError):check_edits(root,error_id,edits)
            (root/'source.pdf').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'身份变化'):
                check_edits(root,report['error_id'],{sid:'CDHE见{v0}。'})

    def test_cli_check_returns_detailed_json_without_model(self):
        from paperlocale.cli import main
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);sid,report=self.prepare(root)
            path=root/'draft.json';path.write_text(json.dumps({sid:'CDHE见{v0}。'}))
            output=io.StringIO()
            with patch('sys.argv',['paperlocale','repair-choice','--run-dir',str(root),'--error-id',report['error_id'],
                 '--choice','e','--edits-file',str(path),'--check-only']),contextlib.redirect_stdout(output):
                self.assertEqual(main(),0)
            self.assertTrue(json.loads(output.getvalue())['valid'])
            self.assertFalse((root/'recovery_undo.json').exists())

    def test_central_workspace_separates_names_and_supports_override(self):
        from paperlocale.workspaces import run_directory
        from paperlocale.cli import build_parser
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            a=run_directory(root/'a/paper.pdf',root/'工作区');b=run_directory(root/'b/paper.pdf',root/'工作区')
            self.assertNotEqual(a,b);self.assertEqual(a.parent,(root/'工作区').resolve())
            parsed=build_parser().parse_args(['run',str(root/'paper.pdf'),'--workspace-dir',str(root/'工作区')])
            self.assertIsNone(parsed.run_dir)
            self.assertEqual(parsed.workspace_dir,root/'工作区')

    def test_invisible_model_character_is_located_before_layout(self):
        from paperlocale.contracts import validate_translation
        from paperlocale.error_categories import rule_detail
        source='Muñoz reports a result.'
        errors=validate_translation(source,'Mu\x7fñoz报告结果。')
        control=next(e for e in errors if e.startswith('character '))
        self.assertIn('U+007F',control)
        self.assertIn('第 3 字符',control)
        self.assertIn('原文不包含',control)
        self.assertEqual(rule_detail(control)['code'],'content.character')
        self.assertFalse(any(e.startswith('character ') for e in validate_translation(source,'Muñoz报告结果。')))

    def test_unwritable_workspace_still_returns_diagnostic_and_font_action_is_relevant(self):
        # 工作区不可写是实际可恢复错误，报告必须仍能经 stdout 交给 App。
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch('paperlocale.diagnostics._save', side_effect=PermissionError('read only')):
                report=record_error(PermissionError('workspace denied'),root)
            self.assertEqual(report['classification']['code'],'file.permission')
            self.assertTrue(report['diagnostic_warnings'])
            issue={'id':'x','source':'source','pages':[1],'font_size':9}
            font=record_error(LocatedError('中文字体缺少字形：x',[issue],'layout'),root)
            self.assertNotIn('f',{a['key'] for a in font['actions']})
            capacity=record_error(LocatedError('完整译文无法放入段落框',[issue],'layout'),root)
            self.assertIn('f',{a['key'] for a in capacity['actions']})
