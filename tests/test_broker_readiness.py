import copy
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

SPEC=importlib.util.spec_from_file_location('broker_readiness',Path(__file__).resolve().parents[1]/'deploy/broker_readiness.py')
gate=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(gate)
NOW=datetime(2026,10,12,6,0,tzinfo=timezone.utc)


def catalog():
    return [{'target_id':gate.TARGET,'provider':'google','model':gate.MODEL,
        'available':True,'configured_available':True,'free_eligible':True,'billing_enabled':False,
        'features':['text_generation','text','document_input','structured_output'],
        'capabilities':['text_generation'],'max_output_tokens':8192,'concurrency_limit':1,
        'shared_concurrency_limit':1,'shared_concurrency_scope':gate.SCOPE,'quota_basis':'local_safety_cap',
        'account_verified_at':'2026-10-08T05:34:09+00:00','account_expires_at':'2026-10-22T03:53:20+00:00',
        'capacity':{'expires_at':'2026-10-22T03:53:20+00:00'},
        'request_limits':{'max_output_tokens':8192,'max_input_token_bound_for_requested_output':131328},
        'local_safety_caps':[{'metric':metric,'window':window,'limit':value,
            'bucket':'google:asus-dev-key:'+metric+':'+window,
            'timezone':'America/Los_Angeles' if window=='day' else 'UTC'} for (metric,window),value in gate.CAPS.items()]}]


def diagnostics(now=NOW):
    return {'as_of':now.isoformat(),'basis':'local_minimum_admission_snapshot','ready_targets':0,
        'execution_policy':dict(gate.POLICY),
        'queue_worker':{'enabled':True,'running':True,'stopped':False,'error_code':None,'last_tick_at':now.isoformat()},
        'targets':[{'target_id':gate.TARGET,'provider':'google','model':gate.MODEL,'state':'unknown',
            'reasons':['credential_inventory_unknown'],'credentials':{'state':'unknown'},
            'cooldown_until':None,'next_retry_at':None,'request_limits':catalog()[0]['request_limits']}]}


def properties():
    cd='/run/credentials/api-quota-broker.service/'
    args=[str(gate.CURRENT/'runtime/bin/python'),'-I','-m','quota_broker.cli','gateway-serve',
        '--config',str(gate.CONFIG),'--db','/var/lib/api-quota-broker/ledger.sqlite3','--port','18084',
        '--digest-key-file',cd+'digest_key','--client-token-file',cd+'client_token',
        '--admin-token-file',cd+'admin_token','--doppler-token-file',cd+'doppler_service_token',
        '--doppler-project','api-quota-broker','--doppler-config','dev','--queue-key-file','/run/api-quota-broker/queue.key',
        '--google-pdf-count-tokens','--serialize-work','--max-media-input-bytes','8388608',
        '--max-request-bytes','16777216','--max-result-bytes','4194304']
    return {'User':'api-quota-broker','Group':'api-quota-broker','WorkingDirectory':str(gate.CURRENT),
        'FragmentPath':str(gate.UNIT),'DropInPaths':'','ActiveState':'active','SubState':'running',
        'NeedDaemonReload':'no','CPUQuotaPerSecUSec':'500ms','MemoryHigh':'402653184','MemoryMax':'536870912',
        'MemorySwapMax':'0','TasksMax':'32','LimitNOFILE':'128','LimitCORE':'0','NoNewPrivileges':'yes',
        'MainPID':'473639','ExecMainStartTimestampMonotonic':'1291300396565',
        'ExecStart':'{ path='+args[0]+' ; argv[]='+' '.join(args)+
            ' ; ignore_errors=no ; start_time=[Mon 2026-10-12 05:55:00 UTC] ; stop_time=[n/a] ; pid=473639 ; code=(null) ; status=0/0 }'}


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'ready.json'
        self.manifest={'schema_version':1,'policy':'asus-release-v1','project':'api-quota-broker',
            'source':{'head':gate.SOURCE,'payload_dirty':False,'included_changes':[]},'files':[]}
        self.receipt={'observed_at':(NOW-timedelta(minutes=5)).isoformat(),
            'expires_at':(NOW+timedelta(minutes=115)).isoformat(), 'release_manifest':self.manifest,
            'current':str(gate.RELEASE),'config_sha256':gate.CONFIG_SHA,'unit_sha256':gate.UNIT_SHA,
            'provider_posts_in_this_read':0,'active_release':{'release':'releases/'+gate.RELEASE.name,'unit_sha256':gate.UNIT_SHA},
            'diagnostics':diagnostics(NOW-timedelta(minutes=5)),'google_catalog':catalog()}
        self.fresh={'diagnostics':diagnostics(),'google_catalog':catalog()}

    def tearDown(self):self.tmp.cleanup()

    def validate(self, after=None):
        raw=gate._canonical(self.receipt);self.path.write_bytes(raw)
        digest=hashlib.sha256(raw).hexdigest()
        with patch.object(gate,'RECEIPT_SHA',digest), \
             patch.object(gate,'MANIFEST_SHA',hashlib.sha256(gate._canonical(self.manifest)).hexdigest()), \
             patch.object(gate.os,'geteuid',return_value=0),patch.object(gate,'_now',return_value=NOW), \
             patch.object(gate,'_read_root',return_value=raw),patch.object(gate,'_host',return_value=properties()), \
             patch.object(gate,'_observe',return_value=self.fresh), \
             patch.object(gate,'_unit_properties',return_value=after or properties()), \
             patch.object(Path,'resolve',return_value=gate.RELEASE):
            return gate.validate_readiness(self.path,digest)

    def test_unknown_inventory_allowed_but_not_pdf_or_account_remaining_ready(self):
        result=self.validate()
        self.assertEqual(result['source_sha'],gate.SOURCE)
        self.assertEqual(result['credential_inventory_state'],'unknown')
        self.assertFalse(result['pdf_admission_ready']);self.assertFalse(result['account_remaining_known'])
        self.assertEqual(result['quota_basis'],'local_safety_cap')
        self.assertGreater(gate._stamp(result['bound_valid_until']),gate._stamp(result['expires_at']))
        self.assertEqual(result['max_output_tokens'],8192)

    def test_receipt_window_and_source_are_fail_closed(self):
        for field,value in (('expires_at',NOW.isoformat()),('observed_at',(NOW+timedelta(seconds=1)).isoformat()),
                            ('expires_at',(NOW+timedelta(days=1)).isoformat())):
            original=self.receipt[field];self.receipt[field]=value
            with self.assertRaises(gate.ReadinessError):self.validate()
            self.receipt[field]=original
        self.manifest['source']['head']='0'*40
        with self.assertRaises(gate.ReadinessError):self.validate()

    def test_hash_pin_and_json_duplicates_never_fallback(self):
        with patch.object(gate.os,'geteuid',return_value=0):
            with self.assertRaises(gate.ReadinessError):gate.validate_readiness(self.path,'0'*64)
        with self.assertRaises(gate.ReadinessError):gate._json(b'{"state":1,"state":2}')
        with self.assertRaises(gate.ReadinessError):gate._json(b'{"state":NaN}')

    def test_changed_free_capability_caps_and_worker_are_rejected(self):
        mutations=[('free_eligible',False),('billing_enabled',True),('max_output_tokens',2048),
                   ('concurrency_limit',2),('features',['text_generation']),
                   ('account_expires_at','2026-10-01T00:00:00+00:00')]
        for field,value in mutations:
            old=self.fresh['google_catalog'][0][field];self.fresh['google_catalog'][0][field]=value
            with self.assertRaises(gate.ReadinessError):self.validate()
            self.fresh['google_catalog'][0][field]=old
        self.fresh['google_catalog'][0]['local_safety_caps'][0]['limit']=4
        with self.assertRaises(gate.ReadinessError):self.validate()
        self.fresh={'diagnostics':diagnostics(),'google_catalog':catalog()}
        self.fresh['diagnostics']['queue_worker']['running']=False
        with self.assertRaises(gate.ReadinessError):self.validate()
        self.fresh['diagnostics']=diagnostics(NOW-timedelta(seconds=61))
        with self.assertRaises(gate.ReadinessError):self.validate()

    def test_policy_exact_types_and_extra_keys_rejected(self):
        self.fresh['diagnostics']['execution_policy']['serialize_work']=1
        with self.assertRaises(gate.ReadinessError):self.validate()
        self.fresh['diagnostics']['execution_policy']=dict(gate.POLICY,unexpected=1)
        with self.assertRaises(gate.ReadinessError):self.validate()

    def test_real_systemd_execstart_shape_and_drift(self):
        gate._effective_unit(properties())
        for field,value in (('MemoryMax','402653184'),('DropInPaths','/extra.conf'),('NeedDaemonReload','yes'),
                            ('ActiveState','inactive'),('ExecStart',properties()['ExecStart'].replace('--serialize-work','--unknown'))):
            value_dict={**properties(),field:value}
            with self.assertRaises(gate.ReadinessError):gate._effective_unit(value_dict)
        with self.assertRaises(gate.ReadinessError):self.validate({**properties(),'MainPID':'123'})

    def test_unique_google_target_used_even_after_unrelated_target(self):
        self.fresh['diagnostics']['targets'].insert(0,{'target_id':'other','provider':'groq','credentials':{'state':'missing'}})
        self.assertEqual(self.validate()['credential_inventory_state'],'unknown')

    def test_observer_uses_only_two_gets_and_safe_projection(self):
        token='fixture-credential-canary-never-live-12345'
        file=Path(self.tmp.name)/'client_token';file.write_text(token);file.chmod(0o600)
        requests=[];responses=[diagnostics(),catalog()]
        class FakeConnection:
            def __init__(self,host,port,timeout):
                self_host=(host,port,timeout);self_outer.assertEqual(self_host,('127.0.0.1',18084,5))
            def request(self,method,path,headers):requests.append((method,path,headers))
            def getresponse(self):return SimpleNamespace(status=200,read=lambda n:json.dumps(responses.pop(0)).encode())
            def close(self):pass
        self_outer=self;output=io.StringIO()
        with patch.dict(os.environ,{'CREDENTIALS_DIRECTORY':self.tmp.name}), \
             patch.object(http.client,'HTTPConnection',FakeConnection), \
             patch.object(signal,'signal'),patch.object(signal,'setitimer'),redirect_stdout(output):
            exec(gate.OBSERVER_CODE,{})
        projected=json.loads(output.getvalue())
        self.assertNotIn(token,output.getvalue())
        self.assertEqual([(m,p)for m,p,h in requests],[('GET','/v1/diagnostics'),('GET','/v1/catalog')])
        self.assertTrue(all(h['Authorization']=='Bearer '+token for m,p,h in requests))
        gate._api(projected['diagnostics'],projected['google_catalog'],NOW,current=True)

    def test_observer_rejects_unknown_numeric_policy_name_without_echo(self):
        token='fixture-credential-canary-never-live-12345'
        file=Path(self.tmp.name)/'client_token';file.write_text(token);file.chmod(0o600)
        bad=diagnostics();bad['execution_policy'][token]=1
        responses=[bad,catalog()]
        connection=SimpleNamespace(request=lambda *a,**k:None,
            getresponse=lambda:SimpleNamespace(status=200,read=lambda n:json.dumps(responses.pop(0)).encode()),close=lambda:None)
        output=io.StringIO()
        with patch.dict(os.environ,{'CREDENTIALS_DIRECTORY':self.tmp.name}), \
             patch.object(http.client,'HTTPConnection',return_value=connection), \
             patch.object(signal,'signal'),patch.object(signal,'setitimer'),redirect_stdout(output):
            with self.assertRaises(SystemExit):exec(gate.OBSERVER_CODE,{})
        self.assertNotIn(token,output.getvalue())
        self.assertEqual(json.loads(output.getvalue()),{'error':'BROKER_READINESS_OBSERVER_FAILED'})

    def test_systemd_run_has_per_service_credential_no_of_files_or_export(self):
        with patch.object(gate,'_run',return_value=b'{}') as run:
            gate._observe()
        argv=run.call_args.args[0]
        self.assertIn('User=orderflow',argv)
        self.assertIn('LoadCredentialEncrypted=client_token:'+str(gate.CREDENTIAL),argv)
        self.assertEqual(argv[-4:],[str(gate.PYTHON),'-I','-c',gate.OBSERVER_CODE])
        self.assertNotIn('PrivateNetwork=yes',argv)
        self.assertFalse(any('morris' in v or '/opt/orderflow' in v for v in argv))

    def test_actual_manifest_payload_package_and_config_hashes_are_checked(self):
        payload=b'pinned module bytes'
        record={'path':'src/quota_broker/__init__.py','size':len(payload),'mode':0o644,
                'sha256':hashlib.sha256(payload).hexdigest()}
        manifest={**self.manifest,'files':[record]}
        manifest_bytes=gate._canonical(manifest)
        config_bytes=b'reviewed config bytes';unit_bytes=b'reviewed unit bytes'
        active={'release':'releases/'+gate.RELEASE.name,'unit_sha256':hashlib.sha256(unit_bytes).hexdigest()}
        receipt={**self.receipt,'release_manifest':manifest,'active_release':active}
        package=gate.RELEASE/'runtime/lib/python3.14/site-packages/quota_broker'
        mapping={gate.RELEASE/'release-manifest.json':manifest_bytes,
            gate.RELEASE/record['path']:payload, package/'__init__.py':payload,
            gate.UNIT:unit_bytes,gate.CONFIG:config_bytes,gate.ACTIVE:gate._canonical(active)}
        def read(path,limit,**kwargs):return mapping[path]
        metadata=SimpleNamespace(st_mode=stat.S_IFREG|0o600,st_uid=0,st_size=42)
        with patch.object(gate,'MANIFEST_SHA',hashlib.sha256(manifest_bytes).hexdigest()),              patch.object(gate,'UNIT_SHA',hashlib.sha256(unit_bytes).hexdigest()),              patch.object(gate,'CONFIG_SHA',hashlib.sha256(config_bytes).hexdigest()),              patch.object(gate,'_trust'),patch.object(gate,'_read_root',side_effect=read),              patch.object(gate,'_unit_properties',return_value=properties()),patch.object(gate,'_effective_unit'),              patch.object(Path,'resolve',return_value=gate.RELEASE),patch.object(Path,'rglob',return_value=[]),              patch.object(Path,'glob',return_value=[package]),patch.object(Path,'lstat',return_value=metadata):
            self.assertEqual(gate._host(receipt)['MainPID'],'473639')
            mapping[package/'__init__.py']=b'changed package'
            with self.assertRaises(gate.ReadinessError):gate._host(receipt)
            mapping[package/'__init__.py']=payload;mapping[gate.CONFIG]=b'changed config'
            with self.assertRaises(gate.ReadinessError):gate._host(receipt)
            mapping[gate.CONFIG]=config_bytes;mapping[gate.RELEASE/record['path']]=b'changed payload'
            with self.assertRaises(gate.ReadinessError):gate._host(receipt)

    def test_non_root_and_observer_failures_do_not_echo_untrusted_content(self):
        with patch.object(gate.os,'geteuid',return_value=1000):
            with self.assertRaises(gate.ReadinessError) as caught:gate.validate_readiness(self.path,gate.RECEIPT_SHA)
        self.assertEqual(caught.exception.code,'BROKER_READINESS_ROOT_REQUIRED')
        with patch.object(gate.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout=b'PRIVATE_CANARY')):
            with self.assertRaises(gate.ReadinessError) as caught:gate._run(['fixture-only'])
        self.assertNotIn('PRIVATE_CANARY',str(caught.exception))

    def test_root_trust_rejects_writable_parent_and_payload_symlink(self):
        safe=SimpleNamespace(st_mode=stat.S_IFDIR|0o755,st_uid=0)
        regular=SimpleNamespace(st_mode=stat.S_IFREG|0o644,st_uid=0)
        def lstat(path):return regular if path==Path('/opt/test') else safe
        with patch.object(Path,'lstat',lstat):gate._trust(Path('/opt/test'))
        bad=SimpleNamespace(st_mode=stat.S_IFDIR|0o777,st_uid=0)
        with patch.object(Path,'lstat',side_effect=[bad]):
            with self.assertRaises(gate.ReadinessError):gate._trust(Path('/opt/test'))
        with patch.object(Path,'lstat',side_effect=[safe,safe,SimpleNamespace(st_mode=stat.S_IFLNK|0o777,st_uid=0)]):
            with self.assertRaises(gate.ReadinessError):gate._trust(Path('/opt/test'))


if __name__=='__main__':unittest.main()
