#!/usr/bin/env python3
"""The preserved pre-Kernel Coordinator console.  **History-only.**

This is *not* the deployed runtime.  It is the research console the published
S0-S6 experiments were driven from, kept whole so those results stay
reproducible and their evidence stays readable.  It constructs
``coordinator.intent_coordinator.IntentCoordinator`` and, with no explicit
executor, the patched-OAI telnet direct-control executor behind it - which is
exactly why the deployed entry point (``main.py``) can no longer reach it.

The deployed runtime is::

    python3 main.py        # Operator Console -> Assurance Kernel -> Write Gateway

Running *this* is an operator act, declared in the environment, never a code
default - the same pattern as the P0-19 ``AIC_ENV_DRIVER_APPROVED`` gate::

    AIC_LEGACY_CONSOLE_APPROVED=1 python3 -m tools.legacy.coordinator_console
    AIC_LEGACY_CONSOLE_APPROVED=1 python3 -m tools.legacy.coordinator_console --no-gui
    AIC_LEGACY_CONSOLE_APPROVED=1 python3 -m tools.legacy.coordinator_console \
        --cmd "Throughput 10Mbps 이상 유지해"

The offline paper pipeline is a separate research tool and is unaffected by
this move: ``python3 -m experiments.runner --mode synthetic --trials 5``.
"""

import argparse
import logging
import os
import sys
import threading

# Setup path: this module is run as ``python3 -m tools.legacy.coordinator_console``
# from the repository root, and also imported by the preserved evidence scripts.
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from config import get_config
from coordinator.intent_coordinator import IntentCoordinator

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("LegacyConsole")


#: The legacy console reaches direct hardware control paths that the O-RAN
#: boundary forbids the Operator Console.  It stays available as reference
#: material, but enabling it is an operator act, never a code default - the same
#: pattern as the P0-19 AIC_ENV_DRIVER_APPROVED gate.
LEGACY_CONSOLE_ENV = "AIC_LEGACY_CONSOLE_APPROVED"

LEGACY_CONSOLE_REFUSAL = (
    "The legacy research console is not enabled.\n"
    "  It is preserved as reference material and reaches direct OAI control\n"
    "  paths that the O-RAN Operator Console deliberately does not have.\n"
    f"  To use it anyway, set {LEGACY_CONSOLE_ENV}=1 explicitly:\n"
    f"    {LEGACY_CONSOLE_ENV}=1 python3 -m tools.legacy.coordinator_console\n"
    "  The deployed console is the Operator Console over the Assurance\n"
    "  Kernel runtime: python3 main.py"
)


def legacy_console_approved() -> bool:
    """True only when the operator explicitly approved the legacy console."""
    return os.environ.get(LEGACY_CONSOLE_ENV, "").strip() == "1"


def run_with_operator_gui(coordinator: IntentCoordinator, *,
                          profile_path: "str | None" = None,
                          runs_root: "str | None" = None):
    """Run the Operator Console *over the preserved Coordinator*.

    The Phase-B configuration, kept for replaying that campaign: the console is
    handed the legacy episode port so Bind and Submit reach
    ``oran.rapp.gui_entry`` again.  A console built by ``main.py`` is handed
    nothing and therefore cannot.
    """
    from gui.operator.app import OperatorConsole
    from gui.operator.session.profile import ExperimentProfile

    from .episode_support import legacy_episode_support

    profile = None
    if profile_path:
        profile = ExperimentProfile.load(profile_path)

    console = OperatorConsole(profile=profile, runs_root=runs_root,
                              llm_manager=getattr(coordinator, "llm_manager",
                                                  None),
                              legacy_episode=legacy_episode_support(coordinator))
    coordinator.start()
    try:
        console.create_window()
        console.restore_gui_state()
        console.run()
    finally:
        coordinator.stop()


def run_with_gui(coordinator: IntentCoordinator):
    """Run the legacy research console (opt-in, see LEGACY_CONSOLE_ENV)."""
    from gui.dashboard import IntentCoordinatorGUI

    gui = IntentCoordinatorGUI(
        title="Agentic Intent Coordinator - Multi-UE Demo", profile="legacy")

    # Connect coordinator to GUI
    coordinator.gui = gui

    # Setup callbacks
    def on_intent_submit(intent_text: str):
        def process():
            result = coordinator.process_intent(intent_text)
            gui.log(f"Result: {result.get('resolution', 'unknown')}")
        threading.Thread(target=process, daemon=True).start()

    def on_command(cmd: str):
        if cmd == "status":
            stats = coordinator.get_stats()
            gui.log(f"Stats: {stats}")
        elif cmd == "reset":
            coordinator.intent_manager.clear()
            gui.log("Intents cleared")

    def on_llm_change(backend_name: str):
        """Handle LLM backend change from GUI dropdown (name string;
        may be a cloud name or a local model like 'local:qwen3-coder')."""
        if coordinator.set_llm_backend(backend_name):
            gui.log(f"Switched to {backend_name}")
        else:
            gui.log(f"Backend not available: {backend_name}")

    def on_llm_refresh():
        """Re-discover local models from the LiteLLM proxy and repopulate the dropdown."""
        names = coordinator.refresh_llm_backends()
        current = coordinator.llm_manager.active_backend_name()
        gui.set_llm_options(names, current)
        gui.update_llm_status(names, current)
        gui.log(f"LLM backends refreshed ({len(names)} available)")

    gui.on_intent_submit = on_intent_submit
    gui.on_command = on_command
    gui.on_llm_change = on_llm_change
    gui.on_llm_refresh = on_llm_refresh

    # Start coordinator
    coordinator.start()

    # Update GUI with available LLM backends
    def update_llm_status():
        import time
        time.sleep(0.5)  # Wait for GUI to initialize
        available = coordinator.llm_manager.get_available_names()
        current_name = coordinator.llm_manager.active_backend_name()
        gui.update_llm_status(available, current_name)
        gui.set_llm_options(available, current_name)

    threading.Thread(target=update_llm_status, daemon=True).start()

    # Run GUI (blocks)
    try:
        gui.run()
    finally:
        coordinator.stop()


def run_cli(coordinator: IntentCoordinator):
    """Run in CLI mode"""
    coordinator.start()

    print("\n" + "=" * 60)
    print("Agentic Intent Coordinator - CLI Mode")
    print("=" * 60)
    print("\nCommands:")
    print("  <intent>  - Submit intent (e.g., 'Throughput 10Mbps 이상')")
    print("  status    - Show status")
    print("  intents   - List active intents")
    print("  llm <name>- Switch LLM backend")
    print("  quit      - Exit")
    print()

    try:
        while True:
            try:
                user_input = input("[Intent] > ").strip()

                if not user_input:
                    continue

                if user_input.lower() in ["quit", "q", "exit"]:
                    break

                if user_input.lower() == "status":
                    stats = coordinator.get_stats()
                    print(f"\nStats:")
                    for k, v in stats.items():
                        print(f"  {k}: {v}")
                    print()
                    continue

                if user_input.lower() == "intents":
                    intents = coordinator.intent_manager.get_all()
                    print(f"\nActive Intents ({len(intents)}):")
                    for i in intents:
                        print(f"  - {i}")
                    print()
                    continue

                if user_input.lower().startswith("llm "):
                    backend_name = user_input[4:].strip()
                    if coordinator.set_llm_backend(backend_name):
                        print(f"Switched to {backend_name}")
                    else:
                        print(f"Backend not available: {backend_name}")
                        print(f"Available: {coordinator.llm_manager.get_available_names()}")
                    continue

                # Process as intent
                print("Processing...")
                result = coordinator.process_intent(user_input)
                print(f"\nResult:")
                print(f"  Success: {result.get('success', False)}")
                print(f"  Resolution: {result.get('resolution', 'unknown')}")
                print(f"  Latency: {result.get('latency_ms', 0):.0f}ms")
                if result.get('error'):
                    print(f"  Error: {result['error']}")
                print()

            except KeyboardInterrupt:
                print("\n")
                break
            except EOFError:
                break

    finally:
        coordinator.stop()
        print("Goodbye!")


def run_single_command(coordinator: IntentCoordinator, cmd: str):
    """Run single command"""
    coordinator.start()

    try:
        print(f"Processing: {cmd}")
        result = coordinator.process_intent(cmd)
        print(f"\nResult:")
        for k, v in result.items():
            print(f"  {k}: {v}")
    finally:
        coordinator.stop()


def run_experiment(coordinator: IntentCoordinator, trials: int):
    """Run the synthetic experiment and print the CURRENT report.

    (Gate B [A4]: this used to print the pre-P-series metric keys -
    total_samples / overall_dual_satisfaction / by_method - which no longer
    exist, so the console showed zeros/empty summaries while the run had
    actually completed.)
    """
    from experiments.runner import ExperimentRunner

    runner = ExperimentRunner(coordinator)
    res = runner.run(mode="synthetic", trials=trials, make_figures=False)
    print("\n" + res["report"])
    print(f"\n{res['n_steps']} step records, {res['n_episodes']} episodes")
    print(f"Metrics: {res['metrics_path']}")


def main():
    parser = argparse.ArgumentParser(
        description="The preserved pre-Kernel Coordinator console "
                    "(history-only; the deployed entry point is main.py)")
    parser.add_argument("--console", choices=["operator", "legacy"],
                        default="legacy",
                        help="Which GUI to start. 'legacy' is the preserved "
                             "research console (default here). 'operator' is "
                             "the Operator Console driven over the preserved "
                             "Coordinator, as in the Phase-B campaign.")
    parser.add_argument("--profile", type=str, default=None,
                        help="Experiment profile JSON to load into the "
                             "Operator Console at startup")
    parser.add_argument("--runs-root", type=str, default=None,
                        help="Directory the Operator Console writes run "
                             "directories into (overrides the profile)")
    parser.add_argument("--no-gui", action="store_true",
                        help="Run in CLI mode without GUI")
    parser.add_argument("--cmd", "-c", type=str, default=None,
                        help="Execute single command and exit")
    parser.add_argument("--experiment", action="store_true",
                        help="Run experiment mode")
    parser.add_argument("--trials", type=int, default=5,
                        help="Number of experiment trials")
    parser.add_argument("--llm", type=str, default=None,
                        help="LLM backend name (e.g. claude-sonnet, gpt-4o, "
                             "local:qwen3-coder). Validated at runtime against "
                             "available backends.")
    parser.add_argument("--calibration-mode", type=str, default="online",
                        choices=["fixed", "phase", "online"],
                        help="Calibration mode for θ* and N_max")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")

    args = parser.parse_args()

    # The gate is checked before anything is constructed, so a refused launch
    # leaves nothing running behind it - and leaves no coordinator built
    # either.  Every mode below reaches the preserved decision runtime, so the
    # gate covers all of them rather than only the legacy window.
    if not legacy_console_approved():
        print(LEGACY_CONSOLE_REFUSAL)
        return 2

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    config = get_config()
    coordinator = IntentCoordinator(config=config)

    # Gate B [A4]: --calibration-mode used to be parsed but never applied
    # (the runtime calibrator silently stayed "online")
    coordinator.calibrator.mode = args.calibration_mode

    if args.llm:
        if not coordinator.set_llm_backend(args.llm):
            print(f"Backend not available: {args.llm}")
            print(f"Available: {coordinator.llm_manager.get_available_names()}")

    # Run mode.  The headless paths are checked first: --cmd, --experiment and
    # --no-gui must keep working with no toolkit import at all.
    if args.cmd:
        run_single_command(coordinator, args.cmd)
    elif args.experiment:
        run_experiment(coordinator, args.trials)
    elif args.no_gui:
        run_cli(coordinator)
    elif args.console == "operator":
        run_with_operator_gui(coordinator, profile_path=args.profile,
                              runs_root=args.runs_root)
    else:
        run_with_gui(coordinator)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
