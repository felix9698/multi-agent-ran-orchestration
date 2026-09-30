#!/usr/bin/env python3
"""Plot an Agent sitting: per-UE throughput over time, with the board it filled.

The picture the owner asked for -- "watch the agents work".  ``x`` is wall-clock
time over one Agent sitting; ``y`` is each UE's delivered downlink throughput
(``DRB.UEThpDl``, one line per UE) read straight from the KPM JSONL the gate
writes.  Over it, one shaded band per trial, each labelled with the **board
cell** that trial recorded: the whole-intent state beside the whole-action
state, e.g. ``(I1', I2, I3) <-> (A1, A2', A3)``.

So a reader sees the Agent try one action combination after another (the
columns), the per-UE throughput respond (the lines), and each trial land as a
row-column cell of the board -- and, when the search reaches ``FOUND``, every
intent held with the throughput settled.

Offline reader.  It takes an ``AGENT-*-run.json`` (schema
``liveconsole-agent-run/1.0.0``) and a KPM JSONL path; it opens nothing else.
Every figure is written beside its CSV.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import matplotlib
matplotlib.use("Agg")
from matplotlib import pyplot as plt

AGENT_RUN_SCHEMA = "liveconsole-agent-run/1.0.0"
#: The per-UE metrics a sitting's throughput story is told with.  The first is
#: the y-axis; the rest are drawn on a secondary axis when asked for.
UE_THROUGHPUT = "DRB.UEThpDl"
UE_CONFIG = ("RAN.UE.DlPrbCap", "RAN.UE.PfWeight", "RRU.PrbTotDl")


class PlotError(ValueError):
    """The supplied record cannot safely be attributed or plotted."""


@dataclass(frozen=True)
class Trial:
    index: int
    opened_at: datetime
    settled_at: Optional[datetime]
    label: str
    terminal_state: str
    outcome: str
    candidate_id: str


@dataclass(frozen=True)
class UeSample:
    at: datetime
    ue: str
    thp_dl_mbps: float
    config: Mapping[str, float]


def _instant(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def load_run(path: Path) -> Mapping[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schemaVersion") != AGENT_RUN_SCHEMA:
        raise PlotError(
            f"{path.name} is {document.get('schemaVersion')!r}, not {AGENT_RUN_SCHEMA!r}")
    return document


def _named_ues(document: Mapping[str, Any]) -> Tuple[str, ...]:
    """The UEs this sitting named, and no others -- never attributed by recency."""
    observed = (document.get("preflight") or {}).get("observedUes") or {}
    return tuple(str(entry.get("amfUeNgapId")) for entry in observed.values())


def _epochs(document: Mapping[str, Any]) -> Tuple[int, ...]:
    binding = ((document.get("preflight") or {}).get("deployment") or {})
    epochs = binding.get("expectedEpochs") or {}
    return tuple(int(v) for v in epochs.values()) if isinstance(epochs, Mapping) else ()


def trials_of(document: Mapping[str, Any]) -> List[Trial]:
    """Each trial's window and its board-cell label, from the run document.

    The board pairs are ordered as the trials ran, so the label of trial *k* is
    the *k*-th board pair rendered in the run document's intent/action order.
    """
    board = document.get("board") or {}
    intent_order = list(board.get("intentOrder") or ())
    action_order = list(board.get("actionOrder") or ())
    pairs = list(board.get("pairs") or [])
    events = document.get("_events") or {}
    trials: List[Trial] = []
    for position, record in enumerate(document.get("trials") or []):
        trial_id = str(record.get("trialId"))
        index = int(trial_id.rsplit(":", 1)[-1]) if ":" in trial_id else position + 1
        pair = pairs[position] if position < len(pairs) else {}
        label = _board_label(pair, intent_order, action_order)
        opened = events.get(trial_id) or _instant(record.get("recordedAt"))
        trials.append(Trial(
            index=index,
            opened_at=opened,
            settled_at=None,
            label=label,
            terminal_state=str(record.get("terminalState", "")),
            outcome=str(record.get("outcome", "")),
            candidate_id=str(record.get("candidateId", pair.get("candidateId", ""))),
        ))
    # A trial ends where the next opens; the last ends at the sitting's edge.
    ordered = sorted((t for t in trials if t.opened_at is not None),
                     key=lambda t: t.opened_at)
    filled: List[Trial] = []
    for position, trial in enumerate(ordered):
        settled = (ordered[position + 1].opened_at
                   if position + 1 < len(ordered) else None)
        filled.append(Trial(trial.index, trial.opened_at, settled, trial.label,
                            trial.terminal_state, trial.outcome, trial.candidate_id))
    return filled


def _prime(name: str, primed: bool) -> str:
    return f"{name}'" if primed else name


def _board_label(pair: Mapping[str, Any], intent_order: Sequence[str],
                 action_order: Sequence[str]) -> str:
    intents = pair.get("intentStates") or {}
    actions = pair.get("actionStates") or {}
    row = []
    for name in intent_order:
        state = intents.get(name)
        row.append(f"{name}?" if state not in ("PRIORITIZED", "DEFERRED")
                   else _prime(name, state == "DEFERRED"))
    col = [_prime(name, actions.get(name) == "DEFERRED") for name in action_order]
    return "(" + ", ".join(row) + ") <-> (" + ", ".join(col) + ")"


def read_events_opened(events_path: Path) -> Mapping[str, datetime]:
    """Trial-open instants from the Kernel event stream, for the exact window."""
    opened: Dict[str, datetime] = {}
    if not events_path.is_file():
        return opened
    for line in events_path.read_text(encoding="utf-8").splitlines():
        try:
            envelope = json.loads(line)
        except (TypeError, ValueError):
            continue
        if envelope.get("eventKind") != "TrialOpened":
            continue
        trial_id = str((envelope.get("payload") or {}).get("trialId"))
        at = _instant(envelope.get("timestamp"))
        if trial_id and at is not None:
            opened[trial_id] = at
    return opened


def read_kpm(path: Path, *, ues: Sequence[str], epochs: Sequence[int],
             window: Tuple[datetime, datetime]) -> List[UeSample]:
    """Per-UE throughput and config samples in the window, for the named UEs only.

    A UE the sitting did not name is never plotted, and an indication whose
    connection epoch is not one the sitting pinned is dropped -- the same
    fail-closed attribution the runtime uses.
    """
    wanted = {str(item) for item in ues}
    epoch_set = {int(item) for item in epochs}
    start, end = window
    samples: List[UeSample] = []
    if not path.is_file():
        raise PlotError(f"KPM JSONL not found: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            continue
        if record.get("event") != "kpm_indication":
            continue
        if epoch_set and int(record.get("connection_epoch", -1)) not in epoch_set:
            continue
        received = record.get("recv_unix_us")
        if not isinstance(received, int):
            continue
        at = datetime.fromtimestamp(received / 1_000_000, tz=timezone.utc)
        if at < start or at > end:
            continue
        for ue in record.get("ues", ()):
            identifier = str(ue.get("amf_ue_ngap_id"))
            if identifier not in wanted:
                continue
            values: Dict[str, float] = {}
            for measurement in ue.get("measurements", ()):
                name, value = measurement.get("name"), measurement.get("value")
                if isinstance(value, (int, float)):
                    values[str(name)] = float(value)
            thp = values.get(UE_THROUGHPUT)
            samples.append(UeSample(
                at=at, ue=identifier,
                thp_dl_mbps=(thp / 1000.0 if thp is not None else 0.0),
                config={k: values[k] for k in UE_CONFIG if k in values}))
    return samples


def read_throughput_csv(path: Path, *, ues: Sequence[str],
                        window: Tuple[datetime, datetime]) -> List[UeSample]:
    """Per-UE DL throughput from a tun-rx_bytes sampler CSV.

    The KPM ``DRB.UEThpDl`` counter reads 0 on this deployment even while real
    downlink data flows, so ``scripts/hardware/sample_ue_throughput.sh`` reads
    the UE's own ``oaitun_ue1`` byte counter and differences it. This loads
    that CSV (``unix_ms,amf_ue_ngap_id,dl_mbps,ul_mbps``) as the honest y
    source; a UE the run did not name is still never attributed.
    """
    wanted = {str(item) for item in ues}
    start, end = window
    samples: List[UeSample] = []
    if not path.is_file():
        raise PlotError(f"throughput CSV not found: {path}")
    for row in csv.DictReader(open(path, encoding="utf-8")):
        ue = str(row.get("amf_ue_ngap_id"))
        if ue not in wanted:
            continue
        try:
            at = datetime.fromtimestamp(int(row["unix_ms"]) / 1000.0, tz=timezone.utc)
            dl = float(row.get("dl_mbps", 0.0))
        except (KeyError, ValueError, TypeError):
            continue
        if at < start or at > end:
            continue
        samples.append(UeSample(at=at, ue=ue, thp_dl_mbps=dl, config={}))
    return samples


def _relative(at: datetime, anchor: datetime) -> float:
    return (at - anchor).total_seconds()


def render(document: Mapping[str, Any], kpm_path: Optional[Path], events_path: Path,
           out_dir: Path, *, throughput_csv: Optional[Path] = None,
           margin_s: float = 5.0) -> Dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    ues = _named_ues(document)
    if not ues:
        raise PlotError("the run names no observed UE to attribute samples to")
    opened = read_events_opened(events_path)
    doc = dict(document)
    doc["_events"] = opened
    trials = trials_of(doc)
    if not trials:
        raise PlotError("the run records no trial to plot")
    anchor = trials[0].opened_at
    last = max((t.settled_at or t.opened_at for t in trials))
    window = (anchor.fromtimestamp(anchor.timestamp() - margin_s, tz=timezone.utc),
              last.fromtimestamp(last.timestamp() + margin_s + 30.0, tz=timezone.utc))
    if throughput_csv is not None:
        samples = read_throughput_csv(throughput_csv, ues=ues, window=window)
        y_source = "UE tun rx_bytes (DRB.UEThpDl reads 0 on this deployment)"
    else:
        samples = read_kpm(kpm_path, ues=ues, epochs=_epochs(document), window=window)
        y_source = "KPM DRB.UEThpDl"

    by_ue: Dict[str, List[UeSample]] = defaultdict(list)
    for sample in samples:
        by_ue[sample.ue].append(sample)

    stem = Path(document.get("caseId", "agent")).name.replace(":", "_")
    prefix = out_dir / f"AGENT-{stem}"
    fig, ax = plt.subplots(figsize=(12, 6))
    colours = plt.cm.tab10.colors
    for index, ue in enumerate(ues):
        rows = sorted(by_ue.get(ue, ()), key=lambda s: s.at)
        xs = [_relative(s.at, anchor) for s in rows]
        ys = [s.thp_dl_mbps for s in rows]
        ax.plot(xs, ys, marker=".", linewidth=1.4, color=colours[index % len(colours)],
                label=f"UE {ue} DL throughput")

    # One band per trial, labelled with the board cell it recorded.
    band_colours = ("#eef4ff", "#fff6ee")
    for position, trial in enumerate(trials):
        x0 = _relative(trial.opened_at, anchor)
        x1 = (_relative(trial.settled_at, anchor) if trial.settled_at is not None
              else x0 + 30.0)
        ax.axvspan(x0, x1, color=band_colours[position % 2], zorder=0)
        ax.axvline(x0, color="black", linewidth=0.8, zorder=1)
        top = ax.get_ylim()[1]
        ax.text(x0 + 0.3, top, f"trial {trial.index}\n{trial.label}\n"
                f"{trial.terminal_state}/{trial.outcome}",
                fontsize=7, va="top", ha="left", color="black",
                zorder=3, family="monospace")

    termination = (document.get("summary") or {}).get("termination")
    ax.set_xlabel("time since the first trial opened (s)")
    ax.set_ylabel(f"delivered DL throughput (Mbps)\n[{y_source}]")
    ax.set_title(f"Agent board search over the air -- {document.get('caseId','')}\n"
                 f"termination: {termination}", fontsize=10)
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(True, linewidth=0.3, alpha=0.5)
    fig.tight_layout()
    figure_path = f"{prefix}-throughput.png"
    fig.savefig(figure_path, dpi=140)
    plt.close(fig)

    csv_path = f"{prefix}-throughput.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["relative_seconds", "ue", "dl_throughput_mbps",
                         "trial", "board_cell"])
        for sample in sorted(samples, key=lambda s: (s.at, s.ue)):
            rel = _relative(sample.at, anchor)
            trial = next((t for t in trials
                          if t.opened_at <= sample.at
                          and (t.settled_at is None or sample.at < t.settled_at)), None)
            writer.writerow([f"{rel:.3f}", sample.ue, f"{sample.thp_dl_mbps:.4f}",
                             trial.index if trial else "",
                             trial.label if trial else ""])

    board_path = f"{prefix}-board.csv"
    with open(board_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["trial", "candidate", "board_cell", "terminal", "outcome",
                         "opened_relative_s"])
        for trial in trials:
            writer.writerow([trial.index, trial.candidate_id, trial.label,
                             trial.terminal_state, trial.outcome,
                             f"{_relative(trial.opened_at, anchor):.3f}"])
    return {"figure": figure_path, "throughputCsv": csv_path, "boardCsv": board_path}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="AGENT-*-run.json")
    parser.add_argument("--kpm-jsonl", default=None,
                        help="KPM JSONL (defaults to the binding's path in the run)")
    parser.add_argument("--events", default=None,
                        help="AGENT-*-events.jsonl (defaults beside the run)")
    parser.add_argument("--throughput-csv", default=None,
                        help="sample_ue_throughput.sh CSV (tun rx_bytes). Use "
                             "this as the y source: the KPM DRB.UEThpDl counter "
                             "reads 0 on this deployment even under real load.")
    parser.add_argument("--out", required=True, help="output directory")
    args = parser.parse_args(argv)

    run_path = Path(args.run)
    document = load_run(run_path)
    events_path = Path(args.events) if args.events else Path(
        str(run_path).replace("-run.json", "-events.jsonl"))
    throughput_csv = Path(args.throughput_csv) if args.throughput_csv else None
    kpm_path: Optional[Path] = None
    if throughput_csv is None:
        if args.kpm_jsonl:
            kpm_path = Path(args.kpm_jsonl)
        else:
            binding = ((document.get("preflight") or {}).get("deployment") or {})
            kpm_path = Path(binding.get("kpmJsonlPath", "") or "")
            if not str(kpm_path):
                parser.error("pass --throughput-csv (preferred) or --kpm-jsonl")
    written = render(document, kpm_path, events_path, Path(args.out),
                     throughput_csv=throughput_csv)
    for key, value in written.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
