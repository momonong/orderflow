"""Pinned, content-free Broker readiness gate for the ASUS deployment window.

Only diagnostics/catalog GETs run, through a per-service systemd credential.
This proves the configuration/runtime prerequisites, not PDF token admission,
provider access, official remaining quota, or answer quality.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess

SOURCE = '81725056364b9c88296a38c93a745e90000fa57e'
RECEIPT_SHA = 'fbf4c879f419412198d5716823281db08d7d46777f4da6e31040efa8843d5e1a'
MANIFEST_SHA = 'd7fccdccbd40e6b9c3d25746bf46b2b1ff1b605b3d37f13d7beec7409a655afa'
CONFIG_SHA = '072f1e79252a27fdeaa4fe06fe1c89c31cb95240efbfd227edf1e2f343ae8831'
UNIT_SHA = 'cba27e1e58c4ff2c63b2b0d76c45e291c8815f1c51967217972342f202bf8ab8'
BASE = Path('/opt/api-quota-broker')
RELEASE = BASE / 'releases' / ('release-' + MANIFEST_SHA)
CURRENT = BASE / 'current'
CONFIG = Path('/etc/api-quota-broker/gateway.json')
UNIT = Path('/etc/systemd/system/api-quota-broker.service')
ACTIVE = Path('/etc/api-quota-broker-ops/active-release.json')
CREDENTIAL = Path('/etc/api-quota-broker/credentials/client_token.cred')
PYTHON = Path('/usr/bin/python3')
TARGET = 'asus-google-pool'
MODEL = 'gemini-3.5-flash-lite'
SCOPE = 'google:asus-dev-key:shared'
POLICY = {'serialize_work': True, 'media_input_bytes': 8388608, 'request_bytes': 16777216,
          'result_bytes': 4194304, 'google_pdf_count_tokens': True,
          'count_estimate_ttl_seconds': 120, 'count_margin_percent': 20, 'count_margin_tokens': 256}
CAPS = {('requests', 'rolling_minute'): 3, ('requests', 'day'): 100,
        ('input_tokens', 'rolling_minute'): 131328}
HASH = re.compile(r'[0-9a-f]{64}\Z')


class ReadinessError(Exception):
    def __init__(self, code='BROKER_READINESS_FAILED'):
        self.code = code
        super().__init__(code)


def need(value, code='BROKER_READINESS_FAILED'):
    if not value:
        raise ReadinessError(code)


def _now():
    return datetime.now(timezone.utc)


def _stamp(value):
    need(isinstance(value, str) and len(value) <= 40, 'BROKER_READINESS_TIME_INVALID')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    need(result.tzinfo is not None, 'BROKER_READINESS_TIME_INVALID')
    return result


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, 'BROKER_READINESS_JSON_INVALID')
            result[key] = value
        return result
    def invalid(_value):
        raise ReadinessError('BROKER_READINESS_JSON_INVALID')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def _trust(path: Path, *, symlink=False):
    need(path.is_absolute() and '..' not in path.parts, 'BROKER_READINESS_ROOT_TRUST')
    for parent in reversed(path.parents):
        info = parent.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and info.st_mode & 0o022 == 0,
             'BROKER_READINESS_ROOT_TRUST')
    info = path.lstat()
    need(info.st_uid == 0 and (stat.S_ISLNK(info.st_mode) if symlink else
         (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) and info.st_mode & 0o022 == 0),
         'BROKER_READINESS_ROOT_TRUST')


def _read_root(path: Path, limit: int, *, mode=None):
    _trust(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_size <= limit
             and info.st_mode & 0o022 == 0 and (mode is None or stat.S_IMODE(info.st_mode) == mode),
             'BROKER_READINESS_ROOT_TRUST')
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            value = stream.read(limit + 1)
        need(len(value) <= limit, 'BROKER_READINESS_TOO_LARGE')
        return value
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _run(argv, timeout=25, limit=262144):
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=timeout, check=False, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
    need(result.returncode == 0 and len(result.stdout) <= limit, 'BROKER_READINESS_OBSERVER_FAILED')
    return result.stdout


def _unit_properties():
    names = ('User','Group','WorkingDirectory','FragmentPath','DropInPaths','ActiveState','SubState',
             'NeedDaemonReload','ExecStart','CPUQuotaPerSecUSec','MemoryHigh','MemoryMax','MemorySwapMax',
             'TasksMax','LimitNOFILE','LimitCORE','NoNewPrivileges','MainPID','ExecMainStartTimestampMonotonic')
    raw = _run(['systemctl','show','api-quota-broker.service',
                '--property=' + ','.join(names)], timeout=5, limit=65536).decode('utf-8')
    result = {}
    for line in raw.splitlines():
        key, value = line.split('=', 1)
        need(key in names and key not in result, 'BROKER_READINESS_UNIT_DRIFT')
        result[key] = value
    need(set(result) == set(names), 'BROKER_READINESS_UNIT_DRIFT')
    return result


def _effective_unit(properties):
    expected = {'User':'api-quota-broker','Group':'api-quota-broker',
        'WorkingDirectory':str(CURRENT),'FragmentPath':str(UNIT),'DropInPaths':'',
        'ActiveState':'active','SubState':'running','NeedDaemonReload':'no',
        'CPUQuotaPerSecUSec':'500ms','MemoryHigh':'402653184','MemoryMax':'536870912',
        'MemorySwapMax':'0','TasksMax':'32','LimitNOFILE':'128','LimitCORE':'0','NoNewPrivileges':'yes'}
    need(all(properties.get(key) == value for key,value in expected.items()), 'BROKER_READINESS_UNIT_DRIFT')
    need(properties.get('MainPID','').isdecimal() and int(properties['MainPID']) > 0,
         'BROKER_READINESS_UNIT_DRIFT')
    match = re.fullmatch(r'\{ path=[^;]+ ; argv\[\]=(.*?) ; ignore_errors=[^}]*\}', properties.get('ExecStart',''))
    need(match is not None, 'BROKER_READINESS_UNIT_DRIFT')
    args = shlex.split(match.group(1))
    credential_dir = '/run/credentials/api-quota-broker.service/'
    expected_args = [str(CURRENT/'runtime/bin/python'),'-I','-m','quota_broker.cli','gateway-serve',
        '--config',str(CONFIG),'--db','/var/lib/api-quota-broker/ledger.sqlite3','--port','18084',
        '--digest-key-file',credential_dir+'digest_key','--client-token-file',credential_dir+'client_token',
        '--admin-token-file',credential_dir+'admin_token','--doppler-token-file',credential_dir+'doppler_service_token',
        '--doppler-project','api-quota-broker','--doppler-config','dev',
        '--queue-key-file','/run/api-quota-broker/queue.key','--google-pdf-count-tokens','--serialize-work',
        '--max-media-input-bytes','8388608','--max-request-bytes','16777216','--max-result-bytes','4194304']
    need(args == expected_args, 'BROKER_READINESS_UNIT_DRIFT')


def _host(receipt):
    _trust(CURRENT, symlink=True)
    need(CURRENT.resolve(strict=True) == RELEASE, 'BROKER_READINESS_RELEASE_DRIFT')
    _trust(RELEASE)
    manifest_raw = _read_root(RELEASE/'release-manifest.json', 262144, mode=0o644)
    need(hashlib.sha256(manifest_raw).hexdigest() == MANIFEST_SHA and
         _json(manifest_raw) == receipt['release_manifest'], 'BROKER_READINESS_PAYLOAD_DRIFT')
    for record in receipt['release_manifest']['files']:
        path = Path(record['path'])
        need(not path.is_absolute() and '..' not in path.parts and len(path.parts) > 0,
             'BROKER_READINESS_PAYLOAD_DRIFT')
        raw = _read_root(RELEASE/path, max(record['size'], 1), mode=record['mode'])
        need(len(raw) == record['size'] and hashlib.sha256(raw).hexdigest() == record['sha256'],
             'BROKER_READINESS_PAYLOAD_DRIFT')
    need(hashlib.sha256(_read_root(UNIT, 65536)).hexdigest() == UNIT_SHA,
         'BROKER_READINESS_UNIT_DRIFT')
    need(hashlib.sha256(_read_root(CONFIG, 1048576)).hexdigest() == CONFIG_SHA,
         'BROKER_READINESS_CONFIG_DRIFT')
    need(_json(_read_root(ACTIVE, 65536)) == receipt['active_release'], 'BROKER_READINESS_RELEASE_DRIFT')
    _trust(CREDENTIAL)
    credential = CREDENTIAL.lstat()
    need(stat.S_ISREG(credential.st_mode) and stat.S_IMODE(credential.st_mode)==0o600
         and 0<credential.st_size<=65536, 'BROKER_READINESS_ROOT_TRUST')
    _trust(PYTHON, symlink=PYTHON.is_symlink())
    _trust(PYTHON.resolve(strict=True))
    runtime = RELEASE/'runtime'
    for path in (runtime, *runtime.rglob('*')):
        if path.is_symlink():
            _trust(path, symlink=True)
            _trust(path.resolve(strict=True))
        else:
            _trust(path)
    packages = list((runtime/'lib').glob('python*/site-packages/quota_broker'))
    need(len(packages) == 1, 'BROKER_READINESS_PAYLOAD_DRIFT')
    for record in receipt['release_manifest']['files']:
        if record['path'].startswith('src/quota_broker/'):
            raw = _read_root(packages[0]/record['path'][len('src/quota_broker/'):], max(record['size'],1))
            need(len(raw) == record['size'] and hashlib.sha256(raw).hexdigest() == record['sha256'],
                 'BROKER_READINESS_PAYLOAD_DRIFT')
    before = _unit_properties()
    _effective_unit(before)
    return before


# This program imports no OF/Broker files and exposes no bearer token or raw body.
OBSERVER_CODE = r'''
import os,stat,json,http.client,re,signal
from pathlib import Path
from datetime import datetime

def need(value):
    if not value: raise ValueError()
def date(value):
    if value is None:return None
    need(isinstance(value,str) and len(value)<=40)
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'));need(parsed.tzinfo is not None)
    return parsed.isoformat()
def pick(value,allowed):
    need(isinstance(value,str) and value in allowed);return value
def alarm(*args):raise TimeoutError()
try:
    signal.signal(signal.SIGALRM,alarm);signal.setitimer(signal.ITIMER_REAL,15)
    path=Path(os.environ['CREDENTIALS_DIRECTORY'])/'client_token'
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    info=os.fstat(fd);need(stat.S_ISREG(info.st_mode) and info.st_mode&0o077==0 and info.st_size<=4096)
    with os.fdopen(fd,'rb') as file:token=file.read(4097).decode('ascii').strip()
    need(re.fullmatch(r'[A-Za-z0-9._~+/-]+=*',token) is not None and 32<=len(token)<=4096)
    def get(route):
        connection=http.client.HTTPConnection('127.0.0.1',18084,timeout=5)
        try:
            connection.request('GET',route,headers={'Authorization':'Bearer '+token,'Connection':'close'})
            response=connection.getresponse();need(response.status==200)
            raw=response.read(262145);need(len(raw)<=262144)
            return json.loads(raw)
        finally:connection.close()
    diagnostics=get('/v1/diagnostics');catalog=get('/v1/catalog')
    need(isinstance(diagnostics,dict) and isinstance(catalog,list))
    ds=[v for v in diagnostics['targets'] if v.get('target_id')=='asus-google-pool']
    cs=[v for v in catalog if v.get('target_id')=='asus-google-pool']
    need(len(ds)==len(cs)==1);d,c=ds[0],cs[0]
    need(d['provider']==c['provider']=='google' and d['model']==c['model']=='gemini-3.5-flash-lite')
    allowed_features={'text_generation','text','document_input','structured_output'}
    need(isinstance(c['features'],list) and all(v in allowed_features for v in c['features']))
    limits={k:c['request_limits'][k] for k in ('max_output_tokens','max_input_token_bound_for_requested_output')}
    need(all(type(v)is int for v in limits.values()))
    need(all(d['request_limits'][k]==v and type(d['request_limits'][k])is int for k,v in limits.items()))
    caps=[]
    for q in c['local_safety_caps']:
        need(q['metric'] in {'requests','input_tokens'} and q['window'] in {'rolling_minute','day'})
        need(q['bucket']=='google:asus-dev-key:'+q['metric']+':'+q['window'])
        need(q['timezone'] in {'UTC','America/Los_Angeles'} and type(q['limit'])is int)
        caps.append({k:q[k] for k in ('metric','window','limit','bucket','timezone')})
    need(c['shared_concurrency_scope']=='google:asus-dev-key:shared')
    wanted=('available','configured_available','free_eligible','billing_enabled')
    need(all(type(c[k])is bool for k in wanted))
    need(c['capabilities']==['text_generation'] and c['quota_basis']=='local_safety_cap')
    projected={k:c[k] for k in wanted}
    projected.update(target_id='asus-google-pool',provider='google',model='gemini-3.5-flash-lite',
        features=c['features'],capabilities=c['capabilities'],quota_basis='local_safety_cap',
        local_safety_caps=caps,request_limits=limits,shared_concurrency_scope='google:asus-dev-key:shared')
    for k in ('max_output_tokens','concurrency_limit','shared_concurrency_limit'):
        need(type(c[k])is int);projected[k]=c[k]
    for k in ('account_verified_at','account_expires_at'):projected[k]=date(c[k])
    projected['capacity']={'expires_at':date(c['capacity']['expires_at'])}
    reasons=[] if not d['reasons'] else (['credential_inventory_unknown'] if d['reasons']==['credential_inventory_unknown'] else ['other_blocker'])
    worker=diagnostics['queue_worker'];need(all(type(worker[k])is bool for k in ('enabled','running','stopped')))
    worker_out={k:worker[k] for k in ('enabled','running','stopped')}
    worker_out.update(error_code=None if worker['error_code'] is None else 'worker_error',last_tick_at=date(worker['last_tick_at']))
    policy=diagnostics['execution_policy']
    wanted_policy={'serialize_work':True,'media_input_bytes':8388608,'request_bytes':16777216,'result_bytes':4194304,
        'google_pdf_count_tokens':True,'count_estimate_ttl_seconds':120,'count_margin_percent':20,'count_margin_tokens':256}
    need(policy==wanted_policy and all(type(policy[k])is type(v) for k,v in wanted_policy.items()))
    policy={k:policy[k] for k in wanted_policy}
    output={'diagnostics':{'as_of':date(diagnostics['as_of']),'basis':pick(diagnostics['basis'],{'local_minimum_admission_snapshot'}),
        'execution_policy':policy,'queue_worker':worker_out,'targets':[{'target_id':'asus-google-pool','provider':'google',
        'model':'gemini-3.5-flash-lite','state':pick(d['state'],{'ready','unknown','blocked','waiting'}),'reasons':reasons,
        'credentials':{'state':pick(d['credentials']['state'],{'unknown','present','missing'})},
        'cooldown_until':date(d['cooldown_until']),'next_retry_at':date(d['next_retry_at']),
        'request_limits':limits}]},'google_catalog':[projected]}
    signal.setitimer(signal.ITIMER_REAL,0)
    print(json.dumps(output,sort_keys=True,separators=(',',':'),allow_nan=False))
except BaseException:
    print('{"error":"BROKER_READINESS_OBSERVER_FAILED"}')
    raise SystemExit(1)
'''


def _observe():
    return _json(_run(['systemd-run','--quiet','--wait','--collect','--pipe',
        '-p','User=orderflow','-p','Group=orderflow','-p','UMask=0077',
        '-p','NoNewPrivileges=yes','-p','ProtectSystem=strict','-p','ProtectHome=yes',
        '-p','PrivateTmp=yes','-p','PrivateDevices=yes','-p','MemoryMax=64M','-p','MemorySwapMax=0',
        '-p','LimitCORE=0','-p','RuntimeMaxSec=20',
        '-p','LoadCredentialEncrypted=client_token:'+str(CREDENTIAL),
        str(PYTHON),'-I','-c',OBSERVER_CODE]))


def _api(diagnostics, catalog, now, *, current=False):
    need(isinstance(diagnostics,dict) and diagnostics.get('basis') == 'local_minimum_admission_snapshot',
         'BROKER_READINESS_API_INVALID')
    need(diagnostics.get('execution_policy') == POLICY and all(type(diagnostics['execution_policy'][k]) is type(v) for k,v in POLICY.items()),
         'BROKER_READINESS_POLICY_DRIFT')
    worker = diagnostics['queue_worker']
    need(worker.get('enabled') is True and worker.get('running') is True and worker.get('stopped') is False
         and worker.get('error_code') is None, 'BROKER_READINESS_WORKER_UNAVAILABLE')
    as_of, tick = _stamp(diagnostics['as_of']), _stamp(worker['last_tick_at'])
    need(as_of <= now and tick <= now and (not current or
         (now-as_of).total_seconds()<=60 and (now-tick).total_seconds()<=240), 'BROKER_READINESS_API_STALE')
    selected = [v for v in diagnostics['targets'] if v.get('target_id') == TARGET]
    google = [v for v in catalog if v.get('target_id') == TARGET]
    need(len(selected)==len(google)==1, 'BROKER_READINESS_TARGET_INVALID')
    target, model = selected[0],google[0]
    need(target['provider']==model['provider']=='google' and target['model']==model['model']==MODEL,
         'BROKER_READINESS_TARGET_INVALID')
    need(target['state'] in {'ready','unknown'} and target['reasons'] in ([],['credential_inventory_unknown'])
         and target['cooldown_until'] is None and target['next_retry_at'] is None,
         'BROKER_READINESS_TARGET_BLOCKED')
    need(model['available'] is True and model['configured_available'] is True and model['free_eligible'] is True
         and model['billing_enabled'] is False and model['capabilities']==['text_generation']
         and {'document_input','structured_output'} <= set(model['features']), 'BROKER_READINESS_TARGET_INVALID')
    verified,account,capacity = _stamp(model['account_verified_at']),_stamp(model['account_expires_at']),_stamp(model['capacity']['expires_at'])
    need(verified <= now < min(account,capacity), 'BROKER_READINESS_ACCOUNT_EXPIRED')
    need(type(model['max_output_tokens'])is int and model['max_output_tokens']==8192
         and model['concurrency_limit']==model['shared_concurrency_limit']==1
         and type(model['concurrency_limit'])is int and type(model['shared_concurrency_limit'])is int
         and model['shared_concurrency_scope']==SCOPE and model['quota_basis']=='local_safety_cap',
         'BROKER_READINESS_TARGET_INVALID')
    limits=model['request_limits']
    need(type(limits['max_output_tokens'])is int and type(limits['max_input_token_bound_for_requested_output'])is int
         and target['request_limits']['max_output_tokens']==8192
         and target['request_limits']['max_input_token_bound_for_requested_output']==131328
         and limits['max_output_tokens']==8192 and limits['max_input_token_bound_for_requested_output']==131328,
         'BROKER_READINESS_TARGET_INVALID')
    caps=model['local_safety_caps']; observed={}
    for q in caps:
        key=(q['metric'],q['window'])
        need(key in CAPS and key not in observed and type(q['limit'])is int and q['limit']==CAPS[key]
             and q['bucket']=='google:asus-dev-key:'+key[0]+':'+key[1]
             and q['timezone']==('America/Los_Angeles' if key==('requests','day') else 'UTC'),
             'BROKER_READINESS_QUOTA_DRIFT')
        observed[key]=q['limit']
    need(observed==CAPS, 'BROKER_READINESS_QUOTA_DRIFT')
    return model


def validate_readiness(receipt_path: Path, expected_sha: str) -> dict:
    """Root-only host/runtime + fresh authenticated metadata checks; no dispatch."""
    try:
        need(os.geteuid()==0, 'BROKER_READINESS_ROOT_REQUIRED')
        need(expected_sha==RECEIPT_SHA, 'BROKER_READINESS_RECEIPT_HASH')
        raw=_read_root(Path(receipt_path),1048576)
        need(hashlib.sha256(raw).hexdigest()==expected_sha, 'BROKER_READINESS_RECEIPT_HASH')
        receipt=_json(raw)
        now=_now(); observed,expiry=_stamp(receipt['observed_at']),_stamp(receipt['expires_at'])
        need(observed<=now<expiry and 0<(expiry-observed).total_seconds()<=7201,
             'BROKER_READINESS_WINDOW_EXPIRED')
        manifest=receipt['release_manifest']
        need(manifest['schema_version']==1 and manifest['policy']=='asus-release-v1'
             and manifest['project']=='api-quota-broker' and manifest['source']['head']==SOURCE
             and manifest['source']['payload_dirty'] is False and manifest['source']['included_changes']==[]
             and hashlib.sha256(_canonical(manifest)).hexdigest()==MANIFEST_SHA,
             'BROKER_READINESS_SOURCE_INVALID')
        need(receipt['current']==str(RELEASE) and receipt['config_sha256']==CONFIG_SHA
             and receipt['unit_sha256']==UNIT_SHA and receipt['provider_posts_in_this_read']==0
             and receipt['active_release']=={'release':'releases/'+RELEASE.name,'unit_sha256':UNIT_SHA},
             'BROKER_READINESS_SOURCE_INVALID')
        _api(receipt['diagnostics'],receipt['google_catalog'],observed)
        before=_host(receipt)
        fresh=_observe()
        model=_api(fresh['diagnostics'],fresh['google_catalog'],_now(),current=True)
        after=_unit_properties();_effective_unit(after)
        need(before['MainPID']==after['MainPID'] and before['ExecMainStartTimestampMonotonic']==after['ExecMainStartTimestampMonotonic'],
             'BROKER_READINESS_RESTARTED')
        need(CURRENT.resolve(strict=True)==RELEASE and _now()<expiry, 'BROKER_READINESS_WINDOW_EXPIRED')
        return {'source_sha':SOURCE,'broker_release':str(RELEASE),'receipt_sha256':expected_sha,
                'config_sha256':CONFIG_SHA,'unit_sha256':UNIT_SHA,'manifest_sha256':MANIFEST_SHA,
                'observed_at':observed.isoformat(),'expires_at':expiry.isoformat(),
                'capacity_expires_at':model['capacity']['expires_at'],'account_expires_at':model['account_expires_at'],
                'bound_valid_until':min(_stamp(model['capacity']['expires_at']),_stamp(model['account_expires_at'])).isoformat(),
                'target_id':TARGET,'model':MODEL,'max_output_tokens':8192,'execution_policy':dict(POLICY),
                'local_caps':{'requests_per_minute':3,'requests_per_day':100,'input_tokens_per_minute':131328,'concurrency':1},
                'quota_basis':'local_safety_cap','account_remaining_known':False,'pdf_admission_ready':False,
                'credential_inventory_state':next(v for v in fresh['diagnostics']['targets'] if v['target_id']==TARGET)['credentials']['state']}
    except ReadinessError:
        raise
    except Exception:
        raise ReadinessError() from None
