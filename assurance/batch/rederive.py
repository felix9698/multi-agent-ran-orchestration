"""Recreate paper-input artifacts from an immutable Batch raw bundle only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from assurance.batch.metrics import derive_batch_metrics
from assurance.batch.runner import BatchRunner, _FIGURE_DERIVATIONS


_OBSERVATION_KIND = "CanonicalTrialObservation"
_OBSERVATION_SCHEMA = "batch-canonical-trial/1"
_REQUIRED_FIELDS = frozenset({
    "recordId", "measurementKind", "interface", "scope", "mode", "outcome", "validity",
    "wallClockS", "inclusionRule", "included",
})


def rederive_raw_bundle(raw_bundle: Path | str, output_dir: Path | str) -> Path:
    """Write normalized, statistical, table, and figure-input artifacts from raw observations."""
    source = Path(raw_bundle)
    output = Path(output_dir)
    if not source.is_file():
        raise ValueError(f"raw bundle is not a file: {source}")
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory must be empty: {output}")
    rows = _canonical_rows(source)
    output.mkdir(parents=True, exist_ok=True)
    BatchRunner._write_normalized(output, rows)
    statistics = derive_batch_metrics(rows)
    summary = output / "summary"
    summary.mkdir(parents=True, exist_ok=True)
    (summary / "statistics.json").write_text(
        json.dumps(statistics, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8",
    )
    BatchRunner._write_latex_table(output, rows)
    included = [row for row in rows if row["included"]]
    mode = str(rows[0]["mode"]) if rows else "REPLAY"
    for figure_id, derivation in _FIGURE_DERIVATIONS.items():
        BatchRunner._write_figure_source_and_traceability(output, figure_id, included, derivation, mode)
    return output


def _canonical_rows(source: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    record_ids: set[str] = set()
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            observation = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"raw bundle line {line_number} is not JSON") from exc
        if not isinstance(observation, Mapping) or observation.get("eventKind") != _OBSERVATION_KIND:
            continue
        if observation.get("observationSchema") != _OBSERVATION_SCHEMA:
            raise ValueError(f"raw bundle line {line_number} has an unsupported observation schema")
        missing = _REQUIRED_FIELDS.difference(observation)
        if missing:
            raise ValueError(f"raw bundle line {line_number} lacks canonical fields: {sorted(missing)}")
        if observation["measurementKind"] != "MEASURED" or observation["interface"] != "E2":
            raise ValueError(f"raw bundle line {line_number} loses measured E2 provenance")
        scope = observation["scope"]
        derived_axes = observation.get("derivedAxes")
        if (not isinstance(scope, Mapping) or observation["mode"] not in {"REPLAY", "EMULATED"}
                or not isinstance(derived_axes, Mapping)
                or derived_axes.get("measurementKind") != "DERIVED"
                or derived_axes.get("interface") != "O1"
                or derived_axes.get("scope") != scope
                or derived_axes.get("mode") != observation["mode"]):
            raise ValueError(f"raw bundle line {line_number} loses derived O1 provenance")
        record_id = observation["recordId"]
        if not isinstance(record_id, str) or record_id in record_ids:
            raise ValueError(f"raw bundle line {line_number} has a duplicate or invalid recordId")
        record_ids.add(record_id)
        rows.append({
            key: value for key, value in observation.items()
            if key not in {"eventKind", "observationSchema", "derivedAxes"}
        })
    if not rows:
        raise ValueError("raw bundle contains no canonical Batch trial observations")
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-bundle", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    rederive_raw_bundle(args.raw_bundle, args.output_dir)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the module command
    raise SystemExit(main())
