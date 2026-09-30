#!/usr/bin/env python3
"""Measure model/role generation budgets without starting a RAN session.

Old episode fixtures are upgraded in memory for representative v2 calls; this
conversion is calibration data preparation, never operational authorization.
"""
from __future__ import annotations

import argparse
import copy
import itertools
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from decision.llm_backend import LLMBackendManager, MockAgentBackend

ROLES = ("target", "control", "trajectory", "monolith-form", "monolith-select", "basic-monolith")


def role_prompts():
    """Use the six authoritative instructions verbatim, independent of V1 landing."""
    text = (ROOT / "orc_task/SINGLE_CALL.md").read_text(encoding="utf-8")
    chunks = text.split("### ")[1:7]
    return {role: chunk.split("\n", 1)[1].split("\n## ", 1)[0].strip()
            for role, chunk in zip(ROLES, chunks)}


def representative_inputs(fixture):
    episode = copy.deepcopy(fixture)
    intents = episode["intents"]
    authorization = {}
    max_priority = max((item.get("priority", 1) for item in intents), default=1)
    for intent in intents:
        req = intent["requirement"]
        req.setdefault("steps", 2 if req.get("relaxable") else 0)
        req.setdefault("bound", req.get("relaxLimit", req["value"]) if req["steps"] else req["value"])
        entry = {"original": req["value"], "bound": req["bound"], "steps": req["steps"],
                 "op": req["op"], "unit": req["unit"], "owner": intent["owner"],
                 "weight": intent.get("weight", 1 + max_priority - intent.get("priority", 1))}
        entry["levels"] = [entry["original"] + (entry["bound"] - entry["original"]) * q / entry["steps"]
                           for q in range(entry["steps"] + 1)] if entry["steps"] else [entry["original"]]
        authorization[req["reqId"]] = entry
    targets = []
    for qs in itertools.product(*(range(entry["steps"] + 1) for entry in authorization.values())):
        targets.append({"requirements": {key: entry["levels"][q] for (key, entry), q in zip(authorization.items(), qs)},
                        "levels": dict(zip(authorization, qs)),
                        "cost": sum(entry["weight"] * q * q for entry, q in zip(authorization.values(), qs))})
    targets.sort(key=lambda item: item["cost"])
    for i, target in enumerate(targets):
        target["targetId"] = f"T{i}"
    # The probe must state the rule the selection path applies, not the weighted
    # square it stopped applying two contract versions ago: this dict is sent to
    # the model verbatim, and a latency probe that misdescribes the ranking
    # measures a different question than the episode it is calibrating for.
    preference = {"costRule": "normalized-concession",
                  "tieBreak": "lexicographic(D_max, D_mean) then intent order"}
    authorization["preference"] = preference
    authorization["jointConditions"] = episode.get("T", {}).get("jointConditions", [])
    contract = {"t0": targets[0], "alternatives": targets[1:], "authorization": authorization, "preference": preference}
    catalog = []
    baseline = {}
    # Reuse the exposed fixture axes; translate each to the contract's function shape.
    axes = episode.get("C", {}).get("actionSpace", {})
    fields = {"pfWeight": "pfWeight", "dlPrbCap": "maxDlPrbs", "servingCell": "servingCell"}
    for axis, values in axes.items():
        kind, ue = axis.split("@", 1)
        field = fields[kind]
        converted = [str(value) if kind == "servingCell" else int(value) for value in values]
        baseline[axis] = converted[0]
        catalog.append({"functionId": axis, "xapp": "calibration", "actionId": kind,
                        "scopes": [f"ue@{ue}"], "axis": f"{kind}@<ue>", "prerequisites": [],
                        "policyFields": {field: {"values": converted, "baseline": converted[0], "unit": "nci" if kind == "servingCell" else "weight" if kind == "pfWeight" else "PRB"}}})
    source = {"intents": intents, "authorization": authorization, "function_catalog": catalog,
              "compatibility": {}, "network_state": {"baselineConfiguration": baseline, "configuration": baseline,
              "unselectedFunctionRule": "baseline"}, "effect_evidence": {"predictions": [], "observations": [],
              "predictorDescription": "Calibration fixture; no measured effect estimates."}}
    control = {key: value for key, value in source.items() if key not in ("intents", "authorization")}
    control.update(target_contract=contract, construction_policy={"retain": 8})
    candidates = MockAgentBackend()._controls(control)["candidates"]
    trajectory = {"target_contract": contract, "control_candidates": candidates,
                  "network_state": source["network_state"], "observations": [], "observed_best": None, "kpi_gaps": None}
    return {"target": {"intents": intents, "authorization": authorization}, "control": control,
            "trajectory": trajectory, "monolith-select": trajectory,
            "monolith-form": {**source, "construction_policy": {"retain": 8}},
            "basic-monolith": {**source, "observations": []}}


def output_schema(role):
    # ``alternatives`` is the selection itself, and both prepared roles carry
    # it: a missing field is a malformed answer, not a licence to keep the
    # whole authorized domain.
    target = {"t0": {"type": "object"}, "levels": {"type": "object"}, "constraints": {"type": "array"},
              "alternatives": {"type": "array"},
              "ranking": {"type": "object"}, "missingInformation": {"type": "array"}, "rationale": {"type": "string"}}
    if role == "target":
        properties, required = target, ["t0", "levels", "alternatives", "constraints", "ranking", "rationale"]
    elif role == "monolith-form":
        properties, required = ({**target, "candidates": {"type": "array"}},
                                ["t0", "levels", "alternatives", "candidates", "rationale"])
    elif role == "control":
        properties, required = {"candidates": {"type": "array"}, "rationale": {"type": "string"}}, ["candidates", "rationale"]
    elif role == "basic-monolith":
        # Required policy content and a short rationale.  The requirement
        # restatement is gone from ``_BASIC_SCHEMA``: no executor read used it,
        # and the arm is judged against T0 whatever it says it aimed at.
        properties, required = {"instructions": {"type": "array"}, "rationale": {"type": "string"}}, ["instructions", "rationale"]
    else:
        properties = {key: {"type": "string"} for key in ("controlId", "targetId", "rationale")}
        required = list(properties)
    return {"type": "object", "properties": properties, "required": required}


def accepted_response(data, role):
    """Schema-shape acceptance for calibration, not executor admission."""
    if not isinstance(data, dict):
        return False
    schema = output_schema(role)
    if not all(key in data for key in schema["required"]):
        return False
    types = {"object": dict, "array": list, "string": str}
    return all(isinstance(data[key], types[value["type"]]) for key, value in schema["properties"].items() if key in data)


def percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def calibrate(models, budgets, repeat, runs_root, roles=ROLES, fixtures=None, resolver=None):
    if repeat < 1:
        raise ValueError("repeat must be positive")
    paths = sorted(Path(fixtures or ROOT / "tests/fixtures/agent_episodes").glob("*.json"))
    inputs = [representative_inputs(json.loads(path.read_text())) for path in paths]
    if not inputs:
        raise ValueError("no episode fixtures found")
    manager = None
    if resolver is None:
        def resolver(name):
            nonlocal manager
            if name == "mock:agent":
                return MockAgentBackend()
            if manager is None:
                manager = LLMBackendManager()
            return manager.resolve_object(name)
    prompts = role_prompts()
    table = {}
    for model in models:
        backend = resolver(model)
        if backend is None:
            raise ValueError(f"unknown backend: {model}")
        table[model] = {}
        for role in roles:
            table[model][role] = {}
            for budget in budgets:
                options = {"maxTokens": 4096 if role in ("target", "control", "monolith-form") else 1500, "jsonMode": True}
                if str(budget).isdigit():
                    options["thinkingBudgetTokens"] = int(budget)
                else:
                    options["reasoningEffort"] = str(budget)
                latencies, accepted = [], 0
                for index in range(repeat):
                    prompt = "INPUTS:\n" + json.dumps(inputs[index % len(inputs)][role]) + "\nOUTPUT SCHEMA:\n" + json.dumps(output_schema(role)) + "\nReturn only the JSON object."
                    started = time.perf_counter()
                    response = backend.generate(prompt, prompts[role], options=options)
                    latencies.append((time.perf_counter() - started) * 1000)
                    data = response.parsed_json
                    if data is None:
                        try:
                            data = json.loads(response.content)
                        except (TypeError, ValueError):
                            pass
                    accepted += bool(response.success and accepted_response(data, role))
                table[model][role][str(budget)] = {"p50": percentile(latencies, .5), "p95": percentile(latencies, .95),
                                                   "n": repeat, "accepted": accepted / repeat}
    destination = Path(runs_root) / "agent-latency-calibration.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(table, indent=2) + "\n")
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", required=True, help="Comma-separated backend names")
    parser.add_argument("--budgets", default="1000,4000", help="Comma-separated thinking token budgets or reasoning efforts")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--runs-root", required=True)
    parser.add_argument("--roles", default=",".join(ROLES))
    parser.add_argument("--fixtures", type=Path)
    args = parser.parse_args(argv)
    print(calibrate(args.models.split(","), args.budgets.split(","), args.repeat, args.runs_root,
                    roles=args.roles.split(","), fixtures=args.fixtures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
