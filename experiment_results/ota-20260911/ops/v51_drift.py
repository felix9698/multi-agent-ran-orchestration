#!/usr/bin/env python3
"""v5.1 bed-drift signal (owner 2026-09-27): the pilot fixed the scenario once, and each block's
reference re-measures the starting point -- but if the bed's *response* changes, the pilot's
difficulty no longer holds.  The uncontrolled trial 0 of every board is scored by the recorded
evaluator anyway; its A per block against the pilot's (0.40 at L10, option 3) says when to re-pilot.

usage: v51_drift.py [--campaign blocks-v51] [--pilot-a 0.40] [--tolerance 0.15]
"""
import argparse
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import export_v5_trajectories as x  # noqa: E402


def trial0_a(ep):
    pe = ep.get('paperEvaluation') or {}
    scale = pe.get('pScale')
    rows = pe.get('trials') or []
    if not scale or not rows or rows[0].get('p') is None:
        return None
    return 1 - rows[0]['p'] / scale


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--campaign', default='blocks-v51')
    ap.add_argument('--from-block', type=int, default=0)
    ap.add_argument('--pilot-a', type=float, default=0.40)
    ap.add_argument('--tolerance', type=float, default=0.15)
    a = ap.parse_args()
    per = {}
    for row, path in x.boards(a.campaign, a.from_block):
        v = trial0_a(json.load(open(path)))
        if v is not None:
            per.setdefault(int(row['block']), []).append(v)
    for block in sorted(per):
        med = statistics.median(per[block])
        flag = 'DRIFT -> re-pilot' if abs(med - a.pilot_a) > a.tolerance else 'ok'
        print(f'block {block:3d}  trial-0 A median {med:.3f}  (n={len(per[block])})  {flag}')
    if not per:
        print('no scored v5.1 boards yet')


if __name__ == '__main__':
    main()
