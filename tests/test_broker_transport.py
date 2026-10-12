"""Offline wrapper gates: no SSH, Doppler, sudo or real packet is used."""
import ast
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shlex
import tempfile
import types
import unittest
from unittest.mock import patch


FILE = Path(__file__).resolve().parents[1] / 'deploy/transport-broker-integration.py'
SPEC = importlib.util.spec_from_file_location('of_transport_fixture', FILE)
wrapper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wrapper)
SOURCE = 'a' * 40
MEMBERS = {'source.tar', 'broker.json', 'ready.json', 'run-broker-integration.py',
           'upgrade-asus-broker.py', 'upgrade-asus-shared-trial.py', 'broker_readiness.py'}

# This fake dependency has the pinned helper's API shape and performs no I/O
# besides exclusive fixture receipts. Production never accepts its fake pin.
FAKE = b'''import json,shlex
from pathlib import Path
HOST='asus-ubuntu2604-server'
READY={}
calls=[]
def need(ok):
    if not ok: raise ValueError('fixture gate')
def guard(): pass
def check_secret_name(): calls.append('names_only')
def remote_argv(pin):
    return ['fixture-ssh',shlex.join(['fixture-systemd','-p','RuntimeMaxSec=200','-c','#fixture\\nsys.exit(remote_main())'])]
def exclusive_json(path,value):
    with Path(path).open('x') as stream: json.dump(value,stream)
def exchange(argv,**kwargs):
    calls.append(('exchange',argv))
    return {'state':'unknown','diagnostic':{'stage':'result_framing','code':'timeout','rc':None,'timeout':True,'possible_dispatch':True}}
def failure(error,stage,possible):
    return {'state':'unknown' if possible else 'blocked','diagnostic':{'stage':stage,'code':'gate_rejected','rc':None,'timeout':False,'possible_dispatch':possible}}
def emit(value): calls.append(('emit',value))
'''


def result():
    return {'deployment': 'passed', 'acceptance_complete': True, 'old_rows_and_files': 'preserved',
            'synthetic': {'job_id': '11111111-1111-4111-8111-111111111111',
                          'document_id': '22222222-2222-4222-8222-222222222222',
                          'request_key': 'of-smoke-' + 'b' * 32, 'state': 'done', 'phase': 'complete',
                          'pdf_sha256': 'c' * 64, 'task_sha256': 'd' * 64, 'source_sha': SOURCE,
                          'max_output_tokens': 2048, 'gateway_state': 'completed', 'finish_reason': 'stop',
                          'response_truncated': False, 'usage_unknown': False, 'expected_items_match': True}}


class TransportGateTests(unittest.TestCase):
    def test_unpinned_dependency_is_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'broker-transport.py').write_bytes(b'raise AssertionError("must not execute")')
            args = ['fixture', '--packet', temp, '--expected-sha256', hashlib.sha256(FILE.read_bytes()).hexdigest(),
                    '--manifest-sha256', '0' * 64]
            with patch('sys.argv', args), patch.object(wrapper, 'exec', create=True) as execute:
                with self.assertRaisesRegex(SystemExit, 'pinned PAM transport mismatch'):
                    wrapper.main()
            execute.assert_not_called()
            self.assertFalse((root / 'transport.claim.json').exists())

    def test_bootstrap_rejects_bad_source_and_members_before_opening_packet(self):
        for source, members in (( '../outside', MEMBERS), (SOURCE, MEMBERS | {'extra.py'})):
            with self.subTest(source=source, members=members):
                manifest = {'source': source, 'files': {name: '0' * 64 for name in members}}
                code = wrapper.bootstrap(manifest, '0' * 64)
                output = io.StringIO()
                with patch('os.umask'), patch('os.getuid', return_value=0), patch('os.uname', return_value=types.SimpleNamespace(nodename='asus-ubuntu2604-server')), \
                     patch.object(Path, 'lstat') as read_metadata, patch('os.open') as open_packet, contextlib.redirect_stdout(output):
                    exec(code, {})
                read_metadata.assert_not_called()
                open_packet.assert_not_called()
                self.assertEqual(json.loads(output.getvalue())['deployment'], 'unverified')

    def test_unknown_claim_does_not_replay_and_private_tmp_has_exact_packet_bind(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'broker-transport.py').write_bytes(FAKE)
            manifest = {'source': SOURCE, 'files': {name: '0' * 64 for name in MEMBERS}}
            raw = json.dumps(manifest).encode()
            (root / 'manifest.json').write_bytes(raw)
            modules = []
            real_module = types.ModuleType
            def make_module(name):
                created = real_module(name)
                modules.append(created)
                return created
            args = ['fixture', '--packet', temp, '--expected-sha256', hashlib.sha256(FILE.read_bytes()).hexdigest(),
                    '--manifest-sha256', hashlib.sha256(raw).hexdigest()]
            with patch('sys.argv', args), patch.object(wrapper, 'TRANSPORT_SHA', hashlib.sha256(FAKE).hexdigest()), \
                 patch.object(wrapper.types, 'ModuleType', side_effect=make_module):
                self.assertEqual(wrapper.main(), 1)
                self.assertEqual(wrapper.main(), 1)
            exchanges = [call for module in modules for call in module.calls if isinstance(call, tuple) and call[0] == 'exchange']
            self.assertEqual(len(exchanges), 1, 'an unknown receipt never re-enters SSH/sudo')
            self.assertEqual(json.loads((root / 'transport.result.json').read_text())['state'], 'unknown')
            remote_code = shlex.split(exchanges[0][1][-1])[-1]
            root_argv = next(ast.literal_eval(node.value) for node in ast.parse(remote_code).body
                             if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'ROOT_ARGV' for target in node.targets))
            self.assertIn('PrivateTmp=yes', root_argv)
            self.assertIn('BindReadOnlyPaths=/var/tmp/orderflow-broker-' + SOURCE, root_argv)
            self.assertNotIn('ReadWritePaths=/opt/orderflow /var/lib ', root_argv)

    def test_projection_bounds_values_and_refuses_false_acceptance(self):
        self.assertTrue(wrapper.projection(result())['acceptance_complete'])
        for field, value in (('finish_reason', {'raw': 'synthetic private marker'}),
                             ('gateway_state', 'synthetic private marker'),
                             ('job_id', 'synthetic private marker'), ('request_key', {'token': 'synthetic private marker'}),
                             ('pdf_sha256', 'bad hash'), ('max_output_tokens', 8192), ('usage_unknown', 'false'),
                             ('response_truncated', True), ('expected_items_match', False), ('state', 'unknown')):
            with self.subTest(field=field, value=value):
                changed = result()
                changed['synthetic'][field] = value
                with self.assertRaises((ValueError, TypeError)):
                    wrapper.projection(changed)
        unknown = wrapper.projection({'deployment': 'unverified', 'acceptance_complete': False,
                                      'raw_stderr': 'synthetic private marker'})
        self.assertNotIn('synthetic private marker', json.dumps(unknown))
        with self.assertRaises((ValueError, TypeError)):
            wrapper.projection({'deployment': 'unverified', 'acceptance_complete': True})
        with self.assertRaises((ValueError, TypeError)):
            wrapper.projection({'deployment': 'unverified', 'acceptance_complete': 'false'})
        usage_unknown = result()
        usage_unknown['synthetic']['gateway_state'] = 'completed_usage_unknown'
        with self.assertRaises((ValueError, TypeError)):
            wrapper.projection(usage_unknown)
        usage_unknown['synthetic']['usage_unknown'] = True
        self.assertTrue(wrapper.projection(usage_unknown)['synthetic']['usage_unknown'])


if __name__ == '__main__':
    unittest.main()
