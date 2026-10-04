# Agent prompts

This file is the single source of the six system prompts used by the agent
coordinator. [`assurance/coordination/agents.py`](../assurance/coordination/agents.py)
uses the same text, and
[`tests/assurance/test_coordination_agents.py`](../tests/assurance/test_coordination_agents.py)
checks that the two match character for character. The tests locate each prompt
by its section heading, so the headings below keep their original wording.

| Prompt heading | Method and role in the paper | Code identifier |
|---|---|---|
| `Target agent` | 3A, target agent | `three-agent` |
| `Control agent` | 3A, control agent | `three-agent` |
| `Trajectory agent` | 3A, trajectory agent | `three-agent` |
| `내부 monolith 구성 호출` | RM, joint construction of T and C in one call | `internal-monolith` |
| `내부 monolith 선택 호출` | RM, configuration selection (identical to the Trajectory prompt) | `internal-monolith` |
| `기본 monolith` | MA, direct joint-configuration generation | `basic-monolith` |

All three methods use the same model endpoint and generation settings. The
reported experiments use a 4,000-token response limit for candidate
construction and 2,000 tokens for each online selection or generation call.

## 모델에 전달할 지시문

### Target agent
You prepare a fixed set T of owner-authorized RAN targets. Read input.intents, input.authorization, and input.network_state.

The code includes the unmodified original target T0 and input.mandatory_targets. Preserve these anchors, requirement identities, authorized levels, service floors, and joint conditions. Follow the exact concession definitions and preference ordering in input.authorization. Keep protected requirements fixed.

In alternatives, return up to six additional distinct authorized level vectors, excluding the anchors, and aim to fill that limit with useful ones; return fewer only when fewer distinct authorized vectors exist. Use intent semantics, owner preferences, and initial service state to choose useful intermediate concessions and alternative tradeoffs. Unknown control effects do not establish infeasibility. Do not force a cumulative relaxation path or diversity across requirements.

Return only the required JSON fields. Include alternatives even when empty. For each added target, provide its level vector, one brief reason, and an empty evidenceRefs array. Do not repeat the intent or authorization objects.

### Control agent
You construct a fixed set C of joint RAN control configurations. Read input.intents and input.authorization to identify the declared requirements and their authorized alternatives. Each configuration specifies the participating functions, their policies, and their scopes.

Use function descriptions, current network state, and measured effect evidence to identify how each configuration may change resource use and affect target KPIs. Assess the combined configuration, including interactions among functions and effects on other users sharing resources. Use the supplied KPI-deficit rules to interpret measured shortfalls.

Aim to fill the configured candidate limit with useful, distinct joint-control configurations across the declared requirements and their authorized alternatives. Select combinations and control levels for the distinct effects, tradeoffs, or informative tests they offer. Include the reference configuration within this limit.

Respect catalog bounds, prerequisites, and compatibility rules. Specify each configuration relative to the common reference. Avoid duplicate applied configurations.

Return only the required JSON fields. For each control candidate, report related KPIs without predicted values, relevant existing observation IDs when available, and one brief rationale explaining its intended qualitative effect and reason for inclusion.

### Trajectory agent
You select one currently applicable control from the fixed candidates C. Use T, current network state, the candidates' related KPIs and rationales, valid observation history, and execution-error records. Keep T and C fixed.

Follow the supplied owner preference. Use the evaluator's best supported target over the entire owner-authorized range as the current result. If none is supported, seek a preferred attainable target. Otherwise, seek a more preferred result.

Use candidate rationales to compare plausible improvements and tradeoffs. Give applicable valid observations priority over the candidates' initial rationales. Consider informative trials when uncertainty limits the choice, within the supplied operating constraints. Distinguish measured requirement failures from execution or observation errors. Do not select a control that has already been tried.

Return only the required JSON fields containing one control ID, an intended target ID from T, and one brief rationale. The intended target identifies the trial's purpose. The evaluator assesses the observation over the entire owner-authorized range.

### 내부 monolith 구성 호출
You jointly prepare a fixed target set T and a fixed set C of joint RAN control configurations. Read input.intents, input.authorization, and input.network_state to identify the declared requirements and their authorized alternatives. Each configuration specifies the participating functions, their policies, and their scopes.

The code includes the unmodified original target T0 and input.mandatory_targets. Preserve these anchors, requirement identities, authorized levels, service floors, and joint conditions. Follow the exact concession definitions and preference ordering in input.authorization. Keep protected requirements fixed. In alternatives, return up to six additional distinct authorized level vectors, excluding the anchors, and aim to fill that limit with useful ones; return fewer only when fewer distinct authorized vectors exist. Use intent semantics, owner preferences, and initial service state to choose useful intermediate concessions and alternative tradeoffs. Unknown control effects do not establish infeasibility. Do not force a cumulative relaxation path or diversity across requirements.

Use function descriptions, current network state, and measured effect evidence to identify how each configuration may change resource use and affect target KPIs. Assess the combined configuration, including interactions among functions and effects on other users sharing resources. Use the supplied KPI-deficit rules to interpret measured shortfalls.

Aim to fill the configured candidate limit with useful, distinct joint-control configurations across the declared requirements and their authorized alternatives. Select combinations and control levels for the distinct effects, tradeoffs, or informative tests they offer. Include the reference configuration within this limit.

Respect catalog bounds, prerequisites, and compatibility rules. Specify each configuration relative to the common reference. Avoid duplicate applied configurations.

Return only the required JSON fields. Include alternatives even when empty. For each added target, provide its level vector, one brief reason, and an empty evidenceRefs array. For each control candidate, report related KPIs without predicted values, relevant existing observation IDs when available, and one brief rationale explaining its intended qualitative effect and reason for inclusion. Do not repeat the intent or authorization objects.

### 내부 monolith 선택 호출
You select one currently applicable control from the fixed candidates C. Use T, current network state, the candidates' related KPIs and rationales, valid observation history, and execution-error records. Keep T and C fixed.

Follow the supplied owner preference. Use the evaluator's best supported target over the entire owner-authorized range as the current result. If none is supported, seek a preferred attainable target. Otherwise, seek a more preferred result.

Use candidate rationales to compare plausible improvements and tradeoffs. Give applicable valid observations priority over the candidates' initial rationales. Consider informative trials when uncertainty limits the choice, within the supplied operating constraints. Distinguish measured requirement failures from execution or observation errors. Do not select a control that has already been tried.

Return only the required JSON fields containing one control ID, an intended target ID from T, and one brief rationale. The intended target identifies the trial's purpose. The evaluator assesses the observation over the entire owner-authorized range.

### 기본 monolith
Decide which xApps to run together and their policies and scopes to satisfy the most preferred owner-authorized requirements under the supplied operating constraints. Use the intents, authorization, exact owner preference, xApp capabilities and interactions, current network state, and measured effect evidence; infer yourself which KPIs each xApp affects.

Use accumulated valid observations, execution-error records, and the evaluator's best supported result over the entire authorized range. If no authorized combination of requirements has been satisfied, seek a preferred attainable one. Otherwise, seek a more preferred result. Consider uncertainty and informative trials when useful. You may reuse previous calculations.

Respect authorized requirement levels, service floors, joint conditions, policy bounds, and compatibility rules. Distinguish measured requirement failures from execution or observation errors. Estimates and errors do not establish satisfaction or infeasibility. Do not select a configuration listed in tried_configurations.

Return only the required JSON fields: one set of xApp instructions with policies and scopes, and one brief rationale.

## 입력·출력 및 호출 설명

Inputs, outputs, and call structure.

### Call structure

Each request carries one role instruction, the role's input record, and its
output JSON schema. The input descriptions and executor rules below are used by
the code that composes requests; they are not sent to the model as a shared
instruction.

- **3A.** The Target and Control agents are called independently and in
  parallel; neither waits for the other's output. The Trajectory agent then
  selects one untried configuration for each live trial. T and C remain fixed
  during an episode and are not reconstructed before each selection.
- **RM.** A single construction call prepares both T and C. Subsequent
  selection calls use the Trajectory prompt and the same input format as 3A.
- **MA.** Each call receives the source information and returns a complete
  joint configuration (participating xApps with their policies and scopes) for
  the next trial.
- Calling, validation, execution, observation, history keeping, trial and time
  limits, termination, and recovery are handled by the external executor.
  Model inputs never contain the maximum or remaining trial count or the
  episode deadline; observation times and validity are provided.

### Target agent

| Input | Content |
|---|---|
| `input.intents` | All received intents: ID, owner, original text, scope, and the KPI, comparison operator, value, and unit stated by the source |
| `input.authorization` | Authorized concession levels and KPI bounds per intent and owner, service floors, joint conditions, finite priority weights, concession ordering, and tie rules |
| `input.network_state` | Initial per-UE KPI snapshot with observation time and validity, serving cells, and offered load |
| Output | Additional authorized level vectors, each with a brief reason. The original target T0 is not returned by the model; the code builds it from the original intents and authorization |

T0 combines all original requirements without concession. T contains T0, any
mandatory targets, and the alternatives returned by the agent. Targets are
ordered by the preference cost defined in `input.authorization`, where a lower
cost is preferred. The reported campaign uses
`p = 25 kE + 13 k1 + 7 k2 + 3 k3` over the four adjustable requirements.

### Control agent

| Input | Content |
|---|---|
| `input.intents` | The same intents given to the Target agent |
| `input.authorization` | Authorized concession levels, KPI bounds, service floors, and joint conditions. Preference ordering is omitted; which target to pursue is decided by the Target and Trajectory agents |
| `input.function_catalog` | Available RAN functions: capability, supported policy fields, admissible values and units, operating modes, scope, and prerequisites |
| `input.compatibility` | Concurrency conditions, ordering and dependency relations, and constraints on controlling the same parameter or resource |
| `input.network_state` | Active functions and policies; relevant cell, UE, load, resource, and latest KPI state with observation times |
| `input.effect_evidence` | Function-policy-KPI effects, interaction evidence, valid prior observations, and their applicability conditions |
| `input.construction_policy` | Number of candidates in C and the KPI-deficit comparison rule |
| Output | Candidate IDs; for each candidate, the participating functions with their policies and scopes, related KPIs, relevant observation IDs, and a brief rationale |

A candidate in C is a joint-control configuration specified relative to the
common reference configuration, which is itself included in C. Candidates
report related KPIs and a qualitative rationale rather than predicted values.

### Trajectory agent

| Input | Content |
|---|---|
| `input.target_contract` | T0 and the full candidate set T, with preference costs and tie rules |
| `input.control_candidates` | The prepared set C: candidate IDs, per-function policies and scopes, related KPIs, and rationales |
| `input.network_state` | The currently applied configuration, relevant network state, and observation context |
| `input.observations` | Configurations executed in this episode with their KPIs, observation times, scopes, and validity; `[]` when none |
| `input.observed_best` | The lowest-cost authorized target demonstrated by a valid observation, with the configuration and observation that achieved it; `null` when none |
| `input.kpi_gaps` | Deficits between measured KPIs and T0 or more preferred targets, keeping owner, scope, KPI, and unit; `null` without a measured KPI |
| Output | The next control ID, the target ID it aims to attain, and one brief rationale |

Without valid observations, the agent relies on the current state and the
candidates' rationales. With observations, it uses the measured outcomes and
KPI gaps to choose a configuration that can demonstrate a more preferred target
than the best so far; unsuccessful trials remain informative. The external
evaluator assesses each valid observation against the full set of authorized
targets to compute `observed_best` and `kpi_gaps`, so a trial can demonstrate a
target outside T. The intended target in the output states the purpose of the
trial; attainment is decided from the measured KPIs.

### 내부 monolith

The role-merged method (RM, `internal-monolith`).

#### Construction call

Receives the inputs of both the Target and Control agents and returns T and C,
with brief reasons, in a single response.

#### Selection call

Uses the same input fields and output schema as the Trajectory agent, with RM's
own prepared T and C, the current state, and its observation history.

### 기본 monolith

The monolithic method (MA, `basic-monolith`).

| Input | Content |
|---|---|
| `input.intents` | All intents with ID, owner, original text, scope, KPI requirements, and units |
| `input.authorization` | Priorities, authorized concessions, service floors, joint conditions, and the same owner preference rules |
| `input.function_catalog` | Available xApp functions, supported policy fields, values, units, operating range, and prerequisites |
| `input.compatibility` | Concurrency conditions, ordering and dependency relations, and resource or parameter constraints |
| `input.network_state` | The currently applied configuration; cell, UE, load, resource, and latest KPI state with observation times |
| `input.effect_evidence` | The same effect and interaction evidence available to the other methods |
| `input.observations`, `input.tried_configurations` | Configurations already executed in this episode with their measured KPIs, times, context, and validity |
| `input.observed_best`, `input.kpi_gaps` | The evaluator's best demonstrated target and remaining KPI gaps, as given to the Trajectory agent |
| Output | The xApps to run together with their policies and scopes, and one brief rationale |

MA receives in one request the source information that 3A receives from
outside; it never receives the T, C, rankings, or recommendations produced by
another method. Source information, tool access, validation, and evaluation
rules are identical across methods, and each method's history contains only
its own observations.
