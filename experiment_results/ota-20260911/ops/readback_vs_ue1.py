#!/usr/bin/env python3
"""되읽기 실패가 **ue1 저하와 함께 오는가** — 시행 단위 대조.

왜 시행 단위인가 (2026-09-17 에 두 번 틀린 뒤):
  · 문서 전체의 문자열 등장 횟수를 세면 종료 상세의 반복까지 세어 3~4배로 부푼다
  · 판 **시작** 시각으로 조건을 걸면 안 된다 — 되읽기는 판 **도중**에 일어나고
    ue1 은 그 사이에 무너졌다 돌아온다 (저하 비율 51%)

그래서 **각 시행이 일어난 시각**의 ue1 MCS 를 붙여, 실패한 시행과 그렇지 않은 시행의
MCS 분포를 비교한다.  읽기 전용이다.
"""
import calendar, glob, json, os, re, statistics as st, subprocess, sys, time

RB = "did not produce an observation"
GNB2 = "enb2"


def _iso(text):
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", text or "")
    return calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")) if m else None


def ue1_mcs_by_minute(hours=6):
    """gnb2 로그에서 분 단위 ue1 MCS 평균.  부하 표본만(goodput > 0.3)."""
    script = r'''
import re,time,glob,statistics as st
from collections import defaultdict
UP=float(open('/proc/uptime').read().split()[0]); NOW=time.time()
b=defaultdict(list)
for G in sorted(glob.glob('/tmp/gnb2-probe-*.log'))[-3:]:
    ts=re.compile(rb'^(\d{6,}\.\d+)')
    dl=re.compile(rb'^UE [0-9a-f]+: dlsch_rounds .*MCS \(\d+\) (\d+).*goodput ([\d.]+)')
    cur=None
    try: f=open(G,'rb')
    except OSError: continue
    for line in f:
        m=ts.match(line)
        if m: cur=float(m.group(1))
        if not cur: continue
        c=dl.match(line)
        if not c or float(c.group(2))<=0.3: continue
        b[int((NOW-(UP-cur))//60)*60].append(int(c.group(1)))
for k in sorted(b):
    if len(b[k])>=5: print('%d %.2f %d'%(k,st.mean(b[k]),len(b[k])))
'''
    out = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", GNB2,
         "python3 - <<'PYX'\n" + script + "\nPYX"],
        capture_output=True, text=True, timeout=180).stdout
    series = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3:
            series[int(parts[0])] = float(parts[1])
    return series


def collect(pattern, series):
    rows = []
    for path in sorted(glob.glob(pattern)):
        doc = json.load(open(path))
        arm = doc.get("method") or "?"
        for i, t in enumerate(doc.get("trials") or []):
            ts = _iso(t.get("appliedAt")) or _iso(((t.get("window") or {}).get("end")) or "")
            if ts is None:
                continue
            mcs = series.get(int(ts // 60) * 60)
            if mcs is None:            # gnb2 로그가 그 분을 덮지 않는다
                continue
            rows.append({"arm": arm, "trial": i, "ts": ts, "mcs": mcs,
                         "rb": RB in json.dumps(t, ensure_ascii=False)})
    return rows


def main(pattern):
    series = ue1_mcs_by_minute()
    if not series:
        print("gnb2 의 분 단위 MCS 를 못 읽었다 — ssh 또는 로그 확인")
        return 1
    rows = collect(pattern, series)
    if not rows:
        print("시각을 붙일 수 있는 시행이 없다 (gnb2 로그 구간 밖)")
        return 1
    bad = [r for r in rows if r["rb"]]
    good = [r for r in rows if not r["rb"]]
    print("시각을 붙인 시행 %d개 (되읽기실패 %d · 정상 %d)" % (len(rows), len(bad), len(good)))
    print()
    print("%-22s %6s %8s %8s %8s" % ("무리", "시행", "MCS중앙", "MCS평균", "MCS<9 비율"))
    for name, g in (("되읽기 실패", bad), ("정상", good)):
        if not g:
            print("%-22s %6d %8s %8s %8s" % (name, 0, "-", "-", "-")); continue
        m = [r["mcs"] for r in g]
        print("%-22s %6d %8.1f %8.1f %7.0f%%"
              % (name, len(g), st.median(m), st.mean(m),
                 100 * sum(1 for x in m if x < 9) / len(m)))
    print()
    for r in bad:
        print("  실패 %s  %s 시행%d  MCS %.1f"
              % (time.strftime("%m-%d %H:%M:%S", time.localtime(r["ts"])),
                 r["arm"], r["trial"], r["mcs"]))
    if len(bad) < 5 or len(good) < 5:
        print()
        print("**표본이 작다 — 방향만 본다.** 각 무리 5개 이상에서 말한다.")
    return 0


def _self_check():
    series = {60: 12.0, 120: 4.0}
    doc = {"method": "x", "trials": [
        {"appliedAt": "1970-01-01T00:01:10Z"},
        {"appliedAt": "1970-01-01T00:02:10Z", "d": RB}]}
    import tempfile, pathlib
    p = pathlib.Path(tempfile.mkdtemp()) / "e.json"
    p.write_text(json.dumps(doc))
    rows = collect(str(p), series)
    assert len(rows) == 2, rows
    assert rows[0]["mcs"] == 12.0 and rows[0]["rb"] is False, rows
    assert rows[1]["mcs"] == 4.0 and rows[1]["rb"] is True, rows
    print("자체 검사 통과")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--self-check":
        _self_check(); raise SystemExit(0)
    _self_check()
    raise SystemExit(main(args[0] if args else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "formal38guarded-2026*T*/evidence/AGENT-*-episode.json")))
