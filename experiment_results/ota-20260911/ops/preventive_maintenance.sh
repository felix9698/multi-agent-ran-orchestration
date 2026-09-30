#!/usr/bin/env bash
# Preventive maintenance between attempts, so the two drifts that recur on a clock stop
# being diagnosed after they break a bed.
#
#   UE context ageing.  2026-09-16 17:50: ue2 (RNTI d27e, 2.6M dlsch rounds, CCE fail
#   219k climbing at ~228/s) delivered 0.05 Mbps while ue1 (b1eb, 260k rounds, reattached
#   at 16:51) took 8.98 on the same cell with the same SNR 19-20, MCS 19-20 and BLER
#   0.14-0.25.  The gNB1 pair sum fell 15.8 -> 4.0 over the afternoon and every collapse
#   was the UE that had gone longest without a reattach.  A fresh context is the fix and
#   it costs about a minute between attempts.
#
#   AMF 116-minute implicit-deregistration echo.  Attempt 136 needed six restarts, four
#   of them session-released, and still expired its gate; after core_echo_reset.sh at
#   16:07 attempt 137 needed two, both real releases, and passed.
#
# Never touches a running attempt: PAUSE first, then wait for the busy lock, exactly as
# core_echo_reset.sh does.  The keeper is stopped for the reattach because it would
# otherwise fight the move (stop-keeper-before-gnb-restart-or-repin).
set -u
O="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UE_MIN=${AIC_UE_REFRESH_MIN:-60}        # reattach every UE this often
# 2026-09-24 (owner: "100분마다 코어를 재시작할 이유가 있나? 다시 붙이는데 시간이 더 들거 같은데"):
# the core is reset on EVIDENCE, not on a clock.  The one thing that really runs out is the
# ue1/ue2 address pool (12.1.1.128/26, never recycled).
# 2026-09-25 00:0x: "the AMF 116-min echo no longer bites because UEs re-register" was FALSE --
# re-registration does not cancel the timer: 13:04:17 -> 15:00:17 -> 16:56:17 (AMF clock), each
# expiry an IMPLICIT_DEREGISTRATION of that IMSI's *current* session (ue3 23:23 and 23:56 KST,
# mid-board) that arms the next one.  Evidence now includes the oldest mobile-reachable timer
# armed since the AMF started: past ECHO_MIN it is about to fire, so reset between boards.  A clock-driven reset
# cost a re-attach of every UE (03:20: two UEs lost the sessions they had just made).
POOL_LIMIT=${AIC_POOL_RESET_OCTET:-180}   # reset when a ue1/ue2 address passes this octet
ECHO_MIN=${AIC_AMF_ECHO_RESET_MIN:-90}   # the timer fires at 116 min
# 2026-09-28 06:3x: a reset mid-block restarts the block and drops its finished boards (830, 831 at
# 06:25).  An echo-only reset waits for the block boundary until ECHO_HARD; at the boundary it goes
# early from ECHO_EARLY so the next ~40-min block never reaches the limit.
ECHO_EARLY=${AIC_AMF_ECHO_EARLY_MIN:-70}
ECHO_HARD=${AIC_AMF_ECHO_HARD_MIN:-95}   # (Codex) 5-min poll + ~12-min board wait + restart must land before 116
# 2026-09-28 14:33: the boundary reset fired while block 1's reference was measuring (slot 0, no .env
# yet), killed the reference and dropped every UE.  The reference is part of the block: a block that
# will reach ECHO_HARD before it ends is flagged from ECHO_FLAG, so the runner resets before the next
# reference instead (a block with its reference is ~45 min).
ECHO_FLAG=${AIC_AMF_ECHO_FLAG_MIN:-50}
ref_running(){ local p; for p in $(pgrep -x python3); do tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null | grep -q "reference_dl\.py" && return 0; done; return 1; }
campaign_progress(){ local c; c=$(grep -oE '^Environment=AIC_CAMPAIGN=[^ ]+' ~/.config/systemd/user/aic-v31-episodes.service.d/campaign.conf 2>/dev/null | tail -1 | cut -d= -f3)
  echo "$O/overnight/${c:-blocks-v52}.progress.json"; }
# Mid-block: a board of this block has finished (slot > 0) or its reference froze (block .env exists).
mid_block(){ python3 -c "import json,sys,os; f='$(campaign_progress)'; s=json.load(open(f)); env=f.replace('.progress.json','.block%d.env' % int(s.get('block',0))); sys.exit(0 if int(s.get('slot',0)) > 0 or os.path.exists(env) else 1)" 2>/dev/null; }
echo_age(){ local st t; st=$(docker inspect -f '{{.State.StartedAt}}' oai-amf 2>/dev/null) || { echo 0; return; }
  t=$(docker logs -t --since "$st" oai-amf 2>&1 | grep -a -m1 'Started mobile reachable timer' | cut -d' ' -f1)
  [ -n "$t" ] && echo $(( ( $(date +%s) - $(date -d "$t" +%s) ) / 60 )) || echo 0; }
pool_octet(){ local m=0 o; for h in ue1 ue2; do o=$(timeout 8 ssh -o BatchMode=yes "$h" "ip -4 -o addr show dev oaitun_ue1 2>/dev/null | awk '{print \$4}'" | cut -d/ -f1 | awk -F. '{print $4}'); [ -n "$o" ] && [ "$o" -gt "$m" ] && m=$o; done; echo "$m"; }
CHECK_S=${AIC_MAINT_CHECK_S:-300}
mkdir -p "$O/overnight"
held(){ p=$(awk '{print $1}' "$O/overnight/episode-busy.lock" 2>/dev/null); [ -n "$p" ] && kill -0 "$p" 2>/dev/null; }
age_min(){ f="$1"; [ -f "$f" ] || { echo 99999; return; }; echo $(( ( $(date +%s) - $(stat -c %Y "$f") ) / 60 )); }
log(){ echo "$(date +%H:%M:%S) $*"; }

# Both stamps start now: the bed was just serviced by hand when this was written.
for f in ue-refresh core-reset; do [ -f "$O/overnight/last-$f" ] || touch "$O/overnight/last-$f"; done
log "started; UE ${UE_MIN}m, core when a ue1/ue2 address passes .${POOL_LIMIT}, check every ${CHECK_S}s"

while true; do
  sleep "$CHECK_S"
  [ -f "$O/overnight/PAUSE" ] && continue          # somebody else is servicing the bed
  ue_age=$(age_min "$O/overnight/last-ue-refresh")
  core_age=$(age_min "$O/overnight/last-core-reset")
  want_ue=0; want_core=0
  [ "$ue_age" -ge "$UE_MIN" ] && want_ue=1
  octet=$(pool_octet); [ "${octet:-0}" -ge "$POOL_LIMIT" ] && want_core=1
  echo_min=$(echo_age)
  if mid_block || ref_running; then
    [ "${echo_min:-0}" -ge "$ECHO_HARD" ] && want_core=1
    if [ "${echo_min:-0}" -ge "$ECHO_FLAG" ] && [ "${echo_min:-0}" -lt "$ECHO_HARD" ]; then
      [ -f "$O/overnight/CORE_RESET_WANTED" ] || log "AMF timer ${echo_min} min, 블록 도중 -- 러너가 다음 블록 기준 측정 전에 초기화(CORE_RESET_WANTED), 강제 ${ECHO_HARD} min"
      touch "$O/overnight/CORE_RESET_WANTED"
    fi
  elif [ "${echo_min:-0}" -ge "$ECHO_EARLY" ]; then
    # (Codex) a boundary reset is the runner's: it runs before the next reference under its own
    # control, so no reference can start inside it.
    touch "$O/overnight/CORE_RESET_WANTED"
  fi
  # 2026-09-27: a stale timer can also kill fresh sessions every few minutes (ue1, 02:4x).
  # Only 15 min after the last reset: its pre-restart events stay in the AMF log's window.
  implicit=$(python3 "$O/amf_implicit_recent.py" 15)
  [ "${implicit:-0}" -ge 2 ] && [ "$core_age" -ge 15 ] && want_core=1
  # (Codex) no reset reason may overlap a running reference: defer it to the runner.
  # ponytail: a reference starting between this check and core_echo_reset.sh can still overlap a
  # forced reset (pool/implicit/ECHO_HARD); a shared lock with reference_dl.py would close it.
  if [ $want_core -eq 1 ] && ref_running; then
    log "core reset due but the block reference is measuring -- CORE_RESET_WANTED for the runner"
    touch "$O/overnight/CORE_RESET_WANTED"; want_core=0
  fi
  [ $want_ue -eq 0 ] && [ $want_core -eq 0 ] && continue

  if [ $want_core -eq 1 ]; then
    log "core reset due: address pool at .${octet} (limit .${POOL_LIMIT}), oldest AMF mobile-reachable timer ${echo_min} min (limit ${ECHO_MIN}), ${implicit:-0} implicit deregistrations in 15 min (limit 2); handing over to core_echo_reset.sh"
    bash "$O/core_echo_reset.sh" 2>&1 | sed 's/^/    /'
    touch "$O/overnight/last-core-reset"; rm -f "$O/overnight/CORE_RESET_WANTED"
    # The core reset re-registers every UE, so their contexts are fresh too.
    touch "$O/overnight/last-ue-refresh"
    continue
  fi

  # 2026-09-26 (block 10 walkovers, ue2's per-attach downlink): a UE re-attached mid-block
  # changes the bed the block's reference described.  The block reference now re-attaches
  # aged UEs itself before measuring, so mid-block this daemon leaves UEs alone.
  if mid_block || ref_running; then   # 2026-09-28: was the stale blocks18-v47 progress file
    log "UE refresh due (${ue_age}m) 이지만 블록 도중 -- 다음 블록 참조 측정이 노화 UE 를 교체"
    touch "$O/overnight/last-ue-refresh"
    continue
  fi
  # A timer says WHEN to look, never WHAT to do.  ue_ageing_check.py names the UEs whose
  # current softmodem instance actually shows ageing (>=8 'SR not served' or >=4 distinct
  # RNTIs); an empty answer means the bed is well and nothing is spent.  2026-09-16 19:06
  # this daemon was about to reattach all three UEs at their healthiest point of the day
  # (1-3 SR failures each over 1,894-2,581 s) purely because sixty minutes had passed --
  # the same shape of mistake as keeper-health-check-was-thrashing-the-bed.
  STALE=$(cd "$O" && python3 ue_ageing_check.py 2>/dev/null | tr '\n' ' ')
  if [ -z "$STALE" ]; then
    log "UE refresh due (${ue_age}m) 이지만 노화 증거 없음 — 건너뜀"
    touch "$O/overnight/last-ue-refresh"
    continue
  fi
  log "UE refresh due (${ue_age}m); 노화 증거 있는 UE: $STALE"
  touch "$O/overnight/PAUSE"
  while held; do sleep 5; done
  # The evidence was read before a whole board was waited out; a steering trial alone
  # adds RNTIs (09-24 06:32 ue2 qualified mid-board, and had one RNTI when the board
  # ended).  Judge again now and service only the UEs that still qualify.
  STALE=$(cd "$O" && python3 ue_ageing_check.py 2>/dev/null | tr '\n' ' ')
  if [ -z "$STALE" ]; then
    log "    판 종료 후 재판정: 노화 증거 없음 — 건너뜀"
    touch "$O/overnight/last-ue-refresh"
    rm -f "$O/overnight/PAUSE"
    continue
  fi
  systemctl --user stop aic-keeper >/dev/null 2>&1
  # read_placement.py reads conductor.py as TEXT.  Importing conductor instead runs its
  # epoch-pin helper, which prints a GATE_EPOCH_PIN line that lands inside the command
  # substitution and turns the cell name into a JSON blob (caught 18:09, before this
  # daemon had ever fired).
  PLACE_ALL=$(cd "$O" && python3 read_placement.py 2>/dev/null)
  PLACE=""
  for u in $STALE; do
    for pair in $PLACE_ALL; do
      case "$pair" in "$u="*) PLACE="$PLACE $pair";; esac
    done
  done
  PLACE=$(echo $PLACE)
  if [ -z "$PLACE" ]; then
    log "배치를 읽지 못해 이번 회차 건너뜀"
    systemctl --user start aic-keeper >/dev/null 2>&1
    rm -f "$O/overnight/PAUSE"
    continue
  fi
  log "    배치 $PLACE"
  ( cd "$O" && AIC_REATTACH_USB_RESET=1 python3 force_reattach.py $PLACE ) 2>&1 | sed 's/^/    /'
  sleep 35
  for h in ue1 ue2 ue3; do
    a=$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$h" 'ip -4 -o addr show up dev oaitun_ue1 2>/dev/null | awk "{print \$4}"')
    log "    $h addr=${a:-없음}"
  done
  systemctl --user start aic-keeper >/dev/null 2>&1
  touch "$O/overnight/last-ue-refresh"
  rm -f "$O/overnight/PAUSE"
  log "UE refresh done; PAUSE removed"
done
