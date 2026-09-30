#!/usr/bin/env python3
"""The no-radio construction check of the integrated reply, section 5.

    "Perform a no-radio construction check on one frozen corrected input
     snapshot. Run Target/Control/first selection, internal formation/first
     selection, and basic first proposal. Capture actual full requests,
     schemas, outputs, validated membership and per-stage costs. Use the
     common candidate budgets and compact encoding. Do not force identical
     generated subsets."

Six stages over ONE frozen snapshot -- the pinned pilot the guarded runner
loads, read through ``atomic_formal_run_guarded.pilot_intents`` so the hash
pins and the host-join remapping are the runner's, not a second reader's:

    three-agent        Target formation, Control formation, first selection
    internal monolith  formation (T and C in one call), first selection
    basic monolith     first proposal

Nothing here is reimplemented.  Each stage is the code path the live runner
uses: ``build_hardware_free_agent_sitting`` performs the two formations while
composing the sitting, and the first decision is ``AgentSitting._select`` /
``_decide_basic`` -- the same seams ``AgentSitting.run`` calls, called once
instead of in a loop, because this check ends at the first proposal.

No radio, no socket, no lab file: the deployment is the seeded emulator of
``tools.hfconsole.agent_env``.  The three arms are handed identical budgets
(axes, ladders, catalog ceiling, retain, deadlines) and the compact encoding
``_user_prompt`` already imposes.  Their generated subsets are NOT aligned --
differing T and C between arms is a result of this check, not a defect.

WHY THE DEFAULT MODEL IS STILL THE MOCK
---------------------------------------
``expand_targets`` shapes the domain only through
``Authorization.mode_constraints``, and an authorization carrying none expands
to the unshaped product.  A real call answered against *that* domain would be
answering a question the owner never asked, so ``guard`` refuses any model but
the mock while the loaded authorization states no mode constraints, and the
default backend stays the mock.

The guard is unchanged.  What changed is the input: the snapshot's second half,
``answers.json``, states the three per-owner ``(g,d)`` mode sets and the
cross-owner deadline quota, and this check now reads it through the same path
the guarded runner's child takes -- ``main._parse_answers`` ->
``intake.merge_answers`` -> ``expand_targets``.  So the constraints are present,
the domain is the authorized one, and the guard opens by itself.  It was never
disabled: pass an unshaped snapshot and it refuses again.

    python3 noradio_construction_check.py                  # hermetic, the mock
    python3 noradio_construction_check.py --model claude-sonnet   # the real run

``claude-sonnet`` is a request *label*, not a model id: the proxy in front of
this lab advertises obfuscated ids, so ``MODEL_IDS`` maps the label to whatever
id is actually served and ``AIC_CLAUDE_MODEL_ID`` re-points it.  Passing a raw
served id instead is what raises ``LookupError: no LLM backend resolves the
model name``.  The real run needs ``ANTHROPIC_BASE_URL`` and
``ANTHROPIC_AUTH_TOKEN`` in the environment; neither is ever written here.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile

DIRECTORY = Path(__file__).resolve().parent
REPO = DIRECTORY.parents[1]
# v3.1 artefacts get their own stem so the v3 record is never overwritten.
STEM = "V31-NORADIO-CONSTRUCTION-20260915"
MOCK = "mock:agent"


def stem_for(result):
    """Artefact stem, marked with the profile so runs cannot be confused.

    The default-rule run keeps the bare stem it already wrote under; a profile
    run gets its own, because the two are meant to be diffed against each other
    and a shared name would mean the second silently replaced the first.
    """
    profile = str(result.get("preference") or "").strip().upper()
    return f"{STEM}-{profile}" if profile in ("P1", "P2", "P3") else STEM

#: The starting placement the snapshot's own domain block states: UE1/UE3 on
#: gNB1, UE2 on gNB2.  A starting placement only -- both cells stay in every
#: UE's servingCell candidate set.
PLACEMENT = {"ue1": "12345678", "ue2": "87654321", "ue3": "12345678"}

#: The common budgets, taken from the guarded runner's own argv so that this
#: check freezes what an episode freezes.  Revised for
#: ``OTA_EXISTING_XAPPS_EXPERIMENT_REVISION_20260914``: all three existing
#: function families (section 3), the 18/12/6 cap ladder and the two-value PF
#: ladder it declares, eight dispatches and the 480 s episode of section 6.
#: The ceiling rises with the scope, and it bounds a DIFFERENT quantity than the
#: revision's 1,000/696: ``--max-catalog`` caps the frozen catalogue -- the
#: product of the exposed axes, which the freeze reports as "steer 8 x cap 64 x
#: pf 8" = 4,096 -- while 1,000 and 696 count admissible *configurations*.  Both
#: of the predicates that produce them do exist and are enforced, one layer
#: down: ``_compatibility_rules`` (tools/liveconsole/agent.py) makes
#: ``dlPrbCap@ue`` and ``pfWeight@ue`` mutually exclusive per UE, and
#: ``MAX_CHANGED_ENTRIES = 4`` bounds the entries one configuration moves off
#: baseline.  Counted over the declared domain they give exactly 10 per UE,
#: 10^3 = 1,000, and 696 at four changed entries -- the revision's arithmetic,
#: reproduced rather than assumed.  So 4,096 is the honest ceiling and it
#: narrows nothing.  ``CELL_CAPACITY_MBPS`` is the measured deployment point
#: (gnb1's pair summed 14.5 Mbps), not an estimate.
AXES = ("servingCell", "dlPrbCap", "pfWeight")
CAP_RUNGS = (18, 12, 6)
PF_RUNGS = (1, 4)
MAX_CATALOG = 4096
RETAIN = 12
BUDGET_TRIALS = 8
DEADLINE_MS, HORIZON_MS = 420_000, 480_000
FORMATION_DEADLINE_MS, DECISION_DEADLINE_MS = 240_000, 60_000
CELL_CAPACITY_MBPS = 14.5

#: Never written into an artefact.  The prompts carry UE ids and AMF NGAP ids,
#: which the episode records already carry; a credential is a different thing.
FORBIDDEN = re.compile(r"\b(imsi|\bki\b|opc|password|passwd|secret|api[_-]?key|"
                       r"bearer|authorization:|token)\b", re.IGNORECASE)


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def frozen_snapshot(tmp):
    """The pinned pilot, through the runner's own loader, as the runner uses it.

    ``pilot_intents`` verifies both hashes and rewrites every ``ueId`` through
    the host join, so the identity mapping is the runner's.  Passing the
    snapshot's own ``ueHosts`` back in is the join that leaves the ids alone --
    this check has no live tun map and must not invent one.  The document is
    then written out and read back through ``main._parse_intents_json``, which
    is the one path ``--intents-json`` takes.

    The answers file is the second half of the same snapshot, and it is read
    through ``main._parse_answers`` -- the one path ``--answers`` takes -- from
    the pinned pilot rather than from ``tmp``, exactly as the guarded runner
    names it: the shaping keys on reqIds and owner strings, which the ueId
    remap does not touch.  Without it the sitting expands the *unshaped*
    product, which is what this check reported until the answers landed.
    """
    guarded = load_module(DIRECTORY / "atomic_formal_run_guarded.py", "guarded")
    # ``AIC_PILOT`` redirects the intents inside ``pilot_intents`` but leaves the
    # module constant alone, so reading the manifest and the answers off
    # ``guarded.PILOT`` would take half the snapshot from one directory and half
    # from another -- and the artefact would name the directory that supplied
    # neither.  Resolve the effective pilot once, through the runner's own
    # resolver, and read the whole snapshot from it.
    guarded.PILOT = guarded._pilot_pins(guarded.PILOT)[0]
    manifest = json.loads((guarded.PILOT / "manifest.json").read_text())
    hosts = dict(manifest["ueHosts"])
    document = guarded.pilot_intents(dict(hosts))
    path = tmp / "intents.json"
    path.write_text(json.dumps(document))
    import main
    answers = main._parse_answers(str(guarded.PILOT / "answers.json"))
    rows = main._parse_intents_json(str(path), answers)
    # 역할 키로 잡는다.  ``pilot_intents`` 는 **의도적으로** intent 의 ueId 를
    # 역할 이름(ue1/ue2/ue3)으로 바꾼다(UE 신원 연속성: 판 도중 번호가 바뀌므로
    # 역할을 봉인하고 실행 시 해석한다).  그런데 여기서 숫자 id 로 키잉하면
    # 에뮬레이터의 UE 집합과 rows 의 ue_id 가 어긋나 `serving_cell('ue1')` 이
    # KeyError 로 죽는다 -- 2026-09-17 에 이 검사기가 전혀 돌지 않고 있었다.
    ues = {host: PLACEMENT[host] for host in hosts.values()}
    return guarded, document, rows, ues, answers


def sitting_for(method, models, rows, ues, directory, preference, answers):
    from tools.liveconsole.agent import AgentRequest, build_hardware_free_agent_sitting
    from tools.hfconsole.agent_env import EmulatedRan

    if preference:
        # The one place the sitting reads a profile from (``_preference_for``);
        # absence means the bare default rule, which is what every recorded
        # episode so far actually ran under.
        os.environ["AIC_PREFERENCE"] = preference
    ran = EmulatedRan(
        ues=dict(ues), cells={cell: CELL_CAPACITY_MBPS for cell in set(ues.values())},
        offered_load_mbps={ue: 10.0 for ue in ues}, noise_sigma=0.0,
        # The snapshot states a response requirement per owner, so the emulated
        # deployment has to carry the tagged echo flow its KPI is differenced
        # out of; 2000 ms is the revision's original deadline (section 6).
        echo_deadline_ms={ue: 2000.0 for ue in ues})
    request = AgentRequest(
        intents=rows, method=method, role_models=models,
        budget_trials=BUDGET_TRIALS, deadline_ms=DEADLINE_MS, horizon_ms=HORIZON_MS,
        timing_mode="cold-start", formation_deadline_ms=FORMATION_DEADLINE_MS,
        decision_deadline_ms=DECISION_DEADLINE_MS,
        cells=tuple(int(cell) for cell in sorted(set(ues.values()))),
        axes=AXES, caps={ue: CAP_RUNGS for ue in ues},
        # Naming the PF ladder per UE is not optional once ``pfWeight`` is an
        # exposed kind: an unnamed scope falls back to DEFAULT_LADDERS, whose
        # four rungs would multiply the frozen catalogue by 4^3 instead of the
        # 2^3 the revision declares.
        pf_weights={ue: PF_RUNGS for ue in ues},
        max_catalog_cardinality=MAX_CATALOG, settings={"retain": RETAIN},
        # The shaping. ``build_agent_sitting`` folds this into the
        # authorization at the one place it builds one (merge_answers), so the
        # arms are handed the authorized domain and not the unshaped product.
        answers=answers)
    directory.mkdir(parents=True, exist_ok=True)
    return build_hardware_free_agent_sitting(
        request, tmp_dir=directory, ran=ran, stamp="20260914T000000Z")


def authorization_of(rows, preference, answers):
    """The authorization the sitting builds, derived without composing one.

    Same three calls ``build_agent_sitting`` makes at the one place it builds
    the sitting's authorization -- parse, ``Authorization.from_intents``, then
    ``merge_answers`` -- so the guard below reads the domain the arms will
    actually be handed rather than a second reading of the same file.  The
    third call is the shaping one: ``merge_answers`` *rebuilds* the
    authorization, and anything it is not given is gone by the time the domain
    is expanded, however carefully it was signed.
    """
    from assurance.coordination.intake import merge_answers
    from assurance.coordination.tc import Authorization
    from tools.liveconsole.agent import (
        AgentRequest, _preference_for, parse_agent_intents,
    )
    intents = tuple(row.intent for row in parse_agent_intents(
        AgentRequest(intents=rows, method="deterministic")))
    _merged, authorization, _settings = merge_answers(
        intents,
        Authorization.from_intents(
            intents, preference=_preference_for(intents, preference)),
        answers, {})
    return authorization


def omega(authorization):
    """What the LOADED authorization expands to, and the arithmetic for it.

    Never a constant, and never asserted: each step is *expanded*, so the
    number this reports is whatever the snapshot in hand actually admits.  The
    two kinds of shaping carve in sequence and are reported that way -- the
    per-owner ``(g,d)`` mode sets first, then the cross-owner deadline quota on
    top of them -- because a single "shaping refused N" cannot say which of the
    two did the refusing, and they are separately signed.
    """
    from dataclasses import replace

    from assurance.coordination.tc import OwnerModeSet, expand_targets

    factors = []
    product = 1
    for req_id, entry in authorization.requirements.items():
        thresholds = max(1, len(entry.levels))
        deadlines = max(1, len(entry.deadline_levels))
        factors.append({"reqId": req_id, "owner": entry.owner, "kpi": entry.kpi,
                        "thresholdLevels": thresholds, "deadlineLevels": deadlines})
        product *= thresholds * deadlines
    # The middle step, expanded rather than multiplied out by hand: the same
    # authorization carrying only its OwnerModeSet entries.  ``replace`` runs
    # __post_init__, which re-reads every constraint, so this is the real
    # container and not a loosened copy of one.
    mode_sets = tuple(item for item in authorization.mode_constraints
                      if isinstance(item, OwnerModeSet))
    after_mode_sets = len(expand_targets(
        replace(authorization, mode_constraints=mode_sets)).targets)
    expanded = expand_targets(authorization)
    return {"perRequirement": factors,
            "unshapedProduct": product,
            "afterOwnerModeSets": after_mode_sets,
            "expandedCardinality": len(expanded.targets),
            "refusedByOwnerModeSets": product - after_mode_sets,
            "refusedByCrossOwnerQuota": after_mode_sets - len(expanded.targets),
            "refusedByShaping": product - len(expanded.targets),
            "modeConstraints": [c.describe() for c in authorization.mode_constraints],
            "jointConditions": [c.to_record() for c in authorization.joint_conditions],
            "preferenceRule": authorization.preference.rule}


def named_by_model(raw):
    """What the model itself put in the answer, before any validation."""
    try:
        answer = json.loads(raw)
    except ValueError:
        return {"parsed": False}
    alternatives = answer.get("alternatives")
    candidates = answer.get("candidates")
    instructions = answer.get("instructions")
    return {
        "parsed": True,
        "alternativesNamed": (None if alternatives is None else len(alternatives)),
        "alternativeLevels": [row.get("levels") for row in (alternatives or [])
                              if isinstance(row, dict)],
        "candidatesNamed": (None if candidates is None else len(candidates)),
        "candidateIds": [row.get("controlId") for row in (candidates or [])
                         if isinstance(row, dict)],
        "instructionsNamed": (None if instructions is None else len(instructions)),
        "targetId": answer.get("targetId"), "controlId": answer.get("controlId"),
    }


def capture(stage, arm, record, schema, membership):
    """One stage: the full request as sent, the schema, the output, the cost."""
    return {
        "stage": stage, "arm": arm, "role": record.role, "phase": record.phase,
        "model": record.model, "servedModel": record.served_model,
        "acceptedByValidator": bool(record.accepted),
        "fallbackReason": record.fallback_reason,
        "request": {"systemPrompt": record.system_prompt, "userPrompt": record.prompt,
                    "systemPromptBytes": len(record.system_prompt.encode()),
                    "userPromptBytes": len(record.prompt.encode()),
                    "inputKeys": list(record.input_keys),
                    "generationOptions": dict(record.options)},
        "schemaInForce": schema,
        "output": {"raw": record.raw, "rawBytes": len(record.raw.encode()),
                   "rationale": record.rationale},
        "membership": membership,
        "validatorNotes": list(record.dropped),
        "cost": {"inputTokens": record.input_tokens,
                 "outputTokens": record.output_tokens,
                 "latencyMs": round(record.latency_ms, 3),
                 "generations": len(record.generations),
                 "repairRetries": record.repair_retries,
                 "perGeneration": [dict(item) for item in record.generations]},
    }


def run(model, preference, out_dir):
    from assurance.coordination.agents import (
        _BASIC_SCHEMA, _CONTROL_SCHEMA, _MONOLITH_FORM_SCHEMA, _PAIR_SCHEMA,
        _TARGET_SCHEMA,
    )
    tmp = Path(tempfile.mkdtemp(prefix="noradio-"))
    guarded, document, rows, ues, answers = frozen_snapshot(tmp)
    # Before any sitting is composed, because composing one is what makes the
    # Target and Control calls: a guard that fired afterwards would already
    # have spent the calls it exists to prevent.  This is the authorization the
    # sitting itself builds (agent.py, one place, same preference injection,
    # same answers folded in).
    guard(model, omega(authorization_of(rows, preference, answers)))
    arms = (("three-agent", {"target": model, "control": model, "trajectory": model}),
            ("internal-monolith", {"monolith": model}),
            ("basic-monolith", {"monolith": model}))
    captures, boards, domain = [], {}, None
    for method, models in arms:
        sitting = sitting_for(method, models, rows, ues, tmp / method,
                              preference, answers)
        if domain is None:
            domain = omega(sitting.contract.authorization)
        contract, controls = sitting.contract, sitting.controls
        if method == "basic-monolith":
            decision, _ = sitting._decide_basic(sitting._basic_inputs())
        else:
            decision, _ = sitting._select(sitting._trajectory_inputs())
        boards[method] = {
            "targetIds": list(contract.target_ids),
            "targetLevels": {t.target_id: dict(t.levels) for t in contract.targets},
            "controlIds": list(controls.control_ids),
            "targetProvenance": dict(contract.provenance or {}),
            "controlProvenance": dict(controls.provenance or {}),
            "catalogCardinality": int(sitting.catalog_cardinality),
            "decision": (None if decision is None else
                         {"targetId": getattr(decision, "target_id", None),
                          "controlId": getattr(decision, "control_id", None),
                          "configuration": dict(getattr(decision, "configuration", {}) or {}),
                          "rationale": getattr(decision, "rationale", "")}),
        }
        for record in sitting.agents.calls:
            if record.phase == "intake":
                continue          # deterministic checklist, not a model call
            schema = {"target": _TARGET_SCHEMA, "control": _CONTROL_SCHEMA,
                      "trajectory": _PAIR_SCHEMA}.get(record.role)
            if record.role == "monolith":
                schema = (_MONOLITH_FORM_SCHEMA if record.phase == "formation"
                          else (_BASIC_SCHEMA if method == "basic-monolith"
                                else _PAIR_SCHEMA))
            membership = {"namedByModel": named_by_model(record.raw),
                          "validatedTargetIds": list(contract.target_ids),
                          "validatedControlIds": list(controls.control_ids)}
            captures.append(capture(f"{method}/{record.role}/{record.phase}",
                                    method, record, schema, membership))
    return {"snapshot": {"pilotDir": str(guarded.PILOT),
                         # Hash the file that was actually read, never the
                         # module constant: AIC_PILOT can point the snapshot at
                         # another directory, and a record that names the
                         # directory it read with the hash of one it did not is
                         # the same class of error as a condition label that
                         # does not describe what ran.
                         "intentsSha256": hashlib.sha256(
                             (guarded.PILOT / "intents.json").read_bytes()).hexdigest(),
                         "manifestSha256": guarded.PILOT_MANIFEST_SHA,
                         "answersPath": str(guarded.PILOT / "answers.json"),
                         "answersSha256": hashlib.sha256(
                             (guarded.PILOT / "answers.json").read_bytes()).hexdigest(),
                         "domain": document.get("domain", {}),
                         "ueCells": ues},
            "model": model, "preference": preference or "(unset: the bare default rule)",
            "budgets": {"axes": list(AXES), "capRungs": list(CAP_RUNGS),
                        "pfRungs": list(PF_RUNGS), "maxCatalog": MAX_CATALOG,
                        "retain": RETAIN, "budgetTrials": BUDGET_TRIALS,
                        "deadlineMs": DEADLINE_MS, "horizonMs": HORIZON_MS,
                        "formationDeadlineMs": FORMATION_DEADLINE_MS,
                        "decisionDeadlineMs": DECISION_DEADLINE_MS,
                        "encoding": "compact (_user_prompt: separators=(',',':'))"},
            "omega": domain, "boards": boards, "captures": captures}


def guard(model, domain):
    """A real model may not answer a domain the correction has not shaped yet."""
    if model == MOCK or domain["modeConstraints"]:
        return
    raise SystemExit(
        f"refused: --model {model!r} on an authorization that states no mode "
        f"constraints.  It expands to {domain['expandedCardinality']} targets, "
        "which is the unshaped product; the owner table authorizes fewer.  A "
        "real call now would be answered against a domain the correction is "
        "about to shrink and the artefact would have to be discarded.  Run the "
        f"check with {MOCK} until the constraint work reaches the "
        "authorization, then this guard opens by itself.")


def markdown(result):
    o = result["omega"]
    arithmetic = " * ".join(
        f"{row['thresholdLevels']}*{row['deadlineLevels']}" for row in o["perRequirement"])
    lines = [f"# No-radio construction check ({result['model']})", "",
             "Integrated reply section 5, on one frozen corrected input snapshot. "
             "Six stages, three arms, one snapshot. No radio, no socket, no lab file: "
             "the deployment is the seeded emulator. Subsets are NOT aligned between "
             "arms -- that is the result, not a defect.", "",
             f"Snapshot: `{Path(result['snapshot']['pilotDir']).name}` "
             f"(intents sha256 `{result['snapshot']['intentsSha256'][:16]}...`)  ",
             f"Preference in force: {result['preference']}  ",
             f"Common budgets: axes {', '.join(result['budgets']['axes'])}; cap rungs "
             f"{result['budgets']['capRungs']}; PF rungs {result['budgets']['pfRungs']}; "
             f"max-catalog {result['budgets']['maxCatalog']}; retain "
             f"{result['budgets']['retain']}; encoding {result['budgets']['encoding']}",
             "", "## What the loaded authorization expands to", "",
             f"`{arithmetic} = {o['unshapedProduct']}` unshaped over "
             f"{len(o['perRequirement'])} requirement rows; the per-owner "
             f"(g,d) mode sets refuse {o['refusedByOwnerModeSets']} of them "
             f"-> {o['afterOwnerModeSets']}; the cross-owner deadline quota "
             f"refuses a further {o['refusedByCrossOwnerQuota']} -> "
             f"**|Omega| = {o['expandedCardinality']}**.  "
             f"(`{o['unshapedProduct']} -> {o['afterOwnerModeSets']} -> "
             f"{o['expandedCardinality']}`, each step expanded, none asserted.)  ",
             f"Mode constraints stated by this authorization: "
             f"{o['modeConstraints'] or 'NONE'}.  ",
             f"Joint conditions: {len(o['jointConditions'])}.  "
             f"Ranking rule: `{o['preferenceRule']}`.", "",
             "| reqId | owner | kpi | threshold levels | deadline levels |",
             "|---|---|---|---|---|"]
    for row in o["perRequirement"]:
        lines.append(f"| {row['reqId']} | {row['owner']} | {row['kpi']} | "
                     f"{row['thresholdLevels']} | {row['deadlineLevels']} |")
    lines += ["", "## Boards each arm built", "",
              "| arm | \\|T\\| | mandatory | model-added | epsilon | T ids | \\|C\\| | C ids | first decision |",
              "|---|---|---|---|---|---|---|---|---|"]
    for arm, board in result["boards"].items():
        decision = board["decision"] or {}
        chosen = (f"{decision.get('targetId')} / {decision.get('controlId')}"
                  if decision.get("controlId") else
                  ("configuration" if decision.get("configuration") else "none"))
        provenance = board.get("targetProvenance") or {}
        membership = provenance.get("targetMembership") or {}
        error = provenance.get("expressionError") or {}
        # v3.1 section 3.4: the exact expression error of the T this arm built,
        # and mandatory membership kept apart from what the model added.  An arm
        # that constructs no T (basic monolith) has neither, and says so.
        epsilon = ("n/a" if not error else
                   "inf" if error.get("epsilonInfinite") else f"{error.get('epsilon')}")
        lines.append(f"| {arm} | {len(board['targetIds'])} | "
                     f"{len(membership.get('mandatory') or []) if membership else 'n/a'} | "
                     f"{len(membership.get('modelAdditions') or []) if membership else 'n/a'} | "
                     f"{epsilon} | {', '.join(board['targetIds'][:12])} | "
                     f"{len(board['controlIds'])} | {', '.join(board['controlIds'])} | "
                     f"{chosen} |")
    lines += ["", "## Per-stage cost and capture size", "",
              "| stage | accepted | in tok | out tok | latency ms | gens | retries "
              "| request B | raw B |", "|---|---|---|---|---|---|---|---|---|"]
    for item in result["captures"]:
        request = item["request"]
        lines.append(
            f"| {item['stage']} | {item['acceptedByValidator']} | "
            f"{item['cost']['inputTokens']} | {item['cost']['outputTokens']} | "
            f"{item['cost']['latencyMs']:.1f} | {item['cost']['generations']} | "
            f"{item['cost']['repairRetries']} | "
            f"{request['systemPromptBytes'] + request['userPromptBytes']} | "
            f"{item['output']['rawBytes']} |")
    lines += ["", "## Backend identity, reported usage and generation settings", "",
              "The real run is the only source of these: under the mock every "
              "one of them is `None`/0.  A `None` from a real backend means the "
              "provider reported nothing and is never filled in from the "
              "requested label -- that substitution is exactly what would make "
              "a fallback read as a normal run.  `requested`/`sent` are the "
              "generation options asked for and the ones the backend reported "
              "putting on the wire; they differ when a setting is gated on the "
              "served model.", "",
              "| stage | requested label | servedModel | responseModel | "
              "requestedRoute | usage | retries | requested opts | sent opts | "
              "refusedBecause |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for item in result["captures"]:
        last = (item["cost"]["perGeneration"] or [{}])[-1]
        lines.append(
            f"| {item['stage']} | {item['model']} | {item['servedModel']} | "
            f"{last.get('responseModel')} | {last.get('requestedRoute')} | "
            f"{last.get('usage')} | {item['cost']['repairRetries']} | "
            f"{item['request']['generationOptions'] or '-'} | "
            f"{last.get('sentOptions')} | {last.get('refusedBecause')} |")
    lines += ["", "## Validated membership versus what the model named", "",
              "| stage | named | validated | validator notes |",
              "|---|---|---|---|"]
    for item in result["captures"]:
        named = item["membership"]["namedByModel"]
        if named.get("alternativesNamed") is not None:
            what = f"{named['alternativesNamed']} alternatives"
            kept = f"{len(item['membership']['validatedTargetIds'])} targets (T0 included)"
        elif named.get("candidatesNamed") is not None:
            what = f"{named['candidatesNamed']} candidates"
            kept = f"{len(item['membership']['validatedControlIds'])} controls (C0 included)"
        elif named.get("instructionsNamed") is not None:
            what = f"{named['instructionsNamed']} instructions"
            kept = "translated onto the axes"
        else:
            what = f"{named.get('targetId')} / {named.get('controlId')}"
            kept = "in T and in C"
        notes = "; ".join(item["validatorNotes"]) or "-"
        lines.append(f"| {item['stage']} | {what} | {kept} | {notes} |")
    lines += ["", "Full requests, schemas, raw outputs and per-generation cost rows "
              f"are in `{stem_for(result)}-captures/`, one JSON file per stage.", ""]
    return "\n".join(lines)


def write(result, out_dir):
    stem = stem_for(result)
    captures_dir = out_dir / f"{stem}-captures"
    captures_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for item in result["captures"]:
        name = item["stage"].replace("/", "-") + ".json"
        path = captures_dir / name
        path.write_text(json.dumps(item, indent=1, sort_keys=True, default=str) + "\n")
        written.append(path)
    board = out_dir / f"{stem}-boards.json"
    board.write_text(json.dumps(
        {key: result[key] for key in ("snapshot", "model", "preference", "budgets",
                                      "omega", "boards")},
        indent=1, sort_keys=True, default=str) + "\n")
    summary = out_dir / f"{stem}.md"
    summary.write_text(markdown(result))
    written += [board, summary]
    for path in written:                       # no credential leaves this script
        hit = FORBIDDEN.search(path.read_text())
        assert hit is None, f"{path.name} carries a forbidden term: {hit.group(0)!r}"
    return written


def check(result):
    """The one runnable check: every stage captured real content."""
    assert len(result["captures"]) == 6, f"{len(result['captures'])} stages, expected 6"
    for item in result["captures"]:
        stage = item["stage"]
        assert item["request"]["userPromptBytes"] > 0, f"{stage}: empty request"
        assert item["request"]["systemPromptBytes"] > 0, f"{stage}: no system prompt"
        assert item["schemaInForce"], f"{stage}: no schema"
        assert item["output"]["rawBytes"] > 0, f"{stage}: empty output"
        assert item["cost"]["generations"] > 0, f"{stage}: no generation recorded"
        assert item["membership"]["namedByModel"]["parsed"], f"{stage}: output not JSON"
    assert result["omega"]["expandedCardinality"] > 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=MOCK,
                        help=f"backend for every role (default {MOCK}; any other "
                             "name is refused while the authorization states no "
                             "mode constraints -- see the module docstring)")
    parser.add_argument("--preference", default=None, choices=("P1", "P2", "P3"),
                        help="owner preference profile; unset means the bare "
                             "default rule, which is what the recorded episodes ran")
    parser.add_argument("--out", default=str(DIRECTORY), help="artefact directory")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(REPO))
    result = run(args.model, args.preference, Path(args.out))
    check(result)
    written = write(result, Path(args.out))
    print(markdown(result))
    print("written:")
    for path in written:
        print(f"  {path}  ({path.stat().st_size} B)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
