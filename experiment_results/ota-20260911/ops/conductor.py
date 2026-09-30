#!/usr/bin/env python3
"""Detached OTA conductor: runs episodes on its own until its own deadline.

It does not depend on an interactive session staying awake.  One hardware lock
so physical episodes never overlap, a hard deadline, a cleanup handler, and a
JSONL ledger written as each stage begins -- so the work survives whatever the
session does.

Each queue entry is one episode: an environment overlay for the existing
runner.  Between episodes the cohort is repaired through the existing
stop/start path.  Nothing here re-implements the runner, the Kernel or the
gateway; it only decides what to launch next and records what happened.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
EXP = REPO / 'experiment_results/ota-20260911'
#: 2026-09-14 05:00.  A new window, not a rewrite: window-20260914T021900 keeps
#: its own records.  Nothing about the scenario changes -- the six frozen
#: manifests are copied byte-identical and every intents.json hash still
#: matches, the axes stay `servingCell,dlPrbCap` and the cap profiles stay
#: U/A12/A6/B12/B6, because the 2026-09-13 reply says in as many words not to
#: enlarge the action space.  What changes is four defects:
#:   * the observation bundle expired in 10 s (servingCell kept its 10 s
#:     default and validity is the MINIMUM over KPIs), so every model answer
#:     arrived stale -- AIC_OBSERVE_CELL now names it;
#:   * `decision-timeout` was declared but not in NON_TRIAL_KINDS, so the one
#:     path that records a late answer crashed the episode;
#:   * the prompt was serialised with indent=1 (42% whitespace) and wrote out
#:     empty candidate fields;
#:   * two hosts could hold the same tunnel address and every episode was
#:     refused DUPLICATE_UP_TUN_ADDRESS until one was restarted.
#: The previous window is void for comparison anyway: its episodes straddle
#: those fixes, and one arm was recorded BUDGET_EXHAUSTED after running zero
#: trials.
WINDOW = EXP / 'window-20260914T050000'
# Beside this file, so the keeper and the conductor share one ``overnight``
# directory wherever the ops folder lives (it used to be a /tmp scratchpad).
SCRATCH = Path(__file__).resolve().parent
OUT = SCRATCH / 'overnight'
LEDGER = OUT / 'conductor.jsonl'
STATE = OUT / 'conductor-state.json'
LOCK = OUT / 'conductor.lock'
KPM_JSONL = Path('/opt/ran-lab/controller/oran-deploy/session-20260819/lower-live/a1-live-kpm.jsonl')

DEADLINE = dt.datetime.fromisoformat('2026-09-14T10:00:00+09:00')
HOSTS = ('ue1', 'ue2', 'ue3')
# ue3 was released by gnb1 twice in a row at 19:10 and 19:12 - session up,
# then 'Error decoding PBCH' and 'request release after UL failure timer
# expiry' - while the same UE attached to gnb2 immediately and held a
# verified ext-dn downlink (12.1.1.197).  The initial association follows
# the measurement, and the change is recorded rather than assumed.
# 2026-09-16 13:48: ue2 and gNB2 fail only as a PAIR, so ue3 takes the solitary cell.
# Controlled, keeper stopped and PAUSE held, same board, same UE-originated ping:
#   ue2 on gNB2  20/20 lost, UL BLER 1.00000, ulsch_DTX 48502, 19,615 SRB1 RETX exhaustions
#   ue2 on gNB1  0/20 lost, ulsch_errors 0, ulsch_DTX 11, BLER 0.00133 at SNR 16.5 dB
#   ue3 on gNB2  0/20 lost  <- the cell is fine with the other board
# So neither the cell nor the board is broken; only that combination is. This is also the
# placement the overflow counts argued for (ue3 0 deaths in 489 starts takes the lone slot);
# it was reverted on 2026-09-14 only because ue3 would not attach to gNB2 then, and today it does.
# 2026-09-16 15:50, 두 번째 배치 교정. gNB2 는 ue1 하고만 맞는다 — 셀도 보드도 각각은 멀쩡하고
# 조합이 문제다. 90 초 연속 부하로 재측정:
#   ue1 @ gNB2  300/300 수신, 손실 0%, 상향 BLER 0.00000, SNR 14~16, 오프셋 1072 Hz 를 초기동기 1 회로 수렴
#   ue3 @ gNB1  초기동기 1 회, 잔차 860 Hz, SNR 16.3~16.7, BLER 0.00000, ulsch_DTX 0
#   ue3 @ gNB2  보정 후 잔차 -3593/+1473 Hz, SNR 7~9, BLER 0.6~1.0  (수렴 실패)
#   ue2 @ gNB2  핑 20/20 손실 (13:45)
# 같은 셀이 두 보드에 똑같이 보정 가능한 오프셋을 주는데 ue1 만 수렴한다. 물리적 이유는 미상이고
# 원장의 열린 항목으로 남겼다. 이 배치는 게이트가 ue3 때문에 900 초씩 만료되던 것을 멈추기 위한 것이다.
INITIAL_CELLS = {'ue1': 'gnb2', 'ue2': 'gnb1', 'ue3': 'gnb1'}  # 2026-09-28 05:3x back from option B (ue1 cannot hold gnb1); placement B of 2026-09-25
CELL_NB = {'gnb1': 3584, 'gnb2': 2816}

_stop = False


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def log(event: str, **kw) -> None:
    row = {'at': now().isoformat(), 'event': event, **kw}
    with LEDGER.open('a') as f:
        f.write(json.dumps(row) + '\n')
    print(json.dumps(row), flush=True)


def save_state(**kw) -> None:
    prior = {}
    if STATE.is_file():
        try:
            prior = json.loads(STATE.read_text())
        except ValueError:
            prior = {}
    prior.update(kw)
    prior['updatedAt'] = now().isoformat()
    STATE.write_text(json.dumps(prior, indent=1) + '\n')


def _sigterm(signum, frame):  # noqa: ANN001 - signal handler
    global _stop
    _stop = True
    log('SIGNAL_RECEIVED', signal=int(signum))


# -- cohort ---------------------------------------------------------------- #

def _ssh(host: str, argv: list[str], *, stdin: str = '', timeout: int = 90):
    return subprocess.run(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', host, shlex.join(argv)],
        input=stdin, capture_output=True, text=True, timeout=timeout)


_STATUS = ("import json,subprocess;"
           "p=subprocess.run(['ps','-C','nr-uesoftmodem','-o','pid='],capture_output=True,text=True);"
           "q=subprocess.run(['ip','-j','-4','addr','show','up','dev','oaitun_ue1'],"
           "capture_output=True,text=True);"
           "import json as j;"
           "a=j.loads(q.stdout) if q.stdout.strip() else [];"
           "print(j.dumps({'pids':p.stdout.split(),"
           "'ips':[x['local'] for e in a for x in e.get('addr_info',[])]}))")


def _current_epochs(default: str = '528,533') -> str:
    """The epochs the RIC reports right now, in CELL_NB order (gnb1, gnb2).

    Hardcoding this pin is what broke the campaign at 20:24: every gNB restart
    moves its connectionEpoch, the gate then advertises the stale value, and the
    runner's preflight refuses with DEPENDENCY_PREFLIGHT_REFUSED:
    FRESH_TWO_CELL_KPM_REQUIRED because expected_epochs no longer match.
    """
    try:
        out = subprocess.run(['docker', 'exec', 'oran-aic-nearrt-ric', 'cat',
                              '/run/ai-ran/flexric-connection-witness.json'],
                             capture_output=True, text=True, timeout=30).stdout
        seen = {c['globalE2NodeId']['nbId']: c['connectionEpoch']
                for c in json.loads(out).get('connections', []) if c.get('active')}
        pinned = ','.join(str(seen[CELL_NB[cell]]) for cell in ('gnb1', 'gnb2'))
        log('GATE_EPOCH_PIN', epochs=pinned)
        return pinned
    except Exception as exc:  # noqa: BLE001 - fall back and say so
        log('GATE_EPOCH_PIN_FAILED', error=f'{type(exc).__name__}: {exc}'[:120],
            using=default)
        return default


INVENTORY = Path('/opt/ran-lab/controller/oran-deploy/session-20260819/deployment/inventory.json')


def _current_inventory_sha256() -> str:
    import hashlib
    return hashlib.sha256(INVENTORY.read_bytes()).hexdigest()


GATE_RUN = [
    'docker', 'run', '-d', '--name', 'oran-aic-kpm-gate', '--network', 'host',
    '-v', '/opt/ran-lab/controller/a1p-stage/phase-b-rc-capability-patch-3/runtime/flexric:/conf:ro',
    '-v', '/opt/ran-lab/controller/a1p-stage/build-qos-kpm-multislice/kpm_gate_xapp:'
          '/usr/local/bin/kpm_gate_xapp:ro',
    '-v', '/opt/ran-lab/controller/oran-deploy/session-20260819/lower-live:'
          '/opt/ran-lab/controller/oran-deploy/session-20260819/lower-live',
    '-v', '/lib/x86_64-linux-gnu/libsctp.so.1:/lib/x86_64-linux-gnu/libsctp.so.1',
    '-v', '/opt/ran-lab/controller/rlive:/opt/ran-lab/controller/rlive',
    '-v', '/opt/ran-lab/controller/flexric-build:/opt/ran-lab/controller/flexric-build:ro',
    '-v', '/opt/ran-lab/controller/flexric-rc-nested-encoder-fix:'
          '/opt/ran-lab/controller/flexric-rc-nested-encoder-fix:ro',
    '-e', 'KPM_GATE_TOPOLOGY=208-95-2-0xe00-32-20895-12345678,208-95-2-0xb00-32-20895-87654321',
    # A week, not 14400 s: the gate exited every four hours and a board running then
    # lost its KPM identity (2026-09-23 audit).  Its watchdog stays as a backstop.
    '-e', 'KPM_GATE_SECONDS=604800',
    '-e', f'KPM_GATE_JSONL={KPM_JSONL}',
    # Measured from the FlexRIC witness at 19:03 after the RIC and gNB2 were
    # restarted (stateSequence 533): nb 3584 -> 528, nb 2816 -> 533.  The old
    # 523,522 pin predates both restarts and would make the gate bind stale.
    '-e', 'KPM_GATE_CONNECTION_EPOCHS=' + _current_epochs(),
    # The inventory the A1-P producer is bound to, read now: a pinned digest
    # went stale at the 2026-09-15 05:27 re-pin, and every keeper gate restart
    # after it stamped KPM rows the producer refuses (AIC_E2_NOT_READY
    # "gate inventory digest mismatch") -- servingCell steering never reached E2.
    '-e', 'KPM_GATE_INVENTORY_SHA256=' + _current_inventory_sha256(),
    '-e', 'KPM_GATE_SNSSAIS=1,222:00007b',
    'ubuntu:22.04', '/usr/local/bin/kpm_gate_xapp', '-c', '/conf/flexric.conf',
    '-p', '/opt/ran-lab/controller/rlive/sm/',
]


def kpm_fresh(bound_s: float = 8.0) -> dict:
    """Newest indication age per nb_id.  The gate self-exits after 4 h."""
    latest: dict[int, float] = {}
    try:
        with KPM_JSONL.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get('event') != 'kpm_indication':
                    continue
                nb, us = row.get('nb_id'), row.get('recv_unix_us')
                if isinstance(nb, int) and isinstance(us, (int, float)):
                    latest[nb] = max(latest.get(nb, 0.0), float(us))
    except OSError:
        return {}
    ts = time.time() * 1e6
    return {nb: (ts - us) / 1e6 for nb, us in latest.items()}


READY = SCRATCH / 'overnight' / 'deployment-ready.json'   # the keeper's verdict
BUSY = SCRATCH / 'overnight' / 'episode-busy.lock'        # held while a trial runs


def deployment_ready(max_age_s: float = 45.0) -> tuple[bool, str]:
    """Ask the keeper, do not probe the radio.  One process owns hardware actions
    and one owns the experiment; on 2026-09-13 both restarted UEs with separate
    budgets and between them drained the DNN address pool."""
    try:
        doc = json.loads(READY.read_text())
    except (OSError, ValueError):
        return False, 'the keeper has not published a verdict yet'
    try:
        stamp = dt.datetime.strptime(doc['at'], '%Y-%m-%dT%H:%M:%S%z')
    except (KeyError, ValueError):
        return False, 'the keeper verdict has no usable timestamp'
    age = (now() - stamp).total_seconds()
    if age > max_age_s:
        return False, f'the keeper verdict is {age:.0f}s old; is the keeper running?'
    return bool(doc.get('ready')), str(doc.get('reason') or '')


def run_episode(entry: dict) -> dict:
    """One episode through the existing runner, with this entry's overlay."""
    env = dict(os.environ)
    env.update({k: str(v) for k, v in entry['env'].items()})
    before = {p.name for p in WINDOW.glob('formal38guarded-*')}
    started = now()
    log('EPISODE_LAUNCH', name=entry['name'], env=entry['env'])
    BUSY.write_text(str(os.getpid()))
    try:
        proc = subprocess.run([sys.executable, '-u', 'execute_once.py'], cwd=str(WINDOW),
                              env=env, capture_output=True, text=True,
                              timeout=entry.get('timeoutS', 900))
        rc, timed_out = proc.returncode, False
        tail = (proc.stdout or '')[-2500:]
    except subprocess.TimeoutExpired:
        rc, timed_out, tail = 124, True, '(timeout)'
    try:
        BUSY.unlink()
    except OSError:
        pass
    after = {p.name for p in WINDOW.glob('formal38guarded-*')}
    fresh = sorted(after - before)
    result = {'name': entry['name'], 'rc': rc, 'timedOut': timed_out,
              'episodeDirs': fresh, 'elapsedS': round((now() - started).total_seconds(), 1)}
    for name in fresh:
        exitp = WINDOW / name / 'exit.json'
        if exitp.is_file():
            try:
                x = json.loads(exitp.read_text())
                result['submissionStatus'] = x.get('submissionStatus')
                result['cliExit'] = x.get('cliExit')
                result['failure'] = x.get('failure')
            except ValueError:
                pass
    log('EPISODE_DONE', tail=tail[-900:], **result)
    return result


#: The six frozen cases (window-20260914T021900/FROZEN-MANIFESTS-20260914.json).
#: L0 = 2 Mbps and L1 = 10 Mbps: gnb1 serves its two UEs ~14.5 Mbps together, so
#: 0.8L = 8 each is genuinely contended there while gnb2 alone gives 14.6-16.9 --
#: which is where steering can relieve it.  The earlier 3.5 Mbps was below every
#: UE's unaided rate and produced walkovers.
def _frozen_cases() -> dict:
    ledger = json.loads((WINDOW / 'FROZEN-MANIFESTS-20260914.json').read_text())
    out = {}
    for case, meta in ledger['cases'].items():
        out[case] = {
            'policy': meta['policy'], 'level': meta['level'], 'L': meta['L'],
            'env': {
                'AIC_PILOT': str(EXP / meta['dir']),
                'AIC_PILOT_INTENTS_SHA': meta['intentsSha256'],
                'AIC_PILOT_MANIFEST_SHA': meta['manifestSha256'],
                'AIC_OFFERED_LOAD_MBPS': str(meta['L']),
            }}
    return out


CASES = _frozen_cases()

#: Rotation order for the campaign: every case is run once per pass.
CASE_ORDER = tuple(sorted(CASES))

COMMON = {
    'AIC_ENTRY': 'main',
    # Case version v2 (2026-09-13 15:0x).  v1 ran at 2 Mbps per UE and every
    # formal block ended T0_SUCCESS at the initial measurement (1.977/1.978/1.978
    # against a 1.6 threshold): no method ever dispatched a trial and the arms
    # were indistinguishable.  The calibration episode at 3.5 Mbps per UE
    # (formal38guarded-20260913T060420) separated the targets for the first time
    # -- ue1 3.448, ue3 3.457, ue2 1.024, best attained T6, D_max 1.000 -- so the
    # formal blocks move to that load.  Same value for every method; v1 records
    # are preserved and reported separately.
    # The three numbers are settleMs:windowMs:validityMs.  Raising the middle
    # one to 45000 on 2026-09-13 changed the KPI *observation window*, not
    # answer freshness -- freshness is validityMs and was already 120 s.  The
    # reply fixes the observation window at 15 s, so it is restored here.
    #
    # CORRECTION 2026-09-14: "freshness was already 120 s" was WRONG, and it
    # cost this window every decision it made.  A bundle's validity is the
    # MINIMUM over its KPIs, and `servingCell` -- measured but not listed here
    # -- kept its built-in default of 10 s.  Measured: windowEnd 19:06:47.394,
    # validUntil 19:06:57.394.  Decision calls take 7-122 s, so every answer
    # arrived stale; one basic-monolith episode ran zero trials and was still
    # recorded BUDGET_EXHAUSTED.  Name every measured KPI kind, always.
    'AIC_OBSERVE_GOODPUT': 'dlGoodputMbps=5000:15000:120000',
    'AIC_OBSERVE_DEADLINE': 'deadlineSuccessRatio=5000:15000:120000',
    'AIC_OBSERVE_CELL': 'servingCell=1500:1500:120000',
    'AIC_STOP_AFTER_RELAXED': '0',
    'AIC_CELLS': '12345678,87654321',
    # Delivery step 3 asks for PF enabled in this same v2 delivery, and the axis
    # is supported everywhere it matters: it is in DEFAULT_AXIS_KINDS, it has a
    # live readback pair with the cap, and the deployment routes
    # AIC_SchedulerPriority_1.0.0.  It is still OFF here, and the reason is
    # measured rather than assumed.
    #
    # Turning it on needs a way to keep a UE out of the product, because
    # _axis_specs builds a PF axis for every UE an intent names once the kind is
    # exposed, and the built-in ladder has four rungs (0.5/1.0/2.0/4.0).  The cap
    # does this by naming fewer rungs.  PF cannot: pinning ue2 to its baseline
    # 1.0 is refused outright --
    #   "a priority axis needs at least one non-neutral weight"
    # (measured 2026-09-14 on the hardware-free entry with
    #  --pf-axis 131:0.5,2.0 --pf-axis 132:1.0 --pf-axis 133:0.5,2.0).
    # And even with ue2 excluded the ladder product is 144 x 2 x 2 = 576 against
    # the runner's --max-catalog 512, so enabling PF needs a decision about the
    # cap ladder or the cap itself, not just this line.  Recorded as the blocker
    # it is; AIC_PF_HOSTS is wired in the runner (_pf_axis_flags) and ready.
    'AIC_AXES': 'servingCell,dlPrbCap',
    # Reply section 4: the cap scope is U / A12 / A6 / B12 / B6 -- caps on the
    # RESIDENT UEs only (ue1, ue3), never both at once, and never on ue2.
    # Leaving AIC_CAP_HOSTS unset gives every UE a 6,12 ladder, which both
    # adds ue2 and admits simultaneous resident caps; the reply forbids
    # enlarging the catalogue that way.
    # Measured: leaving ue2 out does not remove its cap axis -- the runner
    # builds one for every UE an intent names, and ue2 then falls back to the
    # wider default ladder 0,6,12,18, taking the catalogue to 288.  Naming it
    # with a single rung keeps the resident profiles (U/A12/A6/B12/B6 on ue1
    # and ue3) while holding the catalogue to 144, against 216 if ue2 gets the
    # same three rungs.  ue2 still carries a cap axis, which the reply did not
    # ask for; that is recorded rather than hidden.
    'AIC_CAP_HOSTS': 'ue1:6,12;ue2:6;ue3:6,12',
    # The calibration trial ended in INCIDENT_LOCKDOWN and left an
    # APPLIED_VERIFIED policy on ue2, after which every arm was refused before
    # submission ("withdraw policy d5284245-... on UE 1210").  Authorising the
    # withdrawal is what the framework asks for; it archives the record first.
    'AIC_WITHDRAW_VERIFIED_SCOPE': '1',
    # AIC_PILOT / the two hashes / the offered load come from CASES.
    # 2026-09-13 measurement, kept as the standing risk: after ue2 is steered
    # gnb2 -> gnb1 it served 1.97 Mbps for about 50 s and then dropped off the
    # cell (episode formal38guarded-20260913T042409, collapse at 04:25:47).  B
    # and H were cut below the brief's 480 s for that reason.  The 2026-09-14
    # reply overrides that local decision and restores 480 s, so a UE that dies
    # mid-episode is now an outcome to record -- the episode is retained, the
    # interrupted dispatched trial is counted, and the failure cause is kept --
    # rather than a reason to shorten the clock.
    # Reply 2026-09-14: time zero is input release (AIC_TIMING_MODE=cold-start).
    # 480 s total episode, 8 dispatched trials, 5 s settling after readback,
    # 15 s KPI observation unchanged, 120 s answer freshness recorded separately.
    'AIC_TIMING_MODE': 'cold-start',
    'AIC_FORMATION_S': '240',
    'AIC_DECISION_S': '60',
    'AIC_BUDGET': '8',
    'AIC_DEADLINE_S': '480',   # 2026-09-16 오너 결정: B 는 480 유지, 하드웨어 대기만 면제(_past_deadline)
    'AIC_HORIZON_S': '480',
}


def validation_entry() -> dict:
    env = dict(COMMON, **{
        'AIC_METHOD': 'deterministic',
        'AIC_BUDGET': '2',
        'AIC_DEADLINE_S': '120',
        'AIC_HORIZON_S': '180',
        'AIC_PREPARED_BOARD': str(EXP / 'probe-tc-branchM-20260913.json'),
        'AIC_CONDITION': 'branchM-mobility-validation',
    })
    return {'name': 'branchM-validation', 'env': env, 'timeoutS': 1500}


def calibration_entry() -> dict:
    """One deterministic measurement at a raised offered load.

    Every formal block so far ended T0_SUCCESS at the initial measurement: at
    2 Mbps per UE the three flows delivered 1.977 / 1.978 / 1.978 against a
    1.6 Mbps threshold, so no method ever dispatched a trial and the three arms
    were indistinguishable (episodes formal38guarded-20260913T053612 / T054044 /
    T054546).  The threshold for the next case version has to come from a
    measurement rather than a guess, so this reads what the present placement
    delivers at 3.5 Mbps per UE -- the receiver's own guard refuses anything
    above --max-rate-mbps 8.  It runs deterministically (no model cost) and is
    recorded as calibration, kept out of the formal comparison.
    """
    env = dict(COMMON, **{
        'AIC_METHOD': 'deterministic',
            'AIC_BUDGET': '1',
        'AIC_DEADLINE_S': '120',
        'AIC_HORIZON_S': '150',
        'AIC_CONDITION': 'calibration-load3p5',
    })
    return {'name': 'calibration-load3p5', 'env': env, 'timeoutS': 1500}


def formal_block(block: int, rng, case: str = 'C4') -> list[dict]:
    """One matched block: the same frozen case under every active method.

    Method order is randomised per block with the seed recorded in the ledger,
    so an ordering effect cannot be mistaken for a method effect.  The case
    carries its own pilot, hashes and offered load, so every arm of a block sees
    exactly the same demand.
    """
    spec = CASES[case]
    # A missing API configuration is a refusal, not a different experiment arm.
    methods = (['deterministic'] if os.environ.get('AIC_METHOD') == 'deterministic'
               else ['three-agent', 'basic-monolith', 'internal-monolith'])
    rng.shuffle(methods)
    entries = []
    for method in methods:
        env = dict(COMMON, **spec['env'], **{
            'AIC_METHOD': method,
            'AIC_CONDITION': f"{case}-{spec['policy']}-{spec['level']}-block{block}",
            'AIC_BLOCK': str(block),
            'AIC_CASE': case,
            'AIC_REPETITION': str(block // len(CASE_ORDER)),
        })
        # Only the non-secret overlay is logged. Provider credentials stay in
        # the caller's environment and are inherited by run_entry's child.
        sys.path.insert(0, str(EXP))
        try:
            from atomic_formal_run_guarded import llm_configuration_preflight
            configured = dict(os.environ)
            configured.update(env)
            llm_configuration_preflight(environment=configured)
        finally:
            sys.path.pop(0)
        entries.append({'name': f'block{block}-{case}-{method}', 'env': env, 'timeoutS': 1500})
    return entries


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            pid = int(LOCK.read_text().strip())
            os.kill(pid, 0)
            print(f'conductor already running as pid {pid}', file=sys.stderr)
            return 9
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    LOCK.write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)
    log('CONDUCTOR_START', pid=os.getpid(), deadline=DEADLINE.isoformat(), window=WINDOW.name)
    save_state(pid=os.getpid(), stage='start', deadline=DEADLINE.isoformat())
    results: list[dict] = []
    import random
    seed = int(now().timestamp())
    rng = random.Random(seed)
    log('CAMPAIGN_SEED', seed=seed)
    # Persist the verdict: the supervisor restarts this process, and a restart
    # must not re-run a validation that already succeeded.
    flag = OUT / 'branch-validated.json'
    validated = flag.is_file()
    cal_flag = OUT / 'calibration-load3p5.json'
    calibrated = cal_flag.is_file()
    # The counter stamps AIC_BLOCK and AIC_CONDITION, so it has to survive a
    # supervisor restart.  Restarting it at 0 stamps a second, different matched
    # block with an id already used, and nothing downstream can tell them apart:
    # the conductor restarted five times on 2026-09-13 alone.
    block = 0
    if STATE.is_file():
        try:
            block = int(json.loads(STATE.read_text()).get('block') or 0)
        except (ValueError, TypeError):
            block = 0
    if validated:
        log('BRANCH_ALREADY_VALIDATED', record=json.loads(flag.read_text()))
    try:
        while not _stop and now() < DEADLINE:
            if not validated:
                queue = [validation_entry()]
            elif not calibrated:
                queue = [calibration_entry()]
                log('CALIBRATION_START', load='3.5')
            else:
                # One block per case, in order, repeating: block N runs
                # CASE_ORDER[N % 6] and its repetition index is N // 6.  The
                # block counter survives a restart, so the rotation does too --
                # restarting at C1 every time would over-sample the first case.
                case = CASE_ORDER[block % len(CASE_ORDER)]
                queue = formal_block(block, rng, case=case)
                block += 1
                log('BLOCK_START', block=block, case=case,
                    repetition=(block - 1) // len(CASE_ORDER),
                    entries=[e['name'] for e in queue])
            for entry in queue:
                if _stop or now() >= DEADLINE:
                    log('QUEUE_STOPPED',
                        reason='deadline' if now() >= DEADLINE else 'signal',
                        remaining=entry['name'])
                    break
                save_state(stage=entry['name'], block=block)
                # Hold this arm until the deployment is ready. Skipping to the
                # next entry drains a whole matched block in 90 s and burns the
                # block number with nothing run, which is what happened from
                # 20:47 to 20:54 (blocks 2-5, zero arms).
                ready, why = deployment_ready()
                while not ready and not _stop and now() < DEADLINE:
                    log('WAITING_FOR_DEPLOYMENT', name=entry['name'], reason=why)
                    time.sleep(30)
                    ready, why = deployment_ready()
                if not ready:
                    log('QUEUE_STOPPED',
                        reason='deadline' if now() >= DEADLINE else 'signal',
                        remaining=entry['name'])
                    break
                result = run_episode(entry)
                results.append(result)
                save_state(results=results[-20:])
                if (not validated and result.get('submissionStatus') == 'STARTED_EPISODE'
                        and result.get('cliExit') in (0, 2)):
                    validated = True
                    (OUT / 'branch-validated.json').write_text(json.dumps(
                        {'branch': 'M', 'at': now().isoformat(),
                         'episodeDirs': result.get('episodeDirs')}, indent=1) + '\n')
                    log('BRANCH_M_VALIDATED', name=entry['name'])
                if (validated and not calibrated
                        and result.get('submissionStatus') == 'STARTED_EPISODE'):
                    calibrated = True
                    cal_flag.write_text(json.dumps(
                        {'offeredLoadMbpsPerUe': 3.5, 'at': now().isoformat(),
                         'episodeDirs': result.get('episodeDirs')}, indent=1) + '\n')
                    log('CALIBRATION_DONE', episodeDirs=result.get('episodeDirs'))
                time.sleep(10)
            if not validated:
                # A validation attempt that did not start an episode: try again,
                # but never faster than once a minute, and never blindly forever.
                block += 1
                if block >= 3:
                    log('BRANCH_M_UNVALIDATED', attempts=block,
                        note='falling back to Branch F: fixed association, all three UEs scored')
                    validated = True
                    block = 0
                time.sleep(60)
    finally:
        save_state(stage='finished', results=results, finishedAt=now().isoformat())
        log('CONDUCTOR_END', results=results)
        try:
            LOCK.unlink()
        except OSError:
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
