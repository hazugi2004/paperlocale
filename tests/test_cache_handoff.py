"""跨模型导入应复用合格缓存，拒绝源身份变化并可从写入中断恢复。"""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import pymupdf as fitz
from paperlocale.source_layout import extract_layout, save_json, units_from_plan, digest
from paperlocale.workflow import initialize_run, save_manifest
from paperlocale.cache_handoff import import_cache
from paperlocale.contracts import write_jsonl_atomic, read_jsonl
from paperlocale.domains import load_domain_pack

class CacheHandoffTest(unittest.TestCase):
    def prepare_import(self, root):
        old, new, source = root/'old', root/'new', root/'source.pdf'
        new.mkdir()
        with fitz.open() as doc:
            p=doc.new_page();p.insert_text((40,80),'Soil moisture changed.')
            doc.save(source)
        domain=load_domain_pack('atmospheric-science')
        plan=extract_layout(source,paragraph=True);units=units_from_plan(plan,paragraph=True)
        m=initialize_run(source_pdf=source,run_dir=old,source_language='en',target_language='zh-CN')
        save_json(old/'layout_plan.json',plan)
        m.update(layout_mode='paragraph',layout_plan_sha256=digest(old/'layout_plan.json'),
                 domain_sha256=domain.content_sha256,translation_provider={'provider':'old','model':'old'})
        save_manifest(old,m)
        rows=[{'id':u['id'],'source':u['source'],'target':'土壤湿度发生变化。'} for u in units]
        write_jsonl_atomic(old/'translations.jsonl',rows)
        return old, new, source, domain, plan, units, rows

    def interrupt_import(self, old, new, plan, units, domain):
        with patch('paperlocale.cache_handoff.write_jsonl_atomic',side_effect=OSError('full disk')):
            with self.assertRaises(OSError):
                import_cache(old,new,plan,units,domain)
        self.assertTrue((new/'cache_handoff.json').is_file())
        self.assertFalse((new/'translations.jsonl').exists())

    def test_import_records_old_provider_and_recovers_partial_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            old,new,source,domain,plan,units,rows=self.prepare_import(root)
            self.interrupt_import(old,new,plan,units,domain)
            report_before=(new/'cache_handoff.json').read_bytes()
            # JSON key order is not part of plan identity.
            import_cache(old,new,dict(reversed(list(plan.items()))),units,domain)
            self.assertEqual((new/'cache_handoff.json').read_bytes(),report_before)
            imported=read_jsonl(new/'translations.jsonl')
            self.assertEqual(imported[0]['source_run_provider']['model'],'old')
            self.assertFalse(imported[0]['provider_identity_verified'])
            self.assertEqual(read_jsonl(old/'translations.jsonl'),rows)
            # Layout repair metadata does not change translation acceptance identity.
            units[0]['_repair_font_floor']=6
            import_cache(old,new,plan,units,domain)
            self.assertEqual(read_jsonl(new/'translations.jsonl'),imported)
            with self.assertRaises(ValueError):
                import_cache(old,new,{**plan, 'changed':True},units,domain)
            other=root/'other';other.mkdir()
            bad={**plan,'source_sha256':'different'}
            with self.assertRaisesRegex(ValueError,'同一源'):import_cache(old,other,bad,units,domain)

            # 无关保护块被新提取修复，不应使未改变的正文缓存全篇失效。
            changed=deepcopy(plan)
            changed['blocks'].append({'id':'fixed', 'page':1, 'kind':'formula', 'parts':[]})
            reuse=root/'reuse';reuse.mkdir()
            import_cache(old,reuse,changed,units,domain)
            self.assertEqual(len(read_jsonl(reuse/'translations.jsonl')),1)
            shifted=deepcopy(units)
            shifted[0]['slots'][0][0]['chars'][0]['origin'][0] += 1
            reject=root/'reject';reject.mkdir()
            import_cache(old,reject,plan,shifted,domain)
            self.assertEqual(read_jsonl(reject/'translations.jsonl'),[])

    def test_interrupted_import_rejects_changed_identity_before_recovery(self):
        for changed in ('domain', 'source_hash', 'source_file', 'plan', 'units',
                        'source_plan', 'source_translations', 'report_domain'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                old,new,source,domain,plan,units,rows=self.prepare_import(Path(tmp))
                self.interrupt_import(old,new,plan,units,domain)
                report_path=new/'cache_handoff.json'
                if changed == 'domain':
                    domain=load_domain_pack('ecology')
                elif changed == 'source_hash':
                    plan['source_sha256']='different'
                elif changed == 'source_file':
                    source.write_bytes(source.read_bytes()+b'changed')
                elif changed == 'plan':
                    plan['blocks'].append({'id':'fixed', 'page':1, 'kind':'formula', 'parts':[]})
                elif changed == 'units':
                    units[0]['slots'][0][0]['chars'][0]['origin'][0] += 1
                elif changed == 'source_plan':
                    old_plan=json.loads((old/'layout_plan.json').read_text())
                    old_plan['blocks'].append({'id':'fixed', 'page':1, 'kind':'formula', 'parts':[]})
                    save_json(old/'layout_plan.json',old_plan)
                    manifest=json.loads((old/'run_manifest.json').read_text())
                    manifest['layout_plan_sha256']=digest(old/'layout_plan.json')
                    save_manifest(old,manifest)
                elif changed == 'source_translations':
                    rows[0]['target']='土壤湿度改变。'
                    write_jsonl_atomic(old/'translations.jsonl',rows)
                else:
                    report=json.loads(report_path.read_text())
                    report['import_identity']['domain_sha256']='different'
                    save_json(report_path,report)
                report_before=report_path.read_bytes()
                with self.assertRaises(ValueError):
                    import_cache(old,new,plan,units,domain)
                self.assertFalse((new/'translations.jsonl').exists())
                self.assertEqual(report_path.read_bytes(),report_before)

    def test_legacy_report_is_rejected_even_when_translations_exist(self):
        for completed in (False, True):
            with self.subTest(completed=completed), tempfile.TemporaryDirectory() as tmp:
                old,new,source,domain,plan,units,rows=self.prepare_import(Path(tmp))
                self.interrupt_import(old,new,plan,units,domain)
                report_path=new/'cache_handoff.json'
                report=json.loads(report_path.read_text())
                del report['import_identity']
                save_json(report_path,report)
                if completed:
                    write_jsonl_atomic(new/'translations.jsonl',report['accepted'])
                with self.assertRaisesRegex(ValueError,'旧导入报告缺少身份哈希'):
                    import_cache(old,new,plan,units,domain)
                if completed:
                    self.assertEqual(read_jsonl(new/'translations.jsonl'),report['accepted'])
                else:
                    self.assertFalse((new/'translations.jsonl').exists())
