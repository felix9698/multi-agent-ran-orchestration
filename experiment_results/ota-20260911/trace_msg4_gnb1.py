#!/usr/bin/env python3
"""Operator-lane scalar Msg4 trace. No radio/config/source change or IQ capture.

Run from a normal PC1 terminal with sudo. Only a newly-created trace instance
and uniquely-named probes are enabled; all are removed on exit. The gNB keeps
running. Probe offsets are valid only for the fingerprint below.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import time
import uuid

BINARY = Path('/opt/ran-lab/controller/oai-build-campaign5/cmake_targets/ran_build_campaign5/build/nr-softmodem')
EXPECTED_SHA = 'f1f51d49cc5162a15592058b1c977e5599a90fb3ed00c133b1748e052733aa22'
TRACEFS = Path('/sys/kernel/tracing')
OUTPUT = Path(__file__).resolve().parent/'msg4-traces'
LAST_OPERATION = 'not-started'
PROBES = {
    'uci': '0x91c750 frame=%si:u32 slot=%dx:u32 rnti=+8(%cx):u16 bitmap=+0(%cx):u8 fmt=+10(%cx):u8 cqi=+11(%cx):u8 n=+18(%cx):u8 conf=+19(%cx):u8 v0=+20(%cx):u8',
    'check': '0x92fa10 frame=%si:u32 slot=%dx:u32 rnti=+0(%cx):u16 success=%r8:u8',
    'pdu': '0x7dbe70 frame=%dx:s32 slot=%cx:s32 rnti=+0(%r9):u16 bwp_start=+10(%r9):u16 bwp_size=+8(%r9):u16 scs=+12(%r9):u8 fmt=+14(%r9):u8 prb=+18(%r9):u16 hop=+26(%r9):u16 sym=+22(%r9):u8 nsym=+23(%r9):u8 nid=+30(%r9):u16 cs=+32(%r9):u16 freqhop=+24(%r9):u8 grouphop=+28(%r9):u8 seqhop=+29(%r9):u8 bits=+44(%r9):u16 thres=+1809208(%di):s32',
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def proc_start(pid):
    try:
        fields = (Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()
        return None if fields[0] in ('Z','X') else int(fields[19])
    except FileNotFoundError:
        return None


def write_setting(path, value):
    global LAST_OPERATION
    LAST_OPERATION = 'write-setting:'+str(path.relative_to(TRACEFS))
    with path.open('w') as stream:
        stream.write(str(value)+'\n')


def append_probe(command):
    global LAST_OPERATION
    LAST_OPERATION = 'probe-command:'+command.split()[0]
    # Text append mode seeks to EOF on open; tracefs rejects that seek with
    # EINVAL before seeing any command. Append with raw write, without lseek.
    fd = os.open(TRACEFS/'uprobe_events', os.O_WRONLY|os.O_APPEND)
    try:
        payload = (command+'\n').encode('utf-8')
        if os.write(fd, payload) != len(payload):
            raise OSError(5, 'short tracefs command write')
    finally:
        os.close(fd)


def last_errors():
    groups = [p.name for p in OUTPUT.iterdir()
              if re.fullmatch(r'aic_m4_\d+_[0-9a-f]{8}', p.name)] if OUTPUT.exists() else []
    lines = (TRACEFS/'error_log').read_text().splitlines()
    result = []
    for i, line in enumerate(lines):
        if any(group+'/' in line for group in groups):
            before = lines[i-1] if i and 'trace_uprobe' in lines[i-1] else ''
            after = lines[i+1] if i+1 < len(lines) and '^' in lines[i+1] else ''
            result.append({'kernelError': before, 'ourProbeCommand': line, 'location': after})
    return result


def save(path, record, uid, gid):
    fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    os.fchown(fd,uid,gid)
    with os.fdopen(fd,'w') as stream:
        json.dump(record,stream,indent=2)
        stream.write('\n')


def prepare_output(uid, gid):
    try:
        OUTPUT.mkdir(mode=0o700)
    except FileExistsError:
        info = OUTPUT.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise RuntimeError('UNOWNED_OR_UNSAFE_TRACE_OUTPUT')
    else:
        os.chown(OUTPUT, uid, gid)


def selected_pid():
    result = subprocess.run(['ps','-C','nr-softmodem','-o','pid=,pgid='],
                            capture_output=True,text=True,check=False)
    candidates = [int(row.split()[0]) for row in result.stdout.splitlines()
                  if len(row.split())==2 and row.split()[0]==row.split()[1]]
    if len(candidates) != 1:
        raise RuntimeError('EXACTLY_ONE_GNB_SESSION_LEADER_REQUIRED')
    return candidates[0]


def main(argv=None):
    global LAST_OPERATION
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=int,default=120)
    parser.add_argument('--check',action='store_true',help='fingerprint/plan only; no probes or hardware access')
    parser.add_argument('--last-error',action='store_true',help='read only this helper\'s existing kernel parser errors; install no probes')
    args = parser.parse_args(argv)
    if args.last_error:
        if os.geteuid() != 0:
            raise SystemExit('NORMAL_OPERATOR_SUDO_REQUIRED_FOR_KERNEL_ERROR_LOG')
        print(json.dumps({'readOnly': True, 'matchingKernelErrors': last_errors()}), flush=True)
        return 0
    if not 5 <= args.seconds <= 300:
        raise SystemExit('TRACE_DURATION_MUST_BE_5_TO_300_SECONDS')
    if digest(BINARY) != EXPECTED_SHA:
        raise SystemExit('GNB_BINARY_CHANGED_NO_PROBE_INSTALLED')
    if args.check:
        print(json.dumps({'binaryFingerprintMatches':True,'probeNames':list(PROBES),
                          'captures':'typed control scalars only, no IQ or packet payload',
                          'rootRequiredForActualCapture':True}))
        return 0
    if os.geteuid() != 0 or not os.environ.get('SUDO_UID'):
        raise SystemExit('RUN_WITH_SUDO_IN_NORMAL_PC1_TERMINAL_NO_PRIVILEGE_BYPASS')
    if not (TRACEFS/'uprobe_events').is_file():
        raise SystemExit('EXISTING_TRACEFS_UPROBES_REQUIRED_NO_MOUNT_OR_PERMISSION_CHANGE')
    uid,gid=int(os.environ['SUDO_UID']),int(os.environ['SUDO_GID'])
    pid=selected_pid()
    if (Path('/proc')/str(pid)/'exe').resolve() != BINARY.resolve():
        raise SystemExit('RUNNING_GNB_DOES_NOT_MATCH_PINNED_BINARY')
    start=proc_start(pid)
    if start is None:
        raise SystemExit('GNB_ALREADY_EXITED')
    group='aic_m4_'+str(pid)+'_'+uuid.uuid4().hex[:8]
    instance=TRACEFS/'instances'/group
    if instance.exists() or (TRACEFS/'events'/group).exists():
        raise SystemExit('TRACE_NAME_COLLISION')
    prepare_output(uid, gid)
    directory=OUTPUT/group
    directory.mkdir(mode=0o700)
    os.chown(directory,uid,gid)
    installed=[]
    pipe=None
    stopping=False
    reason='duration-ended'
    bytes_written=0
    cleanup_errors=[]
    error_details=None
    def stop_signal(_sig,_frame):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGINT,stop_signal)
    signal.signal(signal.SIGTERM,stop_signal)
    try:
        LAST_OPERATION = 'create-instance'
        instance.mkdir()
        write_setting(instance/'tracing_on','0')
        write_setting(instance/'trace_clock','mono')
        write_setting(instance/'buffer_size_kb','1024')
        tids=sorted(int(p.name) for p in (Path('/proc')/str(pid)/'task').iterdir())
        write_setting(instance/'set_event_pid',' '.join(map(str,tids)))
        fork_option=instance/'options/event-fork'
        if fork_option.exists():
            write_setting(fork_option,'1')
        for name,definition in PROBES.items():
            append_probe(f'p:{group}/{name} {BINARY}:{definition}')
            installed.append(name)
        write_setting(instance/'events'/group/'uci/filter','(bitmap & 2) && n >= 1')
        write_setting(instance/'events'/group/'enable','1')
        LAST_OPERATION = 'open-private-trace-pipe'
        pipe=os.open(instance/'trace_pipe',os.O_RDONLY|os.O_NONBLOCK)
        write_setting(instance/'tracing_on','1')
        raw=directory/'control-scalars.trace'
        fd=os.open(raw,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        os.fchown(fd,uid,gid)
        ready={'at':datetime.datetime.now().astimezone().isoformat(),'pid':pid,'startTicks':start,
               'binarySha256':EXPECTED_SHA,'traceGroup':group,'traceFile':str(raw),
               'stopFile':str(directory/'stop'),'secondsLimit':args.seconds,
               'pidThreadFilter':tids,'radioOrConfigChanged':False,
               'traceOverheadMustBeConsidered':True,'otaEvidence':False}
        save(directory/'ready.json',ready,uid,gid)
        print('MSG4_TRACE_READY '+json.dumps(ready),flush=True)
        end=time.monotonic()+args.seconds
        with os.fdopen(fd,'wb') as stream:
            while time.monotonic()<end and not stopping:
                if (directory/'stop').exists():
                    reason='stop-requested';break
                if proc_start(pid) != start:
                    reason='gnb-exited-or-replaced';break
                readable,_,_=select.select([pipe],[],[],.2)
                if readable:
                    try:
                        data=os.read(pipe,65536)
                    except BlockingIOError:
                        continue
                    stream.write(data)
                    stream.flush()
                    bytes_written+=len(data)
                    if bytes_written>=32*1024*1024:
                        reason='capture-size-limit';break
            if stopping:
                reason='operator-signal'
        result = 0
    except Exception as exc:
        reason='capture-failed:'+type(exc).__name__
        number=getattr(exc,'errno',None)
        error_details={'type':type(exc).__name__,'operation':LAST_OPERATION,
                       'errno':number,'osError':os.strerror(number) if isinstance(number,int) else None}
        print('MSG4_TRACE_FAILED '+json.dumps(error_details),flush=True)
        result = 1
    finally:
        if instance.exists():
            for target,value in ((instance/'tracing_on','0'),(instance/'events'/group/'enable','0')):
                try:
                    if target.exists():write_setting(target,value)
                except OSError as exc:cleanup_errors.append(type(exc).__name__)
        if pipe is not None:
            os.close(pipe)
        if instance.exists():
            try:instance.rmdir()
            except OSError as exc:cleanup_errors.append(type(exc).__name__)
        for name in reversed(installed):
            try:append_probe('-:'+group+'/'+name)
            except OSError as exc:cleanup_errors.append(type(exc).__name__)
        leftovers=[name for name in installed if (TRACEFS/'events'/group/name).exists()]
        done={'endedAt':datetime.datetime.now().astimezone().isoformat(),'reason':reason,
              'failure':error_details,'bytesWritten':bytes_written,'cleanupErrors':cleanup_errors,
              'remainingProbes':leftovers,'gnbNotStoppedByThisTool':True}
        save(directory/'done.json',done,uid,gid)
        print('MSG4_TRACE_DONE '+json.dumps(done),flush=True)
    return result or (70 if cleanup_errors or leftovers else 0)


if __name__=='__main__':
    raise SystemExit(main())
