"""跨模型导入应复用合格缓存，拒绝源身份变化并可从写入中断恢复。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pymupdf as fitz
from paperlocale.source_layout import extract_layout, save_json, units_from_plan, digest
from paperlocale.workflow import initialize_run, save_manifest
from paperlocale.cache_handoff import import_cache
from paperlocale.contracts import write_jsonl_atomic, read_jsonl
from paperlocale.domains import load_domain_pack

class CacheHandoffTest(unittest.TestCase):
    def test_import_records_old_provider_and_recovers_partial_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old=root/'old';new=root/'new';new.mkdir()
            source=root/'source.pdf'
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
            with patch('paperlocale.cache_handoff.write_jsonl_atomic',side_effect=OSError('full disk')):
                with self.assertRaises(OSError):import_cache(old,new,plan,units,domain)
            import_cache(old,new,plan,units,domain)
            imported=read_jsonl(new/'translations.jsonl')
            self.assertEqual(imported[0]['source_run_provider']['model'],'old')
            self.assertFalse(imported[0]['provider_identity_verified'])
            self.assertEqual(read_jsonl(old/'translations.jsonl'),rows)
            other=root/'other';other.mkdir()
            bad={**plan,'source_sha256':'different'}
            with self.assertRaisesRegex(ValueError,'同一源'):import_cache(old,other,bad,units,domain)
