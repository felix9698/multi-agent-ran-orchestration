# Reproducibility

This repository accompanies **Automating Multi-Intent RAN Orchestration through Multi-Agent Live Resolution**.
It publishes the implementation, experiment tooling, and available supporting material.
It does **not** contain the complete frozen dataset needed to regenerate the final paper's OTA results.

## What can be checked

The hardware-free example below exercises the shared agent sitting, Assurance Kernel,
Write Gateway interfaces, evaluator, and result writer using an emulated RAN and mock model.
It requires no radio equipment or model API credentials.
Its measurements, model responses, latency, and token counts are simulated.
It is an implementation demonstration, **not a reproduction of the paper's OTA measurements**.

The historical `experiments.runner` synthetic/emulated pipeline is a separate legacy workflow.
Its outputs must not be substituted for the final 3A/RM/MA comparison.
See [Experiment methods and artifacts](experiments.md) for the code-method mapping.

## Environment

Use Python 3.12 and the repository's [requirements](../requirements.txt).
Run these commands from the repository root:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Dependency installation may need network access; the mock example itself does not need it.
The schema libraries validate the vendored O-RAN contracts locally.
Matplotlib renders the demonstration figures using a noninteractive backend.
Optional live-model SDKs are not needed for this example.

## Hardware-free quickcheck

Start in a fresh shell without inherited live-campaign `AIC_*` overrides.
Choose a new, short output path so records cannot be mixed and generated credential
references stay within the existing profile validator's 128-character limit:

```sh
PAPER_QUICKCHECK="$(mktemp -d /tmp/pqc.XXXXXX)"
PYTHONDONTWRITEBYTECODE=1 MPLBACKEND=Agg python tools/campaign5/run_agent_experiments.py \
  --scenario three-ue --condition contention-boundary \
  --methods three-agent --model mock:agent \
  --repetitions 1 --budget 2 --seed 0 --out "$PAPER_QUICKCHECK"
```

This explicitly uses `mock:agent` and omits `--live`.
Real model names can contact a model service even when the radio is emulated.
Two trials are a small smoke-check budget, not a paper experiment setting.
An unresolved episode is a valid recorded outcome; the check does not promise intent success.

Inspect the generated `manifest.json`, `episodes/`, `runtime/`, `metrics.json`, and `figures/`.
The manifest records the invocation's scenarios, settings, model choices, and episode IDs.
Recompute the demonstration metrics from its saved episodes:

```sh
python -m experiments.agent_metrics "$PAPER_QUICKCHECK/episodes" \
  --out "$PAPER_QUICKCHECK/recomputed-metrics.json"
```

Keep the original manifest when comparing runs.
Matching the seed does not imply byte-identical timestamps or episode identifiers.
Mock figure generation is not evidence that any paper figure has been reproduced.

## Focused offline tests

The following selection covers the rule-based method, prompt views, weighted evaluation,
event replay, and saved-episode metrics without selecting a real model:

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest \
  tests.assurance.test_rule_greedy \
  tests.assurance.test_v52_view \
  tests.assurance.test_v53_view \
  tests.assurance.test_v53_fair_prompts \
  tests.assurance.test_v51_weighted_evaluator.Weighted \
  tests.assurance.test_kern_replay \
  tests.test_agent_metrics
```

Some broader tests require historical pilot files not present in this publication.
For example, `tests/test_v5_design.py` calls the corpus generator using its original base corpus.
The selected `Weighted` class above avoids that unavailable corpus dependency.
These unavailable inputs are not replaced with synthetic paper results.

## Data availability and limits

The published `experiment_results/ota-20260911/` tree contains experiment code,
not the final campaign's raw board records or exported numerical tables.
The analysis tools expect material absent from this snapshot:

- `ops/overnight/<campaign>.plan.json`, progress records, and per-attempt logs;
- per-block reference measurements and the exact campaign environment settings;
- the original `pilot38-v4.5-L10/` corpus and generated block corpora with hashes;
- `formal38guarded-*/live-sitting.stdout` and `evidence/AGENT-*-episode.json`;
- corresponding event streams, stored prompts, and raw goodput/echo measurements.

Existing test fixtures and any retained earlier integration witnesses are not this dataset.
No final-paper figure/table mapping or complete final campaign selection is claimed here.
Full numerical reproduction additionally needs a checksum-bound dataset manifest,
inclusion/exclusion decisions, software/model revisions, and the actual plotting commands.
The available analysis scripts are described in [experiments.md](experiments.md).

Do not execute live campaign, reference-measurement, maintenance, or preparation scripts
as part of this quickcheck. OTA execution requires a separately prepared deployment,
authorized equipment access, live profiles, and explicit operational supervision.
