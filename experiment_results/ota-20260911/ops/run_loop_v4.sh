#!/usr/bin/env bash
# 판 → 판정 → 다음 판 을 반복한다 (2026-09-17 오너 지시: 중간보고 없이 계속).
#
# 고치는 일은 사람(나)이 판정 결과를 보고 한다; 이 루프는 판을 끊기지 않게 하고
# 매 판의 판정을 한 파일에 쌓는 것까지만 한다.  베드를 흔드는 조치는 하지 않는다.
set -u
OPS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXP="$(cd "$OPS/.." && pwd)"
# 판정기는 이 디렉터리에 있다.  2026-09-17 까지 이 줄은 한 세션의 스크래치패드
# (/tmp/claude-.../scratchpad/judge_episode.py) 를 가리키고 있었다 -- 그 세션이
# 끝나면 경로가 사라지고 무인 운용의 판정이 조용히 죽는다.  운영 사슬을
# scratchpad 밖으로 옮긴 것과 같은 이유다(ops/ 로 이동, 2026-09-15).
JUDGE="${JUDGE:-$OPS/judge_episode.py}"
VERDICTS="$OPS/overnight/loop-verdicts.txt"
N="${1:-99}"

# 팔 교대 (2026-09-17 오너 지시: "monolith 도 동작시켜서 진짜 3-agent 효과가 나는지 검증").
# AIC_ARMS 에 팔 이름을 공백으로 나열하면 **한 판씩 돌아가며** 쓴다.  한 바퀴(블록)마다
# 순서를 섞는다 -- conductor.py::formal_block 과 같은 이유로, 순서효과를 팔 효과로
# 오인하지 않기 위해서다.  블록으로 몰아 돌리지 않는 이유는 따로 있다: 2026-09-17 에
# 재 보니 베드가 시간에 따라 흔들린다(MCS 18~25, goodput 5.9~8.1).  팔을 몰아 돌리면
# 그 표류가 통째로 팔 효과로 둔갑한다.  한 판씩 교대하면 세 팔이 같은 시간대를 나눠 갖는다.
ARMS="${AIC_ARMS:-}"
ARMS_N=$(printf '%s\n' $ARMS | grep -c . || true)
ORDER=""


# 한 번에 하나만.  2026-09-17 에 이 스크립트를 여러 번 재시작하면서 여섯 개가 겹쳐 돌았고,
# 서로의 판을 `EXISTING_WORKLOAD_PROCESSES` 로 거절시키며 판을 연달아 잃었다.  flock 은
# 프로세스가 죽으면 저절로 풀리므로 낡은 락이 남지 않는다.
exec 9>"$OPS/overnight/.loop.lock"
if ! flock -n 9; then
  echo "이미 다른 run_loop_v4.sh 가 돌고 있다 — 겹쳐 돌면 서로의 판을 망친다. 멈춘다."
  exit 3
fi

for i in $(seq 1 "$N"); do
  [ -f "$OPS/overnight/STOP-LOOP" ] && { echo "STOP-LOOP 파일이 있어 멈춘다"; break; }
  # 앞 판의 잔해를 먼저 치운다.  판을 kill 로 끊으면 UE 쪽 부하 프로세스와 소유권 도우미가
  # 남고, 다음 판의 프리플라이트가 `EXISTING_WORKLOAD_PROCESSES:<ue>` 로 정당하게 거절한다 --
  # 2026-09-17 에 그렇게 판 두 개를 연달아 잃었다.  정상 종료한 판은 스스로 치우므로 이 줄은
  # 보통 아무것도 지우지 않는다.
  for h in ue1 ue2 ue3; do
    ssh -o BatchMode=yes -o ConnectTimeout=6 "$h" \
      'pkill -f "/tmp/aic-flow-.*/(tagged_echo|flow_goodput)\.py" 2>/dev/null;
       pkill -f "Private per-attempt source ownership helper" 2>/dev/null; true' >/dev/null 2>&1
  done
  # extdn 도 같은 프리플라이트를 받는다 (atomic_formal_run_guarded.py:1039 은 HOSTS 에
  # 'extdn' 을 더해 전부 검사한다).  여기서 빼 놓아 2026-09-17 05:50~05:53 에
  # `EXISTING_WORKLOAD_PROCESSES:extdn:preflight` 로 판 세 개를 잃었다.  extdn 은 ssh 가
  # 아니라 `docker exec oai-ext-dn` 이다.  `pkill -f` 는 자기 셸의 명령줄까지 잡으므로
  # (process-lookup-must-be-exact-cmdline) 프리플라이트와 같은 판별 -- argv 의 basename --
  # 을 그대로 쓴다.
  # 2026-09-17: 이 정리가 실패하면 판은 `EXISTING_WORKLOAD_PROCESSES:extdn:preflight` 로
  # 거절되는데, 출력을 통째로 버려서 **왜 실패했는지 남지 않았다**(05:50~05:53 에 판 3 개를
  # 그렇게 잃었다).  침묵을 성공으로 읽지 않는다 -- 실패하면 그 자리에서 적는다.
  CLEANOUT=$(docker exec -i oai-ext-dn python3 - <<'CLEAN' 2>&1
import os, signal, pathlib
names = {"tagged_echo.py", "flow_goodput.py"}
for entry in pathlib.Path('/proc').iterdir():
    if not entry.name.isdecimal():
        continue
    try:
        argv = (entry / 'cmdline').read_bytes().split(b'\0')
    except OSError:
        continue
    if any(os.path.basename(a.decode('utf-8', 'replace')) in names for a in argv if a):
        try:
            os.kill(int(entry.name), signal.SIGTERM)
        except OSError:
            pass
CLEAN
  ) || echo "$(date '+%F %H:%M:%S') ** extdn 정리 실패 rc=$? :: ${CLEANOUT:-(출력 없음)}" >&2
  [ -n "${CLEANOUT:-}" ] && echo "$(date '+%F %H:%M:%S') extdn 정리가 말을 했다 :: $CLEANOUT" >&2
  STAMP=$(date +%H%M%S)
  LOG="$OPS/overnight/v4-tight90-P1-L8-$STAMP.log"
  echo "=== [$i] 판 시작 $(date +%T) → $(basename "$LOG")"
  rm -f "$OPS/overnight/episode-busy.lock"
  if [ "${ARMS_N:-0}" -gt 0 ]; then
    if [ $(( (i - 1) % ARMS_N )) -eq 0 ]; then
      ORDER=$(printf '%s\n' $ARMS | shuf | tr '\n' ' ')
      echo "    블록 순서: $ORDER"
    fi
    export AIC_METHOD=$(echo $ORDER | cut -d' ' -f$(( (i - 1) % ARMS_N + 1 )))
    echo "    팔: $AIC_METHOD"
  fi
  ( cd "$OPS" && L8TIGHT=1 bash run_case_v4.sh P1 8 > "$LOG" 2>&1 )
  RC=$?
  # **이 시도가 만든 판**을 하나의 출처에서 정한다: 이 시도의 로그가 이름을 부른 판.
  # run_forever.sh 가 쓰던 방법이고, 원장이 깨끗한 이유다(판 123행 중 서로 다른 판 122개).
  #
  # "가장 최근 디렉터리"(ls -td)를 쓰면 게이트에서 끝난 시도 -- UE 가 안 붙어 판 디렉터리를
  # 만들지 못한 시도 -- 가 **이전 판**에 귀속된다. 2026-09-19 에 그 결함이 두 군데서
  # 드러났다: 종료 줄이 앞 판의 FRESH_TWO_CELL_KPM_REQUIRED 를 이 판 것인 양 붙였고,
  # loop-verdicts.txt 는 게이트 거절 7건을 전부 01:51 의 같은 판에 쌓았다(허수 20개, 6.2%).
  #
  # 생성 시각(stat %W)으로 거르는 방법도 썼었지만 출처가 둘이 되어 서로 어긋날 수 있고,
  # %W 는 파일시스템에 따라 0 을 준다. 출처는 하나여야 한다.
  LDIR=$(grep -o 'formal38guarded-[0-9T]*-[0-9a-f]*' "$LOG" 2>/dev/null | tail -1)
  if [ -n "$LDIR" ] && [ -d "$EXP/$LDIR" ]; then MINE_S="$EXP/$LDIR"; else MINE_S=""; LDIR=""; fi
  # 거절 사유는 exit.json 의 failure.code 에만 있고 판 로그에는 한 글자도 안 찍힌다.
  # 2026-09-19 01:41~01:45 에 판 네 개가 이유 없이 1초 만에 사라진 것처럼 보였는데,
  # 전부 DEPENDENCY_PREFLIGHT_REFUSED:FRESH_TWO_CELL_KPM_REQUIRED 였다.
  # 로그만 보는 사람이 원인을 못 보면 없는 것과 같으므로 여기에 끌어올린다.
  WHY=$(python3 - "$MINE_S" <<'PYEOF' 2>/dev/null
import json, sys, pathlib
if not sys.argv[1]:
    raise SystemExit                      # 이 시도는 판 디렉터리를 만들지 않았다
try:
    d = json.loads((pathlib.Path(sys.argv[1]) / 'exit.json').read_text())
except Exception:
    raise SystemExit
f = d.get('failure') or {}
bits = [d.get('submissionStatus') or '']
if f.get('code'):
    bits.append(f"{f.get('phase','?')}:{f['code']}")
if f.get('detail'):
    bits.append(str(f['detail'])[:120])
print(' :: '.join(b for b in bits if b))
PYEOF
)
  [ -n "$MINE_S" ] || WHY="게이트에서 끝남(판 디렉터리 없음)"
  echo "    종료 $(date +%T) exit=$RC${WHY:+  $WHY}"
  # 판정도 **이 시도가 만든 판**에만 돌린다.  게이트에서 끝난 시도에 가장 최근
  # 디렉터리를 먹이면, 그 옛 판의 판정이 새 시도의 것인 양 매번 다시 쌓인다 --
  # 2026-09-19 04:5x 의 loop-verdicts.txt 는 게이트 거절 7건이 전부 01:51 의 같은
  # 판(T165156)에 귀속돼 있었다.  종료 줄만 고치고 여기를 빠뜨린 내 탓이다.
  {
    if [ -n "$MINE_S" ]; then
      echo "########## [$i] $(date -Is)  $(basename "$MINE_S")  log=$(basename "$LOG")"
      python3 "$JUDGE" "$MINE_S" 2>&1
    else
      echo "########## [$i] $(date -Is)  (판 없음)  log=$(basename "$LOG")"
      echo "게이트에서 끝남 — 판 디렉터리가 만들어지지 않았다 (exit=$RC). 판정할 에피소드가 없다."
      grep -aE "refused:|waiting:" "$LOG" 2>/dev/null | tail -2
    fi
    echo
  } >> "$VERDICTS"
  # 판을 원장에 기록한다.  v3.1 사슬(run_forever.sh)은 매 시도마다 한 행을 남겼는데
  # v4 사슬로 넘어오며 그 경로가 사라졌다 -- 원장의 판 행이 2026-09-16 11:28 에서 끊겨
  # 있고 그 뒤 판은 사람이 손으로 적은 것뿐이다(2026-09-19 확인).  같은 스키마로 되살린다.
  # LDIR 은 위에서 이미 정했다 -- 종료 줄·판정·원장이 **같은 판**을 가리키게 하기 위해서다.
  python3 - "$OPS/overnight/v31-ledger.jsonl" "P1-L8" "$i" "$LDIR" "$RC" "${AIC_METHOD:-}" "$EXP" <<'PYEOF'
import json, sys, time, glob, pathlib
path, case, i, d, rc, method, exp = sys.argv[1:]
status = failure = None; term = None
if d:
    root = pathlib.Path(exp) / d
    try:
        ex = json.loads((root / "exit.json").read_text())
        status = ex.get("submissionStatus")
        failure = (ex.get("failure") or {}).get("code")
    except Exception:
        pass
    for f in glob.glob(str(root / "evidence" / "*-episode.json")):
        try:
            e = json.loads(pathlib.Path(f).read_text())
            t = e.get("termination") or {}; c = e.get("completion") or {}
            term = {"reason": t.get("reason"), "kernelTermination": t.get("kernelTermination"),
                    "unresolved": c.get("unresolved")}
        except Exception:
            pass
        break
row = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "version": "v4-tight90", "case": case,
       "method": method or None, "attempt": int(i), "dir": d or None,
       "submissionStatus": status, "episodeStarted": status == "STARTED_EPISODE",
       "refusal": failure, "outerExit": int(rc), "episodeTermination": term,
       "endedAtGate": not bool(d), "loggedBy": "run_loop_v4.sh"}
open(path, "a").write(json.dumps(row, ensure_ascii=False) + "\n")
PYEOF
  tail -1 "$LOG" | cut -c1-160
  sleep 10
done
