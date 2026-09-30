#!/usr/bin/env python3
"""Check what a board actually sent to its models (owner rules of 2026-09-19/20).

  - no predictor output anywhere: predictions, predictorDescription, uncertaintyNote,
    capacityMbps, predictor-named evidence refs;
  - no code-made association table (controlKpiAssociations) in any input;
  - Control / internal formation are not asked for value estimates
    (predicted, uncertainty, predictedTarget) and are asked for relatedKpis;
  - selection inputs (trajectory / internal select) carry each candidate's functions
    and relatedKpis, never a value estimate;
  - the basic monolith gets no structure beyond catalog, state and observations.

Usage: python3 ops/check_model_inputs.py <board dir> [...]   -> exit 1 on any violation
"""
import glob
import json
import os
import re
import sys

FORBIDDEN_INPUT_KEYS = ("predictions", "predictorDescription", "uncertaintyNote",
                        "capacityMbps", "controlKpiAssociations", "predictor_target",
                        "predictorTarget")
ESTIMATE_KEYS = ("predicted", "uncertainty", "predictedTarget", "effectEstimate")


def _json_after(text, marker):
    i = text.find(marker)
    if i < 0:
        return None
    j = text.find("{", i)
    try:
        return json.JSONDecoder().raw_decode(text[j:])[0]
    except ValueError:
        return None


def _keys(node, found):
    if isinstance(node, dict):
        for k, v in node.items():
            found.add(k)
            _keys(v, found)
    elif isinstance(node, list):
        for v in node:
            _keys(v, found)
    return found


def check_request(path):
    text = open(path, errors="replace").read()
    role = re.sub(r"^\d+-|-request\.txt$", "", os.path.basename(path))
    problems = []
    inputs = _json_after(text, "INPUTS:") or _json_after(text, "{")
    schema = _json_after(text, "OUTPUT SCHEMA:")
    keys = _keys(inputs or {}, set())
    for bad in FORBIDDEN_INPUT_KEYS:
        if bad in keys:
            problems.append(f"input carries {bad}")
    if re.search(r"predictor|prediction", json.dumps(inputs or {}), re.I):
        problems.append("input mentions predictor/prediction")
    cands = (inputs or {}).get("input.control_candidates")
    if cands:
        est = _keys(cands, set()) & set(ESTIMATE_KEYS)
        if est:
            problems.append(f"selection input candidates carry {sorted(est)}")
        if not any("relatedKpis" in c for c in cands if isinstance(c, dict)):
            problems.append("selection input candidates carry no relatedKpis")
    if schema is not None:
        sk = _keys(schema, set())
        if role in ("control", "monolith") and "candidates" in sk:
            if sk & set(ESTIMATE_KEYS):
                problems.append(f"schema asks for {sorted(sk & set(ESTIMATE_KEYS))}")
            if "relatedKpis" not in sk:
                problems.append("schema does not ask for relatedKpis")
        if role == "monolith" and "instructions" in sk and "relatedKpis" in sk:
            problems.append("basic monolith schema asks for relatedKpis")
        if "levels" in schema or "ranking" in schema:
            problems.append("schema asks the model to restate levels/ranking")
    return role, problems


def main(dirs):
    bad = 0
    for board in dirs:
        reqs = sorted(glob.glob(os.path.join(board, "evidence", "*-prompts", "*-request.txt")))
        print(f"== {board}: {len(reqs)} requests")
        for path in reqs:
            role, problems = check_request(path)
            mark = "OK " if not problems else "BAD"
            print(f"  {mark} {os.path.basename(path)}" + (": " + "; ".join(problems) if problems else ""))
            bad += bool(problems)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
