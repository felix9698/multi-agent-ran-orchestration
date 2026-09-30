#!/usr/bin/env python3
"""Agentic Intent Coordinator - the deployed entry point.

This starts the **Research Operations Cockpit** over the Assurance Kernel
runtime, and nothing else.  There is no second decision runtime behind this
file: the console is assembled here, a Kernel submission session is attached by
whoever wired the vertical path, and every write leaves through the Write
Gateway.

What this entry point deliberately cannot do
--------------------------------------------
It cannot construct the legacy ``coordinator.intent_coordinator``
``IntentCoordinator``, and it cannot reach the patched-OAI direct-control
executor behind it.  That runtime is preserved as research history and is
reached only through its own explicit entry:

    AIC_LEGACY_CONSOLE_APPROVED=1 python3 -m tools.legacy.coordinator_console --no-gui

The offline paper pipeline is unchanged and is its own tool:

    python3 -m experiments.runner --mode synthetic --trials 5

Usage:
    # The Operator Console over the Kernel runtime (the deployed product)
    python3 main.py

    # Preload an experiment profile and choose where runs are written
    python3 main.py --profile <profile.json> --runs-root <dir>

    # Publish the advisory proposer inventory into the console (probes
    # providers, so it is an operator act rather than a default)
    python3 main.py --llm-inventory
    python3 main.py --llm claude-sonnet

    # The Cockpit over the *real* O-RAN control path, one objective per
    # session, addressed entirely through one live profile document
    python3 main.py --live --profile deployment/liveconsole-profile.json
    python3 main.py --live --profile <live-profile.json> --objective QoSTarget
    python3 main.py --live --profile <live-profile.json> --no-gui

Author: LICS Lab, Korea University
"""

import argparse
import logging
import os
import sys

# 2026-09-22: 시팅이 한 시행에서 코어를 수 분씩 태우는데 ptrace_scope=1 이라 py-spy 로
# 붙을 수 없다.  SIGUSR1 에 파이썬 스택을 뱉게 해 두면 `kill -USR1 <pid>` 한 줄로
# 어느 줄에서 도는지 보인다.  판의 stdout 으로 나가므로 증거와 함께 남는다.
try:
    import faulthandler as _fh
    import signal as _sig
    _fh.register(_sig.SIGUSR1, all_threads=True, chain=False)
except Exception:  # noqa: BLE001 - 진단 보조일 뿐, 없어도 판은 돈다
    pass

from oran.campaign5.families import CAMPAIGN5_FAMILIES
# The flag table only.  ``tools.campaign5.live_run`` is a live composition root
# and is imported inside ``run_live_campaign5_action``, so opening the Cockpit
# still loads no transport.
from tools.campaign5.value_flags import FAMILY_VALUE_FLAGS as CAMPAIGN5_VALUE_FLAGS

# Setup path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("Main")


#: Where the preserved research runtime lives now.  Named in refusals so an
#: operator who wants it is told what to run rather than left guessing.
LEGACY_ENTRY = "tools.legacy.coordinator_console"

#: The objective ``--hardware-free`` runs when none is named.  ``--live`` has no
#: such default: omitting ``--objective`` there selects the Gate 3
#: ``UeCellSteeringPinToCell`` regression case, which is not a registry family.
DEFAULT_HARDWARE_FREE_OBJECTIVE = "UELevelTarget"


def build_llm_inventory(name=None):
    """Build the advisory proposer inventory, or return ``None``.

    Neutral by construction: the inventory comes from ``decision.llm_backend``
    itself, not from a coordinator that happens to own one.  It is *advisory*
    on this runtime - the Contract Studio's Intent Agent is deterministic and
    makes no model call - so it is built only when the operator asks for it,
    because building it probes the configured providers.
    """
    from decision.llm_backend import LLMBackendManager

    manager = LLMBackendManager()
    if name:
        if not manager.set_backend(name):
            available = ", ".join(manager.get_available_names()) or "none"
            print(f"Backend not available: {name}\n  Available: {available}")
    return manager


def build_console(*, profile_path=None, runs_root=None, llm_manager=None,
                  title=None):
    """Assemble the Operator Console.  Touches no toolkit and contacts nothing.

    This is the whole default composition, and it is a function rather than
    inline code in :func:`main` so the reachability test can build exactly what
    ``python3 main.py`` builds without a display.

    No coordinator is constructed and none is passed: the console's decision
    runtime is the Kernel, reached through a submission session that whoever
    wired the vertical path attaches.  Until one is attached the console is
    Disconnected, which is the state it is designed to open in.
    """
    from gui.operator.app import OperatorConsole
    from gui.operator.session.profile import ExperimentProfile

    profile = ExperimentProfile.load(profile_path) if profile_path else None
    kwargs = {"profile": profile, "runs_root": runs_root,
              "llm_manager": llm_manager}
    if title:
        kwargs["title"] = title
    return OperatorConsole(**kwargs)


def run_operator_console(console):
    """Open the window and run the console's own loop."""
    console.create_window()
    console.restore_gui_state()
    console.run()


def run_live_campaign5_action(args) -> int:
    """``--live-action``: one Campaign 5 family, over the air, through A1.

    The same entry point as ``python3 -m tools.campaign5.live_run``, reached
    from the console binary because that is where an operator looks.  It
    composes no Cockpit session: this path drives one named action at one named
    value and reports what it observed, and it says in its own evidence that it
    ran no Kernel case and therefore claims no Kernel decision axis.
    """
    from tools.campaign5.live_run import (
        Campaign5LiveError, print_run, run_live_action)
    from tools.liveconsole import LiveConsoleError

    flags = CAMPAIGN5_VALUE_FLAGS[args.live_action]
    values = {}
    for flag, leaf in flags:
        given = getattr(args, flag.lstrip("-").replace("-", "_"), None)
        if given is None:
            print(f"refused: --live-action {args.live_action} needs {flag}")
            return 2
        values[leaf] = int(given)
    baseline = None
    if args.baseline is not None:
        if len(flags) > 1:
            print(f"refused: --live-action {args.live_action} moves "
                  f"{len(flags)} leaves; --baseline states one and cannot "
                  "describe them")
            return 2
        baseline = {flags[0][1]: int(args.baseline)}
    try:
        document = run_live_action(
            profile_path=args.profile, family_key=args.live_action,
            values=values, hold_s=args.hold_s,
            amf_ue_ngap_id=args.amf_ue_ngap_id, cell_nci=args.cell_nci,
            gnb_id=args.gnb_id, baseline=baseline,
            evidence_dir=args.evidence_dir)
    except (Campaign5LiveError, LiveConsoleError) as exc:
        print(f"refused before anything was submitted:\n  {exc}")
        return 3
    print_run(document)
    return 0 if document["settlement"]["appliedVerified"] else 1


def _replay_target(path):
    """Split a replay argument into the directory an adapter reads and a run id.

    An operator names the file they have in front of them -- the ``run.json``
    an evidence directory is full of -- and an adapter reads a *directory*.
    Both are accepted, and a named file is resolved to its own run so a
    directory of six sittings does not silently open the first one.
    """
    from pathlib import Path

    from gui.operator.sources.adapters import liveconsole_run as _liveconsole

    target = Path(path)
    if target.is_dir():
        return target, None
    if not target.is_file():
        raise FileNotFoundError(f"{target} is neither a directory nor a file")
    name = target.name
    if name.endswith(_liveconsole.RUN_SUFFIX):
        return target.parent, name[:-len(_liveconsole.RUN_SUFFIX)]
    # Any other named file identifies its directory and nothing finer; the
    # adapter's own detection decides what that directory is.
    return target.parent, None


def _print_replayed(store, *, source_path, adapter):
    """Print what the Cockpit's Replay and Results views show for this run.

    Same fields, same order, same distinction between the mode of the session
    and the mode of the recording -- a headless reader must not be shown a
    friendlier picture than the window would give them.
    """
    manifest = store.read_manifest()
    summary = store.read_summary() or {}
    settlement = summary.get("settlement") or {}
    axes = summary.get("axes") or {}

    print(f"--- replay [{manifest.get('mode')}] {manifest.get('runId')} ---")
    print(f"  source        : {source_path} ({adapter})")
    print(f"  source run    : {summary.get('sourceRunId') or summary.get('sourceSessionId')}")
    print(f"  recorded as   : {summary.get('sourceMode') or 'unstated'} "
          f"(this session is {manifest.get('mode')}; nothing is being observed)")
    print(f"  disposition   : {manifest.get('disposition')}")
    print(f"  run directory : {store.run_dir}")
    if summary.get("utterance"):
        print(f"  utterance     : {summary['utterance']}")
    if summary.get("terminalStateHash"):
        print(f"  terminal hash : {summary['terminalStateHash']}")
        print(f"  reducer       : {summary.get('reducerVersion')}")
    if settlement:
        print("  settlement:")
        print(f"    trial state : {settlement.get('trialState')}")
        print(f"    outcome     : {settlement.get('outcome')}")
        print(f"    stop reason : {settlement.get('stopReason')}")
        print(f"    termination : {settlement.get('caseTermination')}")
        print(f"    evidence    : {settlement.get('evidenceStatus')}")
        operations = " -> ".join(f"{kind} {outcome}" for kind, outcome
                                 in settlement.get("gatewayOperations") or ())
        print(f"    gateway     : {operations or 'none recorded'}")
        for charge in settlement.get("harmCharges") or ():
            print(f"    harm        : {charge}")
    if axes:
        print("  axes:")
        print(f"    execution validity     : {axes.get('executionValidity')}")
        print(f"    measurement sufficiency: {axes.get('measurementSufficiency')}")
        print(f"    hold complete          : {axes.get('holdComplete')}")
        print(f"    trial outcome          : {axes.get('trialOutcome')}")
        for name, verdict in sorted((axes.get("predicateVerdicts") or {}).items()):
            print(f"    predicate {name}: {verdict}")
    supplementary = summary.get("supplementary") or []
    if supplementary:
        print("  supplementary:")
        for entry in supplementary:
            controlled = entry.get("controlledUe") or {}
            print(f"    {entry.get('actionId')} via {entry.get('adapterKey')} "
                  f"on {entry.get('policyTypeId')}")
            print(f"      controlled UE : "
                  f"{controlled.get('ueId') or controlled.get('amfUeNgapId')}"
                  f" (cell {controlled.get('cellId')})")
            print(f"      policy        : {entry.get('bindingPolicyId')}")
            print(f"      binding       : {entry.get('bindingState')} "
                  f"/ readback {entry.get('readbackState')}")
            if entry.get("rollbackDetail"):
                print(f"      rollback      : {entry['rollbackDetail']}")
            print(f"      readbacks     : {len(entry.get('readbackLog') or ())}")
    elif summary.get("supplementaryRecorded") is False:
        # Not "no cap": no record of one.  The two are different facts and the
        # printed block never lets the first stand in for the second.
        print("  supplementary : not recorded by this run's schema version")
    issues = manifest.get("dataIssues") or []
    print(f"  data issues   : {len(issues)}")
    for issue in issues:
        print(f"    [{issue.get('kind')}] {issue.get('detail')}")


def run_headless_replay(*, source, runs_root=None, run_id=None):
    """Load a recorded source and print it, opening no window.

    The adapter decides the mode from the source, exactly as it does for the
    window; there is no argument here that makes a recording read as LIVE.
    """
    import tempfile

    from gui.operator.sources import replay as replay_sources

    try:
        directory, detected = _replay_target(source)
    except FileNotFoundError as exc:
        print(f"refused: {exc}")
        return 2
    adapter = replay_sources.detect_adapter(directory)
    if adapter is None:
        print(f"refused: {directory} holds no source this console can read")
        return 2
    root = runs_root or tempfile.mkdtemp(prefix="replay-")
    try:
        store = replay_sources.load(directory, root,
                                    session_id=run_id or detected)
    except replay_sources.ReplayError as exc:
        print(f"refused [{exc.kind}]: {exc.detail}")
        return 3
    try:
        _print_replayed(store, source_path=directory, adapter=adapter)
        return 0 if store.is_success else 1
    finally:
        store.close()


def run_headless_export(*, source, destination, runs_root=None, run_id=None):
    """Replay a recorded source and export it, opening no window.

    The export is the console's own :func:`export_run`, so a headless export and
    an exported-from-the-window one are the same files with the same banners --
    including the mode banner, which is what stops a Replay export from reading
    as LIVE once it is in a spreadsheet.
    """
    import tempfile

    from gui.operator.export.data_export import ExportError, export_run
    from gui.operator.sources import replay as replay_sources

    try:
        directory, detected = _replay_target(source)
    except FileNotFoundError as exc:
        print(f"refused: {exc}")
        return 2
    adapter = replay_sources.detect_adapter(directory)
    if adapter is None:
        print(f"refused: {directory} holds no source this console can read")
        return 2
    root = runs_root or tempfile.mkdtemp(prefix="replay-")
    try:
        store = replay_sources.load(directory, root,
                                    session_id=run_id or detected)
    except replay_sources.ReplayError as exc:
        print(f"refused [{exc.kind}]: {exc.detail}")
        return 3
    try:
        _print_replayed(store, source_path=directory, adapter=adapter)
        try:
            manifest = export_run(store, destination)
        except ExportError as exc:
            print(f"export refused: {exc}")
            return 3
        print(f"  exported to   : {destination}")
        for name in manifest.get("files") or ():
            print(f"    {name}")
        return 0 if store.is_success else 1
    finally:
        store.close()


def run_headless_hardware_free(*, objective, utterance, target_nci=None,
                               controlled_amf_ue_ngap_id=None):
    """Run one hardware-free intent to a terminal without opening a window.

    This is the ``--no-gui`` equivalent of drafting, confirming and starting in
    Contract Studio: it emits the same typed Kernel command and event sequence
    through the same :class:`KernelSubmissionSession`, only over the mock
    actuation adapter.  It is not a Live path and is never OTA evidence.

    Imported lazily so the default composition (and its reachability test) never
    loads the hardware-free composition root.
    """
    from tools.hfconsole import build_hardware_free_session

    kwargs = {} if target_nci is None else {"target_nci": int(target_nci)}
    if controlled_amf_ue_ngap_id is not None:
        kwargs["controlled_amf_ue_ngap_id"] = int(controlled_amf_ue_ngap_id)
    composed = build_hardware_free_session(objective, utterance=utterance, **kwargs)
    session = composed.session
    text = composed.utterance
    logger.info("hardware-free headless run: objective=%s mode=%s", objective, session.mode)
    logger.info("utterance: %s", text)
    preview = session.draft(text)
    logger.info("drafted contract preview: case=%s hash=%s",
                session.case_id, preview.content_hash())
    instance = session.confirm(preview)
    view = session.start(instance)
    axes = view.axes
    settlement = view.settlement
    print(f"objective          : {objective}")
    print(f"mode               : {view.mode} (is_live={view.is_live})")
    print(f"case               : {view.case_id}")
    print(f"trial              : {view.trial_id}")
    print(f"stage              : {view.stage}")
    print(f"execution validity : {getattr(axes, 'execution_validity', None)}")
    print(f"measurement suff.  : {getattr(axes, 'measurement_sufficiency', None)}")
    print(f"predicate verdict  : {getattr(axes, 'predicate_verdict', None)}")
    _print_supplementary(preview, view)
    if settlement is not None:
        print(f"trial state        : {settlement.trial_state}")
        print(f"trial outcome      : {settlement.outcome}")
    if view.refusal:
        print(f"refusal            : {view.refusal} {view.refusal_detail}")
    return 0


def _print_supplementary(preview, view):
    """Print the SUPPLEMENTARY controls this contract carries, and their state.

    Two halves, and they are not the same thing.  The *declared* half is what
    Review & Confirm covered -- which UE, which value, which registered client
    -- and it comes off the contract preview, so a headless run shows exactly
    what an operator would have clicked.  The *observed* half is what the
    deployment then said, and it comes off the session's frozen view.
    """
    for declared in getattr(preview, "supplementary", ()):
        print(f"supplementary      : {declared['actionId']} "
              f"{declared['maxDlPrbs']} PRB on controlledUeId="
              f"{declared['controlledUeId']} via {declared['adapter']} "
              f"({declared['policyTypeId']})")
    for observed in getattr(view, "supplementary", ()):
        print(f"supplementary state: {observed.summary}")


def run_headless_live(*, profile_path, objective=None, utterances=(),
                      target_nci=None, amf_ue_ngap_id=None,
                      controlled_amf_ue_ngap_id=None,
                      withdraw_verified=False):
    """Run one or more **live** intents to a terminal, without opening a window.

    The ``--no-gui`` twin of the Cockpit sitting: the same draft/confirm/start
    on the same :class:`LiveConsoleSession`, one case per sentence, run in
    order in one session -- only the Write Gateway's adapter reaches the
    deployment over R1 -> A1-P -> xApp -> E2SM-RC.  Each case prints the fields
    the hardware-free run prints, plus what only a live run has: the A1-P
    policy ids the case created and the producer's own status rows for them.

    Exit code is 0 only when *every* case settled ``SUCCESS``.  A sitting whose
    third sentence was refused is not a success because the first two worked.

    Imported lazily so the default composition -- and its reachability test --
    never loads a transport.
    """
    from tools.liveconsole import (
        LiveConsoleError, build_live_session, write_run_evidence)

    sentences = [text for text in (utterances or ()) if str(text).strip()]
    try:
        live = build_live_session(
            profile_path, objective=objective,
            utterance=sentences[0] if sentences else None,
            target_nci=target_nci, amf_ue_ngap_id=amf_ue_ngap_id,
            controlled_amf_ue_ngap_id=controlled_amf_ue_ngap_id)
    except LiveConsoleError as exc:
        # Every refusal from the composition root names what it refused and
        # why; printing the reason is more use to an operator than a traceback
        # through a wiring function they did not write.
        print(f"refused before anything was submitted:\n  {exc}")
        return 3

    # With no sentence the sitting still runs one case, on the sentence its own
    # objective generates -- which is what `--live --no-gui` did before this.
    plan = sentences or [live.utterance]
    logger.info("live headless sitting: mode=%s cases=%d", live.mode, len(plan))
    outcomes = []
    for index, sentence in enumerate(plan, 1):
        print(f"--- case {index} of {len(plan)} ---")
        outcomes.append(_run_one_live_case(
            live, sentence, withdraw_verified=withdraw_verified,
            write_run_evidence=write_run_evidence,
            refusal_type=LiveConsoleError))
    print("--- sitting summary ---")
    print(f"mode               : {live.mode}")
    for index, (sentence, outcome) in enumerate(zip(plan, outcomes), 1):
        print(f"case {index:<2}            : {outcome} :: {sentence}")
    settled = sum(1 for outcome in outcomes if outcome == "SETTLED_SUCCESS")
    print(f"settled success    : {settled} of {len(plan)}")
    return 0 if settled == len(plan) else 2


def _parse_observe_flags(values):
    """``<kpi>=<settle>:<window>:<validity>`` in ms (repeatable).

    The statistic and the minimum coverage keep the sitting's own defaults;
    what an operator actually needs to say on a terminal is how long to settle,
    how long to measure and how long the answer stays usable.
    """
    parsed = {}
    for raw in values or ():
        kind, separator, rest = str(raw).partition("=")
        parts = [item.strip() for item in rest.split(":") if item.strip()]
        if not separator or not kind.strip() or len(parts) != 3:
            raise SystemExit(
                f"expected <kpi>=<settleMs>:<windowMs>:<validityMs> and got {raw!r}")
        try:
            settle, window, validity = (int(float(item)) for item in parts)
        except ValueError:
            raise SystemExit(f"the three numbers of {raw!r} must be milliseconds") from None
        parsed[kind.strip()] = {"settleMs": settle, "windowMs": window,
                                "validityMs": validity}
    return parsed


def _parse_generation_flags(values):
    """``<role>=<maxTokens>:<thinkingBudgetTokens>`` (repeatable).

    What the model is *asked* to spend on one call -- never a cutoff (contract
    v2 section 7).  Roles are the prompt roles: ``target``, ``control``,
    ``trajectory``, ``monolith-form``, ``monolith-select``, ``basic-monolith``.
    """
    parsed = {}
    for raw in values or ():
        role, separator, rest = str(raw).partition("=")
        parts = [item.strip() for item in rest.split(":") if item.strip()]
        if not separator or not role.strip() or len(parts) != 2:
            raise SystemExit(
                f"expected <role>=<maxTokens>:<thinkingBudgetTokens> and got {raw!r}")
        try:
            max_tokens, thinking = (int(float(item)) for item in parts)
        except ValueError:
            raise SystemExit(f"the two numbers of {raw!r} must be token counts") from None
        parsed[role.strip()] = {"maxTokens": max_tokens,
                                "thinkingBudgetTokens": thinking}
    return parsed


def _parse_answers(value):
    """``--answers``: a path to a JSON file, or the JSON object itself.

    ``{"I2": {"steps": 2, "bound": 1.0}, "sitting": {"trialsK": 8}}`` -- the
    same shape the Cockpit's question form posts (contract v2 section 2.2).
    """
    import json as _json
    from pathlib import Path as _Path

    text = str(value or "").strip()
    if not text:
        return {}
    candidate = _Path(text)
    if candidate.is_file():
        text = candidate.read_text(encoding="utf-8")
    try:
        parsed = _json.loads(text)
    except ValueError as exc:
        raise SystemExit(f"--answers is neither a readable file nor JSON: {exc}") from None
    if not isinstance(parsed, dict):
        raise SystemExit("--answers must be a JSON object keyed by intent id")
    return {str(key): dict(value or {}) for key, value in parsed.items()}


def _ask_on_terminal(questions):
    """Ask the open questions on this terminal, when there is somebody there.

    Returns the answers in the same shape ``--answers`` carries.  A blank line
    leaves a question unanswered, which is how an operator says "I do not know"
    without the sitting pretending otherwise.
    """
    import json as _json

    answers = {}
    print("the sitting needs these answered before it can start:")
    for item in questions:
        intent_id = str(item.get("intentId") or "sitting")
        field = str(item.get("field") or "")
        try:
            reply = input(f"  [{intent_id}] {item.get('question', field)}\n  {field} = ").strip()
        except EOFError:
            reply = ""
        if not reply:
            continue
        try:
            value = _json.loads(reply)
        except ValueError:
            value = reply
        answers.setdefault(intent_id, {})[field] = value
    return answers


def _parse_axis_flags(values, *, cast):
    """``<ueId>:v1,v2`` (repeatable) -> ``{ueId: (v1, v2)}``."""
    parsed = {}
    for raw in values or ():
        ue, separator, rest = str(raw).partition(":")
        if not separator or not ue.strip() or not rest.strip():
            raise SystemExit(f"expected <ueId>:v1,v2 and got {raw!r}")
        parsed[ue.strip()] = tuple(cast(item) for item in rest.split(",") if item.strip())
    return parsed



def _parse_condition(values):
    """``--condition name=contention-boundary`` (repeatable) -> the request's condition.

    exp_metrics.md section 5 wants the condition, block and repetition stored with
    every episode so a plotted number can be traced back. Without this the
    aggregator groups every run under one unnamed condition, which is what it had
    been doing.
    """
    parsed = {}
    for raw in values or ():
        key, separator, value = str(raw).partition("=")
        if not separator or not key.strip():
            raise SystemExit(f"expected --condition key=value and got {raw!r}")
        parsed[key.strip()] = value.strip()
    return parsed


def _parse_boundaries(values):
    """``--boundary intent:the owner rewrote I2`` into the request's records.

    The kind is checked by :func:`tools.liveconsole.agent._boundary_record`, so
    a typo is refused there with the list of the three kinds rather than here
    with a second copy of it.
    """
    parsed = []
    for raw in values or ():
        kind, separator, detail = str(raw).partition(":")
        if not separator or not kind.strip():
            raise SystemExit(f"expected <kind>:<detail> and got {raw!r}")
        parsed.append({"kind": kind.strip(), "detail": detail.strip()})
    return tuple(parsed)


def _axis_exposure(args):
    """``--axes`` and ``--max-catalog``, only when the operator stated them.

    Omitted, the request's own defaults stand, so the CLI never restates a
    number the executor already owns.
    """
    stated = {}
    axes = getattr(args, "axes", None)
    if axes:
        stated["axes"] = tuple(item.strip() for item in str(axes).split(",")
                               if item.strip())
    ceiling = getattr(args, "max_catalog", None)
    if ceiling:
        stated["max_catalog_cardinality"] = int(ceiling)
    return stated


def _resolve_role_models(args):
    """Which LLM carries which role for this sitting, and under which method.

    Precedence per role: ``--<role>-agent-model`` flag > the Cockpit's handoff
    file ``<runs-root>/agent-role-models.json`` (what the operator chose in the
    GUI, schema ``agent-role-models/2.0.0``) > deterministic.  Nothing is fixed
    in code: the three agents are the operator's models, not ours.
    """
    from pathlib import Path as _Path
    from assurance.coordination import DEFAULT_ROLE_MODELS_FILENAME, load_role_models_file

    chosen = {}
    method = None
    runs_root = getattr(args, "runs_root", None)
    handoff = _Path(runs_root) / DEFAULT_ROLE_MODELS_FILENAME if runs_root else None
    if handoff is not None and handoff.is_file():
        stored = load_role_models_file(handoff)
        chosen.update(stored.to_record())
        method = stored.method
    for role in ("target", "control", "trajectory", "monolith"):
        # The monolith's CLI flag is --monolith-model, not --monolith-agent-model:
        # it names the one model that carries the whole method, not a role agent.
        # Reading only "<role>_agent_model" silently dropped it, so every monolith
        # arm ran deterministically while reporting method=basic-monolith.
        flag = getattr(args, f"{role}_agent_model", None)
        if flag is None and role == "monolith":
            flag = getattr(args, "monolith_model", None)
        if flag is not None:
            chosen[role] = (None if flag.strip().lower() in ("", "deterministic", "none")
                            else flag.strip())
    if getattr(args, "method", None):
        method = args.method
    return chosen, method


def _clean_text(value):
    return str(value).strip() if value is not None else ""


def _parse_intents_json(path, answers):
    if not path:
        return ()
    import json
    from pathlib import Path
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"--intents-json: {exc}") from None
    rows = data.get("intents") if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        raise ValueError("--intents-json must contain a non-empty JSON list of intents")
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or not isinstance(row.get("requirement"), dict):
            raise ValueError(f"--intents-json record {index} needs a requirement object")
        intent_id = row.get("intentId", f"I{index}")
        supplied = answers.get(intent_id, {})
        # owner 가 UE 가 아니라 셀 사업자인 요구는 `ueId` 가 없고 `requirement.scope` 가
        # `cell@<nci>` 다 (KPI_CELL_GOODPUT).  둘 다 없으면 관측 키를 만들 수 없으므로
        # 그때만 거절한다 -- `scope` 가 있으면 Intent.__post_init__ 의 `ue@` 자동 채움도
        # 일어나지 않는다.
        scoped = _clean_text(row["requirement"].get("scope")) or _clean_text(supplied.get("scope"))
        required = ("owner", "kpi", "op", "value", "unit") if scoped \
            else ("owner", "ueId", "kpi", "op", "value", "unit")
        for field in required:
            source = row if field in ("owner", "ueId") else row["requirement"]
            if supplied.get(field, source.get(field)) in (None, ""):
                raise ValueError(f"--intents-json [{intent_id}] missing required field: {field}")
        # Steps and bound use the existing intake question / --answers path.
    return tuple(rows)


def _agent_request(args):
    """The one request object both the hardware-free and the live root take."""
    from tools.liveconsole.agent import AgentRequest

    role_models, method = _resolve_role_models(args)
    thresholds = tuple(
        float(item) for item in (args.quality_thresholds or "0,0.25,0.5,1.0").split(",")
        if str(item).strip())
    from pathlib import Path as _Path

    settings = {}
    observe = _parse_observe_flags(getattr(args, "observe", None))
    if observe:
        settings["observation"] = observe
    generation = _parse_generation_flags(getattr(args, "generation", None))
    if generation:
        settings["generation"] = generation
    if getattr(args, "unselected_rule", None):
        settings["unselectedFunctionRule"] = str(args.unselected_rule)
    if getattr(args, "retain", None):
        settings["retain"] = int(args.retain)
    runs_root = getattr(args, "runs_root", None)
    if runs_root:
        calibration = _Path(runs_root) / "agent-latency-calibration.json"
        if calibration.is_file():
            settings["latencyCalibrationPath"] = str(calibration)
    answers = _parse_answers(getattr(args, "answers", None))
    intents = _parse_intents_json(getattr(args, "intents_json", None), answers)
    return AgentRequest(
        intents=intents,
        sentences=tuple(text for text in (args.cmd or ()) if str(text).strip()),
        method=method or "three-agent",
        role_models=role_models,
        budget_trials=int(args.budget),
        deadline_ms=(None if args.deadline_s is None else int(float(args.deadline_s) * 1000)),
        horizon_ms=(None if args.horizon_s is None else int(float(args.horizon_s) * 1000)),
        timing_mode=(getattr(args, "timing_mode", None) or "prepared"),
        formation_deadline_ms=(None if getattr(args, "formation_deadline_s", None) is None
                               else int(float(args.formation_deadline_s) * 1000)),
        decision_deadline_ms=(None if getattr(args, "decision_deadline_s", None) is None
                              else int(float(args.decision_deadline_s) * 1000)),
        boundaries=_parse_boundaries(getattr(args, "boundary", None)),
        condition=_parse_condition(getattr(args, "condition", None)),
        block=int(getattr(args, "block", 0) or 0),
        repetition=int(getattr(args, "repetition", 0) or 0),
        initial_measurement=bool(getattr(args, "initial_measurement", False)),
        quality_thresholds=thresholds,
        continue_after_relaxed_success=not bool(args.stop_after_relaxed_success),
        retain_best=not bool(args.no_retention),
        formal_reference_trial=bool(getattr(args, 'formal_reference_trial', False)),
        retain_on_improvement=bool(getattr(args, 'retain_on_improvement', False)),
        caps=_parse_axis_flags(args.cap_axis, cast=int),
        pf_weights=_parse_axis_flags(args.pf_axis, cast=float),
        mcs_bounds=_parse_axis_flags(getattr(args, "mcs_axis", None), cast=str),
        tx_attenuations=_parse_axis_flags(getattr(args, "atten_axis", None), cast=str),
        slice_quotas=_parse_axis_flags(getattr(args, "slice_axis", None), cast=str),
        **_axis_exposure(args),
        cells=tuple(int(item) for item in (args.cells or "").split(",") if item.strip()),
        withdraw_verified=bool(args.withdraw_verified_scope),
        settings=settings,
        answers=answers)



def _load_prepared_tc(path):
    """Rehydrate a T and a C formed by an earlier preparation run.

    Both carry their own schemaVersion and from_record, so this is the record
    the episode already writes -- nothing new is invented here.
    """
    if not path:
        return None
    import json as _json
    from pathlib import Path as _Path

    from assurance.coordination.tc import ControlCandidates, TargetContract

    document = _json.loads(_Path(path).read_text(encoding="utf-8"))
    if "T" not in document or "C" not in document:
        raise SystemExit("--prepared-tc needs a JSON object with a 'T' and a 'C'")
    return (TargetContract.from_record(document["T"]),
            ControlCandidates.from_record(document["C"]))


def run_headless_agent(args):
    """Run one Agent sitting: every ``--cmd`` sentence is one intent of one set.

    The ``--no-gui`` twin of the Cockpit's Agent mode, on either runtime:
    ``--hardware-free`` composes the seeded RAN emulator behind the same ports,
    ``--live`` composes the deployment.  Preparation forms ``T`` (the original
    requirement vector plus the authorized relaxations) and ``C`` (the joint
    executable configurations); one joint Kernel case is frozen over the axis
    values ``C`` uses; the Trajectory role then picks cell after cell of the
    ``T`` x ``C`` grid, and each trial's measured KPI vector is judged against
    every authorized target.  Exit code 0 only when ``T0`` -- the original
    requirements, no concession -- was met.
    """
    import tempfile
    from pathlib import Path

    from tools.liveconsole import LiveConsoleError
    from tools.liveconsole.agent import (
        MAX_CLARIFICATION_ROUNDS, ClarificationNeeded, build_agent_sitting,
        build_hardware_free_agent_sitting, write_agent_evidence)

    if not getattr(args, "intents_json", None) and not [text for text in (args.cmd or ()) if str(text).strip()]:
        print("refused before anything was submitted:\n  --agent needs at least "
              "one --cmd sentence or --intents-json file; each record is one intent of the set")
        return 3

    prepared = _load_prepared_tc(getattr(args, "prepared_tc", None))

    def compose(request):
        if args.live:
            return build_agent_sitting(args.profile, request, prepared=prepared)
        # The emulated deployment (its profile, its evidence directory) is
        # written under --runs-root when the operator named one, so the
        # episode is findable afterwards; otherwise into a temporary
        # directory whose path is printed with the episode.
        directory = (str(Path(args.runs_root) / "agent-hardware-free")
                     if args.runs_root else
                     tempfile.mkdtemp(prefix="agent-hardware-free-"))
        return build_hardware_free_agent_sitting(request, tmp_dir=directory)

    try:
        request = _agent_request(args)
        sitting = None
        # Contract v2 section 2.2's clarification loop: the checklist (and the
        # Target agent) may ask the operator for something nobody signed.  The
        # answers come from --answers, or from this terminal when somebody is
        # sitting at it; after MAX_CLARIFICATION_ROUNDS rounds the sitting
        # refuses to start rather than guessing at a bound.
        while sitting is None:
            try:
                sitting = compose(request)
            except ClarificationNeeded as asked:
                for item in asked.questions:
                    print(f"needs an answer     : [{item.get('intentId')}] "
                          f"{item.get('field')} -- {item.get('question')}")
                if asked.refused or request.clarification_round >= MAX_CLARIFICATION_ROUNDS:
                    print("refused before anything was submitted:\n  "
                          + str(asked).splitlines()[0])
                    return 3
                answers = {}
                if sys.stdin is not None and sys.stdin.isatty():
                    answers = _ask_on_terminal(asked.questions)
                if not answers:
                    print("refused before anything was submitted:\n  nobody answered "
                          "these; pass --answers '<json>' or run on a terminal")
                    return 3
                request = request.with_answers(answers)
    except (LiveConsoleError, ValueError) as exc:
        print(f"refused before anything was submitted:\n  {exc}")
        return 3

    preview = sitting.preview()
    print(f"mode               : {sitting.mode} (is_live={sitting.is_live})")
    print(f"method             : {sitting.method}")
    print(f"case               : {sitting.case_id}")
    print(f"timing mode        : {preview['timingMode']} (prep "
          f"{sitting.timing['prepMs']:.0f} ms"
          + (", charged to B" if sitting.is_cold_start else ", charged separately")
          + ")")
    print(f"board              : T {preview['prepared']['tHash'][:12]} x C "
          f"{preview['prepared']['cHash'][:12]}"
          + (" (injected)" if preview["prepared"]["injected"] else ""))
    for boundary in preview["boundaries"]:
        # A boundary the operator declared without a source time reads back as
        # "at start": the sitting resolved it to the preparation time, but
        # echoing that timestamp would claim the operator stated it.
        stated = boundary.get("statedAt", boundary["at"])
        print(f"boundary           : {boundary['kind']} at {stated or 'start'} "
              f":: {boundary['detail']}")
    for role, model in preview["roleModels"].items():
        print(f"role {role:<12} : {model or 'deterministic'}")
    print(f"intake             : {len(preview['intake'].get('missing', []))} open "
          f"questions after {preview['intake'].get('rounds', 0)} round(s)")
    for kind, rule in preview["measurementRules"].items():
        print(f"observe {kind:<11}: settle {rule['settleMs']} ms, window "
              f"{rule['windowMs']} ms, {rule['statistic']}, coverage >= "
              f"{rule['minCoverage']}, valid {rule['validityMs']} ms")
    for intent in preview["intents"]:
        requirement = intent["requirement"]
        print(f"intent {intent['intentId']:<10} : owner {intent['owner']} ueId={intent['ueId']} "
              f"{requirement['kpi']} {requirement['op']} {requirement['value']} "
              f"{requirement['unit']} (steps {requirement['steps']}, bound "
              f"{requirement['bound']})")
    for function in preview["functionCatalog"]:
        fields = ", ".join(f"{name}={values['values']}"
                           for name, values in function["policyFields"].items())
        print(f"function {function['functionId']:<8} : {function['xapp']} on "
              f"{function['scopes']} [{fields}]")
    print(f"unselected rule    : {preview['unselectedFunctionRule']}")
    for target in [preview["T"]["t0"]] + list(preview["T"]["alternatives"]):
        print(f"target {target['targetId']:<10} : cost {target.get('cost', 0.0):>6.2f} "
              f"{target['requirements']}")
    for control in preview["C"]["candidates"]:
        used = ", ".join(f"{item['functionId']}({item['scope']}) {item['policy']}"
                         for item in control.get("functions", ())) or "no function"
        print(f"control {control['controlId']:<9} : {used} -> "
              f"{control.get('predictedTarget') or 'no target'}")
    for item in preview["unmappedControls"]:
        print(f"unmapped control   : {item}")
    print(f"catalog            : {preview['catalogCardinality']} candidates, "
          f"budget {preview['budgetTrials']} trials")
    predictor = preview.get("predictor") or {}
    if predictor:
        print(f"predictor          : {predictor['calibration']['state']}, cells "
              f"{predictor['cells']}, +/- {predictor['relativeUncertainty']:.0%}")
    for item in preview["preconditions"]:
        print(f"precondition       : {item} (authorised by --withdraw-verified-scope)")
    confirmation = sitting.confirm()
    print(f"confirmation       : {confirmation}")

    def on_decision(decision):
        print(f"--- next cell      : aiming at {decision['targetId']} x "
              f"{decision['controlId']} [{decision['model']}"
              + (f" fallback: {decision['fallbackReason']}" if decision['fallbackReason'] else "")
              + (" STALE" if decision.get("staleAtArrival") else "")
              + f"] {decision['rationale']}")

    def on_trial(trial):
        print(f"trial {trial['trialIndex']:<3}          : {trial['controlId']} "
              f"{trial['configuration']} -> {trial['kpis']}")
        print(f"  kernel           : {trial['kernel']['terminalState']}/"
              f"{trial['kernel']['outcome']} rolledBack={trial['rolledBack']}")
        if trial["window"].get("unknownKpis"):
            print(f"  thin window      : {trial['window']['unknownKpis']} "
                  f"(coverage {trial['window'].get('coverage')})")
        for target_id, verdicts in trial["verdicts"].items():
            print(f"  column {target_id:<10}: {verdicts} -> "
                  f"{'SUCCESS' if trial['success'].get(target_id) else 'no'}")

    # Evidence is written on every exit path, not only the clean one.  When the
    # runner was killed mid-episode the whole Kernel event stream went with it,
    # so a lockdown could be seen in the printed trial rows and never explained:
    # its reason lives in the events.  The store already appends durably; this
    # only makes sure the file is materialised even when `run` raises or the
    # process is interrupted.
    try:
        summary = sitting.run(on_trial=on_trial, on_decision=on_decision)
    except BaseException:
        try:
            partial = write_agent_evidence(sitting)
            print(f"wrote partial evidence: {partial['episode']}")
            print(f"wrote partial events  : {partial['events']}")
        except Exception as exc:  # noqa: BLE001 - the original failure matters more
            print(f"partial evidence could not be written: {type(exc).__name__}: {exc}")
        raise
    written = write_agent_evidence(sitting)
    print("--- sitting summary ---")
    print(f"termination        : {summary['termination']} :: {summary['detail']}")
    print(f"kernel termination : {summary['kernelTermination']}")
    counted = sum(1 for trial in sitting.grid.trials
                  if sitting.trial_record(trial)["counted"])
    print(f"trials             : {summary['trials']} recorded, {counted} counted "
          "(the initial measurement is trial 0 and is not counted)")
    for event in summary.get("nonTrialEvents", ()):
        print(f"not a trial        : {event['kind']} at {event['elapsedMs']:.0f} ms "
              f":: {event['detail']}")
    for item in summary.get("reobservations", ()):
        print(f"re-observed        : {item['controlId'] or 'the applied configuration'} "
              f"at {item['windowEnd']} ({item['reason']})")
    for target_id, row in summary["grid"]["cells"].items():
        print(f"grid {target_id:<14}: {row}")
    if summary["bestAttained"]:
        best = summary["bestAttained"]
        print(f"best attained      : {best['targetId']} by {best['controlId']} "
              f"(D_max {best['concession']['max']:.3f}, D_mean {best['concession']['mean']:.3f})")
    retained = summary["retained"]
    print(f"retained           : {retained['controlId']} for {retained['targetId']} "
          f"qualified={retained['qualified']} :: {retained['detail']}")
    print(f"wrote episode       : {written['episode']}")
    print(f"wrote events        : {written['events']}")
    return 0 if summary["termination"] == "T0_SUCCESS" else 2
def _run_one_live_case(live, sentence, *, withdraw_verified,
                       write_run_evidence, refusal_type):
    """Draft, confirm and start one sentence; print the case.  Returns its label.

    The label is the trial state when the Kernel settled one, and the refusal
    reason otherwise -- so a sitting summary distinguishes "the Kernel decided
    against it" from "the console never got that far", which the six case
    terminations make a real difference.
    """
    session = live.session
    try:
        preview = session.draft(sentence)
    except Exception as exc:
        reason = getattr(exc, "reason", type(exc).__name__)
        print(f"utterance          : {sentence}")
        print(f"refused at draft   : {reason}: "
              f"{getattr(exc, 'detail', '') or exc}")
        return f"REFUSED_AT_DRAFT({reason})"

    print(f"objective          : {live.objective}")
    print(f"case               : {live.case_id}")
    print(f"utterance          : {sentence}")
    print(f"observed UE        : amfUeNgapId={live.identity.amf_ue_ngap_id} "
          f"on cell {live.identity.serving_nci} -> target {live.target_nci}")
    _print_supplementary(preview, session.view())
    for item in preview.preconditions:
        print(f"precondition       : {item}")
    if preview.preconditions and not withdraw_verified:
        # No operator at a screen, so nothing has covered this act.  The flag
        # is the headless stand-in for the click, and without it the case is
        # refused rather than the policy being withdrawn on nobody's authority.
        print("refused            : this case needs a precondition met and no "
              "operator confirmed it; re-run with --withdraw-verified-scope to "
              "authorise the withdrawal (the record is archived first)")
        return "REFUSED_PRECONDITION_NOT_AUTHORISED"

    try:
        instance = session.confirm(preview)
        view = session.start(instance)
    except Exception as exc:
        reason = getattr(exc, "reason", type(exc).__name__)
        print(f"refused at start   : {reason}: "
              f"{getattr(exc, 'detail', '') or exc}")
        return f"REFUSED_AT_START({reason})"

    axes, settlement = view.axes, view.settlement
    print(f"mode               : {view.mode} (is_live={view.is_live})")
    print(f"trial              : {view.trial_id}")
    print(f"stage              : {view.stage}")
    print(f"execution validity : {getattr(axes, 'execution_validity', None)}")
    print(f"measurement suff.  : {getattr(axes, 'measurement_sufficiency', None)}")
    print(f"predicate verdict  : {getattr(axes, 'predicate_verdict', None)}")
    for observed in getattr(view, "supplementary", ()):
        print(f"supplementary state: {observed.summary}")
    if settlement is not None:
        print(f"trial state        : {settlement.trial_state}")
        print(f"trial outcome      : {settlement.outcome}")
        print(f"evidence status    : {settlement.evidence_status}")
        print(f"case termination   : {settlement.case_termination}")
    if view.refusal:
        print(f"refusal            : {view.refusal} {view.refusal_detail}")
    print("a1 scope cleared   : "
          + (", ".join(f"{entry.get('policyId')}:"
                       f"{entry.get('action', entry.get('decision'))}"
                       for entry in live.scope_cleared)
             or "nothing occupied the scope"))
    for met in live.preflight.get("preconditionsMet", ()):
        for entry in met["withdrawn"]:
            print(f"precondition met   : {entry.get('policyId')} "
                  f"{entry.get('action')} ({entry.get('reason')})")
    policy_ids = live.policy_ids()
    print(f"a1 policy ids      : {', '.join(policy_ids) or 'none created'}")
    for row in live.policy_status():
        status = (row.get("status") or {}).get("aicStatus") or {}
        print(f"a1 policy status   : {row.get('policyId')} "
              f"present={row.get('present')} "
              f"episode={status.get('episodeState')} "
              f"readback={(status.get('readback') or {}).get('result')}")
    for call in live.transport_calls():
        print(f"a1 transport       : {call.get('method')} "
              f"{call.get('subject')} -> {call.get('outcome')} "
              f"{call.get('detail', '')}".rstrip())
    written = write_run_evidence(live, view)
    for label, path in sorted(written.items()):
        if path:
            print(f"wrote {label:<14}: {path}")
    return (settlement.trial_state if settlement is not None
            else f"NO_TERMINAL({view.refusal or 'poll deadline'})")


def print_xapp_round_trip(objective, *, same_ue_conflict=False):
    """Run and print the verified xApp coordination for one objective, hardware-free.

    Shows the Action Composition Coordinator's resolved plan (or its refusal of a
    contradictory pair) and the specialist xApp actuation over the in-memory
    config store, each write gated by a permit.  Imported lazily.
    """
    from tools.hfconsole.xapp import run_xapp_round_trip

    result = run_xapp_round_trip(objective, same_ue_conflict=same_ue_conflict)
    label = "CONFLICT (cap+priority on the same UE)" if same_ue_conflict else "composition"
    print(f"--- xApp coordination [{label}] : {objective} ---")
    if not result.accepted:
        print(f"  coordinator REFUSED: {result.refusal_type}")
        print(f"  {result.refusal}")
        return
    print(f"  apply_order   : {' -> '.join(result.apply_order)}")
    print(f"  rollback_order: {' -> '.join(result.rollback_order)}")
    for actuation in result.actuations:
        status = actuation.status.split(".")[-1]
        print(f"  {actuation.action_id:<20} {actuation.xapp_id:<22} {status}")


def main():
    parser = argparse.ArgumentParser(
        description="Research Operations Cockpit over the Assurance Kernel "
                    "runtime (the deployed entry point)")
    parser.add_argument("--profile", type=str, default=None,
                        help="Experiment profile JSON to load into the "
                             "Operator Console at startup")
    parser.add_argument("--runs-root", type=str, default=None,
                        help="Directory the Operator Console writes run "
                             "directories into (overrides the profile)")
    parser.add_argument("--llm-inventory", action="store_true",
                        help="Publish the advisory proposer inventory. It "
                             "probes the configured providers, and it informs "
                             "nothing the Kernel decides.")
    parser.add_argument("--llm", type=str, default=None,
                        help="Preselect a proposer by name (implies "
                             "--llm-inventory). Advisory only.")
    parser.add_argument("--hardware-free", action="store_true",
                        help="Attach a hardware-free Kernel submission session "
                             "(mock actuation adapter, no radio) so intents can "
                             "be run from the GUI or headless. Never Live, never "
                             "OTA evidence.")
    parser.add_argument("--live", action="store_true",
                        help="Attach a LIVE Kernel submission session over the "
                             "official O-RAN control path (R1 -> A1-P -> xApp -> "
                             "E2SM-RC). Requires --profile: the live profile is "
                             "the one authority this run addresses the "
                             "deployment through. One objective per session.")
    parser.add_argument("--withdraw-verified-scope", action="store_true",
                        help="With --live, also withdraw an A1-P policy whose "
                             "episode already reached APPLIED_VERIFIED but which "
                             "still occupies this UE's scope (the producer admits "
                             "one policy per scope, so a second case on the same "
                             "UE is otherwise refused HTTP 409). Every occupant "
                             "is archived verbatim before anything is withdrawn.")
    parser.add_argument("--objective", type=str, default=None,
                        help="Objective family. With --hardware-free it "
                             "defaults to UELevelTarget; with --live, omitting "
                             "it runs the Gate 3 UeCellSteeringPinToCell "
                             "regression case. Must be live-capable on this "
                             "deployment (tools.liveconsole.live_capable_families).")
    parser.add_argument("--utterance", type=str, default=None,
                        help="Operator intent sentence. Defaults to a sentence "
                             "generated for the objective; a typed one is "
                             "accepted only when the frozen grammar reads it "
                             "identically to the generated one.")
    parser.add_argument("--target-nci", type=int, default=None,
                        help="Target serving cell NCI (defaults to the "
                             "binding's target cell; with --live, to the one "
                             "advertised cell the UE is not on).")
    parser.add_argument("--amf-ue-ngap-id", type=int, default=None,
                        help="With --live, the UE this case addresses. May "
                             "equally be stated by the profile's "
                             "liveConsole.amfUeNgapId or by the ueId= scope of "
                             "the sentence; when more than one speaks they must "
                             "agree. Stated nowhere, the KPM stream must show "
                             "exactly one fresh UE or the run is refused - a "
                             "case never picks a UE by recency.")
    parser.add_argument("--controlled-amf-ue-ngap-id", type=int, default=None,
                        help="The heavy, non-target UE a SUPPLEMENTARY control "
                             "acts on (the UE DL PRB cap). Naming it is what "
                             "composes the cap beside the PRIMARY steering "
                             "action; naming none runs steering only. May "
                             "equally be stated by the profile's "
                             "liveConsole.controlledUe.amfUeNgapId or by the "
                             "controlledUeId= scope of the sentence, and when "
                             "more than one speaks they must agree. It must "
                             "differ from the objective UE and be fresh on the "
                             "KPM stream, or the run is refused by name.")
    parser.add_argument("--live-action", type=str, default=None,
                        choices=sorted(CAMPAIGN5_FAMILIES),
                        help="Run one Campaign 5 action over the air through "
                             "A1: rApp -> A1-P -> xApp -> E2SM-RC, held for "
                             "--hold-s and then reversed. Needs --profile and "
                             "the family's value flag (--cap / --pf-weight / "
                             "--mcs-min and --mcs-max / --tx-atten-db). "
                             "Equivalent to python3 -m tools.campaign5.live_run.")
    parser.add_argument("--replay", type=str, default=None,
                        help="Load recorded evidence and print it headless: a "
                             "LIVECONSOLE-*-run.json, an LO1 capture directory "
                             "or an experiment_results directory. Always a "
                             "REPLAY session -- the adapter takes the mode from "
                             "the source and nothing here overrides it.")
    parser.add_argument("--export", type=str, default=None,
                        help="Same as --replay, and then export the run into "
                             "--out as CSV and JSON with the mode banner every "
                             "file carries.")
    parser.add_argument("--out", type=str, default=None,
                        help="Destination directory for --export")
    parser.add_argument("--run-id", type=str, default=None,
                        help="Which recorded run to load when --replay or "
                             "--export names a directory holding several")
    for _family, _flags in sorted(CAMPAIGN5_VALUE_FLAGS.items()):
        for _flag, _leaf in _flags:
            parser.add_argument(_flag, type=int, default=None,
                                help=f"--live-action {_family}: {_leaf}")
    parser.add_argument("--hold-s", type=int, default=20,
                        help="With --live-action, seconds to hold the value "
                             "before reversing it")
    parser.add_argument("--cell-nci", type=int, default=None,
                        help="With --live-action, the cell a cell-scoped "
                             "family addresses")
    parser.add_argument("--gnb-id", type=str, default=None,
                        help="With --live-action, the gnbId a cell-scoped "
                             "family names")
    parser.add_argument("--baseline", type=int, default=None,
                        help="With --live-action, state the baseline value "
                             "when the counter cannot be read. Recorded as "
                             "stated, never as observed.")
    parser.add_argument("--evidence-dir", type=str, default=None,
                        help="With --live-action, where to write the run "
                             "document (defaults to the profile's evidence "
                             "directory)")
    parser.add_argument("--no-gui", action="store_true",
                        help="Run headless. With --hardware-free or --live, "
                             "drafts, confirms and starts one intent and prints "
                             "the terminal, opening no window.")
    parser.add_argument("--cmd", type=str, action="append", default=None,
                        help="Intent sentence for --no-gui (alias of "
                             "--utterance for the headless path). Repeatable "
                             "with --live: each sentence is one case, run in "
                             "order in one session, and the exit code is "
                             "non-zero unless every case settled SUCCESS.")
    parser.add_argument("--agent", action="store_true",
                        help="With --no-gui and either --hardware-free or "
                             "--live: run every --cmd sentence as one INTENT "
                             "SET in one joint Kernel case. The Target agent "
                             "forms T (the original requirements plus the "
                             "authorized relaxations), the Control agent forms "
                             "C (joint executable configurations), and the "
                             "Trajectory agent picks cell after cell of the "
                             "T x C grid. Any number of intents.")
    parser.add_argument("--method", type=str, default=None,
                        choices=("three-agent", "three-agent-coverage", "internal-monolith",
                                 "basic-monolith", "deterministic"),
                        help="With --agent, which coordination arm runs: the "
                             "three separate agents, one model forming T and C "
                             "then selecting, one model with no grid at all, or "
                             "no model (the deterministic rules). Default: the "
                             "Cockpit's choice, else three-agent.")
    parser.add_argument("--budget", type=int, default=16,
                        help="With --agent, the search budget in trials "
                             "(the joint case's frozen trial cap).")
    parser.add_argument("--condition", action="append", default=None,
                        help="With --agent, name the operating condition this episode ran "
                             "under as key=value (repeatable), e.g. "
                             "--condition name=contention-boundary. exp_metrics.md "
                             "section 5 stores it with the episode so results can be "
                             "reported per condition instead of pooled.")
    parser.add_argument("--block", type=int, default=0,
                        help="With --agent, the matched condition block this episode "
                             "belongs to (exp_metrics.md section 5).")
    parser.add_argument("--repetition", type=int, default=0,
                        help="With --agent, which repetition within the block this is "
                             "(exp_metrics.md section 5).")
    parser.add_argument("--prepared-tc", type=str, default=None,
                        help="With --agent, a JSON file holding a T and a C formed "
                             "earlier ({\"T\": <target contract>, \"C\": <control "
                             "candidates>}). The Target and Control calls are skipped "
                             "and that exact board is used, so only the Trajectory "
                             "decision runs live. The ids inside must already be the "
                             "ones this run addresses; a candidate naming an axis "
                             "value the frozen catalog does not admit is refused.")
    parser.add_argument("--intent-priority", type=str, default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--action-priority", type=str, default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--target-agent-model", type=str, default=None,
                        help="With --agent, the LLM (backend/model name) that carries "
                             "the Target role -- it forms T from the intents and the "
                             "owner's authorized relaxation limits. 'deterministic' "
                             "runs the rule instead. Default: the Cockpit's choice in "
                             "<runs-root>/agent-role-models.json, else deterministic.")
    parser.add_argument("--control-agent-model", type=str, default=None,
                        help="With --agent, the LLM that carries the Control role: it "
                             "forms C, the joint executable configurations "
                             "(see --target-agent-model).")
    parser.add_argument("--trajectory-agent-model", type=str, default=None,
                        help="With --agent, the LLM that carries the Trajectory role: "
                             "it picks the next (target, control) cell "
                             "(see --target-agent-model).")
    parser.add_argument("--monolith-model", type=str, default=None,
                        help="With --agent --method internal-monolith or "
                             "basic-monolith, the one LLM that carries the whole "
                             "method (see --target-agent-model).")
    parser.add_argument("--deadline-s", type=float, default=None,
                        help="With --agent, the wall-clock deadline B in seconds. "
                             "Never shown to a model; the executor alone enforces it.")
    parser.add_argument("--horizon-s", type=float, default=None,
                        help="With --agent, the observation horizon H in seconds "
                             "(see --deadline-s).")
    parser.add_argument("--formation-deadline-s", type=float, default=None,
                        help="With --agent, the wall-clock allowance from input "
                             "release to the first executable proposal. Covers "
                             "preparation and first selection for the prepared "
                             "methods and the first direct proposal for the basic "
                             "monolith. Executor-enforced; never shown to a model.")
    parser.add_argument("--decision-deadline-s", type=float, default=None,
                        help="With --agent, the allowance for each subsequent "
                             "decision, including tool use and one bounded "
                             "revision (see --formation-deadline-s).")
    parser.add_argument("--timing-mode", type=str, default=None,
                        choices=("prepared", "cold-start"),
                        help="With --agent, where the episode clock starts. "
                             "'prepared' (default): t0 is the live trigger after "
                             "preparation, so prepMs is charged separately and B "
                             "runs from the trigger. 'cold-start': t0 is the release "
                             "of the required inputs, before the Target and Control "
                             "calls, so preparation consumes B with no clock reset "
                             "and no synchronisation wait. The two modes use the "
                             "same B and are never mixed in one summary.")
    parser.add_argument("--boundary", type=str, action="append", default=None,
                        help="With --agent, one predeclared episode boundary as "
                             "'<kind>:<detail>', kind one of intent, policy, "
                             "exogenous. Normal fading, a control change and a new "
                             "approval under the same policy are not boundaries and "
                             "never reset a budget. Repeatable.")
    parser.add_argument("--retain-on-improvement", action="store_true",
                        help="Keep a trial's complete configuration when its P1 "
                             "result is equal to or better than the previous best, "
                             "and make it the next trial's recovery baseline, "
                             "instead of resetting every trial to C0 "
                             "(2026-09-23 decision).")
    parser.add_argument("--formal-reference-trial", action="store_true",
                        help="Count the common initial measurement as one of the "
                             "N_max formal trials and charge its real elapsed time, "
                             "instead of recording it at elapsed 0 and uncounted "
                             "(2026-09-23 decision).")
    parser.add_argument("--initial-measurement", action="store_true",
                        help="With --agent, measure the applied configuration once "
                             "before the search and record it as trial 0: counted "
                             "false, excluded from the recovery trial counts, and "
                             "scored against the targets authorized at t0, so a "
                             "success it supports is at trial 0 and elapsed 0 for "
                             "every method.")
    parser.add_argument("--intents-json", type=str, default=None,
                        help="Agent intent set JSON file: list or {intents: [...]}; combines with --cmd")
    parser.add_argument("--answers", type=str, default=None,
                        help="With --agent, the operator's answers to the "
                             "intake's questions, as JSON or a path to a JSON "
                             "file: '{\"I2\": {\"steps\": 2, \"bound\": 1.0}, "
                             "\"sitting\": {\"trialsK\": 8}}'. Without it, a "
                             "terminal session asks; otherwise the sitting "
                             "refuses to start rather than guess at a bound.")
    parser.add_argument("--observe", action="append", default=None,
                        metavar="KPI=SETTLE:WINDOW:VALIDITY",
                        help="With --agent, how one KPI kind is observed, in "
                             "milliseconds: how long to settle, how long a "
                             "window to measure, and how long the answer stays "
                             "usable (repeatable). Unstated kinds take the "
                             "deployment's own frozen hold, split in half.")
    parser.add_argument("--generation", action="append", default=None,
                        metavar="ROLE=MAXTOKENS:THINKING",
                        help="With --agent, what one role is asked to spend on "
                             "a call (repeatable). Roles: target, control, "
                             "trajectory, monolith-form, monolith-select, "
                             "basic-monolith. Never a cutoff: a slow answer is "
                             "charged as time, not truncated.")
    parser.add_argument("--unselected-rule", type=str, default=None,
                        choices=("baseline", "keep-current"),
                        help="With --agent, what happens to a function a "
                             "candidate did not select: 'baseline' deactivates "
                             "its axis (the default), 'keep-current' leaves the "
                             "value that is applied right now.")
    parser.add_argument("--retain", type=int, default=None,
                        help="With --agent, how many control candidates C keeps "
                             "(the construction policy's retain).")
    parser.add_argument("--quality-thresholds", type=str, default=None,
                        help="With --agent, the concession thresholds A recorded in "
                             "the episode, e.g. 0,0.25,0.5,1.0.")
    parser.add_argument("--stop-after-relaxed-success", action="store_true",
                        help="With --agent, stop as soon as any authorized target is "
                             "met. Default: keep searching for T0 until the budget, "
                             "the deadline or the catalog is spent.")
    parser.add_argument("--no-retention", action="store_true",
                        help="With --agent, do not deploy the best attained control at "
                             "the end of the sitting. Default: the sitting leaves the "
                             "deployment holding it (or records why it could not), "
                             "because searching is not deploying.")
    for _removed in ("--intent-agent-model", "--action-agent-model",
                     "--search-agent-model"):
        parser.add_argument(_removed, type=str, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--priority-policy", type=str, default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--intent-weight", type=str, action="append", default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--action-weight", type=str, action="append", default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--axes", type=str, default=None,
                        help="With --agent, which action-axis KINDS the sitting "
                             "exposes, comma separated, or the word 'all'. The "
                             "kinds are servingCell, dlPrbCap, pfWeight, "
                             "dlMcsBounds, txAttenuationDb, slicePrbQuota; "
                             "servingCell cannot be dropped. Default: "
                             "servingCell,dlPrbCap,pfWeight -- cap and the "
                             "scheduler weight on every intent UE. An exposed "
                             "kind covers every UE, every advertised cell or "
                             "every named S-NSSAI; the per-kind flags below "
                             "change one scope's ladder, not what is exposed.")
    parser.add_argument("--max-catalog", type=int, default=None,
                        help="With --agent, how many combinations one epoch may "
                             "freeze (default 4096). Over it the sitting refuses "
                             "by name before anything is written and says which "
                             "axis to narrow; the freeze costs about 2.5 ms and "
                             "2.5 kB per candidate.")
    parser.add_argument("--cap-axis", type=str, action="append", default=None,
                        help="With --agent, one UE's DL PRB cap ladder: "
                             "<ueId>:6,12 (repeatable). Uncapped is always a "
                             "value. Default ladder: 0,18,12,6.")
    parser.add_argument("--pf-axis", type=str, action="append", default=None,
                        help="With --agent, one UE's scheduler-weight ladder: "
                             "<ueId>:0.5,2.0 (repeatable). Default ladder: "
                             "0.5,1.0,2.0,4.0.")
    parser.add_argument("--mcs-axis", type=str, action="append", default=None,
                        help="With --agent --axes dlMcsBounds, one cell's DL MCS "
                             "bound ladder: <nci>:0..28,0..16 (repeatable). "
                             "Default ladder: 0..28,0..16,10..28.")
    parser.add_argument("--atten-axis", type=str, action="append", default=None,
                        help="With --agent --axes txAttenuationDb, one cell's TX "
                             "attenuation ladder in dB below full gain: "
                             "<nci>:0.0,6.0 (repeatable). Default ladder: "
                             "0.0,6.0,12.0.")
    parser.add_argument("--slice-axis", type=str, action="append", default=None,
                        help="With --agent --axes slicePrbQuota, one S-NSSAI's "
                             "RRM policy-ratio ladder as dedicated:min:max "
                             "percentages: <sst>:0:1:100,0:1:60 (repeatable). "
                             "Naming an S-NSSAI is also how a live deployment "
                             "says which slices exist. Default ladder: "
                             "0:1:100,0:1:60,0:1:30.")
    parser.add_argument("--cells", type=str, default=None,
                        help="With --agent, the cells a steering axis may "
                             "choose from, e.g. 12345678,87654321 (default: "
                             "every cell the deployment advertises).")
    parser.add_argument("--xapp", action="store_true",
                        help="With --no-gui, also run and print the verified "
                             "xApp coordination (composition + specialist "
                             "actuation) for the objective, hardware-free.")
    parser.add_argument("--xapp-conflict", action="store_true",
                        help="Show the coordinator refusing a cap+priority "
                             "conflict on the same UE (implies --xapp).")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.replay and args.export:
        parser.error("--replay prints a recorded run and --export prints and "
                     "then writes it; --export already does both")
    if args.export and not args.out:
        parser.error("--export needs --out: the destination directory the CSV "
                     "and JSON are written into")
    if args.out and not args.export:
        parser.error("--out is the destination for --export")
    if (args.replay or args.export) and (args.hardware_free or args.live):
        parser.error("a recorded source and a runtime are two different "
                     "session sources; a console holding both could start a "
                     "session whose evidence named one and whose data came "
                     "from the other")

    if args.live_action:
        if not args.profile:
            parser.error("--live-action needs --profile: the live profile is "
                         "the one authority a live run addresses a deployment "
                         "through, and the Campaign 5 families are served by "
                         "the action producer it names")
        if args.hardware_free or args.live or args.replay or args.export:
            parser.error("--live-action is its own runtime: it drives one "
                         "action over the official path and composes no "
                         "Cockpit session")
        return run_live_campaign5_action(args)

    if args.replay:
        return run_headless_replay(source=args.replay,
                                   runs_root=args.runs_root,
                                   run_id=args.run_id)
    if args.export:
        return run_headless_export(source=args.export, destination=args.out,
                                   runs_root=args.runs_root,
                                   run_id=args.run_id)

    if args.hardware_free and args.live:
        parser.error("--hardware-free and --live are two different runtimes "
                     "behind the Write Gateway; pick one")
    if args.live and not args.profile:
        parser.error("--live needs --profile: the live profile document is the "
                     "one authority a live run addresses a deployment through "
                     "(binding, integration values, capability, producer "
                     "database, KPM stream, R1 state and evidence directory). "
                     "See deployment/liveconsole-profile.json.")
    if args.withdraw_verified_scope and not (args.live and args.no_gui):
        parser.error("--withdraw-verified-scope is the headless stand-in for "
                     "an operator's click, so it means something only with "
                     "--live --no-gui. In the Cockpit the withdrawal is shown "
                     "in the contract preview as a named precondition and is "
                     "covered by Review & Confirm.")
    if args.amf_ue_ngap_id is not None and not args.live:
        parser.error("--amf-ue-ngap-id names a UE on the live KPM stream, so "
                     "it only means something with --live")
    if args.controlled_amf_ue_ngap_id is not None and not (
            args.live or args.hardware_free):
        parser.error("--controlled-amf-ue-ngap-id composes a supplementary "
                     "control beside a PRIMARY steering action, so it needs a "
                     "runtime: --hardware-free or --live")

    # The intent/action/search triad and its priority board were replaced by
    # Target/Control/Trajectory over the T x C grid.  A flag that named a role
    # which no longer exists is refused with the name that replaced it, rather
    # than silently doing nothing.
    for _flag, _hint in (
            ("intent_agent_model", "--target-agent-model (the Target role forms T)"),
            ("action_agent_model", "--control-agent-model (the Control role forms C)"),
            ("search_agent_model", "--trajectory-agent-model (the Trajectory role "
                                   "picks the next cell)"),
            ("intent_priority", "the intents themselves: write 'priority <n>' in a "
                                "sentence, which orders the owners' concession cost"),
            ("action_priority", "nothing: the Control role constructs C and the "
                                "Trajectory role orders the trials"),
            ("priority_policy", "the preference rule lexicographic(D_max, D_mean), "
                                "which is the one the episode records"),
            ("intent_weight", "the intents' own priorities"),
            ("action_weight", "the intents' own priorities")):
        if getattr(args, _flag, None):
            parser.error(f"--{_flag.replace('_', '-')} was removed with the "
                         f"intent/action/search roles; use {_hint}")
    if args.agent and not args.no_gui:
        parser.error("--agent is the headless sitting; in the Cockpit the Agent "
                     "runs from the Intent & Decision workspace. Add --no-gui.")
    if args.agent and not (args.hardware_free or args.live):
        parser.error("--agent needs a runtime: --hardware-free (the seeded RAN "
                     "emulator behind the same ports) or --live (the deployment "
                     "behind the Write Gateway)")

    # Headless paths: no window, no console composition at all.
    if args.no_gui:
        if not (args.hardware_free or args.live):
            parser.error("--no-gui needs a runtime: --hardware-free (mock "
                         "actuation adapter) or --live (the deployment behind "
                         "the Write Gateway)")
        if args.agent:
            return run_headless_agent(args)
        if args.live:
            return run_headless_live(
                profile_path=args.profile,
                objective=args.objective,
                utterances=list(args.cmd or ())
                or ([args.utterance] if args.utterance else []),
                target_nci=args.target_nci,
                amf_ue_ngap_id=args.amf_ue_ngap_id,
                controlled_amf_ue_ngap_id=args.controlled_amf_ue_ngap_id,
                withdraw_verified=args.withdraw_verified_scope,
            )
        objective = args.objective or DEFAULT_HARDWARE_FREE_OBJECTIVE
        if args.cmd and len(args.cmd) > 1:
            parser.error("--hardware-free --no-gui runs one intent; repeating "
                         "--cmd is a --live sitting")
        code = run_headless_hardware_free(
            objective=objective,
            utterance=(args.cmd[0] if args.cmd else args.utterance),
            target_nci=args.target_nci,
            controlled_amf_ue_ngap_id=args.controlled_amf_ue_ngap_id,
        )
        if args.xapp or args.xapp_conflict:
            print_xapp_round_trip(objective)
            if args.xapp_conflict:
                print_xapp_round_trip(objective, same_ue_conflict=True)
        return code

    llm_manager = None
    if args.llm_inventory or args.llm:
        llm_manager = build_llm_inventory(args.llm)

    console = build_console(profile_path=args.profile,
                            runs_root=args.runs_root,
                            llm_manager=llm_manager)

    # A hardware-free session is attached *after* the default composition, so
    # `build_console` (and its reachability test) still yields a Disconnected
    # console with no session. Imported lazily for the same reason.
    if args.hardware_free:
        from tools.hfconsole import attach_hardware_free_session

        objective = args.objective or DEFAULT_HARDWARE_FREE_OBJECTIVE
        kwargs = {} if args.target_nci is None else {"target_nci": args.target_nci}
        if args.controlled_amf_ue_ngap_id is not None:
            kwargs["controlled_amf_ue_ngap_id"] = args.controlled_amf_ue_ngap_id
        attach_hardware_free_session(
            console, objective, utterance=args.utterance, **kwargs)
        logger.info("attached hardware-free session: objective=%s (mock adapter, "
                    "not Live)", objective)

    # The live session is attached the same way and for the same reason: the
    # console keeps opening Disconnected, and which deployment sits behind the
    # Write Gateway is a composition fact rather than something a console picks.
    if args.live:
        from tools.liveconsole import LiveConsoleError, attach_live_session

        try:
            composed = attach_live_session(
                console, args.profile, objective=args.objective,
                utterance=args.utterance, target_nci=args.target_nci,
                amf_ue_ngap_id=args.amf_ue_ngap_id,
                controlled_amf_ue_ngap_id=args.controlled_amf_ue_ngap_id)
        except LiveConsoleError as exc:
            # Refuse here rather than opening a console that would claim Live
            # with nothing behind it.
            print(f"refused before anything was submitted:\n  {exc}")
            return 3
        logger.info("attached %s session: objective=%s case=%s target cell %s",
                    composed.mode, composed.objective, composed.case_id,
                    composed.target_nci)
        logger.info("type this, or your own sentence that reads the same: %s",
                    composed.utterance)

    run_operator_console(console)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
