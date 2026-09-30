#!/usr/bin/env python3
"""v4.7 block reference DL (owner + scenario author, 2026-09-25).

Before each comparison block, measure every UE's goodput at the campaign's 10 Mbps load in
three fixed configurations on placement B (PF 1, no cap, no handover):

    A  ue1 10  ue2 10  ue3 10    -> ue1 reference, gnb1 reference sum (ue2+ue3 per window)
    B  ue1 10  ue2 10  ue3 keep  -> ue2 reference
    C  ue1 10  ue2 keep ue3 10   -> ue3 reference

"keep" is a 0.2 Mbps keepalive, not a stop: an unloaded UE is RRC-released after ~15 s on
this bed and would miss the next configuration.  Goodput is the UE receiver's application
payload (flow_goodput.py, the same source a board measures), never tun bytes.  Each value is
the median of 5 windows of WINDOW_S after SETTLE_S.  The reference is fixed for the block and
is a goodput under the 10 Mbps load, not a cell capacity.

    python3 ops/reference_dl.py [--out FILE]      (refuses while a board holds the bed)
"""
import argparse
import json
import math
import os
import re
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = '/tmp/aic-flow-9140cea9b49e-beaa921ad26a/flow_goodput.py'
EXT_DN, EXT_DN_IP, PORT = 'oai-ext-dn', '192.168.70.135', 5213
LOAD, KEEP = float(os.environ.get('AIC_CASE_LOAD') or 10.0), 0.2   # the campaign's per-UE load
SETTLE_S, WINDOW_S, WINDOWS = 15.0, 10.0, 5
def configs(load):
    return {'A': {'ue1': load, 'ue2': load, 'ue3': load},
            'B': {'ue1': load, 'ue2': load, 'ue3': KEEP},
            'C': {'ue1': load, 'ue2': KEEP, 'ue3': load}}


CONFIGS = configs(LOAD)


def sh(cmd, timeout=30):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout


def tun_ip(host, tries=1, gap_s=10):
    """The UE's tun address, read up to `tries` times (measure() retries; the other callers keep
    their own timing and read once)."""
    for i in range(tries):
        out = sh(f"ssh {host} \"ip -4 -o addr show up dev oaitun_ue1 | awk '{{print \\$4}}' | cut -d/ -f1\"")
        if out.strip():
            return out.strip()
        if i + 1 < tries:
            time.sleep(gap_s)
    return None


def windows_from(log_lines, settle_s=SETTLE_S, window_s=WINDOW_S, count=WINDOWS):
    """Per-window goodput (Mbps) from receiver heartbeats (UE monotonic atMs, payloadBytes)."""
    beats = [(r['atMs'] / 1000.0, r['payloadBytes']) for r in
             (json.loads(line) for line in log_lines if line.strip())
             if r.get('event') == 'heartbeat' and r.get('payloadBytes') is not None]
    moving = [b for b in beats if b[1] > 0]
    if not moving:
        return []
    t0 = moving[0][0] + settle_s
    out = []
    for k in range(count):
        a, b = t0 + k * window_s, t0 + (k + 1) * window_s
        inside = [x for x in beats if a <= x[0] <= b]
        if len(inside) < 2:
            break
        (ta, pa), (tb, pb) = inside[0], inside[-1]
        if tb - ta < 0.8 * window_s:          # a window must actually be covered
            break
        out.append(round((pb - pa) * 8 / (tb - ta) / 1e6, 3))
    return out if len(out) == count else []   # five complete windows or nothing


def measure(config, stamp, echo=False):
    rates = CONFIGS[config]
    duration = SETTLE_S + WINDOW_S * WINDOWS + 20
    logs = {}
    # 09-30 08:47: ue1's reads over its wireless control link came back empty for ~2 min with the UE
    # attached throughout, and a one-shot read refused the block-4 reference.  Every address is
    # resolved (with retries) before echo and senders start, so no UE's flow starts late (Codex).
    ips = {host: tun_ip(host, tries=6) for host in rates}
    missing = [host for host, ip in ips.items() if not ip]
    if missing:
        raise SystemExit(f'REFERENCE_REFUSED: {missing[0]} has no tun address')
    echo_log = _start_echo(config, stamp, duration) if echo else None
    for host, rate in rates.items():
        ip = ips[host]
        sid, fid = f'ref-{stamp}-{config}', f'{host}-ref'
        logs[host] = f'/tmp/aic-ref-{stamp}-{config}-{host}.jsonl'
        sh(f"ssh {host} 'nohup timeout {duration + 10:.0f} python3 {SRC} receiver --bind-ip {ip} "
           f"--port {PORT} --session-id {sid} --flow-id {fid} --log {logs[host]} "
           f"--allow-source-ip {EXT_DN_IP} --duration-s {duration + 5:.0f} --max-rate-mbps {max(12, 1.2 * max(rates.values())):.0f} "
           f">/dev/null 2>&1 &'")
        time.sleep(1.0)
        # flow_goodput's sender takes no --log (Codex review #1): its output goes to a file.
        sh(f"docker exec -d {EXT_DN} sh -c 'timeout {duration + 5:.0f} python3 {SRC} sender "
           f"--receiver-ip {ip} --port {PORT} --session-id {sid} --flow-id {fid} "
           f"--duration-s {duration:.0f} --rate-mbps {rate} "
           f"> /tmp/aic-ref-{stamp}-{config}-{host}-send.out 2>&1'")
    time.sleep(duration + 3 + (ECHO_TIMEOUT_MS / 1000.0 if echo else 0))
    out = {host: windows_from(sh(f"ssh {host} cat {path}").splitlines())
           for host, path in logs.items()}
    if echo_log:
        out['_ue2EchoRttsMs'] = echo_rtts(sh(f"ssh ue2 cat {echo_log}", timeout=60).splitlines())
    return out



#: Saturation send rate for the solo-max rule: well above what one UE can take (probe 20:54:
#: ue2 11.8, ue3 12.8 at 15 Mbps).
SATURATION_MBPS = 25.0
RULE_FILE = HERE / 'overnight' / 'V47_LOAD_RULE'


def load_rule():
    """'solo-max' when overnight/V47_LOAD_RULE says so (owner 2026-09-25 21:2x), else None."""
    try:
        return RULE_FILE.read_text().strip() or None
    except OSError:
        return None


def solo_max_load(saturation_raw):
    """L = mean of ue2's and ue3's solo maximum goodput (configs B and C at saturation) -- the two
    UEs that share gnb1.  Measured only; no chosen factor.  None if a window set is unusable."""
    b = saturation_raw.get('B', {}).get('ue2') or []
    c = saturation_raw.get('C', {}).get('ue3') or []
    if len(b) < WINDOWS or len(c) < WINDOWS or min(b + c) <= 0:
        return None
    return round((statistics.median(b) + statistics.median(c)) / 2.0, 1)


#: v5 (owner 2026-09-26): no per-block power calibration.  The block records gnb2's baseline
#: attenuation read fresh from KPM (the energy target is baseline + a fixed policy offset set in
#: the corpus), and UE2's deadline d from config A's tagged echo (DEADLINE_RULE; B's before block 15).
POWER_NB = 3584 if os.environ.get('AIC_V51_ENERGY_CELL', '').strip() == 'gnb1' else 2816
#: gnb1 has no power-calibration file; its bed baseline is the runner's (run_episode.BASELINE_ATTENUATION_DB).
GNB1_BASELINE_DB = 8.0
#: A reply later than this is an incomplete request; d is valid only if 90 % of ALL requests
#: sent in B's windows completed within it.  Recorded in the reference JSON.
ECHO_TIMEOUT_MS = 5000
#: Owner 2026-09-26 05:4x (v5 deadline revision, from block 15): d is the time by which 75% of
#: ALL of UE2's echo requests in configuration A (the shared start state) completed -- midway
#: between the requirement 0.90 and the floor 0.60.  B's echo stays as a diagnostic record.
#: Block 14 (d from B at p90 = 52.6 ms, A@d = 0.0 in all 8 trials of its first board) is the
#: earlier preliminary rule.
ECHO_QUANTILE = 0.75
DEADLINE_RULE = 'A-p75'
ECHO_RATE_HZ = 5.0
#: Share of the expected requests the measured span must hold (Codex review 2026-09-26).
ECHO_MIN_COVERAGE = 0.8
ECHO_PORT = 5299
ECHO_SRC = os.path.join(os.path.dirname(SRC), 'tagged_echo.py')
REPO = HERE.parents[2]


def _board_profile():
    """A copy of the newest board's live profile with its own state and evidence dirs."""
    boards = sorted(HERE.parent.glob('formal38guarded-*/profile.json'), key=lambda p: p.stat().st_mtime)
    if not boards:
        raise RuntimeError('no board profile to borrow the live binding from')
    doc = json.loads(boards[-1].read_text())
    out = HERE / 'overnight' / 'v5-reference'
    out.mkdir(parents=True, exist_ok=True)
    block = doc['liveConsole']
    block['r1StateDir'] = str(out / 'r1-state')
    block['evidenceDir'] = str(out / 'evidence')
    if block.get('actionProducer'):
        block['actionProducer']['r1StateDir'] = str(out / 'action-r1-state')
    path = out / 'profile.json'
    path.write_text(json.dumps(doc, indent=1))
    return path, out


def _kpm_path(profile):
    sys.path.insert(0, str(REPO))
    from tools.liveconsole.profile import load_live_deployment
    return Path(load_live_deployment(profile).kpm_jsonl_path)


POWER_UNSAFE = HERE / 'overnight' / 'POWER_UNSAFE'


def cell_attenuation(kpm, nb=POWER_NB, tail_bytes=3_000_000):
    """The newest RAN.Cell.TxAttenuationDb of node ``nb``: (dB, epoch, recv_unix_us) or None.
    Parsed as JSON and compared by typed nb id (Codex #7), so whitespace or key order cannot
    make the cell vanish."""
    try:
        with open(kpm, 'rb') as fh:
            fh.seek(max(0, kpm.stat().st_size - tail_bytes))
            lines = fh.read().decode(errors='ignore').splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if 'TxAttenuationDb' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get('nb_id') != nb:
            continue
        for m in row.get('measurements') or row.get('meas') or []:
            if m.get('name') == 'RAN.Cell.TxAttenuationDb' and m.get('value') is not None:
                try:
                    received = int(row.get('recv_unix_us') or 0)
                except (TypeError, ValueError):
                    received = 0          # never fresh: a wait on it fails closed
                return (round(float(m['value']), 1), row.get('connection_epoch'), received)
    return None


def echo_rtts(log_lines, settle_s=SETTLE_S, span_s=WINDOW_S * WINDOWS):
    """One entry per request issued inside the measured span (after the settle): its RTT in
    ms, or None when no reply was logged.  tagged-echo-log/1: ``issued`` carries seq and
    issuedAtMs, ``reply`` carries seq and rttMs (UE monotonic clock)."""
    rows = []
    for line in log_lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    issued = {r['seq']: r['issuedAtMs'] for r in rows
              if r.get('event') == 'issued' and 'seq' in r and 'issuedAtMs' in r}
    replies = {r['seq']: float(r['rttMs']) for r in rows
               if r.get('event') == 'reply' and 'seq' in r and 'rttMs' in r}
    if not issued:
        return []
    # A source that stopped early is not a measurement (Codex review 2026-09-26): the span
    # must hold at least ECHO_MIN_COVERAGE of the requests a 5 Hz client issues in it.
    expected = ECHO_RATE_HZ * span_s
    start = min(issued.values()) + settle_s * 1000.0
    end = start + span_s * 1000.0
    measured = [replies.get(seq) for seq, at in sorted(issued.items()) if start <= at < end]
    return measured if len(measured) >= ECHO_MIN_COVERAGE * expected else []


def fixed_deadline_ms():
    """The fixed UE2 deadline: ``AIC_FIXED_DEADLINE_MS``, or 35 ms under v5.2; None keeps A-p75."""
    raw = os.environ.get('AIC_FIXED_DEADLINE_MS', '').strip()
    if raw:
        return float(raw)
    return 35.0 if os.environ.get('AIC_V52', '').strip() == '1' else None


def echo_deadline_ms(rtts, quantile=ECHO_QUANTILE, timeout_ms=ECHO_TIMEOUT_MS):
    """The time by which ``quantile`` of ALL requests completed (no reply and a reply later
    than ``timeout_ms`` are incomplete and stay in the denominator), or None if that never
    happens.  Never widened to fit."""
    if not rtts:
        return None
    done = sorted(r for r in rtts if r is not None and r <= timeout_ms)
    need = math.ceil(quantile * len(rtts))
    # The exact order statistic (Codex review 2026-09-26): rounding could put d below the
    # request it was taken from and break the 90% definition.  Round only for display.
    return float(done[need - 1]) if len(done) >= need else None


def echo_success(rtts, deadline_ms):
    """Share of ALL requests answered within ``deadline_ms`` (no reply counts as a miss)."""
    if not rtts or deadline_ms is None:
        return None
    return round(sum(1 for r in rtts if r is not None and r <= deadline_ms) / len(rtts), 4)


def _start_echo(config, stamp, duration):
    """UE2's tagged echo, the tool and cadence a board uses (256 B at 5 Hz); the reflector at
    the ext-DN admits 20 Hz as in guarded_source_process.py."""
    sid = f'ref-{stamp}-{config}-echo'
    log = f'/tmp/aic-ref-{stamp}-{config}-ue2-echo.jsonl'
    sh(f"docker exec -d {EXT_DN} sh -c 'timeout {duration + 20:.0f} python3 {ECHO_SRC} server "
       f"--session-id {sid} --flow-id ue2-refecho --port {ECHO_PORT} --duration-s {duration + 15:.0f} "
       f"--rate-hz 20 --bind-ip {EXT_DN_IP} --allow-subnet 192.168.70.134/32 >/dev/null 2>&1'")
    time.sleep(1.0)
    sh(f"ssh ue2 'nohup timeout {duration + 20:.0f} python3 {ECHO_SRC} client --session-id {sid} "
       f"--flow-id ue2-refecho --port {ECHO_PORT} --duration-s {duration:.0f} --rate-hz 5 "
       f"--server-ip {EXT_DN_IP} --interface oaitun_ue1 --payload-bytes 256 "
       f"--reply-drain-s {ECHO_TIMEOUT_MS / 1000:.0f} --log {log} >/dev/null 2>&1 &'")
    return log


def energy_baseline_db():
    """The energy cell's attenuation now (gnb2, or gnb1 under AIC_V51_ENERGY_CELL=gnb1), from a
    record received in the last 30 s, or refuse."""
    profile, _out = _board_profile()
    got = cell_attenuation(_kpm_path(profile))
    now_us = time.time() * 1e6
    # Bounded in both directions (Codex review 2026-09-26): a future-dated record is not fresh.
    if got is None or not (now_us - 30e6 <= got[2] <= now_us + 5e6):
        raise SystemExit('REFERENCE_REFUSED: gnb2 attenuation not on the KPM stream in the last 30 s')
    # 2026-09-26 05:49: board 681's ue2 re-registered during a 12 dB power trial, the rebound
    # case took 12 dB as its initial configuration, and gnb2 stayed at 12 dB -- block 15's
    # reference would have frozen 12 dB as A0.  The board gate already refuses a gnb2 value off
    # the recorded bed baseline; the reference must too (the runner retries it).
    recorded = HERE / 'overnight' / 'power-calibration' / 'baseline.json'
    if POWER_NB == 3584 or recorded.exists():
        want = (GNB1_BASELINE_DB if POWER_NB == 3584
                else round(float(json.loads(recorded.read_text())['db']), 1))
        if round(float(got[0]), 1) != want:
            raise SystemExit(f'REFERENCE_REFUSED: gnb2 attenuation {got[0]} dB is not the bed '
                             f'baseline {want} dB (an unrestored power trial or retained policy)')
    return got[0]


def unstable_on_last_attempt(out, max_age_s=1800):
    """UEs a failed attempt at this same reference named unstable ('B:ue2' -> 'ue2').
    2026-09-26 04:1x-04:39: block 14 failed three times on one ue2 attach (a ~20 s DL stall,
    echo RTT 5-8.7 s); a fresh attach cleared it.  Retrying on the same attach repeats it."""
    try:
        path = Path(out)
        if time.time() - path.stat().st_mtime > max_age_s:
            return []
        return sorted({u.split(':')[-1] for u in json.loads(path.read_text()).get('unstable') or []})
    except (OSError, ValueError, TypeError):
        return []


def weak_on_previous_block(out, frac=0.85):
    """UEs whose best loaded median in the campaign's previous block reference was under
    `frac` of the load.  2026-09-27 blocks 0-2 (v5.1): ue3 alone on gnb1 read 9.87, 8.40, 6.68
    (RSRP -94, DL MCS p75 5) while the SR/RNTI ageing test saw nothing; a USB-reset reattach
    cleared the same state after the 09-27 power cycle."""
    try:
        out = Path(out)
        head, tail = out.name.split('.block', 1)
        block = int(tail.split('.', 1)[0])
        prev = out.parent / f'{head}.block{block - 1}.reference.json'
        if block < 1 or not prev.exists():
            return []
        raw = json.loads(prev.read_text())
        return weak_hosts(raw['raw'], float(raw.get('loadMbps') or LOAD), frac)
    except (OSError, ValueError, TypeError, KeyError):
        return []


def _refreshed_on_last_attempt(out, max_age_s=1800):
    """Hosts a recent failed attempt at this same reference had already refreshed."""
    try:
        path = Path(out)
        if time.time() - path.stat().st_mtime > max_age_s:
            return []
        return list(json.loads(path.read_text()).get('refreshedThisBlock') or [])
    except (OSError, ValueError, TypeError):
        return []


def deadline_starved(raw, load, frac=0.85):
    """The gnb1 UEs, when ue2 could not carry its load in configuration A -- the deadline d is
    measured there.  Blocks 6 and 8 (2026-09-27): ue2 got 5.7 / 6.0 Mbps in A on a gnb1 whose
    contexts had sunk to a low MCS, its queue grew and d came out 2912 / 2755 ms against 34-40 ms
    in every block where ue2 carried 9.88 -- a deadline requirement nobody can miss."""
    v = (raw.get('A') or {}).get('ue2') or []
    return ['ue2', 'ue3'] if v and statistics.median(v) < frac * load else []


def weak_hosts(raw, load, frac=0.85):
    """Hosts whose best configuration median in `raw` ({config: {host: [Mbps]}}) is under
    `frac` of the load."""
    best = {}
    for rates in raw.values():
        for host, v in rates.items():
            if v:
                best[host] = max(best.get(host, 0.0), statistics.median(v))
    return sorted(h for h, m in best.items() if m < frac * load)


def refresh_aged_ues(also=()):
    """Re-attach, before measuring, every UE whose softmodem shows ageing -- the same test and
    the same procedure the preventive-maintenance daemon uses (ue_ageing_check.py, keeper
    stopped, force_reattach.py with a USB reset, keeper started).  2026-09-26: block 10's
    reference was taken on an aged ue2 (solo 8.66 against 13.6 the block before); the daemon
    refreshed it mid-block and two boards became walkovers against the low levels.  A
    reference has to describe the UEs the boards will run on."""
    stale = sh(f"cd {HERE} && python3 ue_ageing_check.py 2>/dev/null", timeout=120).split()
    # 2026-09-29 08:47: ue1 lost its address in the block 4 reference; the retry stopped the keeper
    # (at streak 2/3 for ue1) and refreshed only the aged ue2/ue3 -- nobody restarts a dead UE here.
    # (Codex) not a UE mid-reattach nor an ssh blip: absent twice, 20 s apart, on a host that answers.
    dead = [h for h in ('ue1', 'ue2', 'ue3') if not tun_ip(h)]
    if dead:
        time.sleep(20)
        dead = [h for h in dead if not tun_ip(h) and sh(f"ssh {h} echo ok").strip() == 'ok']
    # 09-30 13:48: board 991's failed recovery left ue3 on the other cell; the preflight refused the
    # block-0 reference for it on every retry and nothing here re-attached it.
    wrong_cell = [host for host, _cell, _row in misplaced()]
    stale = sorted(set(stale) | set(also) | set(dead) | set(wrong_cell))
    if not stale:
        return []
    placement = sh(f"cd {HERE} && python3 read_placement.py 2>/dev/null", timeout=60).split()
    pairs = [pair for pair in placement if pair.split('=')[0] in stale]
    if not pairs:
        return []
    sh("systemctl --user stop aic-keeper", timeout=150)  # 2026-09-28: the unit may take its whole 90 s TimeoutStopSec mid UE start; 60 s failed three references
    try:
        sh(f"cd {HERE} && AIC_REATTACH_USB_RESET=1 python3 force_reattach.py {' '.join(pairs)}",
           timeout=600)
        time.sleep(35)
    finally:
        # 2026-09-26 04:57: a stop that timed out (keeper mid chain-rebuild, SIGKILL after 90 s)
        # left the unit 'failed' and this start did nothing -- the bed ran without a keeper.
        for _ in range(3):
            sh("systemctl --user reset-failed aic-keeper; systemctl --user start aic-keeper", timeout=150)
            if sh("systemctl --user is-active aic-keeper").strip() == 'active':
                break
            time.sleep(10)
    return pairs


def preflight():
    """Refuse before touching the bed (Codex review #7, #21): the load tool must be where
    the boards put it, and every UE must be attributed to its placement-B cell."""
    if POWER_UNSAFE.exists():
        raise SystemExit('REFERENCE_REFUSED: POWER_UNSAFE is set (' + POWER_UNSAFE.read_text().strip()
                         + '); verify gnb2 attenuation, then remove the file')
    for host in ('ue1', 'ue2', 'ue3'):
        if sh(f"ssh {host} test -f {SRC} && echo ok").strip() != 'ok':
            raise SystemExit(f'REFERENCE_REFUSED: {SRC} missing on {host}')
    if sh(f"docker exec {EXT_DN} test -f {SRC} && echo ok").strip() != 'ok':
        raise SystemExit(f'REFERENCE_REFUSED: {SRC} missing in {EXT_DN}')
    # v5: the UE2 deadline needs the echo tool at both ends.
    if (sh(f"ssh ue2 test -f {ECHO_SRC} && echo ok").strip() != 'ok'
            or sh(f"docker exec {EXT_DN} test -f {ECHO_SRC} && echo ok").strip() != 'ok'):
        raise SystemExit(f'REFERENCE_REFUSED: {ECHO_SRC} missing on ue2 or in {EXT_DN}')
    # A UE with a dead downlink fails all three configurations after five minutes under the
    # bed lock, and the lock keeps the keeper from restarting it (13:09 ue2, 2026-09-25).
    # Unloaded, a live UE answers ping; refuse in seconds and leave the bed to the keeper.
    # 2026-09-26 11:25: a UE just refreshed re-registered once more (12.1.1.138 -> .139) and a
    # single look refused the block's reference.  Re-read the address and retry for 60 s.
    for host in ('ue1', 'ue2', 'ue3'):
        deadline = time.time() + 60
        while True:
            ip = tun_ip(host)
            got = sh(f"docker exec {EXT_DN} timeout 6 ping -c 3 -i 0.5 -W 2 {ip or '0.0.0.0'} "
                     f"| grep -o '[0-9]* received'")
            if ip and got.strip() and not got.strip().startswith('0 '):
                break
            if time.time() > deadline:
                raise SystemExit(f'REFERENCE_REFUSED: {host} downlink dead ({ip}: {got.strip() or "no reply"})')
            time.sleep(5)
    for host, cell, row in misplaced():
        raise SystemExit(f'REFERENCE_REFUSED: {host} not attributed to {cell} only ({row[:80]})')


def misplaced():
    """(host, placed cell, watch row) for every UE the data-path watch does not show on its placed
    cell alone (conductor.py INITIAL_CELLS)."""
    src = (HERE / 'conductor.py').read_text(encoding='utf-8')
    placed = dict(re.findall(r"'(ue[123])'\s*:\s*'(gnb[12])'",
                             re.search(r"INITIAL_CELLS\s*=\s*\{([^}]*)\}", src).group(1)))
    nb = {'gnb1': '3584', 'gnb2': '2816'}
    lines = (HERE / 'overnight' / 'datapath-watch.log').read_text(errors='ignore').splitlines()[-3:]
    out = []
    for host, cell in placed.items():
        row = next((l for l in lines if f' {host} ' in l), '')
        if f'kpm=[{nb[cell]}]' not in row:
            out.append((host, cell, row))
    return out


def _solo(raw, config, host):
    """``host``'s solo reference: the median of ``config``, raised to its median in A when A is higher."""
    solo = raw.get(config, {}).get(host) or []
    shared = raw.get('A', {}).get(host) or []
    values = [statistics.median(v) for v in (solo, shared) if v]
    if not solo:
        return None
    return round(max(values), 3)


def _unstable(raw):
    """Loaded UEs whose five windows are not all positive and within half their median, or that
    gave no window at all (2026-09-28 00:5x: ue1 had lost its gnb2 context, measured nothing, was
    named nowhere and so was never reattached; the block failed on it attempt after attempt)."""
    out = [f'{c}:{h}' for c, rates in CONFIGS.items() for h, rate in rates.items()
           if rate == LOAD and (not raw.get(c, {}).get(h) or
                                min(raw[c][h]) <= 0 or min(raw[c][h]) < 0.5 * statistics.median(raw[c][h]))]
    # 2026-09-30 (v54r blocks 0-1, v54q block 1): ue2 alone in B got 1.9-2.7 Mbps while A, with ue3
    # loaded too, gave it 4.8-5.4 -- B's single TCP sender had collapsed (27 MB in 85 s vs 59 MB in A),
    # and ue2's block reference (the median of B) set its targets from that.  A UE alone cannot
    # get less than when it shares its cell: a solo median under 80 % of the shared one is re-measured.
    for c, host in (('B', 'ue2'), ('C', 'ue3')):
        solo, shared = raw.get(c, {}).get(host), raw.get('A', {}).get(host)
        if solo and shared and statistics.median(solo) < 0.8 * statistics.median(shared):
            out.append(f'solo-below-shared:{host}')
    return out


def main():
    global LOAD, CONFIGS
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=None)
    ap.add_argument('--load', type=float, default=LOAD,
                    help='per-UE offered load in Mbps (diagnostic; the campaign uses the default)')
    args = ap.parse_args()
    LOAD, CONFIGS = args.load, configs(args.load)
    lock = HERE / 'overnight' / 'episode-busy.lock'
    owner = lock.read_text().split()[0] if lock.exists() and lock.read_text().strip() else ''
    if owner and Path(f'/proc/{owner}').exists():
        raise SystemExit('REFERENCE_REFUSED: a board holds the bed')
    refreshed = refresh_aged_ues((unstable_on_last_attempt(args.out) + weak_on_previous_block(args.out))
                                 if args.out else ())
    if refreshed:
        print('refreshed aged UEs before the reference: ' + ' '.join(refreshed), file=sys.stderr)
    preflight()
    energy_baseline_db()   # refuse before measuring, not after 9 minutes (checked again at the end)
    stamp = time.strftime('%Y%m%dT%H%M%S')
    # Hold the bed like a board does (Codex review #8): the keeper and the runner both
    # respect this lock, so nothing steers, caps or restarts a UE under the measurement.
    lock.write_text(f'{os.getpid()} reference-dl {stamp}\n')
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit('SIGTERM')))
    saturation = None
    echoes = {}
    try:
        if load_rule() == 'solo-max' and args.load == float(os.environ.get('AIC_CASE_LOAD') or 10.0):
            # Solo maximum first (B and C at a saturating send rate), then the block's send rate.
            LOAD, CONFIGS = SATURATION_MBPS, configs(SATURATION_MBPS)
            saturation = {c: measure(c, stamp + '-sat') for c in ('B', 'C')}
            chosen = solo_max_load(saturation)
            if chosen is None:
                raise SystemExit('REFERENCE_REFUSED: saturation windows unusable for the solo-max rule')
            LOAD, CONFIGS = chosen, configs(chosen)
        # v5: UE2's tagged echo runs in B (it defines d) and in A (the start state at d).
        raw = {c: measure(c, stamp, echo=c in ('A', 'B')) for c in CONFIGS}
        for c in ('A', 'B'):
            echoes[c] = raw[c].pop('_ue2EchoRttsMs', [])
        energy_db = energy_baseline_db()
    finally:
        # Every host is cleaned independently (Codex review #10); the board's own 10 Mbps
        # sources come back when its runner starts them (plan section 2.1).
        for cmd in [f"ssh {h} \"pkill -f 'session-id ref-{stamp}' || true\"" for h in ('ue1', 'ue2', 'ue3')] \
                + [f"docker exec {EXT_DN} pkill -f 'session-id ref-{stamp}' || true"]:
            try:
                sh(cmd)
            except Exception as exc:  # noqa: BLE001
                print(f'cleanup failed: {cmd}: {exc}', file=sys.stderr)
        try:
            if lock.read_text().split()[0] == str(os.getpid()):
                lock.unlink()
        except Exception:  # noqa: BLE001
            pass
    med = lambda xs: round(statistics.median(xs), 3) if xs else None
    gnb1 = [round(a + b, 3) for a, b in zip(raw['A'].get('ue2', []), raw['A'].get('ue3', []))]
    result = {'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'loadMbps': LOAD, 'keepaliveMbps': KEEP,
              'settleS': SETTLE_S, 'windowS': WINDOW_S, 'windows': WINDOWS, 'raw': raw,
              'loadRule': load_rule(), 'saturationMbps': SATURATION_MBPS if saturation else None,
              'saturation': saturation, 'refreshedBeforeReference': refreshed,
              'design': 'v5', 'energyBaselineDb': energy_db,
              # 2026-09-30 (v54t block 1): a UE alone cannot carry less than when it shares its cell;
              # ue2's B collapsed to 0.26 Mbps after 5.93 in A and set ue2's targets from it.  The solo
              # reference is the larger of the solo and the shared median.
              'reference': {'ue1': med(raw['A'].get('ue1', [])),
                            'ue2': _solo(raw, 'B', 'ue2'), 'ue3': _solo(raw, 'C', 'ue3'),
                            'gnb1Sum': med(gnb1)}}
    # v5 UE2 deadline (DEADLINE_RULE): d from A over ALL requests; A's success at d is recorded
    # (about 0.75 by construction), B's success at d is a diagnostic.  An invalid d refuses the
    # reference; d is never widened.
    d = echo_deadline_ms(echoes.get('A'))
    rule = DEADLINE_RULE
    fixed = fixed_deadline_ms()
    if fixed is not None:
        # Owner 2026-09-27 15:4x ("d는 그렇게 해"): v5.1 blocks 6/8/12 and v5.2 block 0 measured d at
        # 2.6-3.3 s whenever gnb1 could not carry 2 x 10 Mbps in A; every block where it could gave
        # 34-40 ms.  d is fixed; the per-block A-p75 stays in the record as a diagnostic.
        result['ue2DeadlineMeasuredMs'] = d
        d, rule = fixed, f'fixed {fixed:g} ms (A-p75 kept as ue2DeadlineMeasuredMs)'
    result.update({'ue2DeadlineMs': d, 'ue2SuccessAtDInA': echo_success(echoes.get('A'), d),
                   'ue2SuccessAtDInB': echo_success(echoes.get('B'), d),
                   'ue2DeadlineRule': rule,
                   'echoTimeoutMs': ECHO_TIMEOUT_MS, 'echoQuantile': ECHO_QUANTILE,
                   'echoRequests': {c: len(v) for c, v in echoes.items()},
                   'echoRttsMs': echoes})
    # A UE whose link froze mid-configuration gives a median that is not a reference (15:52
    # block 2: ue1 9.9 -> 0 in B, ue2 3.7 -> 0).  Every loaded UE's five windows must be positive
    # and within half of their median; otherwise the block retries rather than freezing bad levels.
    # 2026-09-27 09:24, block 6: measured right after a board whose steering hand-backs left ue2/ue3
    # on stuck low-MCS contexts (gnb1 sum 10.4 against 16-18) -- a stable but weak reference froze the
    # block's corpus.  A weak UE not refreshed for this attempt retries, and the retry reattaches it;
    # one refreshed and still weak is the bed as it is.
    # Refreshes accumulate over this block's attempts (Codex): two weak UEs must not alternate.
    fresh = {p.split('=')[0] for p in refreshed or ()} | set(_refreshed_on_last_attempt(args.out))
    result['refreshedThisBlock'] = sorted(fresh)
    # 2026-09-30: deadline_starved guards a d measured in A; with a fixed deadline nothing is measured
    # there, and it refused v54t block 0 for the very contention the block is meant to hold (ue2 3.4 in A,
    # 5.9 alone in B) and reattached ue2 and ue3 for nothing.
    weak = set(weak_hosts(raw, LOAD)) | (set(deadline_starved(raw, LOAD)) if fixed is None else set())
    # 2026-09-28 00:4x (owner: "우리가 할 수 있는 게 더 없다"): ue3 on gnb1 swung 0.7-2.5 Mbps
    # window to window after antenna swap and USB replug; block 6 failed on 'A:ue3'/'C:ue3' forever.
    # An erratic UE this block already reattached is the bed as it is, exactly like a weak one.
    unstable = ([u for u in _unstable(raw) if u.split(':')[-1] not in fresh]
                + [f'weak:{h}' for h in sorted(weak) if h not in fresh])
    result['unstable'] = unstable
    if unstable:
        result['reference'] = {k: None for k in result['reference']}
    text = json.dumps(result, indent=1)
    out = Path(args.out) if args.out else HERE / 'overnight' / f'reference-dl-{stamp}.json'
    out.write_text(text + '\n')
    print(text)
    # A missing or zero reference never generates a requirement; a measured low positive does.
    # No valid d is no UE2 deadline requirement, so the block retries too.
    # Configuration A's success at d is required too (Codex review 2026-09-26): without it the
    # block would start with no record of the problem state it is meant to measure.
    ok = (all(v is not None and v > 0 for v in result['reference'].values()) and d is not None
          and result.get('ue2SuccessAtDInA') is not None)
    return 0 if ok else 3


if __name__ == '__main__':
    sys.exit(main())
