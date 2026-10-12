#!/usr/bin/env python3
"""One-shot ASUS deployment transport using the reviewed private PAM pipe.

Requires the pinned Broker transport source in the local sealed packet. Values
remain in private pipes/memory; no password is accepted in argv or environment.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import sys
import types

TRANSPORT_SHA = "4527a42c01b4d7c05300a75d8b54f1b0574ebe994214e1ed20f50e749f393d3a"


def bootstrap(manifest, manifest_sha):
    """Fixed reviewed root program; all uploaded code verified before execution."""
    return '''import hashlib,json,os,pathlib,re,stat,subprocess,sys
os.umask(0o077)
M = ''' + repr(manifest) + '''
H = ''' + repr(manifest_sha) + '''
source=pathlib.Path('/var/tmp/orderflow-broker-'+M['source'])
target=pathlib.Path('/var/backups/orderflow')/('broker-packet-'+M['source'])
def need(ok):
    if not ok: raise RuntimeError('packet_gate')
try:
    need(os.getuid()==0 and os.uname().nodename=='asus-ubuntu2604-server')
    need(re.fullmatch('[0-9a-f]{40}',M['source']) is not None)
    need(set(M['files'])=={'source.tar','broker.json','ready.json','run-broker-integration.py','upgrade-asus-broker.py','upgrade-asus-shared-trial.py','broker_readiness.py'})
    st=source.lstat();need(stat.S_ISDIR(st.st_mode) and st.st_uid==1000 and stat.S_IMODE(st.st_mode)==0o700)
    files={**M['files'],'manifest.json':H}
    saved={}
    for name,sha in files.items():
        need(pathlib.Path(name).name==name)
        fd=os.open(source/name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as f:
            st=os.fstat(f.fileno());need(stat.S_ISREG(st.st_mode) and st.st_uid==1000 and st.st_nlink==1 and stat.S_IMODE(st.st_mode)==0o600 and st.st_size<=67108864)
            raw=f.read(67108865);need(len(raw)==st.st_size and hashlib.sha256(raw).hexdigest()==sha)
            saved[name]=raw
    st=target.parent.lstat();need(stat.S_ISDIR(st.st_mode) and st.st_uid==0 and stat.S_IMODE(st.st_mode)==0o700)
    target.mkdir(mode=0o700)
    for name,raw in saved.items():
        with (target/name).open('xb') as f:
            f.write(raw);f.flush();os.fsync(f.fileno())
    fd=os.open(target,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
    child=subprocess.run(['/usr/bin/python3','-B',str(target/'run-broker-integration.py'),'--packet',str(target)],capture_output=True,timeout=800)
    # Only the runner's content-free JSON summary is returned. No raw stderr.
    if child.returncode in (0,2):
        lines=child.stdout.splitlines();result=json.loads(lines[0]);need(result.get('deployment')=='passed')
        result['acceptance_complete']=child.returncode==0
    else:
        result={'deployment':'unverified','acceptance_complete':False,'automatic_retry':False,'exit_code':child.returncode}
    print(json.dumps(result))
except BaseException:
    print(json.dumps({'deployment':'unverified','acceptance_complete':False,'automatic_retry':False}))
'''


def projection(obj):
    if not isinstance(obj, dict) or obj.get("deployment") not in {"passed", "unverified"}:
        raise ValueError("invalid result")
    if type(obj.get("acceptance_complete")) is not bool or (obj["deployment"] == "unverified" and obj["acceptance_complete"]):
        raise ValueError("inconsistent acceptance")
    # Whitelist the nonsecret result shape; never emit unexpected root output.
    result = {"deployment": obj["deployment"], "acceptance_complete": obj.get("acceptance_complete") is True,
              "automatic_retry": False}
    if obj["deployment"] == "passed":
        allowed = {"job_id", "document_id", "request_key", "state", "phase", "pdf_sha256", "task_sha256",
                   "source_sha", "max_output_tokens", "gateway_state", "finish_reason", "response_truncated",
                   "usage_unknown", "expected_items_match"}
        smoke = obj.get("synthetic")
        if not isinstance(smoke, dict) or set(smoke) != allowed:
            raise ValueError("invalid smoke result")
        patterns = {"job_id": r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                    "document_id": r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                    "request_key": r"of-smoke-[0-9a-f]{32}", "source_sha": r"[0-9a-f]{40}",
                    "pdf_sha256": r"[0-9a-f]{64}", "task_sha256": r"[0-9a-f]{64}"}
        if any(not isinstance(smoke[k], str) or re.fullmatch(pattern, smoke[k]) is None
               for k, pattern in patterns.items()):
            raise ValueError("invalid smoke identity")
        enums = {"state": {"queued", "running", "unknown", "failed", "done"},
                 "gateway_state": {None, "completed", "completed_usage_unknown", "unknown", "failed", "rejected", "quota_exhausted"},
                 "finish_reason": {None, "stop", "length", "max_tokens", "tool_calls", "content_filter", "safety", "recitation", "missing", "unclassified"}}
        if any(not (smoke[k] is None or isinstance(smoke[k], str)) or smoke[k] not in values
               for k, values in enums.items()):
            raise ValueError("invalid smoke status")
        if not isinstance(smoke["phase"], str) or re.fullmatch(
                r"prepared|submitting|pending|unknown|complete|failed|expired|busy_paused|(?:busy|submitting)_(?:[0-9]|[12][0-9]|3[0-2])", smoke["phase"]) is None:
            raise ValueError("invalid smoke phase")
        if (type(smoke["max_output_tokens"]) is not int or smoke["max_output_tokens"] != 2048
                or type(smoke["usage_unknown"]) is not bool
                or any(smoke[k] is not None and type(smoke[k]) is not bool for k in ("response_truncated", "expected_items_match"))):
            raise ValueError("invalid smoke bounds")
        if smoke["gateway_state"] == "completed_usage_unknown" and not smoke["usage_unknown"]:
            raise ValueError("inconsistent usage")
        if result["acceptance_complete"] and not (smoke["state"] == "done" and smoke["phase"] == "complete"
                and smoke["gateway_state"] in {"completed", "completed_usage_unknown"}
                and smoke["expected_items_match"] is True and smoke["finish_reason"] == "stop"
                and smoke["response_truncated"] is False and obj.get("old_rows_and_files") == "preserved"):
            raise ValueError("invalid acceptance result")
        result["synthetic"] = smoke
        result["old_rows_and_files"] = obj.get("old_rows_and_files") == "preserved"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args()
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != args.expected_sha256:
        raise SystemExit("transport source mismatch")
    raw = (args.packet / "broker-transport.py").read_bytes()
    if hashlib.sha256(raw).hexdigest() != TRANSPORT_SHA:
        raise SystemExit("pinned PAM transport mismatch")
    t = types.ModuleType("private_transport")
    t.__file__ = str(args.packet / "broker-transport.py")
    exec(compile(raw, t.__file__, "exec"), t.__dict__)
    claimed = False
    result = {"state": "blocked", "automatic_retry": False}
    try:
        raw = (args.packet / "manifest.json").read_bytes()
        t.need(hashlib.sha256(raw).hexdigest() == args.manifest_sha256)
        manifest = json.loads(raw)
        source = bootstrap(manifest, args.manifest_sha256)
        root_sha = hashlib.sha256(source.encode()).hexdigest()
        t.READY = {"kind": "ready", "host": t.HOST, "principal": "morris",
                   "operation": "orderflow-broker-integration", "root_sha256": root_sha}
        argv = t.remote_argv(TRANSPORT_SHA)
        remote = shlex.split(argv[-1])
        remote[remote.index("RuntimeMaxSec=200")] = "RuntimeMaxSec=950"
        root = ["/usr/bin/systemd-run", "--quiet", "--wait", "--pipe", "--collect",
                "--unit=orderflow-broker-deploy-" + manifest["source"][:12],
                "-p", "MemorySwapMax=0", "-p", "LimitCORE=0", "-p", "RuntimeMaxSec=900",
                "-p", "UMask=0077", "-p", "ProtectSystem=strict", "-p", "ProtectHome=yes",
                "-p", "ReadWritePaths=/opt/orderflow /var/lib/orderflow /var/backups/orderflow /etc/orderflow /etc/systemd/system",
                "-p", "PrivateTmp=yes",
                "-p", "BindReadOnlyPaths=/var/tmp/orderflow-broker-" + manifest["source"],
                "-p", "IPAddressDeny=any", "-p", "IPAddressAllow=localhost",
                "/usr/bin/python3", "-I", "-B", "-S", "-c", source]
        remote[-1] = remote[-1].removesuffix("sys.exit(remote_main())") + "\nROOT_ARGV=" + repr(root) + "\nREADY=" + repr(t.READY) + r'''
raw=bytearray();stage="local_guard";possible=False
try:
    guard();stage="host_gate"
    need(os.getuid()==os.geteuid()==1000 and pwd.getpwuid(1000).pw_name=="morris" and os.uname().nodename==HOST)
    def supply():
        emit(READY)
        return read_line(sys.stdin.buffer,MAX_SECRET,30,"input_write",False)
    stage="command_dispatch";possible=True
    child=subprocess.Popen(["/usr/bin/sudo","-S","-k","-p",PROMPT_FORMAT,"--",*ROOT_ARGV],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=ENV,start_new_session=True)
    rc,raw,count=capture(child,32768,920,supply)
    stage="result_framing"
    if rc!=0: raise TransportFailure(stage,"child_exit",rc=rc,possible_dispatch=True)
    need(count in (0,1))
    emit({"kind":"orderflow_integration","state":"received","authentication_submissions":count,"result":json.loads(raw)})
except BaseException as error:
    emit({"kind":"orderflow_integration",**failure(error,stage,possible)})
finally:
    erase(raw)
'''
        argv[-1] = shlex.join(remote)
        t.guard()
        t.check_secret_name()
        t.exclusive_json(args.packet / "transport.claim.json", {"root_sha256": root_sha,
                         "manifest_sha256": args.manifest_sha256, "automatic_retry": False})
        claimed = True
        result.update(t.exchange(argv, seconds=930, kind="orderflow_integration", field="result", projection=projection))
    except BaseException as error:
        result.update(t.failure(error, "local_guard", claimed))
    finally:
        if claimed:
            t.exclusive_json(args.packet / "transport.result.json", result)
        t.emit(result)
    return 0 if result.get("result", {}).get("acceptance_complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())
