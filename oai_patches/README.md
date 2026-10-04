# OpenAirInterface integration patches

These patches adapt OAI control, readback, and UE/gNB behavior for the testbed
used in the paper. They are applied in the dependency order below rather than
alphabetically. Check the source revision and the context of each diff before
applying it.

Upstream-derived portions retain their upstream license; see
[third-party notices](../THIRD_PARTY_NOTICES.md). Patch files identify additions
and removals relative to upstream source and are the authors' modification record.

## Control and observation

The patches are based on OAI revision
`d8433e8d7fd6b44dc8ab38554caa9bd8eeeb44d7`. The core control stack is applied in
this order:

1. `d2_actionspace_runtime_knobs.w30.patch`
2. `e2sm_rc_style2_action6_slice_prb.patch`
3. `e2sm_rc_style2_ue_actions.w30.patch`
4. `e2sm_rc_power_action104.patch`
5. Matching KPM readback/counter patches
6. `e2sm_rc_style2_action6_slice_enforcement.patch`

The OAI source tree itself is obtained from upstream.

| Patch group | Purpose |
|---|---|
| `d2_actionspace_runtime_knobs*` | Scheduler controls and diagnostic interface |
| `e2sm_rc_style2_action6_slice*` | Slice PRB-ratio actuation and enforcement |
| `e2sm_rc_style2_ue_actions*` | Deployment-local MCS bounds, UE PRB cap and PF priority |
| `e2sm_rc_power_action104.patch` | Deployment-local cell power/attenuation action |
| `kpm_*`, `e2_kpm_*` | Configuration readback, UE attribution and counter continuity |
| `nr_38prb_15mhz_samplerate.patch` | 38-PRB, 15-MHz operating configuration support |
| `ci_stats_json.patch` | Structured diagnostic observability |

Original and `.w30` variants are alternate applicability targets. Do not apply
both; in particular, duplicating the PF-weight modification would change the
scheduler semantics. A successful control acknowledgement still requires the
separate configuration readback and KPI assessment performed by the framework.

## UE/gNB robustness and RF-specific changes

The remaining patches cover handover/reestablishment, PDCP/RLC continuity,
synchronization, buffer bounds, power control and device-specific streaming.
Their suitability depends on the radio hardware; review them before use on a
different deployment.

Some later patches contain cumulative context. In particular,
`nr_ue_tx_digital_gain_plus4db.w30.patch` includes a diff against an already-modified
UE source; treat it as a source comparison, not an independent additive step.
The resynchronization consensus patch supersedes the fixed-anchor approach;
the PUCCH power-control patch supersedes the temporary fixed-amplitude experiment.
RF gain and attenuation values must match the intended radio setup.

Patches are applied individually; no apply-all script is provided.
