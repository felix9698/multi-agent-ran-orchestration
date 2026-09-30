#!/usr/bin/env bash
# 2026-09-25 (owner): serving-cell membership rule "consistent" (coverage 0.8, every present sample
# on one cell) from v4.7 block 4 on; earlier blocks keep "last"/1.0 (assurance/coordination/intake.py).
_pf="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/overnight/${AIC_CAMPAIGN:-}.progress.json"
if [ -n "${AIC_CAMPAIGN:-}" ] && [ -f "$_pf" ]; then
  _blk=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['block'])" "$_pf" 2>/dev/null || echo 0)
  [ "${_blk:-0}" -ge 4 ] && export AIC_SERVINGCELL_CONSISTENT=1
fi
# v5.2 (2026-09-27): the "block >= 4" above was v4.7's boundary, but it re-applies to every new
# campaign, so v5.2 blocks 0-3 ran membership at "last"/1.0 -- one late poll voided the window.
[ "${AIC_V52:-}" = 1 ] && export AIC_SERVINGCELL_CONSISTENT=1
# One of the revision's six cases: `run_case.sh <P1|P2|P3> <8|10>`.
#
# Section 6 fixes the block as three preference profiles at each of two loads.
# Both halves have to be *frozen*, and they freeze in different places:
#
#   the preference   is an environment name the sitting reads where it builds
#                    its authorization, validated in the runner's preflight so
#                    a typo is refused rather than silently ranked under the
#                    default rule (which is how six labelled cases once ran
#                    under one unlabelled ranking);
#   the load         is NOT just a sender rate.  The goodput requirement is
#                    0.9L/0.8L/0.7L, so L=8 has its own hash-pinned snapshot
#                    with 7.2/6.4/5.6; running L=8 against the L=10 snapshot
#                    would drive 8 Mbps at a 9.0 Mbps requirement and call the
#                    shortfall a result.
#
# Everything else -- the 54-member authority, the owner (g,d) tables, the
# cross-owner deadline quota, the 2000/3000 ms deadlines, the action scope and
# the clocks -- is identical across all six, which is what makes them one block.
set -Eeuo pipefail
SP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXP=/opt/ran-lab/controller/agentic_ran_coordinator_based_on_ORAN/experiment_results/ota-20260911

PREF="${1:-}"
LOAD="${2:-10}"
# v4.7 solo-max rule (2026-09-25): a block corpus carries its own send rate, measured at the
# block's start; it replaces the case argument for that block only.
if [ -n "${AIC_BLOCK_PILOT:-}" ] && [ -n "${AIC_BLOCK_LOAD:-}" ]; then LOAD="$AIC_BLOCK_LOAD"; fi
# v4.7 cell power (owner 2026-09-25): a block that carries an energy intent also carries the
# attenuation ladder of the cells it controls ("<nci>:<dB>,..;.."); only those cells get the
# power axis (agent.py).  Absent -> axes and ladders unchanged.
if [ -n "${AIC_BLOCK_PILOT:-}" ] && [ -n "${AIC_BLOCK_ATT_CELLS:-}" ]; then
  export AIC_AXES="${AIC_AXES:-servingCell,dlPrbCap,pfWeight},txAttenuationDb"
  export AIC_ATT_CELLS="$AIC_BLOCK_ATT_CELLS"
  # The ladder multiplies the catalog (4096 x (1 + rungs)); since 2026-09-19 the epoch freezes
  # the domain, not an enumeration, so the ceiling is only a typo guard (run_case_v4.sh).
  export AIC_MAX_CATALOG="${AIC_MAX_CATALOG:-1000000}"
fi
# The case arguments are consumed here; only what follows them reaches the runner.
shift $(( $# >= 2 ? 2 : $# ))
case "$PREF" in P1|P2|P3) ;; *) echo "usage: run_case.sh <P1|P2|P3> <8|10>" >&2; exit 2;; esac

case "$LOAD" in
  10)
    # 2026-09-23 오너 기준안 v4.4: owner **4개** — 셀 사업자 1 + 테넌트 3.
    #   operator-gnb2  cellGoodputMbps@cell@87654321 >= 12.0 -> 10.0  (1단)
    #                  그 셀 UE 들의 goodput 합.  **UE 가 아니라 셀이 owner** 다.
    #                  8.0 이 아니라 10.0 인 이유: 초기 배치에서 두 UE 요구가 강제하는
    #                  gNB2 합의 하한이 최대 완화에서 6.0+3.0 = 9.0 이므로 8.0 은 자동
    #                  충족되어 공허하다.  10.0 은 9.0 을 넘어 모든 T 에서 추가 요구다.
    #   ue1-video      하향 7.5->7.0->6.0 (2단) · 마감 **0.85**@3000 **고정**
    #                  0.9 는 기준선 통과가 56% 뿐이고 그것이 **모든 T 의 천장**이라
    #                  판의 44% 가 에이전트와 무관하게 실패했다.  0.85 는 62%.
    #   ue2-map        하향 8.0 **고정** · 마감 0.9->0.7->0.6 @3000 (2단)   ← ue1 과 정반대
    #   ue3-incumbent  하향 4.0->3.0 · 마감 0.6->0.4 @3000->4000  (전면 협상)
    # 사다리는 owner 마다 **실제로 먹히는 축**에 걸었다 (09-22 실측 달성률):
    # ue1 은 처리량 7.5->7.0 에 19%->69% 절벽, ue2 는 마감 시각이 안 듣고(44%->44%)
    # 비율이 듣는다(0.9 44% -> 0.6 81%).  근거는 pilot38-v4.5-L10/intents.json 의
    # domain.baselineAttainment 와 docs/experiment/SCENARIO-BRIEF.md.
    # 사업자 요구의 역할은 "특정 UE 희생 방지" 가 **아니다** (12 는 ue1 9.0 + ue3 3.0
    # 으로도 충족된다).  "개별 요구를 맞추는 과정에서도 셀 총량을 유지" 하는 것이다.
    PILOT="$EXP/pilot38-v4.5-L10"
    INTENTS_SHA=dd57c7573c24730626dfab5b8da12417017f992b89080c3633379a4038b8da45
    ;;
  8)
    PILOT="$EXP/pilot38-v3-existing3-L8-20260915T0030"
    INTENTS_SHA=30a79c53d48e3d398bc20b6324e9a9bf0b140af4c7eb795d217cdc6854d41d6d
    ;;
  *)
    # 2026-09-25 (GPT review option 2 / owner solo-max rule): any positive send rate, v4.7 only --
    # the block corpus (AIC_BLOCK_PILOT, below) replaces this pilot.
    [[ "$LOAD" =~ ^[0-9]+(\.[0-9]+)?$ ]] && [ -n "${AIC_BLOCK_PILOT:-}" ] \
      || { echo "load must be 8 or 10, or a v4.7 block send rate (got $LOAD)" >&2; exit 2; }
    PILOT="$EXP/pilot38-v4.5-L10"
    INTENTS_SHA=dd57c7573c24730626dfab5b8da12417017f992b89080c3633379a4038b8da45
    ;;
esac

MANIFEST_SHA=18bf24b2a46bdec20f1fefdf1eac72354e0e9aa45694b2c9042c8b4178569992
# v4.7 (.orca/drops/V47_FINAL_PLAN.md section 2): a block's corpus is generated from that
# block's reference DL by make_v47_corpus.py, which writes both hashes beside it.  The pin
# stays: the hashes are read from the files the generator wrote and must match the corpus.
if [ -n "${AIC_BLOCK_PILOT:-}" ]; then
  PILOT="$AIC_BLOCK_PILOT"
  INTENTS_SHA="$(cat "$PILOT/INTENTS_SHA.txt")"; MANIFEST_SHA="$(cat "$PILOT/MANIFEST_SHA.txt")"
  # The hashes frozen in the block env win over the sidecars (Codex review #14).
  if [ -n "${AIC_BLOCK_INTENTS_SHA:-}" ]; then
    [ "$INTENTS_SHA" = "$AIC_BLOCK_INTENTS_SHA" ] && [ "$MANIFEST_SHA" = "${AIC_BLOCK_MANIFEST_SHA:-}" ] \
      || { echo "block corpus sidecars changed after the block froze its hashes" >&2; exit 2; }
  fi
  [ "$(sha256sum "$PILOT/intents.json" | cut -d' ' -f1)" = "$INTENTS_SHA" ] \
    && [ "$(sha256sum "$PILOT/manifest.json" | cut -d' ' -f1)" = "$MANIFEST_SHA" ] \
    || { echo "block corpus $PILOT does not match its frozen hashes" >&2; exit 2; }
fi

export AIC_PREFERENCE="$PREF"
export AIC_OFFERED_LOAD_MBPS="$LOAD"
export AIC_PILOT="$PILOT"
export AIC_PILOT_INTENTS_SHA="$INTENTS_SHA"
export AIC_PILOT_MANIFEST_SHA="$MANIFEST_SHA"
# 2026-09-22: 이름표를 코퍼스와 **따로 타이핑**해 두었더니 v4.2 판 7개가
# `v3-existing3-P1-L10` 으로 찍혔다.  experiments/agent_metrics.py:1266 이
# condition.name 으로 묶으므로 v3 판과 v4.2 판이 한 무리가 된다.  이름표는 실제로
# 쓰이는 pilot 에서 파생시켜 다시 어긋날 수 없게 한다.
export AIC_CONDITION="$(basename "$PILOT")-${PREF}"

exec bash "$SP/run_formal_v3.sh" "$@"
