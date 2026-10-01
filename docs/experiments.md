# Experiment methods and artifacts

This page maps the paper's method names to the existing implementation.
Code identifiers are preserved for compatibility; older names remain in historical tools.
For a safe local demonstration, use the [hardware-free quickcheck](reproducibility.md).

## Method mapping

| Paper label | Existing implementation selection | Decision organization |
|---|---|---|
| 3A | `three-agent` | Target and Control agents form validated T and C; Trajectory selects trials. |
| RM | `internal-monolith` | One model call forms both T and C; later calls use the same selection instructions and format as 3A. |
| MA | `basic-monolith` with the selected language-model backend | One model proposes a complete configuration directly. |

RM means **role-merged agent**, not a rule-based method. The submitted manuscript
compares `three-agent`, `internal-monolith`, and `basic-monolith` using the same
language-model endpoint and generation settings. The additional `rule-greedy`
selector is a no-model baseline retained in the code, not the manuscript's RM.
It is a model-selection sentinel inside the basic-monolith execution path,
not a general `--methods rule-greedy` option for the hardware-free matrix runner.
See [agents.py](../assurance/coordination/agents.py) and
[rule_greedy.py](../assurance/coordination/rule_greedy.py).

The retained [v5.4r campaign runner](../experiment_results/ota-20260911/ops/run_blocks_campaign_v54r.sh)
also recognizes plan entries `three-agent-qwen3`, `basic-monolith-qwen3`, and `rule-greedy`.
The `-qwen3` entries select the underlying method and `local:qwen3` model alias;
the rule entry selects `basic-monolith` with `AIC_MONOLITH_MODEL=rule-greedy`.
These extra branches do not define the submitted manuscript's comparison.
An alias alone does not identify model weights, quantization, serving configuration, or version.

## Submitted evaluation settings

The manuscript reports 56 episodes across 17 blocks: 17 for 3A, 20 for RM, and
19 for MA, including episodes from restarted blocks. A measured reference is
trial 0; resolution allows six additional trials within 480 seconds from input
release. Each trial has a 15-second KPI window. All three methods use
`claude-5.5-sonnet` through Anthropic Messages API, with 4,000-token construction
and 2,000-token online-selection/generation response limits. These are reported
campaign settings, not a claim that default arguments reconstruct the original
dataset or endpoint.

## Shared execution and evaluation

All methods use the shared [agent sitting](../tools/liveconsole/agent.py),
[Assurance Kernel](../assurance/kernel/), [Write Gateway](../assurance/gateway/),
and [measurement collector](../assurance/collector/).
Agent proposals do not bypass deterministic admission, measurement validation, or recovery.
The coordination code preserves protected requirements and separates missing evidence
from observed predicate failure.

The [concession evaluator](../assurance/coordination/concession.py) implements
the common evaluation independent of a method's selected target list.
The v5.1 weighted rule uses `p = 25 kE + 13 k1 + 7 k2 + 3 k3`, with lower better;
its four adjustable coordinates have 20 steps, while the UE2 goodput floor is protected.
The exact evaluation rule must be read from the episode/corpus records;
older lexicographic variants also remain in the code.
Transmit attenuation is a control/measurement proxy, not a measurement of electrical power consumption.

## OTA procedure retained in the source

The campaign runs methods in blocks and records interruptions and attempt outcomes.
Its chain is `run_blocks_campaign_v54r.sh` → `run_case.sh` → `run_formal_v3.sh`
→ `run_episode.py` → `atomic_formal_run_guarded.py` → the shared sitting.
These files remain under [experiment_results/ota-20260911/](../experiment_results/ota-20260911/).

`ops/reference_dl.py` measures block reference traffic; `ops/make_v47_corpus.py`
constructs and hashes the block's intent corpus from those measurements.
`ops/keeper.py` and related operational helpers maintain the laboratory between trials.
These are live-capable tools, not offline smoke commands.

The runner expects a pre-existing final campaign plan and deployment settings.
Its automatic fallback plan names `three-agent`, `internal-monolith`, and
`basic-monolith`, which correspond to the manuscript's three method families.
Those identifiers alone do not reconstruct the original campaign: the frozen
plan, deployment settings, input corpus, and recorded episodes are also required.
Likewise, default trial/time limits are not a frozen statement of the paper's final settings.
Prompt versions, fair-prompt selection, energy-step settings, model identity, timing mode,
and campaign inclusion rules need the original manifest.

A new OTA run requires prepared OAI gNB/UE and core deployments, RIC/xApps,
measurement sources, validated live profiles, model serving for model-based methods,
and independent equipment authorization. The repository does not provision these automatically.
[OAI patches](../oai_patches/) and [xApp source](../src/xapp/) describe implementation pieces;
they do not supply all external software, hardware, or deployment state.

## Analysis scripts

The [analysis_v53 directory](../experiment_results/ota-20260911/ops/analysis_v53/)
contains the retained exports. Names reflect development history.

| Script | Reads/writes | Important boundary |
|---|---|---|
| `common.py` | Discovers completed boards from campaign logs and loads echo records. | Requires missing final board/log inputs. |
| `export_history.py` | Board, trial, model-call CSVs and raw trial JSONL. | Exports saved observations; does not regenerate them. |
| `export_board_detail.py` | Calls, T/C definitions, preparation, rollback CSVs. | Requires episode records and event streams. |
| `export_profile.py` | Prompt/call, gateway, observation-window, UE-event CSVs. | Requires original prompts and execution evidence. |
| `v53_report.py` | Historical deadline sweeps and comparative summaries. | Assumes populated three-method data; not the complete final figure recipe. |
| `rejudge_deadline.py` | Historical deadline reassessment. | Hard-coded historical campaign/path assumptions. |
| `replay_fair_prompts.py` | Sends stored prompts to a model again. | Radio-free but **not offline**; makes model-service calls. |

The three `export_*.py` scripts use the standard library and are file-only.
Their working directory must be `experiment_results/ota-20260911/`.
Set `AIC_ANALYSIS_CAMPAIGNS` explicitly from the original dataset manifest;
the loader defaults to earlier v5.3 campaigns, not an asserted final-paper selection.
Use fresh output directories. A zero-board export is not a reproduced result.

`common.boards()` includes only boards with a recorded termination marker and episode file.
Before numerical comparison, reconcile this selection with failed formations,
interrupted blocks, excluded attempts, and the paper's denominator.
No unavailable records should be filled with zeros, mock samples, or inferred successes.

The [data-availability statement](reproducibility.md#data-availability-and-limits)
lists the missing originals. Without them, these scripts document the extraction procedure
but cannot independently regenerate the final paper's numerical results.
