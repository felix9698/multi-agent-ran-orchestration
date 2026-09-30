#!/usr/bin/env python3
"""v5.1 pilot (owner 2026-09-26): the fixed control list every load is measured with, split
into calibration boards of at most PER_BOARD trials (the scripted basic-monolith path tries a
board's list in order; trial 0 is the baseline).  Values are absolute, from the reference's A0.

usage: v51_pilot_list.py <reference.json> <out prefix>   -> <prefix>-1.json, -2.json, -3.json
"""
import json
import sys
from pathlib import Path

GNB2, GNB1 = '87654321', '12345678'
PER_BOARD = 7


def controls_gnb1(a0):
    """Option 3 (owner 2026-09-27): energy on gnb1, where ue2 and ue3 share the cell.  L10 board 747:
    the victim of gnb1 attenuation is UE2's deadline success (0.75 -> 0.59 at +2 dB, 0 beyond) and a
    ue3-favouring PF zeroes it, so board 1 pairs attenuation +2/+4/+6 with a ue2-favouring PF 4
    (GPT's 4:1); board 2 tries holding ue3 back with a PRB cap and keeps one ue3-PF point.  +0 with
    1:1 is each board's baseline trial."""
    att = lambda d: {f'txAttenuationDb@{GNB1}': f'{a0 + d:.1f}'}
    pf2, pf3, cap3 = {'pfWeight@ue2': '4.0'}, {'pfWeight@ue3': '4.0'}, {'dlPrbCap@ue3': '12'}
    return [[att(2), att(4), att(6), dict(pf2), {**att(2), **pf2}, {**att(4), **pf2}, {**att(6), **pf2}],
            [{**att(2), **cap3}, {**att(4), **cap3}, {**att(2), **pf2, **cap3}, {**att(4), **pf2, **cap3},
             {**att(2), **pf3}],
            # A board with an empty list still ran its full horizon (L10 part 3: 12 min for nothing);
            # repeat the configurations that decided L10 instead -- the pilot's repetition.
            [att(2), dict(pf2), {**att(2), **pf2}, {**att(4), **pf2}, {**att(6), **pf2}, {**att(2), **cap3}]]


def controls(a0):
    att = lambda d: {f'txAttenuationDb@{GNB2}': f'{a0 + d:.1f}'}
    boards = [
        # attenuation alone, then with a UE2 / UE3 scheduling weight on the shared gnb1
        [att(3), att(6), att(9), att(12), att(15),
         {**att(9), 'pfWeight@ue2': '4.0'}, {**att(9), 'pfWeight@ue3': '4.0'}],
        [{**att(15), 'pfWeight@ue2': '4.0'}, {**att(15), 'pfWeight@ue3': '4.0'},
         {**att(9), 'dlPrbCap@ue3': '12'}, {**att(15), 'dlPrbCap@ue3': '12'},
         {**att(15), 'dlPrbCap@ue2': '12'}],
        # UE3 moved to gnb2 last: its hand-over and return are the slow, risky part
        [{f'servingCell@ue3': GNB2}, {f'servingCell@ue3': GNB2, **att(9), 'pfWeight@ue2': '4.0'},
         {f'servingCell@ue3': GNB2, **att(15), 'pfWeight@ue2': '4.0'}],
    ]
    assert all(len(b) <= PER_BOARD for b in boards)
    # 2026-09-26 23:3x: UE3 moves left out until its hand-over/return is fixed (gain patch +
    # soak); an empty list ends that board at its baseline. Delete the flag to put them back.
    if (Path(__file__).resolve().parent / 'overnight' / 'V51_PILOT_SKIP_UE3_MOVE').exists():
        boards[2] = []
    return boards


if __name__ == '__main__':
    ref = json.load(open(sys.argv[1]))
    import os
    build = controls_gnb1 if os.environ.get('AIC_V51_ENERGY_CELL', '').strip() == 'gnb1' else controls
    for n, board in enumerate(build(float(ref['energyBaselineDb'])), 1):
        json.dump(board, open(f'{sys.argv[2]}-{n}.json', 'w'), indent=1)
        print(f'{sys.argv[2]}-{n}.json', len(board))
