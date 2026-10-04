# Experiment methods and artifacts

This page maps the methods in the paper to the implementation and describes the
retained OTA procedure and analysis tools. For a local demonstration, use the
[hardware-free quickcheck](reproducibility.md).

## Method mapping

| Paper label | Code identifier | Decision organization |
|---|---|---|
| 3A | `three-agent` | Target and Control agents construct T and C independently and in parallel; the Trajectory agent selects each live trial. |
| RM | `internal-monolith` | One model call constructs both T and C; later calls use the same selection instructions and input format as 3A. |
| MA | `basic-monolith` | One model generates a complete joint-control configuration at each live trial. |

RM denotes the **role-merged agent**. All three methods use the same model
endpoint, generation settings, authorized service levels, pre-episode evidence,
available RAN actions, validation, execution, and recovery rules. The prompts
are in [`orc_task/SINGLE_CALL.md`](../orc_task/SINGLE_CALL.md).

The code also contains a no-model `rule-greedy` selector, used as a sanity
baseline inside the `basic-monolith` execution path, and campaign-runner plan
entries for other model backends (for example `three-agent-qwen3`). These are
not part of the comparison reported in the paper. A model alias alone does not
identify model weights, quantization, serving configuration, or version.
See [agents.py](../assurance/coordination/agents.py) and
[rule_greedy.py](../assurance/coordination/rule_greedy.py).

## Reported evaluation settings

| Setting | Value |
|---|---|
| Campaign | 17 blocks with varying method order; 56 recorded episodes (17 for 3A, 20 for RM, 19 for MA), including episodes from restarted blocks |
| Trials | Measured reference as trial 0, then at most 6 additional live trials |
| Time budget | 480 s from input release, including candidate preparation, model calls, execution, and recovery |
| KPI window | 15 s per trial |
| Requirements | Goodput of UE1-3, UE2 deadline success (256-byte tagged UDP echo at 5 Hz, 35-ms deadline), and gNB1 transmit attenuation; UE2 goodput is protected |
| Authorized targets | 21 levels for each of the four adjustable requirements: 21⁴ targets |
| Model | `claude-5.5-sonnet` through the Anthropic Messages API |
| Response limits | 4,000 tokens per candidate-construction call; 2,000 tokens per online selection or generation call |

These are the settings of the reported campaign; default command-line
arguments of individual tools are not a substitute for them.

## Shared execution and evaluation

All methods use the shared [agent sitting](../tools/liveconsole/agent.py),
[Assurance Kernel](../assurance/kernel/), [Write Gateway](../assurance/gateway/),
and [measurement collector](../assurance/collector/). Agent proposals do not
bypass deterministic admission, measurement validation, or recovery. Protected
requirements are preserved, and missing evidence is kept separate from an
observed requirement failure.

The [concession evaluator](../assurance/coordination/concession.py) evaluates
each valid observation against all authorized targets, independent of a
method's prepared target list. The preference cost is
`p = 25 kE + 13 k1 + 7 k2 + 3 k3`, where `kE`, `k1`, `k2`, and `k3` index the
adjustments of gNB1 transmit attenuation, UE1 goodput, UE2 deadline success,
and UE3 goodput (0 = original requirement, 20 = authorized limit). A lower cost
is preferred, and each episode record stores the evaluation rule it used.
Transmit attenuation is a control setting, not a measurement of electrical
power consumption.

## OTA procedure

The campaign runs methods in blocks and records interruptions and attempt
outcomes. The execution chain is
`ops/run_blocks_campaign_v54r.sh` → `ops/run_case.sh` → `ops/run_formal_v3.sh`
→ `ops/run_episode.py` → `atomic_formal_run_guarded.py` → the shared sitting,
all under [experiment_results/ota-20260911/](../experiment_results/ota-20260911/).

`ops/reference_dl.py` measures the per-block reference traffic, and
`ops/make_v47_corpus.py` constructs and hashes the block's intent corpus from
those measurements. `ops/keeper.py` and related helpers maintain the testbed
between trials. These tools drive live equipment.

The runner takes a campaign plan and deployment settings; its default plan names
`three-agent`, `internal-monolith`, and `basic-monolith`. A new OTA run requires
prepared OAI gNB/UE and core deployments, the RIC and xApps, measurement
sources, validated live profiles, model serving for model-based methods, and
authorized equipment access. [OAI patches](../oai_patches/) and
[xApp source](../src/xapp/) contain the RAN-side modifications; external
software and hardware are obtained separately.

## Analysis scripts

[`ops/analysis_v53/`](../experiment_results/ota-20260911/ops/analysis_v53/)
contains the export and analysis scripts used on the campaign records.

| Script | Reads and writes |
|---|---|
| `common.py` | Discovers completed episodes from campaign logs and loads echo records. |
| `export_history.py` | Episode, trial, and model-call CSVs and raw trial JSONL. |
| `export_board_detail.py` | Model calls, T/C definitions, preparation, and recovery CSVs. |
| `export_profile.py` | Prompt and call, gateway, observation-window, and UE-event CSVs. |
| `v53_report.py` | Deadline sweeps and comparative summaries across the three methods. |
| `rejudge_deadline.py` | Deadline reassessment of recorded echo data. |
| `replay_fair_prompts.py` | Sends stored prompts to a model again; this makes model-service calls. |

The three `export_*.py` scripts use only the standard library and work on files.
Run them from `experiment_results/ota-20260911/`, set `AIC_ANALYSIS_CAMPAIGNS`
to the campaigns to analyze, and use fresh output directories.
`common.boards()` includes episodes with a recorded termination marker and
episode file; reconcile this selection with interrupted blocks and excluded
attempts before computing summary statistics.

The [data availability section](reproducibility.md#data-availability-and-limits)
lists the campaign records these scripts read.
