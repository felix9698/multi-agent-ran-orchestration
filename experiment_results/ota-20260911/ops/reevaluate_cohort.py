#!/usr/bin/env python3
"""핸드오프 2026-09-18 §6: 고정된 68판 cohort 와 그 뒤 판을 새 판정기로 다시 센다.

- cohort 는 ``ops/overnight/census.md`` 에 적힌 68 개 판 id 로 **고정**한다.
- 모든 시작된 판이 분모다 (판 기록이 없는 판도 '기록 없음' 으로 센다).
- 새 판정기 = ``assurance.coordination.tc.Trial.observation_validity`` (§4.2) 와
  ``CompatibilityRules.configuration_refusals`` (§3).  옛 ``success`` 는 관측이
  유효할 때만 달성으로 친다.
- 이것은 **구현 진단**이다 -- 과거의 잘못된 online best·호환성·정보 전달 아래 나온 선택은
  사후 채점으로 복구되지 않으며, 수정된 시스템의 공정한 비교 결과가 아니다 (§6.1).

사용: python3 ops/reevaluate_cohort.py   → ops/overnight/reeval/ 에 CSV 두 개와 요약 md
"""
from __future__ import annotations

import csv
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = os.path.dirname(HERE)
sys.path.insert(0, os.path.abspath(os.path.join(EXP, "..", "..")))
from assurance.coordination.tc import CompatibilityRules, Trial  # noqa: E402

OUT = os.path.join(HERE, "overnight", "reeval")
RULES = CompatibilityRules(exclusive_axes=(("dlPrbCap", "pfWeight"),))
#: 판 기록에 코드·프롬프트 해시가 없다.  프롬프트는 기록된 경계로만 추정한다 (arm_compare.py).
PROMPT_NOTE = "code version unknown (dirty worktree, no snapshot in the board); prompt inferred from recorded epochs"


def cohort_ids() -> list:
    text = open(os.path.join(HERE, "overnight", "census.md"), encoding="utf-8").read()
    return sorted(set(re.findall(r"\b2026091[78]T\d{6}\b", text)))


def board_dir(stamp: str):
    found = glob.glob(os.path.join(EXP, f"formal38guarded-{stamp}-*"))
    return found[0] if found else None


def t_index(target_id: str):
    digits = re.sub(r"\D", "", str(target_id))
    return int(digits) if digits else None


def best_valid(trials) -> tuple:
    """(가장 선호되는 T 번호, 시행 번호) -- 유효한 관측에서 통과한 것만."""
    best = (None, None)
    for trial in trials:
        if not trial.observation_valid:
            continue
        for target, ok in trial.success.items():
            n = t_index(target)
            if ok and n is not None and (best[0] is None or n < best[0]):
                best = (n, trial.trial_index)
    return best


def failure_cause(doc: dict) -> str:
    term = doc.get("termination") or {}
    reason = str(term.get("reason") or "")
    detail = str(term.get("detail") or "")
    if reason == "PROPOSAL_FAILURE":
        return "PROPOSAL_FAILURE/decision-timeout" if "decision allowance" in detail else "PROPOSAL_FAILURE/no-executable-proposal"
    return reason or "none"


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    ids = cohort_ids()
    trial_rows, board_rows = [], []
    for stamp in ids:
        path = board_dir(stamp)
        records = glob.glob(os.path.join(path, "evidence", "AGENT-*-episode.json")) if path else []
        if not records:
            board_rows.append({"board": stamp, "arm": "", "record": "missing", "note": PROMPT_NOTE})
            continue
        doc = json.load(open(records[0]))
        raw = doc.get("trials") or []
        trials = [Trial.from_record(item) for item in raw]
        base = (raw[0].get("configuration") if raw else {}) or {}
        for record, trial in zip(raw, trials):
            clash = RULES.configuration_refusals(trial.configuration, base)
            passed_old = sorted(t for t, ok in trial.success.items() if ok)
            att = trial.attainment
            trial_rows.append({
                "board": stamp, "arm": doc.get("method"), "trial": trial.trial_index,
                "control": trial.control_id,
                "changed": ";".join(f"{k}={v}" for k, v in sorted(trial.configuration.items())
                                    if base.get(k) != v),
                "capPfSameUe": "; ".join(clash),
                "terminalState": trial.execution_status["terminalState"] or "",
                "outcome": trial.execution_status["outcome"] or "",
                "stopReason": trial.execution_status["stopReason"] or "",
                "rolledBack": int(trial.rolled_back),
                "observationValid": int(trial.observation_valid),
                "observationReason": trial.observation_validity["reason"],
                "attainment": att["status"],
                "passedOld": len(passed_old), "passedNew": len(att["targets"]),
                "appliedAt": trial.applied_at, "elapsedMs": record.get("elapsedMs", ""),
            })
        best_old = ((doc.get("bestAttained") or {}).get("targetId") or "")
        best_new, best_trial = best_valid(trials)
        fs = doc.get("firstSuccess") or {}
        first_new = next((t.trial_index for t in trials if t.observation_valid and any(t.success.values())), None)
        board_rows.append({
            "board": stamp, "arm": doc.get("method"), "record": "present",
            "termination": (doc.get("termination") or {}).get("reason"),
            "failureCause": failure_cause(doc),
            "trials": len(trials),
            "voidObservations": sum(1 for t in trials if not t.observation_valid),
            "capPfSameUeTrials": sum(1 for r in trial_rows if r["board"] == stamp and r["capPfSameUe"]),
            "firstSuccessOld": fs.get("trialIndex", "") if fs else "",
            "firstSuccessNew": "" if first_new is None else first_new,
            "bestOld": best_old, "bestNew": "" if best_new is None else f"T{best_new}",
            "bestNewTrial": "" if best_trial is None else best_trial,
            "bestElapsedMs": (doc.get("bestAttained") or {}).get("elapsedMs", ""),
            # §5.3·§6.2: B 안의 달성은 증거가 B 안에 확보된 것만 인정한다.
            "B_ms": (doc.get("settings") or {}).get("deadlineMs") or 480000,
            "bestWithinB": ("" if (doc.get("bestAttained") or {}).get("elapsedMs") in (None, "")
                            else int(float((doc.get("bestAttained") or {}).get("elapsedMs"))
                                     <= float((doc.get("settings") or {}).get("deadlineMs") or 480000))),
            "concessionMeanOld": ((doc.get("bestAttained") or {}).get("concession") or {}).get("mean", ""),
            "retainedQualified": int(bool((doc.get("retained") or {}).get("qualified"))),
            "note": PROMPT_NOTE,
        })
    for name, rows in (("trials.csv", trial_rows), ("boards.csv", board_rows)):
        keys = sorted({k for r in rows for k in r}, key=lambda k: list(rows[0]).index(k) if k in rows[0] else 99)
        with open(os.path.join(OUT, name), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)

    # 요약 -- 원래 집계와 수정 집계의 차이, 분모는 시작된 판 전부
    by_arm = defaultdict(list)
    for r in board_rows:
        by_arm[r.get("arm") or "(기록 없음)"].append(r)
    lines = ["# §6 재평가 — 고정 68판 cohort", "",
             f"cohort {len(ids)}판 (census.md 고정). 판 기록 있음 "
             f"{sum(1 for r in board_rows if r['record'] == 'present')}판. {PROMPT_NOTE}.", "",
             "구현 진단이다 — 수정된 시스템의 공정한 비교가 아니다 (§6.1).", "",
             "| 팔 | 시작 판 | 첫 성공(옛) | 첫 성공(새) | 최선 달라짐 | 최선이 B 밖 | 무효 관측 시행 | 같은 UE cap+PF 시행 | 결정 60초 초과 종료 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for arm, rows in sorted(by_arm.items()):
        present = [r for r in rows if r["record"] == "present"]
        lines.append("| %s | %d | %d | %d | %d | %d | %d | %d | %d |" % (
            arm, len(rows),
            sum(1 for r in present if r["firstSuccessOld"] != ""),
            sum(1 for r in present if r["firstSuccessNew"] != ""),
            sum(1 for r in present if r["bestOld"] != r["bestNew"]),
            sum(1 for r in present if r["bestWithinB"] == 0),
            sum(r["voidObservations"] for r in present),
            sum(r["capPfSameUeTrials"] for r in present),
            sum(1 for r in present if r["failureCause"] == "PROPOSAL_FAILURE/decision-timeout")))
    open(os.path.join(OUT, "summary.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
