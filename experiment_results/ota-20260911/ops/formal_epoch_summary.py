#!/usr/bin/env python3
"""핸드오프 §8.1 의 formal epoch 판만 모아 요약한다.

경계는 **`manifest.json` 에 `codeVersion` 이 있는 첫 판**이다 —
`formal38guarded-20260918T153729`(KST 2026-09-19 00:37:29).  그 앞 판에는 그 필드가
없어서 "이 판이 어느 수정을 안고 돌았는가" 를 시작 시각과 파일 mtime 대조로 **추정**해야
했고, 밤새 그 추정을 하다가 두 번 틀렸다.  이 경계가 있는 이유가 그것이므로
**앞뒤를 섞지 않는다.**

경계 이전 판은 결함 작업의 증거로는 계속 유효하다(전력 축 사슬이 그렇게 증명됐다).
여기서 빼는 것은 formal 집계뿐이다.

읽기 전용이다 — 아무것도 쓰지 않고 베드를 건드리지 않는다
([[measure-the-bed-without-touching-it]]).
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FLOOR_MBPS = 7.2          # L=8 의 UE 별 DL goodput 바닥


def boards():
    """codeVersion 을 든 판만, 시작 시각 순으로."""
    for d in sorted(glob.glob(os.path.join(ROOT, "formal38guarded-*"))):
        try:
            manifest = json.load(open(os.path.join(d, "manifest.json")))
        except (OSError, ValueError):
            continue
        if not manifest.get("codeVersion"):
            continue          # 경계 이전: formal 집계에서 뺀다
        yield d, manifest


def episode(board):
    found = glob.glob(os.path.join(board, "evidence", "AGENT-*-episode.json"))
    if not found:
        return None
    try:
        return json.load(open(found[0]))
    except (OSError, ValueError):
        return None


def goodputs(trial):
    """한 시행의 UE 별 DL goodput.  success 플래그가 아니라 측정값에서 읽는다 --
    유효성 규칙이 바뀌어도 이 숫자는 그대로다."""
    return {k.split("@")[-1]: v
            for k, v in (trial.get("kpis") or {}).items()
            if isinstance(v, (int, float)) and "dlGoodput" in k}


def main() -> int:
    rows = []
    for d, manifest in boards():
        e = episode(d)
        name = os.path.basename(d)[16:31]
        kst = (datetime.datetime.strptime(name, "%Y%m%dT%H%M%S")
               + datetime.timedelta(hours=9))
        if e is None:
            # 아직 도는 판과 에피소드를 못 만든 판은 **다른 것**이다.  exit.json 이
            # 없으면 그 판은 진행 중이고, 실패로 세면 formal 집계가 시작하자마자
            # 오염된다.  ([[a-metric-that-reads-failure-as-success]] 의 사촌 --
            # 이 줄을 쓴 직후 진행 중인 판이 NO_EPISODE_RECORD 로 찍히는 걸 봤다.)
            running = not os.path.exists(os.path.join(d, "exit.json"))
            rows.append({"board": name, "kst": kst, "method": None,
                         "termination": "RUNNING" if running else "NO_EPISODE_RECORD",
                         "running": running})
            continue
        trials = e.get("trials") or []
        events = e.get("nonTrialEvents") or []
        calls = e.get("calls") or []
        allclear = sum(1 for t in trials
                       for g in [goodputs(t)]
                       if len(g) == 3 and all(v >= FLOOR_MBPS for v in g.values()))
        rows.append({
            "board": name, "kst": kst, "method": e.get("method"),
            "termination": (e.get("termination") or {}).get("kernelTermination"),
            "t0": e.get("t0Success"),
            "trials": len(trials),
            "dispatched": sum(1 for t in trials if t.get("catalogCandidateId")),
            "allThreeClearFloor": allclear,
            "power": [t.get("controlId") for t in trials
                      if "atten" in str(t.get("controlId", ""))],
            "steering": [t.get("controlId") for t in trials
                         if "steer" in str(t.get("controlId", "")).lower()],
            "disconnects": len(e.get("hardwareDisconnects") or []),
            "rebinds": len(e.get("identityRebinds") or []),
            "decisionTimeouts": sum(1 for x in events
                                    if x.get("kind") == "decision-timeout"),
            "inputTokens": sum(int(c.get("inputTokens") or 0) for c in calls),
            "outputTokens": sum(int(c.get("outputTokens") or 0) for c in calls),
            "codeVersion": str(manifest["codeVersion"].get("codeVersionSha256"))[:12],
        })

    if not rows:
        print("formal epoch 판이 아직 없다 (codeVersion 을 든 판 0개)")
        return 0

    print("formal epoch 판 %d개 — 경계는 codeVersion 보유 여부" % len(rows))
    print("  %-9s %-18s %-20s %5s %5s %6s %s"
          % ("KST", "팔", "종료", "시행", "전부", "결정초과", "codeVersion"))
    for r in rows:
        print("  %-9s %-18s %-20s %5s %5s %6s %s"
              % (r["kst"].strftime("%m-%d %H:%M"), r.get("method") or "—",
                 str(r.get("termination"))[:20], r.get("trials", "—"),
                 r.get("allThreeClearFloor", "—"), r.get("decisionTimeouts", "—"),
                 r.get("codeVersion", "")))

    judged = [r for r in rows if r.get("method")]
    running = [r for r in rows if r.get("running")]
    if running:
        print()
        print("  진행 중 %d개 — 집계에서 뺀다: %s"
              % (len(running), ", ".join(r["board"] for r in running)))
    print()
    print("  종료 분포:", Counter(r["termination"] for r in judged).most_common())
    print("  세 UE 가 함께 %.1f Mbps 를 넘은 시행: %d (판 %d개에서)"
          % (FLOOR_MBPS,
             sum(r["allThreeClearFloor"] for r in judged),
             sum(1 for r in judged if r["allThreeClearFloor"])))
    print("  전력 축 시행 %d · 조종 축 시행 %d"
          % (sum(len(r["power"]) for r in judged),
             sum(len(r["steering"]) for r in judged)))
    print("  단절 %d · 재바인딩 %d · 결정 초과 %d"
          % (sum(r["disconnects"] for r in judged),
             sum(r["rebinds"] for r in judged),
             sum(r["decisionTimeouts"] for r in judged)))
    print("  토큰 입력 %d · 출력 %d"
          % (sum(r["inputTokens"] for r in judged),
             sum(r["outputTokens"] for r in judged)))
    versions = {r["codeVersion"] for r in judged}
    if len(versions) > 1:
        print("  ** codeVersion 이 %d 종이다 %s — 이 집계는 한 판본이 아니다"
              % (len(versions), sorted(versions)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
