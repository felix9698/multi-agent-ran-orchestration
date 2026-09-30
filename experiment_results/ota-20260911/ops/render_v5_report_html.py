"""Render the export_v5_trajectories.py CSVs as one self-contained HTML page (report.html).

    python3 ops/render_v5_report_html.py reports/v5-trajectories-<stamp>
"""
import csv
import html
import json
import math
import os
import statistics as st
import sys

METHOD_ORDER = ('three-agent', 'internal-monolith', 'basic-monolith')
METHOD_LABEL = {'three-agent': '3A · three-agent', 'internal-monolith': 'IM · internal-monolith',
                'basic-monolith': 'BM · basic-monolith'}
METHOD_VAR = {'three-agent': '--m3a', 'internal-monolith': '--mim', 'basic-monolith': '--mbm'}
AXES = ('servingCell@ue1', 'servingCell@ue2', 'servingCell@ue3', 'dlPrbCap@ue1', 'dlPrbCap@ue2',
        'dlPrbCap@ue3', 'pfWeight@ue1', 'pfWeight@ue2', 'pfWeight@ue3', 'txAttenuationDb@87654321')


def load(d, name):
    p = os.path.join(d, name + '.csv')
    return list(csv.DictReader(open(p))) if os.path.exists(p) else []


def e(x):
    return html.escape('' if x is None else str(x))


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def f(x, nd=2):
    v = num(x)
    return '' if v is None else (f'{v:.{nd}f}' if nd else f'{v:.0f}')


def chart(boards, trials):
    """Running best p (log scale) against trial index, one faint line per board, bold method median."""
    W, H, L, R, T, B = 760, 330, 64, 150, 18, 42
    maxp = max([num(t['runningBestP']) or 0 for t in trials] + [1])
    ymax = math.log10(maxp + 1)
    xs = sorted({int(t['trial']) for t in trials})
    xmax = max(xs) if xs else 8
    X = lambda i: L + (W - L - R) * i / max(xmax, 1)
    Y = lambda p: T + (H - T - B) * (1 - math.log10(p + 1) / ymax)
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="시행별 누적 최선 p">']
    for p in (0, 10, 100, 1000, 10000, 100000):
        if p <= maxp:
            y = Y(p)
            out.append(f'<line x1="{L}" y1="{y:.1f}" x2="{W-R}" y2="{y:.1f}" class="grid"/>'
                       f'<text x="{L-8}" y="{y+4:.1f}" class="tick" text-anchor="end">{p:,}</text>')
    for i in xs:
        out.append(f'<text x="{X(i):.1f}" y="{H-B+18}" class="tick" text-anchor="middle">{i}</text>')
    out.append(f'<text x="{(L+W-R)/2:.1f}" y="{H-6}" class="axis" text-anchor="middle">시행 번호 (0 = 시작 측정)</text>')
    out.append(f'<text x="14" y="{(T+H-B)/2:.1f}" class="axis" transform="rotate(-90 14 {(T+H-B)/2:.1f})" text-anchor="middle">누적 최선 p (로그, 작을수록 좋음)</text>')
    for m in METHOD_ORDER:
        per = {}
        for t in trials:
            if t['method'] == m and num(t['runningBestP']) is not None:
                per.setdefault(t['attempt'], []).append((int(t['trial']), num(t['runningBestP'])))
        for pts in per.values():
            pts.sort()
            out.append(f'<polyline class="board" style="stroke:var({METHOD_VAR[m]})" points="'
                       + ' '.join(f'{X(i):.1f},{Y(p):.1f}' for i, p in pts) + '"/>')
        med = []
        for i in xs:
            v = [dict(pts).get(i) for pts in per.values() if dict(pts).get(i) is not None]
            if v:
                med.append((i, st.median(v)))
        if med:
            out.append(f'<polyline class="median" style="stroke:var({METHOD_VAR[m]})" points="'
                       + ' '.join(f'{X(i):.1f},{Y(p):.1f}' for i, p in med) + '"/>')
            li, lp = med[-1]
            out.append(f'<circle cx="{X(li):.1f}" cy="{Y(lp):.1f}" r="3.5" style="fill:var({METHOD_VAR[m]})"/>'
                       f'<text x="{X(li)+8:.1f}" y="{Y(lp)+4:.1f}" class="lab" style="fill:var({METHOD_VAR[m]})">{ {"three-agent":"3A","internal-monolith":"IM","basic-monolith":"BM"}[m]} {lp:,.0f}</text>')
    out.append('</svg>')
    return ''.join(out)


def table(headers, rows, cls=''):
    h = ''.join(f'<th>{e(x)}</th>' for x in headers)
    b = ''.join('<tr>' + ''.join(f'<td>{c}</td>' for c in r) + '</tr>' for r in rows)
    return f'<div class="tw"><table class="{cls}"><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table></div>'


def main():
    d = sys.argv[1]
    boards, trials = load(d, 'boards'), load(d, 'trials')
    targets, controls, calls = load(d, 'targets'), load(d, 'controls'), load(d, 'calls')

    # summary
    srows = []
    for m in METHOD_ORDER:
        bs = [b for b in boards if b['method'] == m]
        if not bs:
            continue
        att = [b for b in bs if b['attained'] == 'True']
        ps = [num(b['bestP']) for b in att if num(b['bestP']) is not None]
        eheld = sum(1 for b in att if any(t['attempt'] == b['attempt'] and t['trial'] == b['bestTrial'] and t['binE'] == '0' for t in trials))
        srows.append([f'<span class="m" style="--c:var({METHOD_VAR[m]})">{e(METHOD_LABEL[m])}</span>', len(bs), f'{len(att)}/{len(bs)}',
                      sum(1 for b in bs if b['t0'] == 'True'), f'{eheld}/{len(att)}',
                      f'{st.median(ps):,.0f}' if ps else '', f'{min(ps):,.0f}' if ps else '',
                      f'{st.median(num(b["llmLatencyMs"]) for b in bs)/1000:.0f} s',
                      f'{st.median(num(b["inputTokens"]) for b in bs):,.0f}'])

    sections = []
    for b in boards:
        a = b['attempt']
        bt = [t for t in trials if t['attempt'] == a]
        bT = [t for t in targets if t['attempt'] == a]
        bC = [c for c in controls if c['attempt'] == a]
        bK = [c for c in calls if c['attempt'] == a]
        reqs = [k for k in (bT[0].keys() if bT else []) if k.endswith('.r1') and not k.startswith('level:')]
        base = {k: bC[0].get('cfg:' + k, bC[0].get(k)) for k in AXES} if bC else {}
        crow = []
        for c in bC:
            diff = ', '.join(f'{k}={c.get(k)}' for k in AXES if c.get(k) and c.get(k) != base.get(k)) or '기준'
            crow.append([f'<b>{e(c["controlId"])}</b>', f'<code>{e(diff)}</code>', e(c.get('rationale'))])
        trow = []
        for t in bt:
            bins = '·'.join(x if x != '' else '–' for x in (t['binE'], t['binUE1'], t['binI2d'], t['binUE3'], t['binI2g']))
            ok = t['attained'] == 'True'
            trow.append([t['trial'], e((t['appliedAt'] or '')[11:19]), e(t['targetId']), f'<b>{e(t["controlId"])}</b>',
                         e(t['outcome']), f(t['ue1Mbps']), f(t['ue2Mbps']), f(t['ue3Mbps']), f(t['ue2DeadlineRatio'], 3),
                         f(t['gnb2AttenDb'], 1), f'<span class="{"ok" if ok else "no"}">{"달성" if ok else "미달"}</span>',
                         f'{num(t["p"]):,.0f}' if num(t['p']) is not None else '', f'<code>{bins}</code>',
                         f'{num(t["runningBestP"]):,.0f}' if num(t['runningBestP']) is not None else '',
                         f(t['decisionLatencyMs'], 0), e(t['rationale'])])
        sections.append(f'''
<details class="board" id="b{e(a)}"><summary><span class="m" style="--c:var({METHOD_VAR.get(b["method"], "--ink")})">{e(METHOD_LABEL.get(b["method"], b["method"]))}</span>
<span class="bid">판 {e(a)} · 블록 {e(b["block"])} 슬롯 {e(b["slot"])}</span>
<span class="res">최선 p {f'{num(b["bestP"]):,.0f}' if num(b['bestP']) is not None else '–'}{' · T0 달성' if b['t0'] == 'True' else ''} · {e(b["termination"])}</span></summary>
<dl class="facts">
<div><dt>블록 참조</dt><dd>L {e(b["loadMbps"])} Mbps · UE1 {f(b["refUe1"])} · UE2 {f(b["refUe2"])} · UE3 {f(b["refUe3"])} · gnb1 합 {f(b["refGnb1Sum"])}</dd></div>
<div><dt>UE2 마감 d</dt><dd>{f(b["deadlineMs"],1)} ms · 참조 A@d {e(b["aAtD"])}</dd></div>
<div><dt>종료</dt><dd>{e(b["termination"])} · Kernel {e(b["kernelTermination"])}</dd></div>
<div><dt>유지</dt><dd>{e(b["retainedControl"])} · 적격 {e(b["retainedQualified"])}</dd></div>
<div><dt>최선</dt><dd>시행 {e(b["bestTrial"])} · 첫 달성 시행 {e(b["firstAttainTrial"])}</dd></div>
<div><dt>LLM</dt><dd>{e(b["llmCalls"])}회 · {f(num(b["llmLatencyMs"])/1000 if num(b["llmLatencyMs"]) else 0,0)} s · 입력 {f(b["inputTokens"],0)} / 출력 {f(b["outputTokens"],0)} 토큰</dd></div>
<div><dt>기록</dt><dd><code>{e(b["dir"])}</code></dd></div>
</dl>
<h4>궤적: 시행마다 고른 목표·제어와 측정 결과</h4>
{table(["시행","시각(UTC)","목표","제어","실행","UE1","UE2","UE3","UE2 마감","gnb2 dB","평가","p","칸 E·UE1·I2d·UE3·I2g","누적 최선 p","결정 ms","선택 근거"], trow, "traj")}
<h4>T: 목표 집합 ({len(bT)}개, T0 = 원래 요구)</h4>
{table(["목표","비용"] + reqs, [[e(t["targetId"]), f(t["cost"],1)] + [f(t.get(r),3) for r in reqs] for t in bT])}
<h4>C: 제어 후보 ({len(bC)}개, C0 대비 바뀐 축)</h4>
{table(["제어","바뀐 축","형성 근거"], crow)}
<h4>LLM 호출</h4>
{table(["#","역할","단계","모델","지연 ms","입력","출력","수용","재질문"], [[e(c["n"]), e(c["role"]), e(c["phase"]), e(c["model"]), f(c["latencyMs"],0), e(c["inputTokens"]), e(c["outputTokens"]), e(c["accepted"]), e(c["repairRetries"])] for c in bK])}
</details>''')

    page = f'''<title>v5 궤적 기록</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root{{--bg:#f6f7f9;--paper:#ffffff;--ink:#1b2430;--mute:#5b6675;--line:#dfe3e9;--acc:#1f5fbf;--m3a:#c2410c;--mim:#6d28d9;--mbm:#0f766e;--ok:#15803d;--no:#9aa3ae;}}
@media (prefers-color-scheme: dark){{:root:not([data-theme="light"]){{--bg:#0f141b;--paper:#161d27;--ink:#e3e8ef;--mute:#98a3b3;--line:#2a3341;--acc:#7aa7ff;--m3a:#fb8c4a;--mim:#b39cff;--mbm:#3fd1bd;--ok:#4ade80;--no:#6b7686;color-scheme:dark}}}}
:root[data-theme="dark"]{{--bg:#0f141b;--paper:#161d27;--ink:#e3e8ef;--mute:#98a3b3;--line:#2a3341;--acc:#7aa7ff;--m3a:#fb8c4a;--mim:#b39cff;--mbm:#3fd1bd;--ok:#4ade80;--no:#6b7686;color-scheme:dark}}
body{{background:var(--bg);color:var(--ink);font:15px/1.55 "IBM Plex Sans KR","Apple SD Gothic Neo","Noto Sans KR",sans-serif}}
.wrap{{max-width:1180px;margin:0 auto;padding-inline:16px;padding-block:28px 60px}}
h1{{font-size:26px;margin:0 0 4px;text-wrap:balance}} h2{{font-size:18px;margin:34px 0 10px}} h4{{font-size:13px;margin:18px 0 6px;color:var(--mute);letter-spacing:.02em}}
.lede{{color:var(--mute);max-width:75ch;margin:0 0 18px}}
code,.tw td{{font-family:"IBM Plex Mono",ui-monospace,monospace}}
.tw{{overflow-x:auto;border:1px solid var(--line);border-radius:6px;background:var(--paper)}}
table{{border-collapse:collapse;width:100%;font-size:12.5px;font-variant-numeric:tabular-nums}}
th{{text-align:left;font-weight:600;color:var(--mute);background:var(--bg);position:sticky;top:0;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}}
td{{padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}}
table.traj td:last-child,table td:nth-child(3):last-child{{font-family:"IBM Plex Sans KR",sans-serif;min-width:320px;max-width:520px}}
.m{{display:inline-flex;align-items:center;gap:6px;font-weight:600;white-space:nowrap}} .m::before{{content:"";width:10px;height:10px;border-radius:2px;background:var(--c)}}
.ok{{color:var(--ok);font-weight:600}} .no{{color:var(--no)}}
.chart{{background:var(--paper);border:1px solid var(--line);border-radius:6px;padding:10px}}
.chart svg{{width:100%;height:auto;display:block}}
.grid{{stroke:var(--line);stroke-width:1}} .tick{{fill:var(--mute);font:11px "IBM Plex Mono",monospace}} .axis{{fill:var(--mute);font:12px "IBM Plex Sans KR",sans-serif}}
.board{{fill:none;stroke-width:1.2;opacity:.28}} .median{{fill:none;stroke-width:3}} .lab{{font:600 12px "IBM Plex Mono",monospace}}
details.board{{background:var(--paper);border:1px solid var(--line);border-radius:6px;margin:10px 0;padding:0 14px}}
details.board>summary{{cursor:pointer;display:flex;flex-wrap:wrap;gap:6px 16px;align-items:baseline;padding:12px 0}}
details.board>summary:focus-visible{{outline:2px solid var(--acc);outline-offset:2px}}
.bid{{font-weight:600}} .res{{color:var(--mute);font-size:13px}}
.facts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:6px 18px;margin:0 0 6px}}
.facts div{{display:flex;gap:8px}} .facts dt{{color:var(--mute);min-width:74px;font-size:13px}} .facts dd{{margin:0;font-size:13px}}
.note{{font-size:13px;color:var(--mute);max-width:80ch}}
.filters{{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}} .filters button{{font:inherit;font-size:13px;padding:5px 10px;border:1px solid var(--line);background:var(--paper);color:var(--ink);border-radius:999px;cursor:pointer}}
.filters button[aria-pressed="true"]{{border-color:var(--acc);color:var(--acc)}}
@media (prefers-reduced-motion:reduce){{*{{transition:none!important}}}}
</style>
<div class="wrap">
<h1>v5 궤적 기록</h1>
<p class="lede">blocks18-v47 캠페인, 블록 16 이후 판 {len(boards)}개. 판마다 형성 단계가 만든 목표(T)와 제어 후보(C), 매 시행에서 궤적이 고른 목표·제어와 그 근거, 측정값, 공통 평가기 점수를 담았다. p 는 사전식 점수로 작을수록 좋고(E → UE1 → UE2 마감 → UE3, 칸 = ceil(20·양보율)), 1순위 E(gnb2 감쇠)를 전부 양보하면 18만을 넘는다.</p>
<h2>방식별 요약</h2>
{table(["방식","판","평가 달성","T0 달성","최선에서 E 지킴","p 중앙값","p 최소","판당 LLM 시간 중앙값","판당 입력 토큰 중앙값"], srows)}
<p class="note">블록 22 는 참조 처리량이 평소 절반으로 낮게 잡혀 세 판 모두 T0 를 달성했다(방식 간 변별력이 낮음). 블록 23~25 의 RECOVERY_FAILURE 는 대부분 ue1 을 gnb1 으로 조종한 시행의 되읽기 실패다.</p>
<h2>시행에 따른 누적 최선 p</h2>
<div class="chart">{chart(boards, trials)}</div>
<p class="note">가는 선은 판 하나, 굵은 선은 방식별 시행 번호마다의 중앙값이다. 원자료는 같은 폴더의 trials.csv(runningBestP 열).</p>
<h2>판별 상세</h2>
<div class="filters" role="group" aria-label="방식 필터">
<button type="button" id="f-all" aria-pressed="true" data-m="">전체</button>
{''.join(f'<button type="button" id="f-{m}" aria-pressed="false" data-m="{m}">{e(METHOD_LABEL[m])}</button>' for m in METHOD_ORDER)}
<button type="button" id="f-open" aria-pressed="false" data-open="1">모두 펼치기</button>
</div>
<div id="boards">{''.join(sections)}</div>
<h2>그래프용 원자료</h2>
<p class="note">같은 폴더에 CSV 다섯 개가 함께 게시돼 있다: <a href="boards.csv">boards.csv</a> · <a href="trials.csv">trials.csv</a> · <a href="targets.csv">targets.csv</a> · <a href="controls.csv">controls.csv</a> · <a href="calls.csv">calls.csv</a>. 저장소 경로는 <code>experiment_results/ota-20260911/reports/</code> 아래 같은 이름의 폴더.</p>
</div>
<script>
const data={json.dumps({b['attempt']: b['method'] for b in boards})};
const btns=[...document.querySelectorAll('.filters button[data-m]')];
btns.forEach(b=>b.addEventListener('click',()=>{{btns.forEach(x=>x.setAttribute('aria-pressed',x===b?'true':'false'));
document.querySelectorAll('details.board').forEach(d=>{{d.hidden=!!b.dataset.m && data[d.id.slice(1)]!==b.dataset.m}});}}));
document.getElementById('f-open').addEventListener('click',e=>{{const on=e.currentTarget.getAttribute('aria-pressed')!=='true';
e.currentTarget.setAttribute('aria-pressed',on);e.currentTarget.textContent=on?'모두 접기':'모두 펼치기';
document.querySelectorAll('details.board').forEach(d=>{{if(!d.hidden)d.open=on}});}});
</script>
'''
    with open(os.path.join(d, 'report.html'), 'w') as fh:
        fh.write(page)
    print(os.path.join(d, 'report.html'), len(page))


if __name__ == '__main__':
    main()
