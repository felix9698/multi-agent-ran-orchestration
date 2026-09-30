# Control profiles and implementation boundaries

An objective name identifies a project contract. O-RAN specifies interfaces and
service models; it does not standardize the framework's natural-language intents
or agent role names.

| Function | Scope | Mapping in this implementation |
|---|---|---|
| Traffic steering | UE | E2SM-RC Style 3 / Action 1 |
| Downlink PRB cap | UE | Deployment-specific Style 2 / Action 102 |
| Scheduling priority | UE | Deployment-specific Style 2 / Action 103 |
| Downlink MCS bounds | Cell | Deployment-specific Style 2 / Action 101 |
| Transmit attenuation | Cell | Deployment-specific Style 2 / Action 104 |
| PRB quota | Slice | Style 2 / Action 6 with the installed slice-actuator mapping |

The advertised RAN-function definitions, field scopes and bounds determine what a
deployment can execute. An agent's proposal does not create a new RAN capability.
The action catalog and composition policy check compatibility and ordering, and
the gateway validates the action and its recovery path before applying it.

Key sources:

- [`assurance/actions/catalog.py`](../assurance/actions/catalog.py) and
  [`composition_policy.py`](../assurance/actions/composition_policy.py)
- [`assurance/xapps/`](../assurance/xapps/)
- [`oran/campaign5/`](../oran/campaign5/) and
  [`oran/slice_actuator/`](../oran/slice_actuator/)
- [`contracts/oran-aic/campaign5/`](../contracts/oran-aic/campaign5/)
- [`oai_patches/`](../oai_patches/)

The forward path uses R1, the Non-RT RIC, A1-P and xApps before FlexRIC/E2SM-RC.
Applied-configuration readback and KPI observation windows serve different
purposes: command acceptance alone is not a successful service outcome.
KPM, O1 and user-plane measurements retain their distinct scopes and timing.

Installed profile support, readback coverage and the controls actually exercised
in a particular campaign must be reported separately. A hardware-free fixture
showing all dimensions is not evidence that all combinations were measured OTA.
