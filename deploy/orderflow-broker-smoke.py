#!/usr/bin/env python3
"""Prepare one durable synthetic job; readiness uses metadata GETs only.

Root CLI validates a stopped writer before Store initialization. A private intent
fixes the owner/document/request keys before any DB/PDF write. Existing jobs are
read without Store initialization, including while the real worker is running.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid

from orderflow.app import Store, ROOT, MAX_STORED_BYTES, MAX_DOCUMENTS_PER_SESSION
from orderflow.broker import BrokerConfig, make_task, MIB
from orderflow.broker_jobs import canonical

TOKEN_PATH = Path('/run/credentials/orderflow.service/client_token')
SHA = re.compile(r'[0-9a-f]{64}\Z')
COMMIT = re.compile(r'[0-9a-f]{40}\Z')
ID = re.compile(r'[A-Za-z0-9._~-]{1,120}\Z')


class SmokeError(Exception):
    def __init__(self, code='SMOKE_GATE_FAILED'):
        super().__init__(code)
        self.code = code


def require(condition, code='SMOKE_GATE_FAILED'):
    if not condition:
        raise SmokeError(code)


def synthetic_pdf() -> bytes:
    lines = (
        'SYNTHETIC INVOICE - NO PRIVATE DATA', 'Invoice No: OF-SYNTHETIC-ONE',
        'Billed Customer: SYNTHETIC CUSTOMER', 'Date: 2026-10-12',
        'Currency: USD', 'Product: SYNTHETIC WIDGET', 'Code: SYN-001',
        'Quantity: 2', 'Unit: piece', 'Unit Price: 3.50', 'Amount: 7.00',
    )
    commands = ['BT /F1 14 Tf 50 780 Td']
    for index, text in enumerate(lines):
        if index:
            commands.append('0 -24 Td')
        commands.append('(' + text + ') Tj')
    content = ('\n'.join(commands) + '\nET\n').encode('ascii')
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Length ' + str(len(content)).encode() + b' >>\nstream\n' + content + b'endstream']
    result = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(str(number).encode() + b' 0 obj\n' + value + b'\nendobj\n')
    xref = len(result)
    result.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets[1:]:
        result.extend(f'{offset:010d} 00000 n \n'.encode())
    result.extend(f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(result)


PDF = synthetic_pdf()
PDF_SHA256 = hashlib.sha256(PDF).hexdigest()
EXPECTED_TEXT = {'invoiceNo': 'OF-SYNTHETIC-ONE', 'client': 'SYNTHETIC CUSTOMER',
                 'product': 'SYNTHETIC WIDGET', 'code': 'SYN-001', 'currency': 'USD',
                 'date': '2026-10-12', 'unit': 'piece'}
EXPECTED_DECIMALS = {'qty': '2', 'unitPrice': '3.50', 'amount': '7.00'}


@contextmanager
def writer_identity(identity):
    """Use the service owner for DB/WAL/PDF; restore root before intent writes."""
    if identity is None:
        yield
        return
    uid, gid = identity
    saved_uid, saved_gid = os.geteuid(), os.getegid()
    try:
        os.setegid(gid)
        os.seteuid(uid)
        yield
    finally:
        os.seteuid(saved_uid)
        os.setegid(saved_gid)


def private_file(path: Path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
            and info.st_mode & 0o077 == 0, 'SMOKE_PRIVATE_FILE_INVALID')


def atomic_private(path: Path, data: bytes):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name + '-', delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def config_shape(config: BrokerConfig):
    require(config.token_file == TOKEN_PATH and config.endpoint == 'http://127.0.0.1:18084'
            and config.model == 'gemini-3.5-flash-lite'
            and (config.max_pdf_bytes, config.max_request_bytes, config.max_result_bytes,
                 config.max_output_tokens) == (8*MIB, 16*MIB, 4*MIB, 8192), 'SMOKE_CONFIG_INVALID')


def load_intent(path: Path) -> dict:
    private_file(path)
    require(path.stat().st_size <= 65536, 'SMOKE_INTENT_INVALID')
    value = json.loads(path.read_text())
    required = {'version', 'source_sha', 'owner_id', 'document_id', 'upload_key', 'job_key',
                'request_key', 'pdf_sha256', 'config', 'task_sha256'}
    require(isinstance(value, dict) and set(value) == required and value['version'] == 1,
            'SMOKE_INTENT_INVALID')
    require(COMMIT.fullmatch(value['source_sha']) and value['pdf_sha256'] == PDF_SHA256
            and SHA.fullmatch(value['task_sha256']), 'SMOKE_INTENT_INVALID')
    for field in ('owner_id', 'document_id', 'upload_key', 'job_key', 'request_key'):
        require(isinstance(value[field], str) and ID.fullmatch(value[field]), 'SMOKE_INTENT_INVALID')
    config_shape(BrokerConfig.from_snapshot(value['config']))
    return value


def expected_items_match(raw: object) -> bool:
    if not isinstance(raw, str):
        return False
    try:
        rows = json.loads(raw)
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            return False
        row = rows[0]
        if any(row.get(field) != value for field, value in EXPECTED_TEXT.items()):
            return False
        for field, expected in EXPECTED_DECIMALS.items():
            value = row.get(field)
            if not isinstance(value, str) or len(value) > 200:
                return False
            number = Decimal(value)
            if not number.is_finite() or number != Decimal(expected):
                return False
        return True
    except (ValueError, TypeError, InvalidOperation):
        return False


def lookup(data_dir: Path, intent: dict) -> dict | None:
    with closing(sqlite3.connect(f'file:{data_dir / "orderflow.sqlite3"}?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('''SELECT j.id,j.state,j.scenario,b.phase,b.request_key,b.task_sha256,
                            b.gateway_state,b.execution_key,b.finish_reason,b.response_truncated,b.usage_unknown,
                            CASE WHEN j.state='done' AND length(j.result)<=65536 THEN j.result END AS match_result,
                            b.task_json,b.config_json,
                            b.session_id AS broker_owner,b.document_id AS broker_document,
                            d.session_id,d.sha256,d.size,d.purpose,d.document_kind
                            FROM jobs j JOIN broker_jobs b ON b.job_id=j.id
                            JOIN documents d ON d.id=j.document_id
                            WHERE j.session_id=? AND j.document_id=? AND j.request_key=?''',
                         (intent['owner_id'], intent['document_id'], intent['job_key'])).fetchone()
    if row is None:
        return None
    require(row['session_id'] == intent['owner_id'] and row['request_key'] == intent['request_key']
            and row['task_sha256'] == intent['task_sha256'] and row['sha256'] == PDF_SHA256
            and row['size'] == len(PDF) and row['scenario'] == 'real'
            and row['purpose'] == 'management' and row['document_kind'] == 'invoice'
            and row['broker_owner'] == intent['owner_id']
            and row['broker_document'] == intent['document_id']
            and hashlib.sha256(row['task_json'].encode()).hexdigest() == intent['task_sha256'],
            'SMOKE_MAPPING_CONFLICT')
    saved_config = BrokerConfig.from_snapshot(json.loads(row['config_json']))
    expected_config = replace(BrokerConfig.from_snapshot(intent['config']), max_output_tokens=2048)
    require(saved_config.snapshot() == expected_config.snapshot(), 'SMOKE_MAPPING_CONFLICT')
    file = data_dir / 'files' / (intent['document_id'] + '.pdf')
    require(file.is_file() and not file.is_symlink() and file.stat().st_size == len(PDF)
            and hashlib.sha256(file.read_bytes()).hexdigest() == PDF_SHA256,
            'SMOKE_PDF_CONFLICT')
    require(ID.fullmatch(row['id']), 'SMOKE_MAPPING_CONFLICT')
    state = row['state'] if row['state'] in {'queued', 'running', 'unknown', 'failed', 'done'} else 'unknown'
    phase = row['phase'] if row['phase'] in {'prepared', 'submitting', 'pending', 'unknown', 'complete', 'failed', 'expired', 'busy_paused'} else 'unknown'
    if isinstance(row['phase'], str) and re.fullmatch(r'(busy|submitting)_(0|[1-9]|[12][0-9]|3[0-2])', row['phase']):
        phase = row['phase']
    gateway = row['gateway_state'] if row['gateway_state'] in {'completed', 'completed_usage_unknown', 'unknown', 'failed', 'rejected', 'quota_exhausted'} else None
    finish = row['finish_reason'] if row['finish_reason'] in {'stop', 'length', 'max_tokens', 'tool_calls', 'content_filter', 'safety', 'recitation', 'missing', 'unclassified'} else None
    truncated = bool(row['response_truncated']) if type(row['response_truncated']) is int and row['response_truncated'] in (0,1) else None
    usage_unknown = not (gateway == 'completed' and type(row['usage_unknown']) is int and row['usage_unknown'] == 0)
    matched = expected_items_match(row['match_result']) if state == 'done' else None
    return {'job_id': row['id'], 'document_id': intent['document_id'],
            'request_key': intent['request_key'], 'state': state, 'phase': phase,
            'pdf_sha256': PDF_SHA256, 'task_sha256': intent['task_sha256'],
            'source_sha': intent['source_sha'], 'max_output_tokens': 2048,
            'gateway_state': gateway, 'finish_reason': finish, 'response_truncated': truncated,
            'usage_unknown': usage_unknown, 'expected_items_match': matched}


def prepare_document(store: Store, intent: dict, fault):
    """Fixed file identity also covers fsync-to-DB-commit crashes."""
    document_path = store.files / (intent['document_id'] + '.pdf')
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        require(not db.execute('SELECT 1 FROM session_auth WHERE session_id=?', (intent['owner_id'],)).fetchone(),
                'SMOKE_OWNER_CONFLICT')
        if not db.execute('SELECT 1 FROM sessions WHERE id=?', (intent['owner_id'],)).fetchone():
            # No session_auth entry and no retained token: this owner cannot log in.
            db.execute('INSERT INTO sessions VALUES (?,?,?)',
                       (intent['owner_id'], hashlib.sha256(secrets.token_bytes(32)).hexdigest(), int(time.time()*1000)))
    fault('after_owner')
    if document_path.exists():
        require(document_path.is_file() and not document_path.is_symlink()
                and document_path.stat().st_size == len(PDF)
                and hashlib.sha256(document_path.read_bytes()).hexdigest() == PDF_SHA256,
                'SMOKE_PDF_CONFLICT')
    else:
        atomic_private(document_path, PDF)
    fault('after_pdf')
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT session_id,request_key,sha256,size,purpose,document_kind FROM documents WHERE id=?',
                              (intent['document_id'],)).fetchone()
        stored_key = 'management:' + intent['upload_key']
        if existing:
            require(tuple(existing) == (intent['owner_id'], stored_key, PDF_SHA256, len(PDF), 'management', 'invoice'),
                    'SMOKE_MAPPING_CONFLICT')
        else:
            total = db.execute('SELECT COALESCE(SUM(size),0) FROM documents').fetchone()[0]
            count = db.execute('SELECT COUNT(*) FROM documents WHERE session_id=? AND purpose=\'management\'',
                               (intent['owner_id'],)).fetchone()[0]
            require(total + len(PDF) <= MAX_STORED_BYTES and count < MAX_DOCUMENTS_PER_SESSION, 'SMOKE_STORAGE_LIMIT')
            db.execute('''INSERT INTO documents
                (id,session_id,request_key,size,sha256,created_ms,upload_ms,steps,page_count,purpose,document_kind)
                VALUES (?,?,?,?,?,?,0,?,1,'management','invoice')''',
                (intent['document_id'], intent['owner_id'], stored_key, len(PDF), PDF_SHA256,
                 int(time.time()*1000), canonical({'upload': 'pass', 'integrity': 'pass'})))
            db.execute('INSERT INTO management_upload_keys VALUES (?,?,?,?,?,?)',
                       (intent['owner_id'], stored_key, intent['document_id'], PDF_SHA256, len(PDF), 'invoice'))
    fault('after_document')


def write_receipt(path: Path, receipt: dict):
    if path.exists() or path.is_symlink():
        private_file(path)
        require(path.stat().st_size <= 65536, 'SMOKE_RECEIPT_CONFLICT')
        previous = json.loads(path.read_text())
        require(isinstance(previous, dict), 'SMOKE_RECEIPT_CONFLICT')
        for key in ('job_id', 'document_id', 'request_key', 'pdf_sha256', 'task_sha256', 'source_sha', 'max_output_tokens'):
            require(previous.get(key) == receipt[key], 'SMOKE_RECEIPT_CONFLICT')
    atomic_private(path, canonical(receipt).encode())


def create_or_recover(data_dir: Path, intent_path: Path, config: BrokerConfig, source_sha: str,
                      *, require_stopped, require_ready, identity=None, fault=lambda _phase: None) -> dict:
    require(COMMIT.fullmatch(source_sha), 'SMOKE_SOURCE_INVALID')
    parent = intent_path.parent
    info = parent.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and info.st_mode & 0o077 == 0, 'SMOKE_PRIVATE_FILE_INVALID')
    lock = os.open(str(intent_path) + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        lock_info = os.fstat(lock)
        require(stat.S_ISREG(lock_info.st_mode) and lock_info.st_uid == os.geteuid()
                and lock_info.st_mode & 0o077 == 0, 'SMOKE_PRIVATE_FILE_INVALID')
        fcntl.flock(lock, fcntl.LOCK_EX)
        if intent_path.exists() or intent_path.is_symlink():
            intent = load_intent(intent_path)
            require(intent['source_sha'] == source_sha, 'SMOKE_SOURCE_CONFLICT')
            original = BrokerConfig.from_snapshot(intent['config'])
            with writer_identity(identity):
                existing = lookup(data_dir, intent)
            if existing:
                write_receipt(intent_path.with_suffix('.receipt.json'), existing)
                return existing
        else:
            config_shape(config)
            require_stopped()
            require_ready(config, source_sha)
            require(config.bound_ready(), 'SMOKE_BOUND_REQUIRED')
            intent = {'version': 1, 'source_sha': source_sha, 'owner_id': str(uuid.uuid4()),
                      'document_id': str(uuid.uuid4()), 'upload_key': str(uuid.uuid4()),
                      'job_key': str(uuid.uuid4()), 'request_key': 'of-smoke-' + uuid.uuid4().hex,
                      'pdf_sha256': PDF_SHA256, 'config': config.snapshot()}
            task = make_task(replace(config, max_output_tokens=2048), PDF, 'invoice', intent['request_key'])
            intent['task_sha256'] = hashlib.sha256(canonical(task).encode()).hexdigest()
            atomic_private(intent_path, canonical(intent).encode())
        fault('after_intent')
        require_stopped()
        original = BrokerConfig.from_snapshot(intent['config'])
        require(config.snapshot() == original.snapshot(), 'SMOKE_CONFIG_CONFLICT')
        config_shape(config)
        require_ready(original, source_sha)
        require(original.bound_ready(), 'SMOKE_BOUND_REQUIRED')
        smoke_config = replace(original, max_output_tokens=2048)
        task = make_task(smoke_config, PDF, 'invoice', intent['request_key'])
        require(hashlib.sha256(canonical(task).encode()).hexdigest() == intent['task_sha256'], 'SMOKE_MAPPING_CONFLICT')
        pdf_copy = intent_path.with_suffix('.pdf')
        if pdf_copy.exists() or pdf_copy.is_symlink():
            private_file(pdf_copy)
            require(pdf_copy.stat().st_size == len(PDF) and pdf_copy.read_bytes() == PDF, 'SMOKE_PDF_CONFLICT')
        else:
            atomic_private(pdf_copy, PDF)
        with writer_identity(identity):
            # This is the first Store initialization; the guard ran as root first.
            store = Store(data_dir)
            store.public_limits = True
            prepare_document(store, intent, fault)
            store.add_job(intent['owner_id'], intent['document_id'], intent['job_key'], 'real', 'management',
                          broker_plan=(task, smoke_config))
            fault('after_job')
            receipt = lookup(data_dir, intent)
        require(receipt is not None, 'SMOKE_MAPPING_CONFLICT')
        fault('before_receipt')
        write_receipt(intent_path.with_suffix('.receipt.json'), receipt)
        return receipt
    finally:
        os.close(lock)


def stopped_guard(data_dir: Path):
    result = subprocess.run(['systemctl', 'show', 'orderflow', '-p', 'ActiveState', '--value'],
                            capture_output=True, text=True, timeout=5)
    require(result.returncode == 0 and result.stdout.strip() == 'inactive', 'SMOKE_WRITER_ACTIVE')
    account = pwd.getpwnam('orderflow')
    for process in Path('/proc').iterdir():
        if process.name.isdecimal():
            try:
                require(process.stat().st_uid != account.pw_uid, 'SMOKE_WRITER_ACTIVE')
            except FileNotFoundError:
                continue
    with writer_identity((account.pw_uid, account.pw_gid)), closing(
            sqlite3.connect(f'file:{data_dir / "orderflow.sqlite3"}?mode=ro', uri=True)) as db:
        require(db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] == 0,
                'SMOKE_PENDING_JOBS')


def reviewed_readiness(path: Path, expected_sha: str, config: BrokerConfig, source_sha: str):
    require(isinstance(expected_sha, str) and SHA.fullmatch(expected_sha), 'SMOKE_READINESS_INVALID')
    require(COMMIT.fullmatch(source_sha) and config.bound_ready(), 'SMOKE_READINESS_INVALID')
    try:
        spec = importlib.util.spec_from_file_location('smoke_readiness', Path(__file__).with_name('broker_readiness.py'))
        readiness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(readiness)
        checked = readiness.validate_readiness(path, expected_sha)
    except Exception:
        raise SmokeError('SMOKE_READINESS_FAILED') from None
    require(config.input_bound_evidence == 'broker-pdf-' + readiness.SOURCE
            and config._bound_expiry() <= datetime.fromisoformat(checked['bound_valid_until']),
            'SMOKE_READINESS_CONFIG_MISMATCH')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path('/var/lib/orderflow'))
    parser.add_argument('--config', type=Path, default=Path('/etc/orderflow/broker.json'))
    parser.add_argument('--intent', type=Path, required=True)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--readiness-receipt', type=Path, required=True)
    parser.add_argument('--readiness-sha256', required=True)
    args = parser.parse_args()
    require(os.geteuid() == 0, 'SMOKE_ROOT_REQUIRED')
    current = Path('/opt/orderflow/current').resolve(strict=True)
    require(current.name == args.source_sha and ROOT.resolve() == current, 'SMOKE_SOURCE_INVALID')
    account = pwd.getpwnam('orderflow')
    info = args.data_dir.stat()
    require(args.data_dir.is_dir() and not args.data_dir.is_symlink() and info.st_uid == account.pw_uid
            and info.st_mode & 0o077 == 0, 'SMOKE_STATE_INVALID')
    require(args.config.stat().st_size <= 65536, 'SMOKE_CONFIG_INVALID')
    supplied = BrokerConfig.from_snapshot(json.loads(args.config.read_text()))
    require(supplied.token_file in {None, TOKEN_PATH}, 'SMOKE_CONFIG_INVALID')
    config = replace(supplied, token_file=TOKEN_PATH)
    for path, directory in ((args.data_dir / 'files', True), (args.data_dir / 'orderflow.sqlite3', False)):
        state = path.lstat()
        require((stat.S_ISDIR(state.st_mode) if directory else stat.S_ISREG(state.st_mode))
                and state.st_uid == account.pw_uid and state.st_mode & 0o077 == 0, 'SMOKE_STATE_INVALID')
    receipt = create_or_recover(args.data_dir, args.intent, config, args.source_sha,
        require_stopped=lambda: stopped_guard(args.data_dir),
        require_ready=lambda selected, source: reviewed_readiness(args.readiness_receipt, args.readiness_sha256, selected, source), identity=(account.pw_uid, account.pw_gid))
    print(canonical(receipt))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('{"error":"SMOKE_GATE_FAILED"}')
        raise SystemExit(1) from None
