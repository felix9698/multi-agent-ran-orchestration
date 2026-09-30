"""The legacy episode runtime, handed to a console from outside it.

The Operator Console used to reach the preserved three-stage Coordinator by
importing ``oran.rapp.gui_entry`` from four of its own modules.  That made the
old decision runtime reachable from the default entry point: loading a profile
and pressing Bind was enough to construct ``IntentCoordinator`` and, behind it,
the patched-OAI direct-control executor.

The cutover inverts that dependency.  The console now holds a *port* -
``OperatorConsole.legacy_episode`` - and this module is the only implementation
of it.  A console that was handed nothing refuses every legacy-episode action
by name, and it refuses structurally rather than by checking a flag: the
console package imports none of this, so there is nothing for it to call.

This is the same shape as :meth:`OperatorConsole.attach_kernel_session`.  Which
runtime is behind a console is a composition fact, and composition is done by
whoever is entitled to choose - here, the env-gated
``tools.legacy.coordinator_console``, or a test that states in its own name
that it is exercising the preserved path.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping


class LegacyEpisodeSupport:
    """Everything the console needs to run one preserved-Coordinator episode.

    Four operations, each a thin binding to the module that already owns it.
    Nothing is re-implemented here: a second copy of the episode entry, the
    integration loader or either validator would be a second opinion about what
    the preserved runtime does, which is the one thing this package must never
    introduce.

    ``coordinator`` is the running :class:`IntentCoordinator` instance, when the
    entry that built this port has one.  The console never calls a decision
    method on it - it reads the FSM transition snapshot for the topology row,
    and it routes a proposer switch through the coordinator's own audit rather
    than around it.  ``None`` is normal: a port without one still runs
    episodes, and the topology row then says no adapter is attached instead of
    claiming one is.
    """

    #: Stated so a reader of an evidence bundle can tell at a glance which
    #: runtime produced it.
    runtime = "legacy-coordinator"

    def __init__(self, coordinator: Any = None) -> None:
        self.coordinator = coordinator

    def load_integration(self, path: Any, **kwargs: Any) -> Any:
        """One integration-values document -> a bound legacy deployment."""
        from oran.rapp.gui_entry import LiveIntegration

        return LiveIntegration.load(path, **kwargs)

    def episode_runner(self) -> Callable[..., Mapping[str, Any]]:
        """The canonical episode entry, ``oran.rapp.gui_entry.run_gui_once``.

        Returned rather than wrapped: a caller that wants to *count* entries
        wraps this, and the wrapper still has to call the real entry to return
        a result.
        """
        from oran.rapp.gui_entry import run_gui_once

        return run_gui_once

    def validate_policy(self, policy: Mapping[str, Any],
                        schema_name: str) -> None:
        """Re-check a dispatched policy against the frozen contract schema."""
        from oran.rapp.contract_support import validate

        validate(dict(policy), schema_name)

    def validate_intent_parse(self, parsed: Mapping[str, Any]) -> Mapping[str, Any]:
        """The strict validator an episode uses, for the preview only."""
        from coordinator.schema import validate_intent_parse

        return validate_intent_parse(dict(parsed))


def legacy_episode_support(coordinator: Any = None) -> LegacyEpisodeSupport:
    """Construct the port.  Importing this module is the opt-in."""
    return LegacyEpisodeSupport(coordinator)


__all__ = ["LegacyEpisodeSupport", "legacy_episode_support"]
