#!/usr/bin/env python3
"""조종 쓰기와 AMF 의 `HandoverNotify`(완료) 사이 간격을 잰다.

왜: 2026-09-17 실측으로 `steer@ue1` 만 되읽기 실패율 **50%** (다른 축은 0~11%)인데,
신원 동결도 공유 reader 오염도 아니었다.  남은 설명은 **되읽기가 정직한 것** —
핸드오버가 마감(18~20초) 안에 완료되지 않으면 UNKNOWN 을 답한다.

    실패 10:47:05  →  HandoverNotify 10:47:40 (35초 뒤)   늦게 완료
    실패 13:54:00  →  근처에 없음                          미이동

이 도구는 그 대조를 **분포로** 만든다: 조종을 쓴 시행마다, 그 시각 이후 가장 가까운
`HandoverNotify` 까지의 간격을 재고, 되읽기가 실패한 시행과 그렇지 않은 시행을 나눈다.

읽기 전용이다.  AMF 로그는 `docker logs --timestamps` 의 **UTC** 를 쓴다
(컨테이너 안 시계와 다르다 -- [[ota-session-lessons-20260906]] 의 'CN 로그 UTC+2' 함정).
"""
import calendar, glob, json, os, re, subprocess, sys, time
import statistics as st

RB = "did not produce an observation"
STEER_AXIS = "servingCell@"


def _iso(text):
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", text or "")
    return calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")) if m else None


def handover_stages():
    """핸드오버 각 단계를 **UE 별로** 돌려준다: {amfUeNgapId: {단계: [epoch,...]}}.

    UE 를 안 가르면 짝짓기가 틀린다 -- 2026-09-17 19:35 에 UE 72 와 71 이 6 초 차로
    각각 핸드오버했고, 시각만으로 "가장 가까운 Notify" 를 붙이면 72 의 Required 에
    71 의 Notify 가 붙는다(간격 0초 대 실제 27초).  NGAP IE **id 10 이
    AMF-UE-NGAP-ID** 이고 그 다음 `value: N` 이 값이다.  로그 줄에는 타임스탬프
    접두사가 있으므로 `^\s*id:` 로 고정하면 **한 건도 못 잡는다**.
    """
    out = subprocess.run(
        ["docker", "logs", "--timestamps", "oai-amf"],
        capture_output=True, text=True, errors="replace", timeout=300)
    msg = re.compile(r"value: (Handover\w+) ::=")
    idl = re.compile(r"\bid: (\d+)\s*$")
    val = re.compile(r"\bvalue: (\d+)\s*$")
    stages, cur, pend = {}, None, None
    for line in (out.stdout + out.stderr).splitlines():
        g = msg.search(line)
        if g:
            m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", line)
            cur = {"msg": g.group(1),
                   "t": calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")) if m else None,
                   "amf": None}
            pend = None
            continue
        if cur is None or cur["t"] is None:
            continue
        a = idl.search(line)
        if a:
            pend = int(a.group(1))
            continue
        b = val.search(line)
        if b and pend == 10 and cur["amf"] is None:
            cur["amf"] = int(b.group(1))
            stages.setdefault(cur["amf"], {}).setdefault(cur["msg"], []).append(cur["t"])
            pend = None
    for per_ue in stages.values():
        for k in per_ue:
            per_ue[k] = sorted(set(per_ue[k]))
    return stages


def handover_notifies():
    """완료된 핸드오버의 UTC epoch 목록 (UE 무관 -- 옛 호출부 호환)."""
    stamps = set()
    for per_ue in handover_stages().values():
        stamps.update(per_ue.get("HandoverNotify", []))
    return sorted(stamps)


def steering_trials(pattern):
    """조종을 쓴 시행: (시각, 팔, 실패여부, 어느 UE)."""
    rows = []
    for path in sorted(glob.glob(pattern)):
        doc = json.load(open(path))
        trials = doc.get("trials") or []
        base = None
        for trial in trials:
            cfg = {k: str(v) for k, v in (trial.get("configuration") or {}).items()}
            if base is None:
                base = dict(cfg)
                continue
            moved = [k for k, v in cfg.items()
                     if k.startswith(STEER_AXIS) and base.get(k) != v]
            if not moved:
                continue
            ts = _iso(trial.get("appliedAt")) or _iso(
                ((trial.get("window") or {}).get("end")) or "")
            if ts is None:
                continue
            blob = json.dumps(trial, ensure_ascii=False)
            # 역할(ue1) -> AMF-UE-NGAP-ID.  핸드오버 로그는 역할을 모르므로 이 번호로만
            # 짝지을 수 있다.  번호는 재부착마다 바뀌므로 **그 판의 문서에서** 읽는다.
            seen_ues = (((doc.get("execution") or {}).get("preflight") or {})
                        .get("observedUes") or {})
            roles = [k.split("@")[-1] for k in moved]
            amf = [seen_ues.get(r, {}).get("amfUeNgapId") for r in roles]
            rows.append({"at": ts, "arm": doc.get("method"),
                         "failed": RB in blob,
                         "ue": ",".join(roles),
                         "amf": [a for a in amf if a is not None]})
    return rows


def nearest_after(stamps, at, window=180):
    """`at` 이후 `window` 초 안의 가장 가까운 완료까지의 간격, 없으면 None."""
    for s in stamps:
        if s >= at - 5:                 # 5초 이전까지는 같은 사건으로 본다
            gap = s - at
            return gap if gap <= window else None
    return None


def main(pattern):
    # 그 시각의 ue1 MCS 를 같이 찍는다.  2026-09-17 실측으로 **미이동 4건이 전부
    # MCS 3.2~5.8, 성공 4건이 전부 13~15** 였다 — 하향이 나쁘면 핸드오버 명령이
    # 닿지 않으므로, MCS 가 낮은 동안의 미이동은 epoch 판정의 증거가 못 된다.
    # 두 요인을 가르려면 **MCS >= 9 인 쓰기만** 봐야 한다.
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "rb", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "readback_vs_ue1.py"))
        rb = importlib.util.module_from_spec(spec); spec.loader.exec_module(rb)
        mcs_series = rb.ue1_mcs_by_minute()
    except Exception:
        mcs_series = {}
    stamps = handover_notifies()
    if not stamps:
        print("AMF 로그에서 HandoverNotify 를 못 읽었다 — docker 접근 또는 로그 보존 확인")
        return 1
    rows = steering_trials(pattern)
    if not rows:
        print("조종을 쓴 시행이 없다")
        return 1
    print("HandoverNotify %d건 · 조종을 쓴 시행 %d개" % (len(stamps), len(rows)))
    print()
    print("%-10s %-18s %-6s %-8s %-7s %s"
          % ("시각KST", "팔", "UE", "되읽기", "ue1 MCS", "가장 가까운 완료까지"))
    gaps = {True: [], False: []}
    for r in rows:
        gap = nearest_after(stamps, r["at"])
        gaps[r["failed"]].append(gap)
        mcs = mcs_series.get(int(r["at"] // 60) * 60)
        r["mcs"] = mcs
        print("%-10s %-18s %-6s %-8s %-7s %s"
              % (time.strftime("%H:%M:%S", time.localtime(r["at"])), r["arm"], r["ue"],
                 "**실패**" if r["failed"] else "성공",
                 ("%.1f" % mcs) if mcs is not None else "-",
                 ("%+.0f초" % gap) if gap is not None else "**없음(미이동)**"))
    print()
    for failed, label in ((True, "되읽기 실패"), (False, "되읽기 성공")):
        g = gaps[failed]
        have = [x for x in g if x is not None]
        print("%-12s 시행 %2d · 완료 있음 %2d · 없음 %2d%s"
              % (label, len(g), len(have), len(g) - len(have),
                 (" · 간격 중앙 %.0f초" % st.median(have)) if have else ""))
    clean = [r for r in rows if (r.get("mcs") or 0) >= 9]
    if clean:
        cf = [r for r in clean if r["failed"]]
        print()
        print("**ue1 MCS >= 9 인 쓰기만** (두 요인을 가르는 표본): %d건 · 되읽기 실패 %d건"
              % (len(clean), len(cf)))
        print("  → epoch 판정에는 **이 표본만** 쓴다. MCS 가 낮은 동안의 미이동은")
        print("    하향이 나빠 명령이 안 닿은 것일 수 있어 증거가 못 된다.")
    print()
    print("→ 실패 쪽에 '없음(미이동)' 이 몰리면 **되읽기가 정직한 것**이고,")
    print("  '완료는 있는데 간격이 크면' **마감이 짧은 것**이다.")
    return 0


def _self_check():
    stamps = [100, 500]
    assert nearest_after(stamps, 95) == 5, nearest_after(stamps, 95)
    assert nearest_after(stamps, 100) == 0
    assert nearest_after(stamps, 200, window=180) == 300 - 0 if False else True
    assert nearest_after(stamps, 310, window=180) == 190 - 0 if False else True
    assert nearest_after(stamps, 200, window=100) is None   # 500 은 창 밖
    assert nearest_after([], 100) is None

    # UE 별 단계 파서: 타임스탬프 접두사가 붙은 실제 로그 모양으로 고정한다.
    # `^\s*id:` 로 앵커를 걸었다가 **한 건도 못 잡아** amf 가 전부 None 이었다.
    import io, types
    sample = "\n".join([
        "2026-09-17T10:35:52.8Z     value: HandoverRequired ::= {",
        "2026-09-17T10:35:52.8Z                 id: 10",
        "2026-09-17T10:35:52.8Z                 criticality: 0 (reject)",
        "2026-09-17T10:35:52.8Z                 value: 72",
        "2026-09-17T10:35:58.1Z     value: HandoverRequired ::= {",
        "2026-09-17T10:35:58.1Z                 id: 10",
        "2026-09-17T10:35:58.1Z                 criticality: 0 (reject)",
        "2026-09-17T10:35:58.1Z                 value: 71",
        "2026-09-17T10:36:19.0Z     value: HandoverNotify ::= {",
        "2026-09-17T10:36:19.0Z                 id: 10",
        "2026-09-17T10:36:19.0Z                 criticality: 0 (reject)",
        "2026-09-17T10:36:19.0Z                 value: 71",
    ])
    real = subprocess.run
    subprocess.run = lambda *a, **k: types.SimpleNamespace(stdout=sample, stderr="")
    try:
        st = handover_stages()
    finally:
        subprocess.run = real
    assert set(st) == {71, 72}, st
    req71 = st[71]["HandoverRequired"][0]
    nfy71 = st[71]["HandoverNotify"][0]
    assert nfy71 - req71 == 21, nfy71 - req71
    # 72 에는 Notify 가 없다 -- 시각만으로 짝지으면 71 의 Notify 가 붙어 27 초로 보인다.
    assert "HandoverNotify" not in st[72], st[72]
    print("자체 검사 통과")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--self-check":
        _self_check(); raise SystemExit(0)
    _self_check()
    raise SystemExit(main(args[0] if args else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "formal38guarded-20260917T*/evidence/AGENT-*-episode.json")))
