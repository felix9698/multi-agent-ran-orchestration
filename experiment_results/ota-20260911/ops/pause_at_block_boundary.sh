#!/usr/bin/env bash
# 시험·정비용 PAUSE 를 **블록 경계에서만** 건다 (2026-09-28: 블록 3 도중 PAUSE 로 판 868 이 제외됐다).
# 러너는 판을 시작하기 전에 slot 을 올리고 PAUSE 를 본다: 블록의 마지막 판(slot 2)이 돌기 시작한 뒤에
# PAUSE 를 걸면 그 판이 끝나고 다음 블록 기준 측정 전에 멈춘다.  돌려주는 값 0 = 멈춤 완료(판 없음).
set -u
OPS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
C=$(grep -oE '^Environment=AIC_CAMPAIGN=[^ ]+' ~/.config/systemd/user/aic-v31-episodes.service.d/campaign.conf | tail -1 | cut -d= -f3)
P="$OPS/overnight/$C.progress.json"
busy(){ ps -eo args | awk '$1=="python3" && $2 ~ /(run_episode|reference_dl)\.py$/' | grep -q .; }
slot(){ python3 -c "import json;d=json.load(open('$P'));print(d['slot'])"; }
# v5.4: the last slot is the plan row's length - 1 (4 methods per block), not a fixed 2.
last(){ python3 -c "import json;d=json.load(open('$P'));print(len(json.load(open('${P%.progress.json}.plan.json'))['plan'][d['block']])-1)"; }
echo "$(date +%T) waiting for the last board of the block ($C)"
until [ "$(slot)" = "$(last)" ] && busy; do sleep 10; done
B0=$(python3 -c "import json;print(json.load(open('$P'))['block'])")
touch "$OPS/overnight/PAUSE"; echo "$(date +%T) PAUSE set during the block's last board"
# (Codex) the runner still tallies handovers and writes ledger/progress after run_episode.py exits.
# Wait for that ledger row.  A refused last board (slot not advanced) must be retried WITHOUT the runner
# ever seeing the PAUSE -- a seen PAUSE restarts the whole block (restart_block_after_pause.py, 20:16 on
# 2026-09-29 excluded 928/929).  The runner sleeps 5 s after the row before it looks, so poll at 1 s.
# ponytail: a 4 s race window; move the PAUSE check into the runner if it is ever lost.
L="$OPS/overnight/v31-ledger.jsonl"; N0=$(wc -l < "$L")
until [ "$(wc -l < "$L")" -gt "$N0" ]; do sleep 1; done; sleep 1   # the same python writes progress right after the row
if [ "$(python3 -c "import json;print(json.load(open('$P'))['block'])")" = "$B0" ]; then
  rm -f "$OPS/overnight/PAUSE"; echo "$(date +%T) last board refused; PAUSE removed before the runner saw it, waiting again"
  exec bash "$0"
fi
echo "$(date +%T) block boundary reached, nothing running"
