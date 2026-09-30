#!/usr/bin/env python3
"""판 하나를 한 줄로 요약한다 — 감시·판정에서 같이 쓴다.

왜 스크립트인가: 같은 논리를 감시 명령 문자열에 매번 다시 적었고, 그러다
**되읽기 실패를 문서 전체 문자열 등장 횟수로 세는 실수**를 했다(2026-09-17).
종료 상세가 같은 문구를 반복하므로 그 수는 시행 수가 아니다. 한 곳에 두고 고친다.
"""
import glob, json, os, re, sys, time, calendar

RB = "did not produce an observation"


def _iso(text):
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", text or "")
    return calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")) if m else None


def summarize(path):
    doc = json.load(open(path))
    term = doc.get("termination") or {}
    reason = term.get("reason") if isinstance(term, dict) else term
    trials = doc.get("trials") or []
    # **시행 단위**로 센다 -- 문서 전체 문자열 수가 아니다
    rb_trials = [i for i, t in enumerate(trials)
                 if RB in json.dumps(t, ensure_ascii=False)]
    decided = [t for t in trials
               if str(((t.get("decision") or {}).get("model") or "")).lower()
               not in ("", "deterministic", "none")]
    calls = [c for c in (doc.get("calls") or [])
             if isinstance(c, dict) and (c.get("inputTokens") or 0)]
    stamps = []
    for i in rb_trials:
        ts = _iso(trials[i].get("appliedAt")) or _iso(
            ((trials[i].get("window") or {}).get("end")) or "")
        stamps.append("%d@%s" % (i, time.strftime("%H:%M:%S", time.localtime(ts))
                                 if ts else "?"))
    return ("팔 **%s** | %s | 시행 %d(결정 %d) | 유지 %s | 호출 %d | "
            "**되읽기실패 시행 %d**%s | scope409 %d"
            % (doc.get("method"), reason, len(trials), len(decided),
               bool((doc.get("retained") or {}).get("qualified")), len(calls),
               len(rb_trials), (" [%s]" % ",".join(stamps)) if stamps else "",
               json.dumps(doc, ensure_ascii=False).count("already owned")))


def _self_check():
    """문서 전체 문자열 수가 아니라 **시행 수**를 세는지 고정한다."""
    doc = {"method": "x", "termination": {"reason": "R", "detail": RB + " " + RB},
           "trials": [{"a": 1}, {"b": RB}], "retained": {"qualified": False}, "calls": []}
    import tempfile, pathlib
    p = pathlib.Path(tempfile.mkdtemp()) / "e.json"
    p.write_text(json.dumps(doc))
    line = summarize(str(p))
    assert "되읽기실패 시행 1" in line, line      # 종료 상세의 2회는 세지 않는다
    print("자체 검사 통과")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--self-check":
        _self_check(); raise SystemExit(0)
    if not args:
        raise SystemExit("usage: episode_line.py <판디렉터리|증거파일>")
    target = args[0]
    if os.path.isdir(target):
        found = glob.glob(os.path.join(target, "evidence", "AGENT-*-episode.json"))
        if not found:
            print("%s — 증거 없음 (제출 전 거절이거나 기록 중)" % os.path.basename(target.rstrip("/"))[:46])
            raise SystemExit(0)
        target = found[0]
    print("%s | %s" % (os.path.basename(os.path.dirname(os.path.dirname(target)))[15:31],
                       summarize(target)))
