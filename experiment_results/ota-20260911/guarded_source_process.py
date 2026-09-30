#!/usr/bin/env python3
"""Private per-attempt source ownership helper; never operates an OAI process.

The runner transmits this helper to Python over SSH/docker stdin. It installs
nothing. A child publishes its PID/start identity before exec; cancellation is
published before cleanup, so a delayed start cannot escape a failed attempt.
"""
import base64
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time


SOURCE_HASHES = {
    # Re-pinned 2026-09-16 for UE identity continuity: the sink and the echo client
    # rebind to a re-registered UE's new tun (rebind-wait/rebind events, bindEpoch,
    # voided requests) and the sender retries its bounded connect.
    'flow_goodput.py': '2f3046ce5a0ddae243bfa1238b00cb0c5121b89e511372e850e525b0b81da936',
    # Re-pinned for the windowed cohort: snapshot() gained keyword-only
    # window_start_ms/window_end_ms (and the matching --window-*-ms flags), so
    # a deadline ratio can be read over the trial's own window instead of the
    # whole log.  The 5-positional call in handle('snapshot') is unchanged.
    # Re-pinned 2026-09-15 for v3.1 (amendment section 5): the issued cohort's
    # identity block (issued count, first/last seq and issue time, seq sha256).
    # Redeployed to ue1/ue2/ue3/oai-ext-dn and verified by sha256 on all four.
    'tagged_echo.py': 'b08f00181038ebafb1e0ba20e664a8907042d08d13b6f042af22ca17ad6e4466',
}
STATE_PARENT = Path('/tmp')
SESSION_RE = re.compile(r'formal38guarded-\d{8}T\d{6}-[0-9a-f]{32}')
SLOT_RE = re.compile(r'[a-z][a-z0-9_-]{0,39}')
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024


class GuardError(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise GuardError(code)


def exclusive_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic publish, with O_EXCL semantics even for an existing symlink.
        os.link(temporary, path, follow_symlinks=False)
    finally:
        os.unlink(temporary)


def safe_file(path):
    st = path.lstat()
    require(stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid(), 'UNOWNED_OR_UNSAFE_FILE')
    return st


def session_dir(session, create=False):
    require(isinstance(session, str) and SESSION_RE.fullmatch(session), 'INVALID_SESSION')
    path = STATE_PARENT / ('aic-' + session)
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    if path.exists() or path.is_symlink():
        st = path.lstat()
        require(stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()
                and stat.S_IMODE(st.st_mode) == 0o700, 'UNOWNED_OR_UNSAFE_SESSION')
    return path


def paths_for(session, slot, create=False):
    require(isinstance(slot, str) and SLOT_RE.fullmatch(slot), 'INVALID_SLOT')
    root = session_dir(session, create)
    return {name: root / (slot + suffix) for name, suffix in
            [('claim', '.claim.json'), ('owner', '.owner.json'),
             ('source_owner', '.source-owner.json'), ('log', '.jsonl'), ('out', '.out')]}


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def proc_identity(pid):
    try:
        root = Path('/proc') / str(int(pid))
        fields = (root / 'stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] in ('Z', 'X'):
            return None
        argv = [s.decode() for s in (root / 'cmdline').read_bytes().split(b'\0') if s]
        return {'pid': int(pid), 'startTicks': int(fields[19]), 'pgid': int(fields[2]),
                'ppid': int(fields[1]), 'bootId': boot_id(), 'uid': root.stat().st_uid,
                'argv': argv}
    except (FileNotFoundError, ProcessLookupError):
        return None


def same_process(current, pinned):
    return current is not None and all(current.get(k) == pinned.get(k)
                                       for k in ('pid', 'startTicks', 'pgid', 'bootId', 'uid'))


def checked_sources(source_dir):
    source = Path(source_dir)
    require(source.is_absolute(), 'SOURCE_PATH_NOT_ABSOLUTE')
    for name, expected in SOURCE_HASHES.items():
        path = source / name
        require(path.is_file(), 'SOURCE_MISSING:' + name)
        require(hashlib.sha256(path.read_bytes()).hexdigest() == expected,
                'SOURCE_HASH_MISMATCH:' + name)
    return source


def load_sources(source_dir):
    source = checked_sources(source_dir)
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        modules = []
        for name in ('tagged_echo', 'flow_goodput'):
            spec = importlib.util.spec_from_file_location(name, source / (name + '.py'))
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            modules.append(module)
        return modules
    finally:
        sys.dont_write_bytecode = old


def identity(source_dir):
    common, _ = load_sources(source_dir)
    try:
        value = common._interface(common.read_interface('oaitun_ue1'))
    except (OSError, common.TaggedEchoError):
        # A tun that is missing or down is a named refusal; the uncaught TaggedEchoError used to
        # escape main() as a traceback, which the runner reported as REMOTE_INVALID_REPLY (attempt 95).
        raise GuardError('SOURCE_INTERFACE_DOWN') from None
    address = ipaddress.IPv4Address(value['ip'])
    require(address in ipaddress.IPv4Network('12.1.1.0/24'), 'UNEXPECTED_UE_NETWORK')
    return {**value, 'bootId': common.boot_clock_id()}


def check_identity(expected, source_dir):
    if expected is not None:
        require(identity(source_dir) == expected, 'SOURCE_INTERFACE_CHANGED')


def source_argv(session, endpoint, spec, source_dir):
    source = checked_sources(source_dir)
    role = spec['role']
    port = spec['port']
    require(type(port) is int and 1024 <= port <= 65535, 'INVALID_PORT')
    common = ['--session-id', session, '--flow-id', spec['flowId'], '--port', str(port)]
    # How long the source runs.  It used to be hardcoded at 590/600 s, which is
    # right for a 480 s episode and wrong for anything shorter: a 150 s
    # reference pass left senders and echo servers running for another seven
    # minutes, holding 192.168.70.135:650x, and the next attempt's client then
    # met a listener that belonged to the previous one.  The runner derives it
    # from the episode horizon; the listener gets the longer of the pair so it
    # is up before its client and still up after it.
    listen_s = int(spec.get('durationS') or 600)
    speak_s = max(1, listen_s - 10)
    where = paths_for(session, spec['slot'])
    if role == 'receiver':
        require(endpoint in ('ue1', 'ue2', 'ue3'), 'WRONG_RECEIVER_ENDPOINT')
        require(spec.get('identity') is not None and spec['ip'] == spec['identity']['ip'],
                'RECEIVER_IDENTITY_MISSING')
        return [sys.executable, str(source/'flow_goodput.py'), 'receiver', *common,
                '--duration-s', str(listen_s), '--bind-ip', spec['ip'],
                # v2 (owner-policy-joint-control-v2): the handoff selects the
                # offered load from a bounded reference pass over 10/8/6/4 Mbps,
                # so the receiver may no longer cap below that.  30 is
                # ``flow_goodput.MAX_RATE_MBPS``, the script's own ceiling --
                # this stops being a second, lower limit and the sender guard
                # below is the one that states the admissible range.
                '--allow-source-ip', '192.168.70.135', '--max-rate-mbps', '30',
                '--log', str(where['log'])]
    if role == 'sender':
        require(endpoint == 'extdn', 'DL_SENDER_MUST_BE_EXTDN')
        require(ipaddress.IPv4Address(spec['ip']) in ipaddress.IPv4Network('12.1.1.0/24'),
                'UNEXPECTED_UE_NETWORK')
        # The offered downlink load is the environment, not an action axis (P0-19):
        # it is never proposed, permitted or rolled back, and no agent can reach it.
        # At 1 Mbps per UE a 38 PRB cell carrying two UEs meets every original
        # requirement on the initial measurement, so T0 succeeds at trial 0 and the
        # three agents are never called -- exp_metrics.md section 1 wants main cases
        # that require joint concessions. AIC_OFFERED_LOAD_MBPS raises it; the
        # receiver refuses anything above its own --max-rate-mbps 8, so that is the
        # ceiling until both sides change together. Default unchanged.
        rate = os.environ.get('AIC_OFFERED_LOAD_MBPS') or '1'
        # Both guards moved together, as the comment above requires: the
        # receiver now admits up to flow_goodput's own 30 Mbps ceiling, so this
        # is the single statement of the admissible range.  v2 probes 10/8/6/4
        # Mbps per UE; the old limit of 8 would have refused the first of them.
        require(re.fullmatch(r'[0-9]+(\.[0-9]+)?', rate) and 0 < float(rate) <= 30,
                'OFFERED_LOAD_OUT_OF_RANGE')
        return [sys.executable, str(source/'flow_goodput.py'), 'sender', *common,
                '--duration-s', str(speak_s), '--receiver-ip', spec['ip'], '--rate-mbps', rate]
    if role == 'echo_server':
        require(endpoint == 'extdn', 'ECHO_SERVER_MUST_BE_EXTDN')
        # The reflector's reply budget must exceed the client's issue rate, not
        # equal it.  At '5' run_server's gate is 200.000 ms measured from the
        # last admitted arrival while the client's realised cadence is
        # 200.281 ms, so ordinary jitter pushed ~37 % of requests under the gate,
        # where they were received, validated and dropped with no reply and no
        # record -- capping deadlineSuccessRatio at 1/(2-p) <= 2/3 and making the
        # 0.90 requirement unsatisfiable at every load and every deadline.  20 is
        # tagged_echo.MAX_RATE_HZ and that script's own server default: 4x
        # headroom over a 5 Hz client.  The client below stays at '5'.
        return [sys.executable, str(source/'tagged_echo.py'), 'server', *common,
                '--duration-s', str(listen_s), '--rate-hz', '20', '--bind-ip', '192.168.70.135',
                '--allow-subnet', '192.168.70.134/32']
    if role == 'echo_client':
        # I4-I6 give every UE its own response requirement, so every UE runs its
        # own echo client.  Pinning this to ue1 is why deadlineSuccessRatio was
        # never observed for ue2 or ue3: the client was refused before it
        # started.  The guard still holds -- the client may only run on the UE
        # its own spec names, and only with a pinned identity.
        require(endpoint in ('ue1', 'ue2', 'ue3')
                and endpoint == spec.get('host')
                and spec.get('identity') is not None,
                'WRONG_ECHO_CLIENT_ENDPOINT')
        return [sys.executable, str(source/'tagged_echo.py'), 'client', *common,
                '--duration-s', str(speak_s), '--rate-hz', '5', '--server-ip', '192.168.70.135',
                '--interface', 'oaitun_ue1', '--payload-bytes', '256',
                '--reply-drain-s', '2', '--log', str(where['log'])]
    raise GuardError('UNKNOWN_SOURCE_ROLE')


def helper_code():
    original = getattr(sys, 'orig_argv', [])
    if '-c' in original:
        return original[original.index('-c') + 1]
    return Path(__file__).read_text()


def start(request):
    session, spec = request['session'], request['spec']
    root = session_dir(session, True)
    require(not (root/'cancelled').exists(), 'SESSION_CANCELLED')
    where = paths_for(session, spec['slot'])
    argv = source_argv(session, request['endpoint'], spec, request['sourceDir'])
    check_identity(spec.get('identity'), request['sourceDir'])
    require(not where['log'].exists() and not where['log'].is_symlink(), 'SOURCE_LOG_EXISTS')
    source = helper_code()
    child_argv = [sys.executable, '-c', source, '--child', str(where['claim'])]
    source_child_argv = [sys.executable, '-c', source, '--source-child', str(where['claim'])]
    claim = {**request, 'sourceArgv': argv, 'sourceBootstrapArgv': source_child_argv,
             'bootstrapArgv': child_argv}
    exclusive_json(where['claim'], claim)
    out = os.open(where['out'], os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    try:
        process = subprocess.Popen(child_argv, stdin=subprocess.DEVNULL, stdout=out,
                                   stderr=out, start_new_session=True, close_fds=True)
    finally:
        os.close(out)
    return {'reserved': True, 'slot': spec['slot'], 'launcherPid': process.pid,
            'logPath': str(where['log']), 'outPath': str(where['out'])}


def child(claim_path, source_child=False):
    claim_path = Path(claim_path)
    require(claim_path.name.endswith('.claim.json'), 'INVALID_CLAIM_PATH')
    safe_file(claim_path)
    claim = json.loads(claim_path.read_text())
    where = paths_for(claim['session'], claim['spec']['slot'])
    require(where['claim'] == claim_path, 'CLAIM_PATH_MISMATCH')
    owner = proc_identity(os.getpid())
    require(owner is not None and owner['pgid'] == owner['pid'], 'CHILD_NOT_OWN_SESSION')
    exclusive_json(where['source_owner' if source_child else 'owner'], owner)
    # Each child publishes BEFORE testing cancellation. Even a lost SSH reply
    # cannot hide traffic: cleanup finds the receipt, or the child sees cancel.
    require(not (claim_path.parent/'cancelled').exists(), 'SESSION_CANCELLED')
    check_identity(claim['spec'].get('identity'), claim['sourceDir'])
    require(source_argv(claim['session'], claim['endpoint'], claim['spec'], claim['sourceDir'])
            == claim['sourceArgv'], 'SOURCE_COMMAND_CHANGED')
    require(not (claim_path.parent/'cancelled').exists(), 'SESSION_CANCELLED')
    if source_child:
        os.execv(claim['sourceArgv'][0], claim['sourceArgv'])
    process = subprocess.Popen(claim['sourceBootstrapArgv'], stdin=subprocess.DEVNULL,
                               start_new_session=True, close_fds=True)
    # A bounded supervisor lives on the same endpoint as the traffic, including
    # inside oai-ext-dn. No host-side timeout is relied on to end container work.
    try:
        listen_s = int(claim['spec'].get('durationS') or 600)
        return process.wait(
            timeout=listen_s + (20 if claim['spec']['role'] == 'receiver' else 10))
    except subprocess.TimeoutExpired:
        try:
            fd = os.open(claim_path.parent/'cancelled', os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        if where['source_owner'].exists():
            safe_file(where['source_owner'])
            stop_process(json.loads(where['source_owner'].read_text()),
                         [claim['sourceArgv'], claim['sourceBootstrapArgv']])
        return 124


def owner_for(where):
    if not where['owner'].exists():
        return None
    safe_file(where['owner'])
    return json.loads(where['owner'].read_text())


def verified_supervisor(claim, owner):
    current = proc_identity(owner['pid'])
    if not same_process(current, owner):
        return None
    require(current['argv'] == claim['bootstrapArgv'], 'OWNER_COMMAND_CHANGED')
    return current


def source_for(claim, where, owner):
    if not where['source_owner'].exists():
        return None
    safe_file(where['source_owner'])
    pinned = json.loads(where['source_owner'].read_text())
    current = proc_identity(pinned['pid'])
    require(same_process(current, pinned) and current['ppid'] == owner['pid'],
            'SOURCE_OWNER_CHANGED_OR_EXITED')
    # An empty cmdline on the pinned pid/startTicks is execv in progress: the
    # kernel installs the new mm before its arguments, so /proc reads nothing
    # (2026-09-15 attempt 65: state R, argvLength 0).  Not ready yet, never
    # ready -- the next inspection still demands the exact source argv.
    if current['argv'] in (claim['sourceBootstrapArgv'], []):
        return None
    if current['argv'] != claim['sourceArgv']:
        record_refusal(where, 'SOURCE_COMMAND_CHANGED', current)
    require(current['argv'] == claim['sourceArgv'], 'SOURCE_COMMAND_CHANGED')
    return current


def record_refusal(where, code, current):
    """Leave what was actually seen; the reply carries only the code.

    2026-09-15 attempts 47-48 were refused twice with no trace of the argv the
    check saw.  Appended next to the slot log so ``archive`` returns it.
    """
    try:
        state = (Path('/proc')/str(current['pid'])/'stat').read_text().rsplit(')', 1)[1].split()[0]
    except OSError:
        state = None
    row = {'code': code, 'pid': current['pid'], 'ppid': current['ppid'], 'state': state,
           'argvLength': len(current['argv']), 'argv': [arg[:120] for arg in current['argv'][:8]]}
    try:
        fd = os.open(where['log'].with_suffix('.refusal.jsonl'),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as stream:
            stream.write(json.dumps(row, sort_keys=True) + '\n')
    except OSError:
        pass


def listening_socket(process, protocol, ip, port):
    root = Path('/proc')/str(process['pid'])
    sockets = set()
    for fd in (root/'fd').iterdir():
        try:
            target = os.readlink(fd)
        except FileNotFoundError:
            continue
        match = re.fullmatch(r'socket:\[(\d+)\]', target)
        if match:
            sockets.add(match[1])
    for line in (root/'net'/protocol).read_text().splitlines()[1:]:
        fields = line.split()
        addr, hex_port = fields[1].split(':')
        local_ip = socket.inet_ntoa(bytes.fromhex(addr)[::-1])
        wanted_state = '0A' if protocol == 'tcp' else '07'
        if (fields[3] == wanted_state and local_ip == ip and int(hex_port,16) == port
                and fields[9] in sockets):
            return {'pid':process['pid'], 'startTicks':process['startTicks'],
                    'inode':fields[9], 'protocol':protocol, 'ip':ip, 'port':port}
    return None


def inspect_slot(request):
    where = paths_for(request['session'], request['spec']['slot'])
    safe_file(where['claim'])
    claim = json.loads(where['claim'].read_text())
    require(claim['spec'] == request['spec'], 'SOURCE_SPEC_CHANGED')
    owner = owner_for(where)
    if owner is None:
        return {'ready':False, 'reason':'OWNER_NOT_PUBLISHED'}
    require(verified_supervisor(claim, owner) is not None, 'SOURCE_SUPERVISOR_EXITED_OR_CHANGED')
    source = source_for(claim, where, owner)
    if source is None:
        return {'ready':False, 'reason':'SOURCE_NOT_EXECUTED'}
    check_identity(claim['spec'].get('identity'), claim['sourceDir'])
    result = {'ready':True, 'owner':owner, 'sourceOwner':source}
    if request.get('listener'):
        spec = claim['spec']
        require(spec['role'] in ('receiver','echo_server'), 'ROLE_HAS_NO_LISTENER')
        listener = listening_socket(source, 'tcp' if spec['role']=='receiver' else 'udp',
                                    spec['ip'] if spec['role']=='receiver' else '192.168.70.135',
                                    spec['port'])
        result.update(ready=listener is not None, listener=listener)
    require(same_process(proc_identity(source['pid']), source), 'SOURCE_CHANGED_DURING_INSPECTION')
    return result


def signal_exact(pinned, allowed_argv, sig):
    current = proc_identity(pinned['pid'])
    if current is None:
        return 'already-exited'
    require(same_process(current,pinned) and current['argv'] in allowed_argv, 'PID_REUSED_OR_UNOWNED')
    fd = os.pidfd_open(pinned['pid'])
    try:
        current = proc_identity(pinned['pid'])
        require(same_process(current,pinned) and current['argv'] in allowed_argv, 'PID_CHANGED_BEFORE_SIGNAL')
        signal.pidfd_send_signal(fd,sig)
    finally:
        os.close(fd)
    return 'signalled'


def stop_process(pinned, argv):
    try:
        state = signal_exact(pinned,argv,signal.SIGTERM)
        end = time.monotonic()+1
        while state != 'already-exited' and time.monotonic()<end:
            if proc_identity(pinned['pid']) is None:
                return 'stopped'
            time.sleep(.05)
        if state != 'already-exited':
            signal_exact(pinned,argv,signal.SIGKILL)
            end = time.monotonic()+1
            while time.monotonic()<end:
                if not same_process(proc_identity(pinned['pid']), pinned):
                    return 'stopped'
                time.sleep(.05)
            raise GuardError('PROCESS_STOP_UNCONFIRMED')
        return state
    except ProcessLookupError:
        return 'already-exited'


def cleanup(request):
    root = session_dir(request['session'], True)
    try:
        fd = os.open(root/'cancelled',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        os.close(fd)
    except FileExistsError:
        safe_file(root/'cancelled')
    results=[]
    for path in root.glob('*.claim.json'):
        try:
            safe_file(path)
            claim=json.loads(path.read_text());where=paths_for(request['session'],claim['spec']['slot'])
            require(path==where['claim'] and claim['session']==request['session'], 'FOREIGN_CLAIM')
            owner=owner_for(where)
            if owner is None:
                results.append({'slot':claim['spec']['slot'],'status':'cancelled-before-owner-publication'})
                continue
            # Stop the supervisor first. Any not-yet-published source child
            # will see cancellation after publishing; already-running sources
            # necessarily published their receipt before the marker existed.
            outcomes = []
            for key, commands in (('owner', [claim['bootstrapArgv']]),
                                  ('source_owner', [claim['sourceArgv'], claim['sourceBootstrapArgv']])):
                if not where[key].exists():
                    continue
                try:
                    safe_file(where[key])
                    pinned = json.loads(where[key].read_text())
                    outcomes.append({'kind':key, 'status':stop_process(pinned,commands)})
                except (GuardError,OSError,ValueError,KeyError,TypeError) as exc:
                    outcomes.append({'kind':key,'status':'cleanup-unconfirmed','errorType':type(exc).__name__})
            status = ('cleanup-unconfirmed' if any(item['status']=='cleanup-unconfirmed' for item in outcomes)
                      else 'stopped-or-cancelled')
            results.append({'slot':claim['spec']['slot'],'status':status,'owners':outcomes})
        except (GuardError,OSError,ValueError) as exc:
            results.append({'slot':path.name,'status':'cleanup-unconfirmed','errorType':type(exc).__name__})
    return {'cancelled':True,'processes':results,
            'complete':not any(r['status']=='cleanup-unconfirmed' for r in results)}


def archive(request):
    root=session_dir(request['session']);files=[];errors=[]
    if not root.exists():
        return {'files':[],'errors':[]}
    for path in root.iterdir():
        if not path.name.endswith(('.jsonl','.out','.owner.json','.source-owner.json')):
            continue
        try:
            st=safe_file(path)
            require(st.st_size<=MAX_ARCHIVE_BYTES,'ARCHIVE_TOO_LARGE')
            data=path.read_bytes()
            files.append({'name':path.name,'sha256':hashlib.sha256(data).hexdigest(),
                          'base64':base64.b64encode(data).decode('ascii')})
        except (GuardError,OSError) as exc:
            errors.append({'name':path.name,'errorType':type(exc).__name__})
    return {'files':files,'errors':errors}


def handle(request):
    action=request['action']
    if action=='cleanup': return cleanup(request)
    if action=='archive': return archive(request)
    source=checked_sources(request['sourceDir'])
    if action=='preflight':
        require(hasattr(os,'pidfd_open') and hasattr(signal,'pidfd_send_signal'), 'PIDFD_REQUIRED')
        fd=os.pidfd_open(os.getpid());os.close(fd)
        foreign=[]
        for entry in Path('/proc').iterdir():
            if not entry.name.isdecimal():continue
            try:
                process=proc_identity(int(entry.name))
            except OSError:
                raise GuardError('SOURCE_INVENTORY_UNREADABLE') from None
            if process and any(Path(arg).name in SOURCE_HASHES for arg in process['argv']):
                foreign.append({'pid':process['pid'],'startTicks':process['startTicks']})
        require(not foreign,'EXISTING_WORKLOAD_PROCESSES')
        return {'sourceHashes':dict(SOURCE_HASHES),'bootId':boot_id()}
    if action=='identity':return identity(request['sourceDir'])
    if action=='start':return start(request)
    if action=='inspect':return inspect_slot(request)
    if action=='sample':
        ownership = inspect_slot(request)
        require(ownership.get('ready') is True, 'SOURCE_NOT_READY_FOR_SAMPLE')
        value = handle({**request, 'action':'snapshot'})
        return {'ownership':ownership, 'snapshot':value}
    if action=='snapshot':
        where=paths_for(request['session'],request['spec']['slot'])
        common,flow=load_sources(request['sourceDir'])
        if request['spec']['role']=='receiver':
            return flow.snapshot(str(where['log']),request['session'],request['spec']['flowId'],1500)
        require(request['spec']['role']=='echo_client','ROLE_HAS_NO_SNAPSHOT')
        return common.snapshot(str(where['log']),request['session'],request['spec']['flowId'],[200,300],1500)
    raise GuardError('UNKNOWN_ACTION')


def main():
    try:
        if len(sys.argv)==3 and sys.argv[1] in ('--child', '--source-child'):
            return child(sys.argv[2], source_child=sys.argv[1]=='--source-child') or 0
        result=handle(json.load(sys.stdin))
        print(json.dumps({'ok':True,'result':result}),flush=True)
        return 0
    except (GuardError,OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as exc:
        # The caller keeps only an error that looks like a token ([A-Z][A-Z0-9_]*) and
        # replaces anything else with a bare REMOTE_REFUSED, so that a remote message can
        # never carry a secret into the log.  A Python type name is secret-free by
        # construction -- the rest of this runner records it as `errorType` everywhere --
        # but `OSError` has lowercase letters and was being thrown away with the reason.
        # Eight attempts died as `REMOTE_REFUSED:<host>:sample` with nothing to diagnose
        # (88, 92, 93, 104, 107, 115, 125, 137).  Sent as a token so it survives, with the
        # caller's filter left exactly as strict as it was.
        code=str(exc) if isinstance(exc,GuardError) else 'EXC_'+type(exc).__name__.upper()
        print(json.dumps({'ok':False,'error':code}),flush=True)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
