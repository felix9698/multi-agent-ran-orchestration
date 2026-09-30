#!/usr/bin/env bash
# One attempt under v3-select10-existing3 (OTA_EXISTING_XAPPS_EXPERIMENT_REVISION_20260914).
#
# What changed from run_formal.sh, and why -- every line is the drop's, not mine:
#
#   action scope   steering-only -> steering + PRB cap + PF weight (section 3).
#                  The steering-only catalogue was MY configuration: the runner's
#                  own default is 'servingCell,dlPrbCap' and AIC_CAP_HOSTS=''
#                  means "no cap axis at all" (_cap_axis_flags), so the previous
#                  script narrowed the scope by hand -- the exact mistake
#                  section 5 warns about.
#   deadlines      1000/1500 ms -> 2000/3000 ms (section 6: "Do not inherit the
#                  unexplained 1,000/1,500-ms values from the latest corpus").
#                  They live in the pilot's intents.json, which is hash-pinned,
#                  so this points at a new pilot directory instead of editing a
#                  signed one.  The 54-member authority, the owner (g,d) tables
#                  and answers.json are byte-identical to v3-select10.
#   budget         4 -> 8 dispatches (section 6).
#   formation      330 s -> 240 s (section 6: "first executable proposal within
#                  240 seconds").  Measured formation was 205.7 s with a
#                  62-132 s per-role spread, so this CAN time out.  That is the
#                  drop's own rule -- a timeout is an outcome, not permission to
#                  restart the clock -- so the overrun is recorded, not tuned away.
#   ceiling        --max-catalog 512 refuses this scope outright.  It bounds the
#                  frozen CATALOGUE -- "steer 8 x cap 64 x pf 8" = 4,096 -- which
#                  is not the drop's 1,000/696: those count admissible
#                  CONFIGURATIONS, and both predicates behind them are real and
#                  enforced a layer down (the per-UE dlPrbCap/pfWeight exclusion
#                  in _compatibility_rules, and MAX_CHANGED_ENTRIES = 4).
#                  Counting the declared domain reproduces them exactly: 10 per
#                  UE, 1,000 over three, 696 at four changed entries.  So 4,096
#                  is the honest ceiling and narrows nothing.
#
# Unchanged on purpose: load 10 Mbps/UE (the upper case of the 8/10 pair), the
# cell/PRB conditions the models are told, the observation windows, the
# withdrawal authorisation, and the model aliases.
set -Eeuo pipefail
SP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXP=/opt/ran-lab/controller/agentic_ran_coordinator_based_on_ORAN/experiment_results/ota-20260911

export AIC_ENTRY="${AIC_ENTRY:-main}"
# OTA_IMPLEMENTATION_AMENDMENT_20260914: every record names the definition it ran under.
# 2026-09-16: intents are sealed on UE roles and a re-registered UE is followed
# (docs/design/ue-identity-continuity.md); targets, load, policy and candidates unchanged.
export AIC_EXPERIMENT_VERSION="${AIC_EXPERIMENT_VERSION:-v3.1-identity-continuity}"
export AIC_METHOD="${AIC_METHOD:-three-agent}"

# The revised frozen snapshot.  Both hashes are stated so a silent edit of
# either file refuses the attempt instead of quietly changing the case.
# Defaults only: run_case.sh exports the L8 snapshot, and an unconditional
# assignment here once ran a "P1-L8" attempt against the L10 intents.
export AIC_PILOT="${AIC_PILOT:-$EXP/pilot38-v3-existing3-20260914T2300}"
export AIC_PILOT_INTENTS_SHA="${AIC_PILOT_INTENTS_SHA:-b342830f925f3e34664b4aa8934e94110fd6ee0695fae2aee7ca7806c2473ca0}"
export AIC_PILOT_MANIFEST_SHA="${AIC_PILOT_MANIFEST_SHA:-18bf24b2a46bdec20f1fefdf1eac72354e0e9aa45694b2c9042c8b4178569992}"

export AIC_OFFERED_LOAD_MBPS="${AIC_OFFERED_LOAD_MBPS:-10}"
# 2026-09-17 (오너: "유용하게 쓰고 있던 v3 세팅은 v4가 알아서 쓰게 만들라").
# 아래 값들은 v4 가 자기 파일에 직접 적는다(`run_case_v4.sh`).  여기서는 **v3 단독
# 실행용 기본값**으로만 남기고, 이미 정해진 값이 있으면 존중한다 -- 그래야 실험
# 조건의 소유가 v4 에 있고, v3 가 바뀌어도 v4 가 흔들리지 않는다.
export AIC_CELL_CAPACITY_MBPS="${AIC_CELL_CAPACITY_MBPS:-14.5}"
export AIC_PRB_TOTAL="${AIC_PRB_TOTAL:-38}"

export AIC_OBSERVE_GOODPUT="${AIC_OBSERVE_GOODPUT:-dlGoodputMbps=1000:15000:120000}"
export AIC_OBSERVE_DEADLINE="${AIC_OBSERVE_DEADLINE:-deadlineSuccessRatio=1000:15000:120000}"
export AIC_OBSERVE_CELL="${AIC_OBSERVE_CELL:-servingCell=1500:15000:120000}"
# v4.4 운영자 요구(cellGoodputMbps).  **위 세 줄과 같은 창·유효기간이어야 한다** --
# 같은 goodput 표본을 타므로 창이 다르면 coverage 가 달라지고, 관측 유효기간은
# 번들의 KPI 최솟값이 지배하므로 여기만 짧으면 번들 전체가 그만큼 짧아진다.
export AIC_OBSERVE_CELL_GOODPUT="${AIC_OBSERVE_CELL_GOODPUT:-cellGoodputMbps=1000:15000:120000}"
export AIC_OBSERVE_CELL_ATTENUATION="${AIC_OBSERVE_CELL_ATTENUATION:-cellTxAttenuationDb=1000:15000:120000}"

# 조종 확증에 걸리는 실측 시간에 맞춘 액션 한도.  기본값 10초는 **실측보다 짧다**:
# 2026-09-23 실기에서 조종 정책 하나를 혼자 쏘았을 때 KPM 이 목표 셀을 보기까지 6.0초,
# 프로듀서 readback 이 VERIFIED 가 되기까지 7.8초였다.  여유 2.2초인데 판은 9축을 한
# 트랜잭션으로 쓰므로 앞의 축들이 그것을 먹고, 조종 시행이 매번
# `PARTIAL_APPLY 9/9 acknowledged (readback confirmed only 2)` 로 판정돼 롤백됐다.
# 30초는 실측의 약 4배이며 harm bound 는 여전히 유계다.
export AIC_ENFORCED_TIMEOUT_MS="${AIC_ENFORCED_TIMEOUT_MS:-30000}"
export AIC_STOP_AFTER_RELAXED="${AIC_STOP_AFTER_RELAXED:-0}"
# 2026-09-17: v4 는 이 값을 **일부러 빈 문자열**로 둔다 — 그 주석: "an empty candidate
# list pins every UE to the cell it is on while keeping the kind the composition
# requires".  그런데 이 줄이 무조건 덮어써서 v4 의 고정이 풀리고 UE 마다 두 셀이
# 후보가 됐다(거절 메시지의 `steer 8`).  빈 값을 살리려면 **콜론 없는** 기본값
# 치환이어야 한다: `${VAR:-기본}` 은 빈 값도 기본으로 바꾸지만 `${VAR-기본}` 은
# **설정되지 않았을 때만** 기본을 쓴다.
export AIC_CELLS="${AIC_CELLS-12345678,87654321}"

# Section 3's declared per-UE domains.  Uncapped is the baseline state, not a
# rung: the ladders name the capped values only and the framework's own
# restoration semantics carry the baseline.
# 2026-09-17 고침: 이 세 줄이 **무조건 export** 라서, `run_case_v4.sh` 가 정해 놓고
# `exec` 로 넘긴 v4 의 축·사다리를 통째로 되돌리고 있었다.  실제로 판에 닿던 값은
# v4 가 쓴 `servingCell,dlPrbCap,pfWeight,txAttenuationDb` 와 `ue1:18,12;ue2:18` 이
# 아니라 아래 v3 의 값이었다 -- **전력 축이 통째로 사라지고 캡 좁힘도 풀렸다.**
# `${VAR:-기본}` 으로 바꿔 **이미 정해진 값이 있으면 존중**한다.  v3 단독 실행은
# 그대로 아래 기본값을 쓴다.
export AIC_AXES="${AIC_AXES:-servingCell,dlPrbCap,pfWeight}"
export AIC_CAP_HOSTS="${AIC_CAP_HOSTS:-ue1:18,12,6;ue2:18,12,6;ue3:18,12,6}"
export AIC_PF_HOSTS="${AIC_PF_HOSTS:-ue1:1,4;ue2:1,4;ue3:1,4}"
# 2026-09-17: 이 줄도 무조건 덮어쓰고 있었다 — 축·사다리 세 줄을 고치면서 놓쳤고,
# 그래서 v4 가 올린 상한이 먹지 않아 전력 축을 켠 첫 판이 4096 으로 거절됐다.
export AIC_MAX_CATALOG="${AIC_MAX_CATALOG:-4096}"

export AIC_WITHDRAW_VERIFIED_SCOPE="${AIC_WITHDRAW_VERIFIED_SCOPE:-1}"
export AIC_BUDGET="${AIC_BUDGET:-8}"
export AIC_HORIZON_S="${AIC_HORIZON_S:-480}"
# 2026-09-16, owner's call: the episodes that died on DEADLINE died waiting for a UE the
# radio had dropped, which is a hardware fault and not a cost of the control being
# measured, so that wait must not end a sitting. The first answer was "off", which omits
# --deadline-s and skips the check entirely; it kept episodes alive but left the
# manuscript's 480 s attainment curves with no clock to be drawn against. The owner's
# decision later the same day is the middle course: keep B at 480 s and relieve it of the
# hardware wait, exactly as the 240 s formation allowance was already relieved
# (AgentSitting._past_deadline subtracts _hardware_wait_ms, the same number hardwareWaitMs
# reports). "off" still works and still skips the check. AIC_EPISODE_CAP_S stays the
# backstop bounding the sources, the sender retargeter and the subprocess together.
export AIC_DEADLINE_S="${AIC_DEADLINE_S:-480}"
export AIC_EPISODE_CAP_S="${AIC_EPISODE_CAP_S:-3600}"
export AIC_FORMATION_S="${AIC_FORMATION_S:-240}"
export AIC_DECISION_S="${AIC_DECISION_S:-60}"
export AIC_CONDITION="${AIC_CONDITION:-v3-existing3-L10}"
export AIC_ROLE_MODEL="${AIC_ROLE_MODEL:-claude-sonnet}"
export AIC_MONOLITH_MODEL="${AIC_MONOLITH_MODEL:-claude-sonnet}"

exec python3 "$SP/run_episode.py" "$@"
