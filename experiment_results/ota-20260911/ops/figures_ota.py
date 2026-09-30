#!/usr/bin/env python3
"""OTA 결과 그림 넷을 **자료에서 다시 계산해** 그린다.

수치를 박아 넣지 않는다 -- 2026-09-17 하루에 §1·§2·§3·§4·§4.5·§4.8 의 손으로 적은
수가 전부 낡아 다시 써야 했다.  그림은 판이 쌓일 때마다 다시 돌리면 된다.

**한 조건 안에서만 그린다** (`control_effects.CONDITION`).  같은 날 시나리오를
섞은 비교가 "깊이 1.6→5.5"·"CATALOG_EXHAUSTED 86%→4%" 같은 세 배 부풀린 수를
만들었다.  섞으면 그림도 똑같이 거짓말한다.

    python3 ops/figures_ota.py [출력디렉터리]
"""
import json, glob, os, sys, statistics, collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import control_effects as ce

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# 이 호스트의 한글 폰트.  없으면 라벨이 통째로 네모로 나온다.
matplotlib.rcParams["font.family"] = ["Noto Sans CJK JP", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False

PATTERN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "formal38guarded-*", "evidence", "AGENT-*-episode.json")
DEADLINE_MS = "2000"
THRESHOLD = ce.DEADLINE_THRESHOLD   # 문턱은 한 곳에서만 정한다


def _clean_episodes():
    for path in sorted(glob.glob(PATTERN)):
        doc = json.load(open(path))
        if (doc.get("condition") or {}).get("name") != ce.CONDITION:
            continue
        if ce.is_dirty(doc):
            continue
        yield doc


def _style(ax, title, xlabel, ylabel):
    ax.set_title(title, fontsize=11)
    ax.set_xlabel(xlabel, fontsize=9)
    ax.set_ylabel(ylabel, fontsize=9)
    ax.tick_params(labelsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def figure_deadline_threshold(out):
    """§2: ue2 굿풋과 마감 성공의 관계.  한쪽으로 거의 완벽한 규칙."""
    xs, ys = [], []
    for doc in _clean_episodes():
        for trial in doc.get("trials", []):
            kpis = trial.get("kpis") or {}
            good = kpis.get("dlGoodputMbps@ue2")
            ratio = kpis.get("deadlineSuccessRatio@ue2")
            value = (ratio or {}).get("byDeadlineMs", {}).get(DEADLINE_MS) \
                if isinstance(ratio, dict) else None
            if isinstance(good, (int, float)) and isinstance(value, (int, float)):
                xs.append(good); ys.append(value)
    fig, ax = plt.subplots(figsize=(6.2, 4.0))
    ok = [(x, y) for x, y in zip(xs, ys) if (x >= THRESHOLD) == (y >= 0.9)]
    bad = [(x, y) for x, y in zip(xs, ys) if (x >= THRESHOLD) != (y >= 0.9)]
    ax.scatter([p[0] for p in ok], [p[1] for p in ok], s=14, alpha=0.55,
               color="#3b6ea5", label="규칙과 일치 (%d)" % len(ok))
    ax.scatter([p[0] for p in bad], [p[1] for p in bad], s=42, marker="x",
               color="#c0392b", label="반례 (%d)" % len(bad))
    ax.axvline(THRESHOLD, color="black", lw=1.1, ls="--")
    ax.axhline(0.9, color="black", lw=0.7, ls=":")
    ax.annotate("%.2f Mbps" % THRESHOLD, (THRESHOLD, 1.04), fontsize=9,
                color="black", ha="center")
    _style(ax, "마감 인텐트의 문턱 (n=%d 시행, %s)" % (len(xs), ce.CONDITION),
           "ue2 하향 굿풋 (Mbps)", "마감 성공 비율 (2000 ms)")
    ax.legend(fontsize=8, frameon=False, loc="center left")
    ax.set_ylim(-0.05, 1.12)
    fig.tight_layout(); fig.savefig(out, dpi=150); plt.close(fig)
    return len(xs), len(bad)


def figure_target_ladder(out):
    """§4.5: 양보를 키울수록 달성 가능해진다."""
    rows = collections.defaultdict(lambda: [0, 0])
    for doc in _clean_episodes():
        for trial in doc.get("trials", []):
            for target, row in (trial.get("verdicts") or {}).items():
                if not isinstance(row, dict) or not row:
                    continue
                for verdict in row.values():
                    rows[target][1] += 1
                    rows[target][0] += (verdict == "PASS")
    order = sorted(rows, key=lambda t: rows[t][0] / max(rows[t][1], 1))
    rates = [100 * rows[t][0] / rows[t][1] for t in order]
    # 타깃이 27 개까지 늘면서 6.2 인치 안에서 라벨이 겹쳐 **읽을 수 없게** 됐다
    # ("n=846n=846n=846...", "100%100%").  폭을 개수에 비례시키고, n 은 x 축 라벨에
    # 합치고(T0 846), 퍼센트는 이웃과 겹칠 때만 건너뛴다 (2026-09-17).
    fig, ax = plt.subplots(figsize=(max(6.2, 0.42 * len(order) + 1.6), 4.2))
    bars = ax.bar(range(len(order)), rates, color="#5b8c5a", width=0.62)
    bars[0].set_color("#c0392b"); bars[-1].set_color("#2e6b2d")
    # **표본이 극단적으로 불균등하다** — T0~T6 은 618~846 인데 통과율이 높은 오른쪽
    # 타깃은 대부분 n=12 다.  같은 진하기로 그리면 "양보할수록 달성된다" 가 표본 12 짜리
    # 막대에 기대고 있다는 사실이 숨는다.  n < 50 은 흐리게 그려 구분한다 (2026-09-17).
    THIN = 50
    for bar, t in zip(bars, order):
        if rows[t][1] < THIN:
            bar.set_alpha(0.42)
            bar.set_hatch("//")
    last = None
    for i, (t, r) in enumerate(zip(order, rates)):
        if last is None or abs(r - last) >= 1.5 or i == len(order) - 1:
            ax.text(i, r + 1.2, "%.0f%%" % r, ha="center", fontsize=7.5, color="black")
            last = r
    ax.set_xticks(range(len(order)))
    ax.set_xticklabels(["%s\n%d" % (t, rows[t][1]) for t in order],
                       fontsize=7, rotation=0)
    ax.tick_params(axis="x", pad=2)
    _style(ax, "타깃 사다리 — 양보할수록 달성된다 (%s · 빗금 = 판정 %d 회 미만)"
           % (ce.CONDITION, THIN),
           "타깃 / 요구조건 판정 수 (왼쪽이 양보 없음)", "요구조건 통과율 (%)")
    ax.set_ylim(0, max(rates) + 10)
    fig.tight_layout(); fig.savefig(out, dpi=150); plt.close(fig)
    return order, rates


def figure_negotiation(out):
    """4.6: 한 번 크게 양보하고 그 자리에 머문다.

    달성 용이도와 '제안한 타깃' 은 `control_effects` 의 것을 **그대로** 쓴다.
    2026-09-17 에 이 계산을 다시 구현했다가 모든 시행이 40% 로 평평해졌다 --
    제안 타깃(`proposedTargetId`)이 아니라 판정된 타깃 전부를 평균했기 때문이다.
    **표와 그림이 어긋나면 그림이 틀린 것이다.**
    """
    ease = ce.target_ease(PATTERN)      # 자료에서 계산된다 (박힌 수가 아니다)
    by_index = collections.defaultdict(list)
    for doc in _clean_episodes():
        for index, trial in enumerate(
                [t for t in doc.get("trials", []) if t.get("proposedTargetId")]):
            value = ease.get(trial["proposedTargetId"])
            if value is not None:
                by_index[index].append(value)
    idx = [i for i in sorted(by_index) if len(by_index[i]) >= 5]
    means = [statistics.mean(by_index[i]) for i in idx]
    fig, ax = plt.subplots(figsize=(6.2, 4.0))
    ax.plot(idx, means, marker="o", color="#3b6ea5", lw=1.8)
    for i, m in zip(idx, means):
        ax.annotate("%.0f%%" % m, (i, m + 2.5), fontsize=8, ha="center", color="black")
        ax.annotate("n=%d" % len(by_index[i]), (i, m - 5.0), fontsize=6.5,
                    ha="center", color="#555555")
    _style(ax, "에이전트의 양보 — 한 번 크게, 그 뒤 평평 (%s)" % ce.CONDITION,
           "판 안의 시행 순서", "제안 타깃의 평균 달성 용이도 (%)")
    ax.set_ylim(0, max(means) + 14)
    fig.tight_layout(); fig.savefig(out, dpi=150); plt.close(fig)
    return list(zip(idx, ["%.1f" % m for m in means]))


def figure_control_inversion(out):
    """1: 한 인텐트에 최고인 제어가 전체 판정에서는 바닥이다.

    두 비율 모두 `control_effects.collect` 가 만든 행에서 나온다 -- 표를 찍는
    것과 **같은 자료, 같은 셈**이다.
    """
    rows, kept, skipped = ce.collect(PATTERN)
    labels = [l for l in rows if len(rows[l]) >= 8]
    def rate(label, key):
        v = rows[label]
        return 100 * sum(r[key] for r in v) / len(v)
    labels.sort(key=lambda l: -rate(l, "crossed"))
    a = [rate(l, "crossed") for l in labels]
    b = [rate(l, "attained") for l in labels]
    fig, ax = plt.subplots(figsize=(7.6, 4.3))
    x = range(len(labels))
    ax.bar([i - 0.2 for i in x], a, width=0.38, color="#3b6ea5",
           label="(a) 단일 KPI: ue2 굿풋 >= %.2f Mbps" % ce.DEADLINE_THRESHOLD)
    ax.bar([i + 0.2 for i in x], b, width=0.38, color="#c98b3a",
           label="(b) 타깃 전부 PASS")
    # 역전은 **양방향**이다.  p-q 만 보면 (a) 에서 좋고 (b) 에서 나쁜 제어만 표시되고,
    # 그 반대(cap@ue3=18: (a) 45% -> (b) 95%)가 빠진다 -- 그쪽이 오히려 "진짜 최선" 이므로
    # 논지의 절반을 잃는다.  두 방향을 화살표로 구분해 둘 다 찍는다 (2026-09-17).
    for i, (p, q) in enumerate(zip(a, b)):
        if p - q >= 25:
            ax.annotate("역전 ↓", (i, max(p, q) + 3.5), fontsize=9, ha="center", color="black")
        elif q - p >= 25:
            ax.annotate("역전 ↑", (i, max(p, q) + 3.5), fontsize=9, ha="center", color="black")
        ax.text(i, -6.5, "n=%d" % len(rows[labels[i]]), ha="center",
                fontsize=6.5, color="#555555")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=18, ha="right", fontsize=8)
    _style(ax, "제어의 효과 — 두 기준의 1위가 다르다 (깨끗한 판 %d개, %s)"
           % (kept, ce.CONDITION), "", "비율 (%)")
    ax.legend(fontsize=8, frameon=False, loc="upper right")
    ax.set_ylim(-10, max(a + b) + 14)
    fig.tight_layout(); fig.savefig(out, dpi=150); plt.close(fig)
    return [(l, "%.0f/%.0f" % (p, q)) for l, p, q in zip(labels, a, b)]


def main(outdir):
    os.makedirs(outdir, exist_ok=True)
    for name, fn in (("fig-deadline-threshold.png", figure_deadline_threshold),
                     ("fig-target-ladder.png", figure_target_ladder),
                     ("fig-negotiation.png", figure_negotiation),
                     ("fig-control-inversion.png", figure_control_inversion)):
        path = os.path.join(outdir, name)
        result = fn(path)
        print("%-30s %s" % (name, result))
    print("→", os.path.abspath(outdir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "figures-20260917"))
