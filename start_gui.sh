#!/bin/bash
# Start the Agentic Intent Coordinator GUI.
#
# This starts the deployed runtime: the Research Operations Cockpit
# (gui/operator) over the Assurance Kernel, where every write leaves through
# the Write Gateway.  It cannot construct the pre-Kernel Coordinator.
#
# That runtime is preserved as reference material and has its own entry, which
# still requires an explicit operator approval because it reaches direct OAI
# control paths the O-RAN boundary forbids the Operator Console:
#
#     AIC_LEGACY_CONSOLE_APPROVED=1 python3 -m tools.legacy.coordinator_console
#     AIC_LEGACY_CONSOLE_APPROVED=1 python3 -m tools.legacy.coordinator_console --no-gui
#     AIC_LEGACY_CONSOLE_APPROVED=1 python3 -m tools.legacy.coordinator_console --cmd "<intent text>"
cd "$(dirname "$0")"

# LLM backend config is read from the environment and from ./.env, which is
# loaded by config.py at startup. Precedence: real shell exports override .env.
# Warn only if nothing is configured at all (no exports and no .env file).
if [ -z "$ANTHROPIC_API_KEY" ] && [ -z "$OPENAI_API_KEY" ] && [ -z "$LITELLM_BASE_URL" ] && [ ! -f .env ]; then
    echo "Warning: No LLM backend configured."
    echo "  Set ANTHROPIC_API_KEY / OPENAI_API_KEY, or LITELLM_BASE_URL (+ LITELLM_API_KEY)"
    echo "  for the local LLM server. See env.example (copy it to .env)."
fi

# Start GUI
python3 main.py "$@"
