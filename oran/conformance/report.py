"""Suite manifests and the requirement-to-scenario execution matrix."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .contracts import ContractBundle
from .runner import ScenarioResult


def suite_manifest(results: Iterable[ScenarioResult], path: str | Path) -> dict[str, Any]:
    rows = [{"scenarioId": result.scenario_id, "disposition": result.disposition, "reason": result.reason,
             "evidence": result.evidence_paths} for result in results]
    manifest = {"scenarios": rows, "summary": {name: sum(row["disposition"] == name for row in rows)
               for name in ("PASS", "FAIL", "SKIPPED_NOT_APPLICABLE")}}
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def traceability_report(bundle: ContractBundle, results: Iterable[ScenarioResult], path: str | Path) -> dict[str, Any]:
    target = Path(path)
    by_scenario: dict[str, dict[str, Any]] = {}
    if target.is_file():
        try:
            previous = json.loads(target.read_text(encoding="utf-8"))
            for requirement in previous.get("matrix", []):
                for row in requirement.get("scenarios", []):
                    if row.get("disposition") != "NOT_RUN":
                        by_scenario[row["scenarioId"]] = {"disposition": row["disposition"], "reason": row.get("reason")}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, KeyError):
            by_scenario = {}
    for result in results:
        by_scenario[result.scenario_id] = {"disposition": result.disposition, "reason": result.reason}
    matrix = []
    for requirement in bundle.catalog["requirements"]:
        linked = []
        for scenario_id in requirement["scenarioIds"]:
            result = by_scenario.get(scenario_id)
            linked.append({"scenarioId": scenario_id, "disposition": result["disposition"] if result else "NOT_RUN",
                           "reason": result["reason"] if result else None})
        matrix.append({"requirementId": requirement["id"], "scenarios": linked})
    report = {"requirementCount": len(matrix), "scenarioCount": len(bundle.catalog["scenarios"]), "matrix": matrix}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report
