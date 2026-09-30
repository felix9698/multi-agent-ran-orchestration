#!/usr/bin/env bash
# One of v4's six cases: `run_case_v4.sh <P1|P2|P3> <6|8>`.
#
# The redesign (OTA_SCENARIO_REDESIGN_20260916) keeps the six-case factorial --
# three preference profiles at each of two loads -- and changes what is inside
# each case:
#
#   requirements   three heterogeneous ones instead of six homogeneous, with
#                  INDEPENDENT owner permissions, so |Omega| = 3*3*3 = 27 and
#                  boundary_targets() reduces to the single all-level-2 anchor
#                  (verified against the code, not assumed);
#   action scope   the cell transmit-power axis is exposed alongside cap and PF,
#                  and servingCell is PINNED: AIC_CELLS is empty, so each UE's
#                  SteeringAxisSpec unions in only its own baseline cell and the
#                  mandatory steering kind stays exposed with one value.  The
#                  handover comparison is a separate extension, as section 1 asks.
#
# The load is not just a sender rate: the goodput requirements are fractions of
# L, so each L has its own hash-pinned pilot.  Only L=8 exists so far; L=6 is
# section 7's calibration decision and is refused here until its pilot is built,
# because running L=6 against the L=8 snapshot would drive 6 Mbps at a 7.2 Mbps
# requirement and call the shortfall a result.
set -Eeuo pipefail
SP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXP=/opt/ran-lab/controller/agentic_ran_coordinator_based_on_ORAN/experiment_results/ota-20260911

PREF="${1:-}"
LOAD="${2:-8}"
shift $(( $# >= 2 ? 2 : $# ))
case "$PREF" in P1|P2|P3) ;; *) echo "usage: run_case_v4.sh <P1|P2|P3> <8>" >&2; exit 2;; esac

# 2026-09-17, owner's call: at a 7.2 Mbps goodput target the two-cell bed met every
# requirement from the common initial measurement alone -- T0_SUCCESS with the agent
# making no control decision at all (ue1 7.934, ue2 8.201, ue3 7.908 against 7.2, and
# the ue2 deadline ratio at 1.000).  A run that decides nothing measures nothing about
# coordination, so the goodput target rises to 9.0 while the concession floors (ue1 5.6,
# ue3 6.4), the steps, the deadline intent, the load, the policy and the action candidates
# all stay exactly as they were.  The arithmetic behind 9.0: gnb1 carried its two UEs at a
# measured 15.84 Mbps combined, so 9.0x2 = 18.0 is out of reach without control, while
# 9.0 + 6.4 = 15.4 is inside it -- the episode has to choose whom to raise and whom to
# concede.  L8TIGHT=0 restores the original 7.2 pilot for a paired comparison.
case "$LOAD" in
  8)
    if [ "${L8TIGHT:-1}" = 1 ]; then
      PILOT="$EXP/pilot38-v4-tight90-L8-20260917T0310"
      INTENTS_SHA=7bea7b6b2fc32aee054570d5f159be1b2693d0765227a885890b1a0afc6b2bdc
      MANIFEST_SHA=dd74b4103774acdcd183ce634623a7e2e3c91d46188b9421900b36b7c6c4897e
    else
      PILOT="$EXP/pilot38-v4-heterogeneous-L8-20260916T2045"
      INTENTS_SHA=11c2aea633d3ae824c769b1a8119a79871c2ffac4456683c365b23d51ac55906
      MANIFEST_SHA=dd74b4103774acdcd183ce634623a7e2e3c91d46188b9421900b36b7c6c4897e
    fi
    ;;
  6)
    echo "L=6 has no pilot yet: section 7 picks the second load from the physical" >&2
    echo "observations, and a pilot built before that would freeze a guess." >&2
    exit 2
    ;;
  *) echo "load must be 8 (6 once calibration names it)" >&2; exit 2;;
esac

export AIC_PREFERENCE="$PREF"
export AIC_OFFERED_LOAD_MBPS="$LOAD"
export AIC_PILOT="$PILOT"
export AIC_PILOT_INTENTS_SHA="$INTENTS_SHA"
export AIC_PILOT_MANIFEST_SHA="$MANIFEST_SHA"
# The label has to name the pilot that actually ran: a condition name is not
# evidence of what ran (2026-09-14, 134 attempts mislabelled P1/P2/P3).
export AIC_CONDITION="v4-$([ "${L8TIGHT:-1}" = 1 ] && echo tight90 || echo heterogeneous)-${PREF}-L${LOAD}"

# The power axis, cell scoped on both advertised cells.  Proven on the radio
# 2026-09-16: 0/6/12/0 dB applied and read back in RAN.Cell.TxAttenuationDb with
# the other cell unmoved.  The ladder is a calibration proposal, not a measured
# operating point (redesign section 5).
# 2026-09-17 밤: 이 줄의 `txAttenuationDb` 는 **지금까지 판에 닿은 적이 없다** —
# `run_formal_v3.sh` 가 `AIC_AXES` 를 무조건 덮어쓰고 있었다(그쪽을 고쳤다).
# 이제 살아나므로, 켜기 전에 카탈로그 기수를 정해야 한다: 감쇠는 **셀마다** 걸려
# 두 셀이면 제곱이고, 0/6/12 만으로도 1296 x 9 = 11664 로 `AIC_MAX_CATALOG=4096` 을
# 넘겨 `CatalogTooLargeError` 로 **판이 시작도 못 한다**.
# 상한을 올릴지·간격을 얼마로 할지·어느 셀에 걸지는 오너 결정 대기 중이므로,
# 그때까지 수집이 끊기지 않게 축을 뺀 채로 둔다.  결정되면 이 줄과
# `AIC_MAX_CATALOG`(아래)를 같이 고친다.
# 2026-09-17 (오너 결정: "3 단위로 두 셀 다 적용해").  전력 축을 액션 후보에 넣는다.
# 여기까지 오는 데 결함 둘을 먼저 고쳐야 했다:
#   - `run_formal_v3.sh` 가 이 줄을 무조건 덮어써서 **한 번도 판에 닿은 적이 없었다**
#   - 결정론적 대체 경로가 곱을 통째로 만들어, 촘촘한 사다리를 쓰면 거기서 먼저 터졌다
# 사다리는 3 dB 간격의 rung 네 개(3,6,9,12)이고 기준값 0.0 dB 는 rung 이 아니다 --
# 캡 축이 '무캡' 을 기준으로 두는 것과 같은 규약이다.  셀당 선택지 5 개, 두 셀이면 25 배.
# 2026-09-18 **일시 되돌림 — 수집 보호.**  전력 축을 켜자 판마다 즉사했다:
#   AttributeError: 'TxAttenuationAxisSpec' object has no attribute 'ue_id'
#   (tools/liveconsole/agent.py:4362·4368, `_compose`)
# `_compose` 가 보조 축을 **전부 UE 범위**로 가정한다(`spec.ue_id`,
# `identities[spec.ue_id]`).  전력 축은 **셀 범위**(`TxAttenuationAxisSpec.cell_nci`)라
# 이 조립 경로가 아직 지원하지 않는다 -- 축 이름·사다리·액추에이터·KPM 되읽기는 다 있는데
# **조립만 없다.**  조립 경로를 고친 뒤 이 줄을 되돌린다(아래 AIC_ATT_CELLS 는 그대로 둔다).
# 2026-09-18: 조립 경로를 고쳐 되돌린다.  `_compose` 가 보조 축을 전부 UE 범위로
# 가정하던 것을, 셀 축이면 **그 셀이 서빙하는 UE** 를 controlled 로 고르게 했다
# (조립기 `build.py` 는 이미 `scope_kind == "NRCellDU"` 를 다루고 있었다).
# 검증: tests.test_agent_sitting + 되읽기/게이트웨이 묶음 197개 통과.
# 2026-09-18 (오너 결정 "(다)로 해"): 전력 축 복귀.
# 앞서 5판 중 3판이 `REJECTED_CONFIG_MISMATCH` 로 죽었다 — 계획은 선언 기준값 `0.0`
# 인데 gnb2 는 배포 시점부터 10 dB 였다.  이제 `_live_cell_attenuations` 가 조립 전에
# KPM 에서 셀별 실제 값을 읽어 기준값으로 쓴다(실측 확인: {87654321:'10.0', 12345678:'0.0'}).
export AIC_AXES='servingCell,dlPrbCap,pfWeight,txAttenuationDb'
# 2026-09-17 (오너: "지금 전력 기준으로 옵션 개수를 맞춰").  셀마다 **자기 현재 전력에서**
# 3 dB 씩, 같은 개수(4 단)로 내려간다.  두 셀의 지금 값이 다르기 때문이다:
#   nb 3584 = NCI 12345678 (ue2·ue3 공유)  현재 0 dB  -> 3,6,9,12
#   nb 2816 = NCI 87654321 (ue1 단독)      현재 10 dB -> 13,16,19,22
# 롤백은 선언 기준값을 쓰지 않고 **읽은 값으로 되돌린다**
# (`live_actuation.observed_baseline`: "the first value read per axis since the
#  last commit scope armed ... the same value the executor recorded for its
#  rollback").  그러므로 축을 켜도 gnb2 의 10 dB 는 그대로 유지된다.
# `ci rfatt` 의 허용 범위는 0~60 dB, 소수 한 자리이므로 22 까지 안전하다.
export AIC_ATT_CELLS='12345678:3,6,9,12;87654321:13,16,19,22'
# 카탈로그 기수는 곱이므로 상한을 함께 올린다.  이 수는 **판의 비용이 아니다** --
# epoch 이 얼릴 수 있는 영역의 크기이고, 실제 후보는 Control 이 고르는 6 개 안팎이다.
# 2026-09-17: 첫 시도에서 거절 메시지가 실제 기수를 알려줬다 --
# `steer 8 x cap 24 x pf 16 x atten 25 = 76800`.  내 계산(1296 x 25)은 틀렸다:
# 이름을 주지 않은 ue3 가 cap/pf 의 **기본 사다리**를 받아 곱이 커진다.
# 같은 메시지가 비용도 말한다 -- 후보당 약 2.5 ms · 2.5 kB, 즉 76800 이면 판마다
# **약 190 초 · 190 MB** 가 epoch 동결에 더 든다.  판이 10 분 안팎이므로 3 분은 크다.
# 우선 통과시켜 **실제 동결 시간을 재고**, 비싸면 그때 사다리를 좁힌다(ue3 를 명시해
# cap/pf 에서 빼는 것이 가장 큰 절감이다).
# 2026-09-19: the epoch freezes the domain, not an enumeration (catalog.py), so
# the freeze no longer grows with the product; the owner's ladder of today is
# 8 x 64 x 64 x 25 = 819,200.  The ceiling stays only as a typo guard.
export AIC_MAX_CATALOG=1000000000

# No per-cell attenuation flag is needed or exists: exposure follows the KIND,
# so turning txAttenuationDb on gives every advertised cell an axis, and
# DEFAULT_LADDERS already carries exactly the redesign's 0/6/12 dB rungs.
#
# The UE ladders DO have to be narrowed here, and not for tidiness.  Left at
# their defaults the space is cap 4 ^3 x pf 4 ^3 x 9 power pairs = 36,864
# combinations, over the 4096 ceiling, and the epoch would be refused by name
# before anything ran.  Section 5's declared domains give the 216 the redesign
# states: ue1 cap {ref,12,18}, ue2 cap {ref,18}, ue3 cap fixed (left out, so it
# gets no axis at all), pf {1,4} on ue1 and ue2 only, and the two cell ladders.
# 2026-09-17 정정: 바로 위 주석의 "ue3 cap fixed (left out, so it gets no axis at
# all)" 는 **사실이 아니다.**  `_cap_axis_flags` 의 주석이 기전을 말한다 --
# "`_axis_specs` builds a cap axis for every UE named by an intent *or* by a
#  `--cap-axis`, so omitting both is the only way to leave one alone".
# ue3 는 인텐트 I3g 를 가지므로 이름을 빼도 축을 받고, 그때는 **기본 사다리**를 받아
# 곱이 커진다.  실측 거절 메시지가 그것을 그대로 보여 줬다:
#   steer 8 x cap 24 x pf 16 x atten 25 = 76,800   (cap 의 4, pf 의 4 가 ue3 기본값)
# 완전히 빼는 것은 기전상 불가능하므로 **한 단으로 묶는다**:
#   cap  ue1 3 x ue2 2 x ue3 2 = 12      pf  ue1 2 x ue2 2 x ue3 2 = 8
#   전체 8 x 12 x 8 x 25 = 19,200  (동결 약 48 초 · 48 MB)
# pf 의 기준값은 1.0 이므로 '1,4' 는 rung 이 {4} 하나다 -- 그래서 UE 당 2 선택이다.
# 2026-09-19 오너 결정: 사다리를 넓힌다 -- PF 2·4·8, cap 6·12·18 (세 UE 모두), 전력은 3 dB 단위 유지.
# 위의 '한 단으로 묶는다'는 조합 열거 비용 때문이었고, 그 좁힘이 5:5:5 에서 세 방식을 같은 네 수로
# 수렴시켰다.  카탈로그를 영역 동결로 바꾸기 전에는 이 곱(8x64x64x25)이 열거 상한을 넘으므로
# 그 변경이 들어가기 전에 이 파일로 판을 돌리지 말 것.
export AIC_CAP_HOSTS='ue1:18,12,6;ue2:18,12,6;ue3:18,12,6'
export AIC_PF_HOSTS='ue1:1,2,4,8;ue2:1,2,4,8;ue3:1,2,4,8'
# 2026-09-17 (오너: "셀 고정 아니야").  앞서 이 값을 `''` 로 두면 각 UE 가 현재 셀에
# 고정된다고 읽었는데, 그것은 v4 의 의도가 아니다 -- 조종은 계속 쓴다.  두 셀을
# **명시적으로** 적는다: v3 에서 물려받지 않고 v4 가 자기 설정을 소유한다.
export AIC_CELLS='12345678,87654321'

# --- v3 에서 조용히 물려받던 값들을 v4 가 **직접** 적는다 -------------------- #
# 2026-09-17 (오너: "유용하게 쓰고 있던 v3 세팅은 v4가 알아서 쓰게 만들라").
# 아래 여덟은 지금까지 `run_formal_v3.sh` 가 정해 v4 판을 지배하고 있었다.  값은
# 그대로 두되(유용하게 쓰이던 것들이다) **소유를 v4 로 옮긴다** -- 그래야 v4 를 읽는
# 사람이 실험 조건을 한 파일에서 전부 볼 수 있고, v3 가 바뀌어도 v4 가 흔들리지 않는다.
export AIC_CELL_CAPACITY_MBPS=14.5
export AIC_PRB_TOTAL=38
export AIC_OBSERVE_GOODPUT='dlGoodputMbps=1000:15000:120000'
export AIC_OBSERVE_DEADLINE='deadlineSuccessRatio=1000:15000:120000'
export AIC_OBSERVE_CELL='servingCell=1500:15000:120000'
export AIC_STOP_AFTER_RELAXED=0
export AIC_WITHDRAW_VERIFIED_SCOPE=1
export AIC_ENTRY=main

exec bash "$SP/run_formal_v3.sh" "$@"
