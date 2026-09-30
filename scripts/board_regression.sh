#!/bin/bash
# 실제 판이 죽은 모양을 재현한 회귀 묶음 (2026-09-24 판 466·468·470·Q-3).
# 러너·게이트웨이·producer·ops 코드를 바꾸면 반영 전에 이것부터 통과해야 한다.
# 오너: "왜 맨날 다른 오류가 생기는거야?" -- 수정이 기록된 판 모양으로 시험되지 않아
# 내 수정이 만든 결함을 실판에서 처음 발견했다.  새 판 사망 원인을 고치면 그 모양의
# 테스트를 여기 한 줄 더한다.
set -e
cd "$(dirname "$0")/.."
python3 -m unittest -q \
  tests.assurance.test_r1_handback_after_reregistration \
  tests.assurance.test_r1_lost_ack_matrix \
  tests.test_agent_recovery_not_charged_to_b \
  tests.test_agent_refill_wait_is_capped \
  tests.test_ota_ops_placement_restore \
  tests.oran.test_campaign5_live_worker \
  tests.test_ota_ops_bed_ready
