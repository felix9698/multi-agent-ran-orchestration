"""Confirmed, reproducible hardware-free batch experiments.

The package deliberately consumes a Kernel-case executor instead of making a
second decision path.  A live deployment supplies its adapter-backed executor;
the included deterministic mock is only for Replay/hardware-free runs.
"""

from .plan import BatchCase, BatchPlan, Budget, IntentProfile, RetryPolicy, Windows
from .runner import (
    BatchRunResult,
    BatchRunner,
    CaseExecution,
    DeterministicMockKernelExecutor,
    KernelCaseExecutor,
)


def build_demo_plan():
    """Lazily expose the checked-in hardware-free demonstration plan."""
    from .demo import build_demo_plan as _build_demo_plan
    return _build_demo_plan()


def run_demo(runs_root):
    """Lazily execute the hardware-free demonstration plan."""
    from .demo import run_demo as _run_demo
    return _run_demo(runs_root)

__all__ = [
    "BatchCase", "BatchPlan", "Budget", "IntentProfile", "RetryPolicy", "Windows",
    "BatchRunResult", "BatchRunner", "CaseExecution", "DeterministicMockKernelExecutor",
    "KernelCaseExecutor",
    "build_demo_plan", "run_demo",
]
