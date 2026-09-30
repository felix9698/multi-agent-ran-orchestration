"""Hardware-free console composition root.

This package wires the **Research Operations Cockpit** (``gui/operator``) to a
**hardware-free** Assurance Kernel submission session, so an operator can draft,
confirm and start an intent from the GUI (or headless) with no radio attached.

Why this is a separate composition root, not part of ``gui`` or ``assurance``
----------------------------------------------------------------------------
The Cockpit is a reader: ``gui`` imports ``assurance`` and never a transport,
an actuator or a composition root (``tests/gui/test_cockpit_acceptance.py``).
``assurance`` never imports ``gui`` (``tests/assurance/test_seams.py``).  A
composition root that must touch *both* — build a ``VerticalPath`` from
``assurance`` and wrap it in the ``gui``-side ``KernelSubmissionSession`` — can
therefore live only *outside* both, exactly as the live composition root
``tools/g3ota/run_ota.py`` does.  ``tools/hfconsole`` is its hardware-free
sibling.

What "hardware-free" means here, precisely
------------------------------------------
The session is built over
:class:`assurance.gateway.mock_adapter.MockActuationAdapter` through the same
``assurance.live.objective_runtime.build_live_objective_runtime`` wiring the
live path uses — only the ``adapter_override`` is swapped in.  The Kernel, the
Write Gateway, the epoch freeze, the trial state machine and the evidence rules
are the real ones; the radio is off.  The session mode is ``MODE_MOCK`` and is
never badged ``LIVE``: a hardware-free run is **not** OTA evidence, and the
registry's evidence level is untouched by anything here (see
``docs/architecture/FINAL-ARCHITECTURE.md`` section 6.2 and
``assurance/objectives/registry.py::validate_record``).
"""

from tools.hfconsole.build import (
    HardwareFreeCap,
    HardwareFreeConsoleError,
    HardwareFreeSession,
    attach_hardware_free_session,
    build_hardware_free_cap,
    build_hardware_free_runtime,
    build_hardware_free_session,
)

__all__ = [
    "HardwareFreeCap",
    "HardwareFreeConsoleError",
    "HardwareFreeSession",
    "attach_hardware_free_session",
    "build_hardware_free_cap",
    "build_hardware_free_runtime",
    "build_hardware_free_session",
]

from tools.hfconsole.agent_env import (
    CONDITIONS,
    EmulatedActuationAdapter,
    EmulatedClock,
    EmulatedKpiObserver,
    EmulatedKpmStream,
    EmulatedRan,
    HardwareFreeAgentRuntime,
    HermeticDeployment,
    KpiObserver,
    build_hardware_free_agent_runtime,
    three_ue_topology,
)

__all__ += [
    "CONDITIONS", "EmulatedActuationAdapter", "EmulatedClock",
    "EmulatedKpiObserver", "EmulatedKpmStream", "EmulatedRan",
    "HardwareFreeAgentRuntime", "HermeticDeployment", "KpiObserver",
    "build_hardware_free_agent_runtime", "three_ue_topology",
]
