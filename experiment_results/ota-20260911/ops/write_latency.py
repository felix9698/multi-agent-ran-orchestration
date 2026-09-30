#!/usr/bin/env python3
"""쓰기 지연을 **시간순으로** 낸다 — 프로듀서 누적 곡선을 보기 위한 도구.

2026-09-17: 쓰기 중앙값이 하루 동안 7.3초 → 12.5초로 올랐고, 프로듀서를 재기동하자
**4.8초**로 떨어졌다(n=291 대 n=9, Mann-Whitney p=2.0e-06).  원인은 무선이 아니라
**프로듀서에 쌓인 상태**였다(RESULTS 4.9.2).

누적 원인 셋 중 둘은 그날 고쳤지만(KPM 줄 캐시 창 · 거절된 생성 되돌리기) 갇힌
정책은 남아 있다.  다시 쌓이면 같은 곡선을 그리므로, **재기동 주기는 이 곡선을
보고 정한다.**

    python3 ops/write_latency.py [--bucket 분] [판 글롭]
"""
import json, glob, os, sys, statistics, collections
from datetime import datetime

DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "formal38guarded-2026*", "action-r1-state", "*", "*-operations.jsonl")
CONTROL_DEADLINE_S = 18.0      # 프로듀서 인자 --control-deadline-seconds


def _stamp(value):
    text = str(value or "")
    try:
        return datetime.strptime(text[:26] + "Z" if len(text) > 20 else text,
                                 "%Y-%m-%dT%H:%M:%S.%fZ").timestamp()
    except ValueError:
        return None


def writes(pattern):
    """(끝난 시각, 소요 ms, 연산) — CREATE/UPDATE 만.  저널의 시각은 UTC 문자열이다."""
    rows = []
    for path in sorted(glob.glob(pattern)):
        in_flight = {}
        for line in open(path, errors="replace"):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            moment = _stamp(record.get("at"))
            if moment is None:
                continue
            if record.get("outcome") == "IN_FLIGHT":
                in_flight[record.get("reference")] = (moment, record.get("operation"))
                continue
            key = (record.get("resolves") if record.get("resolves") in in_flight
                   else record.get("reference"))
            if key not in in_flight:
                continue
            started, operation = in_flight.pop(key)
            if operation in ("CREATE", "UPDATE"):
                rows.append((moment, (moment - started) * 1000.0, operation))
    rows.sort()
    return rows


def main(pattern, bucket_minutes):
    rows = writes(pattern)
    if not rows:
        print("쓰기 기록이 없다:", pattern)
        return 1
    buckets = collections.defaultdict(list)
    for moment, millis, _operation in rows:
        slot = int(moment // (bucket_minutes * 60))
        buckets[slot].append(millis)
    print("쓰기 %d건 · %d분 단위 (시각은 저널의 UTC 문자열 그대로)" % (len(rows), bucket_minutes))
    print("%-10s %5s %10s %10s %8s" % ("시각", "n", "중앙 ms", "90분위", "마감여유"))
    for slot in sorted(buckets):
        values = sorted(buckets[slot])
        if len(values) < 3:
            continue
        median = values[len(values) // 2]
        p90 = values[int(len(values) * 0.9)]
        # 제어 마감까지 남는 여유 -- 이것이 0 에 가까워지면 NACK 이 시작된다 (4.8.4)
        headroom = CONTROL_DEADLINE_S - p90 / 1000.0
        print("%-10s %5d %10.0f %10.0f %7.1fs%s"
              % (datetime.fromtimestamp(slot * bucket_minutes * 60).strftime("%H:%M"),
                 len(values), median, p90, headroom,
                 "  ← 여유 5초 미만" if headroom < 5 else ""))
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    minutes = 30
    if args and args[0] == "--bucket":
        minutes = int(args[1]); args = args[2:]
    raise SystemExit(main(args[0] if args else DEFAULT, minutes))
