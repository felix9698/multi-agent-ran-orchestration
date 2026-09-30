# Connecting model backends

Model selection is separate from the RAN deployment. You can use commercial APIs,
your own inference server, or the offline mock. The artifact does not require the
authors' local server, network, or subscription proxy.

## Available adapters

| Backend | Configuration | Optional Python dependency |
|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY`, `AIC_CLAUDE_MODEL_ID` | `anthropic` |
| OpenAI | `OPENAI_API_KEY` | `openai` |
| Google Gemini | `GOOGLE_API_KEY` | `google-generativeai` |
| OpenAI-compatible server | `LITELLM_BASE_URL`, optional `LITELLM_API_KEY` | `openai` |
| Ollama native API | Service available at `http://localhost:11434` | `requests` |
| Offline scripted responses | `mock:agent` | None beyond core requirements |

Install only the SDK for your chosen service. Backend support means a client
adapter exists; it is not a claim that every model offers equivalent structured
output quality or has been evaluated in the paper. Provider model names and
availability can change. The fixed aliases and current mappings are in
[`decision/llm_backend.py`](../decision/llm_backend.py); use your service's actual
model identifier rather than assuming the GUI alias identifies the served model.

Export the relevant settings in the shell used to launch the application.
Alternatively, copy [`env.example`](../env.example) to `.env`, uncomment and set
the entries you need, then load your own trusted file before launching:

```bash
set -a
. ./.env
set +a
```

Current entry points read environment variables; they do not automatically load
`.env`. Shell quoting is required for values containing special characters.
Do not put credentials in committed profiles, prompts, screenshots, or results.
OpenAI-compatible server models are discovered from its model-list endpoint and
exposed as `local:<model-id>`. “Local” identifies this adapter route, not a promise
that the service is on the same machine or that requests are free.

## Select each role

In the Cockpit's **Main → Agent models** area, select the method and the Target,
Control and Trajectory models. **Use for the next sitting** applies the selection
to a new episode; it does not relabel records already collected.

For the matched experiment runner, role assignments can be supplied explicitly:

```bash
ROLE_EXAMPLE="$(mktemp -d /tmp/roles.XXXXXX)"
python tools/campaign5/run_agent_experiments.py \
  --scenario three-ue --methods three-agent \
  --models 'target=mock:agent,control=mock:agent,trajectory=mock:agent' \
  --repetitions 1 --budget 2 --out "$ROLE_EXAMPLE"
```

Replace the mock names with configured backend names for model-driven experiments.
Radio emulation does **not** imply offline inference: choosing an API-backed model
makes real model requests and may incur charges. Model discovery and calibration
may also contact the selected service.

The main entry exposes `--target-agent-model`, `--control-agent-model`,
`--trajectory-agent-model`, and `--monolith-model`; see `python main.py --help` for
the surrounding experiment options. Keep the requested model, provider-reported
served model, generation options, prompts and returned usage with each experiment.
Unknown served-model identity remains unknown rather than being inferred from an
alias.
