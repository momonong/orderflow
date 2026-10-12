import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from pypdf import PdfReader
from orderflow.app import Store
from orderflow.broker import BrokerConfig, MIB

SPEC = importlib.util.spec_from_file_location('broker_smoke', Path(__file__).resolve().parents[1] / 'deploy/orderflow-broker-smoke.py')
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)
SOURCE = 'a' * 40


class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root/'state'
        Store(self.data)
        self.private = self.root/'private'; self.private.mkdir(mode=0o700)
        self.intent = self.private/'intent.json'
        self.config = BrokerConfig(token_file=smoke.TOKEN_PATH, max_pdf_bytes=8*MIB,
            max_request_bytes=16*MIB, max_result_bytes=4*MIB,
            input_bound_verified=True, input_bound_evidence='fixture-window',
            input_bound_expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())
        self.guard = lambda: None

    def tearDown(self):
        self.tmp.cleanup()

    def prepare(self, **kwargs):
        return smoke.create_or_recover(self.data, self.intent, self.config, SOURCE,
            require_stopped=self.guard, require_ready=lambda _config,_source:None, **kwargs)

    def counts(self):
        with sqlite3.connect(self.data/'orderflow.sqlite3') as db:
            return tuple(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                         for table in ('sessions','session_auth','documents','jobs','broker_jobs'))

    def test_fixed_pdf_is_valid_and_publicly_checkable(self):
        pdf = PdfReader(io.BytesIO(smoke.PDF))
        self.assertEqual(len(pdf.pages), 1)
        text = pdf.pages[0].extract_text()
        for expected in ('SYNTHETIC INVOICE', 'SYNTHETIC WIDGET', 'Quantity: 2', 'Unit Price: 3.50', 'Amount: 7.00'):
            self.assertIn(expected, text)
        self.assertEqual(hashlib.sha256(smoke.PDF).hexdigest(), smoke.PDF_SHA256)
        self.assertEqual(smoke.synthetic_pdf(), smoke.PDF)

    def test_one_job_snapshot_2048_formal_config_stays_8192_no_token_io(self):
        before = self.config.snapshot()
        with patch('orderflow.broker._read_token', side_effect=AssertionError('must not read credential')):
            receipt = self.prepare()
        self.assertEqual(self.counts(), (1,0,1,1,1))
        self.assertEqual(self.config.snapshot(), before)
        with sqlite3.connect(self.data/'orderflow.sqlite3') as db:
            saved, task, digest = db.execute('SELECT config_json,task_json,task_sha256 FROM broker_jobs').fetchone()
        self.assertEqual(json.loads(saved)['max_output_tokens'], 2048)
        self.assertEqual(json.loads(task)['max_output_tokens'], 2048)
        self.assertEqual(json.loads(task)['max_attempts'], 1)
        self.assertEqual(self.config.max_output_tokens, 8192)
        self.assertEqual(hashlib.sha256(task.encode()).hexdigest(), digest)
        self.assertEqual(receipt['task_sha256'], digest)
        self.assertEqual(receipt['max_output_tokens'],2048)
        self.assertNotIn('output_tokens',receipt)
        self.assertEqual(list((self.data/'files').glob('*.pdf'))[0].stat().st_uid, os.geteuid())
        for path in self.data.glob('orderflow.sqlite3*'):
            self.assertEqual(path.stat().st_uid, os.geteuid())
        intent = json.loads(self.intent.read_text())
        self.assertNotIn('token_hash', intent)
        self.assertNotIn('token_hash', receipt)
        self.assertEqual(intent['config']['max_output_tokens'], 8192)
        self.assertEqual(self.intent.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.intent.with_suffix('.pdf').read_bytes(), smoke.PDF)

    def test_every_persistence_gap_reuses_one_owner_pdf_document_job_and_key(self):
        for gap in ('after_intent', 'after_owner', 'after_pdf', 'after_document', 'after_job', 'before_receipt'):
            with self.subTest(gap=gap), tempfile.TemporaryDirectory() as name:
                root = Path(name); data=root/'state'; Store(data)
                private=root/'private'; private.mkdir(mode=0o700); intent_path=private/'intent.json'
                def crash(phase):
                    if phase == gap:
                        raise RuntimeError('synthetic crash')
                with self.assertRaises(RuntimeError):
                    smoke.create_or_recover(data, intent_path, self.config, SOURCE, require_stopped=lambda:None, require_ready=lambda _config,_source:None, fault=crash)
                original = json.loads(intent_path.read_text())
                receipt = smoke.create_or_recover(data, intent_path, self.config, SOURCE, require_stopped=lambda:None, require_ready=lambda _config,_source:None)
                replay = smoke.create_or_recover(data, intent_path, self.config, SOURCE, require_stopped=lambda:None, require_ready=lambda _config,_source:None)
                self.assertEqual(receipt['job_id'], replay['job_id'])
                self.assertEqual(receipt['request_key'], original['request_key'])
                self.assertEqual(receipt['document_id'], original['document_id'])
                self.assertEqual(json.loads(intent_path.read_text())['owner_id'], original['owner_id'])
                with sqlite3.connect(data/'orderflow.sqlite3') as db:
                    for table in ('sessions','documents','jobs','broker_jobs'):
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0], 1)
                self.assertEqual(len(list((data/'files').glob('*.pdf'))), 1)
                self.assertEqual(json.loads(intent_path.with_suffix('.receipt.json').read_text())['job_id'], receipt['job_id'])

    def test_reentry_only_reads_existing_job_even_if_worker_live_failed_or_expired(self):
        receipt=self.prepare()
        for phase in ('submitting','unknown','failed','expired','complete','busy_paused','busy_0','busy_32','submitting_0','submitting_32'):
            with sqlite3.connect(self.data/'orderflow.sqlite3') as db:
                db.execute('UPDATE broker_jobs SET phase=?', (phase,))
            with patch.object(smoke,'Store',side_effect=AssertionError('must not initialize live Store')):
                self.guard=lambda: self.fail('must not require stopped for existing job')
                replay=self.prepare()
            self.assertEqual(replay['job_id'], receipt['job_id'])
            self.assertEqual(replay['phase'],phase)
        self.assertEqual(self.counts(), (1,0,1,1,1))

    def test_guard_failure_precedes_intent_store_and_db_write(self):
        self.guard=lambda: smoke.require(False,'SMOKE_WRITER_ACTIVE')
        with patch.object(smoke,'Store',side_effect=AssertionError('guard must run first')):
            with self.assertRaises(smoke.SmokeError): self.prepare()
        self.assertFalse(self.intent.exists())
        self.assertEqual(self.counts(),(0,0,0,0,0))

    def test_partial_intent_resume_still_requires_stopped_writer(self):
        def crash(phase):
            if phase=='after_document': raise RuntimeError('crash')
        with self.assertRaises(RuntimeError): self.prepare(fault=crash)
        self.guard=lambda: smoke.require(False,'SMOKE_WRITER_ACTIVE')
        with patch.object(smoke,'Store',side_effect=AssertionError('guard must run first')):
            with self.assertRaises(smoke.SmokeError): self.prepare()
        self.assertEqual(self.counts(),(1,0,1,0,0))

    def test_corrupt_pdf_or_mapping_never_creates_another_job(self):
        receipt=self.prepare()
        pdf=self.data/'files'/(receipt['document_id']+'.pdf')
        pdf.write_bytes(b'changed')
        with self.assertRaises(smoke.SmokeError): self.prepare()
        self.assertEqual(self.counts(),(1,0,1,1,1))
        pdf.write_bytes(smoke.PDF)
        with sqlite3.connect(self.data/'orderflow.sqlite3') as db:
            db.execute("UPDATE broker_jobs SET task_json='{}'")
        with self.assertRaises(smoke.SmokeError): self.prepare()
        self.assertEqual(self.counts(),(1,0,1,1,1))

    def test_all_identity_and_snapshot_drift_is_rejected_without_new_job(self):
        changes = (('jobs', "scenario='mock'"), ('documents', "purpose='diagnostic'"),
                   ('documents', "document_kind='purchase_order'"), ('documents', 'size=size+1'),
                   ('broker_jobs', "session_id='wrong-owner'"), ('broker_jobs', "document_id='wrong-document'"))
        for table, change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as name:
                root=Path(name); data=root/'state'; Store(data); private=root/'private'; private.mkdir(mode=0o700)
                intent=private/'intent.json'
                kwargs={'require_stopped':lambda:None, 'require_ready':lambda _config,_source:None}
                smoke.create_or_recover(data,intent,self.config,SOURCE,**kwargs)
                with sqlite3.connect(data/'orderflow.sqlite3') as db: db.execute('UPDATE '+table+' SET '+change)
                with self.assertRaises(smoke.SmokeError): smoke.create_or_recover(data,intent,self.config,SOURCE,**kwargs)
                with sqlite3.connect(data/'orderflow.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],1)
        self.prepare()
        with sqlite3.connect(self.data/'orderflow.sqlite3') as db:
            value=json.loads(db.execute('SELECT config_json FROM broker_jobs').fetchone()[0])
            value['max_request_bytes']=2*MIB
            db.execute('UPDATE broker_jobs SET config_json=?',(json.dumps(value),))
        with self.assertRaises(smoke.SmokeError): self.prepare()
        self.assertEqual(self.counts(),(1,0,1,1,1))

    def test_current_config_drift_only_reads_completed_mapping_not_new_prepare(self):
        receipt=self.prepare()
        self.config=replace(self.config,max_output_tokens=1024,input_bound_verified=False)
        with patch.object(smoke,'Store',side_effect=AssertionError('no initialization')):
            self.assertEqual(self.prepare()['job_id'],receipt['job_id'])
        self.assertEqual(self.counts(),(1,0,1,1,1))

    def test_partial_config_drift_and_readiness_missing_fail_before_new_db_writes(self):
        def crash(phase):
            if phase=='after_intent': raise RuntimeError('crash')
        with self.assertRaises(RuntimeError): self.prepare(fault=crash)
        self.config=replace(self.config,input_bound_verified=False)
        with self.assertRaises(smoke.SmokeError): self.prepare()
        self.assertEqual(self.counts(),(0,0,0,0,0))
        other=self.private/'other.json'
        with self.assertRaises(smoke.SmokeError):
            smoke.create_or_recover(self.data,other,replace(self.config,input_bound_verified=True),SOURCE,
                require_stopped=lambda:None, require_ready=lambda config,source:smoke.reviewed_readiness(self.private/'never-read', '0'*64,config,source))
        self.assertFalse(other.exists())
        self.assertEqual(self.counts(),(0,0,0,0,0))

    def test_concurrent_prepare_returns_one_opaque_work(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts=list(pool.map(lambda _:self.prepare(),range(2)))
        self.assertEqual(receipts[0]['job_id'],receipts[1]['job_id'])
        self.assertEqual(receipts[0]['request_key'],receipts[1]['request_key'])
        self.assertEqual(self.counts(),(1,0,1,1,1))
        self.assertEqual(len(list((self.data/'files').glob('*.pdf'))),1)

    def test_unrelated_existing_receipt_is_not_overwritten(self):
        receipt=self.prepare()
        path=self.intent.with_suffix('.receipt.json')
        wrong={**receipt,'job_id':'unrelated-job'}
        path.write_text(json.dumps(wrong))
        with self.assertRaises(smoke.SmokeError):self.prepare()
        self.assertEqual(json.loads(path.read_text()),wrong)
        self.assertEqual(self.counts(),(1,0,1,1,1))

    def test_corrupt_oversized_pdf_is_rejected_before_content_read(self):
        receipt=self.prepare()
        pdf=self.data/'files'/(receipt['document_id']+'.pdf')
        pdf.write_bytes(smoke.PDF+b'extra')
        original=Path.read_bytes
        def bounded(path):
            if path==pdf:self.fail('oversized PDF must not be read')
            return original(path)
        with patch.object(Path,'read_bytes',bounded):
            with self.assertRaises(smoke.SmokeError):self.prepare()
        self.assertEqual(self.counts(),(1,0,1,1,1))

    def mark_done(self, rows, gateway='completed', usage_unknown=0, finish='stop', truncated=0):
        with sqlite3.connect(self.data/'orderflow.sqlite3') as db:
            db.execute("UPDATE jobs SET state='done',result=?",(json.dumps(rows),))
            db.execute("UPDATE broker_jobs SET phase='complete',gateway_state=?,usage_unknown=?,finish_reason=?,response_truncated=?",
                (gateway,usage_unknown,finish,truncated))

    def test_expected_items_match_and_usage_unknown_remain_separate(self):
        self.prepare()
        row={**smoke.EXPECTED_TEXT, **smoke.EXPECTED_DECIMALS}
        row['qty']='2.0'; row['unitPrice']='3.5'
        self.mark_done([row],gateway='completed_usage_unknown',usage_unknown=0)
        receipt=self.prepare()
        self.assertTrue(receipt['expected_items_match'])
        self.assertTrue(receipt['usage_unknown'])
        self.assertEqual(receipt['gateway_state'],'completed_usage_unknown')
        self.assertEqual(receipt['finish_reason'],'stop')
        self.assertIs(receipt['response_truncated'],False)
        self.mark_done([row])
        self.assertFalse(self.prepare()['usage_unknown'])
        self.assertEqual(self.counts(),(1,0,1,1,1))

    def test_expected_mismatch_never_echoes_business_result(self):
        self.prepare()
        row={**smoke.EXPECTED_TEXT, **smoke.EXPECTED_DECIMALS}
        for rows in ([],[row,row],[{**row,'qty':'3'}],[{**row,'unitPrice':'4'}],
                     [{**row,'product':'PRIVATE_RESULT_MARKER'}],[{**row,'qty':'NaN'}]):
            self.mark_done(rows)
            receipt=self.prepare()
            self.assertFalse(receipt['expected_items_match'])
            self.assertNotIn('PRIVATE_RESULT_MARKER',json.dumps(receipt))
            self.assertNotIn('PRIVATE_RESULT_MARKER',self.intent.with_suffix('.receipt.json').read_text())
        self.assertEqual(self.counts(),(1,0,1,1,1))

    def test_safe_completion_metadata_is_unknown_until_supported(self):
        receipt=self.prepare()
        self.assertIsNone(receipt['expected_items_match'])
        self.assertTrue(receipt['usage_unknown'])
        self.assertIsNone(receipt['finish_reason'])
        self.assertIsNone(receipt['response_truncated'])
        row={**smoke.EXPECTED_TEXT, **smoke.EXPECTED_DECIMALS}
        self.mark_done([row],gateway='PRIVATE_UNKNOWN_METADATA',usage_unknown=0,
                       finish='PRIVATE_UNKNOWN_FINISH',truncated='false')
        receipt=self.prepare()
        self.assertIsNone(receipt['gateway_state'])
        self.assertIsNone(receipt['finish_reason'])
        self.assertIsNone(receipt['response_truncated'])
        self.assertTrue(receipt['usage_unknown'])
        self.assertNotIn('PRIVATE_UNKNOWN',json.dumps(receipt))
        with sqlite3.connect(self.data/'orderflow.sqlite3') as db:
            db.execute("UPDATE jobs SET state='failed'")
        self.assertIsNone(self.prepare()['expected_items_match'])

    def test_writer_identity_restores_root_before_receipt_on_exception(self):
        events=[]
        with patch.object(smoke.os,'geteuid',return_value=0), patch.object(smoke.os,'getegid',return_value=0), \
             patch.object(smoke.os,'setegid',side_effect=lambda value:events.append(('gid',value))), \
             patch.object(smoke.os,'seteuid',side_effect=lambda value:events.append(('uid',value))):
            with self.assertRaises(RuntimeError):
                with smoke.writer_identity((1001,1001)):
                    raise RuntimeError('synthetic crash')
        self.assertEqual(events,[('gid',1001),('uid',1001),('uid',0),('gid',0)])


if __name__=='__main__':
    unittest.main()
