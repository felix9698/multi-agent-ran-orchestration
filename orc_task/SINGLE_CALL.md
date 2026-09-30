# RAN agent 프롬프트 사용 안내

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

### 호출 방식

각 요청에는 해당 역할의 지시문 하나, 입력 자료, 출력 JSON 스키마를 함께 전달한다. 아래 입력 설명과 실행기 규칙은 요청을 구성하는 사람이 사용한다. 모든 모델에 붙이는 공통 지시문으로 전송하지 않는다. 지시문 원문은 동봉한 AI_RAN_SINGLE_CALL_PROMPTS_20260907.json의 역할별 키로도 제공한다.

- 3-agent는 Target가 T를 구성하고, Control이 T를 받아 C를 구성한 뒤, Trajectory가 다음 실행 하나를 선택한다. T·C는 구성에 필요한 정보가 바뀔 때 갱신한다. 각 선택 때 다시 구성하지 않는다.
- 내부 monolith는 한 모델에 구성 호출과 선택 호출을 구분해 보낸다. 유효한 T·C가 준비되어 있으면 선택 지시문과 선택에 필요한 입력만 전달한다.
- 기본 monolith는 원천 자료를 한 요청에 받아 실행할 xApp들과 각 policy를 정한다.
- 호출·검증·실행·관측·이력 보관·시간 및 횟수 제한·종료·복구는 외부 실행기가 담당한다. 모델 입력에는 최대·잔여 시도 횟수나 실험 마감 시각을 넣지 않는다. 관측 시각과 유효성은 전달한다.

### Target agent

| 위치 | 내용 |
|---|---|
| input.intents | 실제 받은 intent 전체. ID, owner, 원문, 적용 범위와 원천에 명시된 KPI·비교 연산·요구 값·단위 |
| input.authorization | intent·owner별 사전 허용 완화 단계와 KPI bound, 최저 서비스, 공동 조건, 유한 priority 가중치, 완화 수준과 동점 규칙 |
| input.network_state | 초기 UE별 KPI 스냅샷과 관측 시각·유효성, 서빙 셀, 제공 부하 |
| output_schema | 허용 완화 단계·공동 조건·정렬 규칙으로 표현한 추가 대안, 짧은 근거. **T0 본문은 답하지 않는다** — 코드가 원본 인텐트·인가로 만든다(2026-09-18 핸드오프 §4.1) |

T0는 원래 intent들의 요구조건을 완화 없이 모두 묶은 목표이다. T는 T0와 허용된 대안 전체이다. 후보 순서는 priority 가중치와 완화 수준 제곱의 곱을 합산한 비용으로 정한다. 작은 비용이 우선이며 낮은 priority의 intent도 최저 서비스와 평가에 포함한다. 전체 조합은 완화 단계와 조건을 이용해 손실 없이 압축 표현한다.

### Control agent

| 위치 | 내용 |
|---|---|
| input.intents | Target 이 받는 것과 같은 intent 전체 |
| input.authorization | 사전 허용 완화 단계와 KPI bound, 최저 서비스, 공동 조건. **선호 순서(preference)는 빼고 보낸다** — 어떤 목표를 먼저 좇을지는 Target 과 Trajectory 가 정한다 |
| input.function_catalog | 사용 가능한 function ID, 기능, 지원 policy 필드·허용 값·단위·동작 모드, 적용 범위와 전제 |
| input.compatibility | 동시 사용 조건, 선행·종속 관계, 동일 파라미터·자원에 대한 중복 제어 제약 |
| input.network_state | 현재 실행 중인 function과 policy, 관련 셀·UE·부하·자원·최신 KPI와 관측 시각, 선택에서 제외된 기존 function의 유지·비활성 규칙 |
| input.effect_evidence | function-policy-KPI 영향과 상호작용 근거, 유효한 기존 관측, 중앙 공동 효과 예측 모델 또는 호출 도구와 필요한 입력, 불확실성·적용 조건 |
| input.construction_policy | C에 담을 후보 수와 KPI 부족량 비교 규칙. function에 전달하는 policy와 구분한다. |
| output_schema | C의 후보 ID, 선택된 function별 ID·policy·scope, 효과 추정·불확실성·적용 조건·근거 참조, 짧은 구성 근거 |

C의 한 후보는 동시에 사용할 함수들과 각 함수에 지시할 policy·scope를 묶은 구성이다. 공동 예측 KPI가 만족하는 T 중 완화 비용이 가장 작은 목표를 기준으로 순위를 정한다. 동률은 제공된 KPI 부족량 규칙으로 비교하고 지정된 수만큼 후보를 보존한다. 수치 예측에는 제공된 모델이나 도구를 사용하며, 근거가 없는 효과는 unknown으로 표시한다.

### Trajectory agent

| 위치 | 내용 |
|---|---|
| input.target_contract | T0와 전체 T, 완화 비용과 동점 규칙 |
| input.control_candidates | 준비된 C의 후보 ID, 함수별 policy·scope, 예측 KPI·불확실성·적용 조건 |
| input.network_state | 현재 실제 적용된 구성, 관련 망 상태와 관측 문맥 |
| input.observations | 해당 방식의 실제 실행 구성과 KPI, 관측 시각·범위·유효성. 기록이 없으면 [] |
| input.observed_best | 유효한 관측 전체가 충족한 T 중 최소 완화 비용의 목표 ID·비용과 이를 달성한 구성·근거 관측. 충족한 목표가 없으면 null |
| input.kpi_gaps | 실제 KPI와 T0 및 더 선호되는 목표 사이의 부족량. owner·scope·KPI·단위를 보존하며 실제 KPI가 없으면 null |
| output_schema | 다음 control ID, 달성을 노리는 target ID, 짧은 근거 하나 |

유효한 기록이 없으면 현재 상태와 후보의 효과 추정으로 판단한다. 기록이 있으면 관측 결과와 부족한 KPI를 참고해 지금까지의 최선보다 T0에 가까운 목표를 달성할 구성을 선택한다. 아직 만족한 목표가 없어도 실패 기록은 활용한다.

외부 평가기가 실제 KPI를 전체 T에 대조하여 observed_best와 kpi_gaps를 계산한다. 최근 결과가 나빠도 기존 최선의 유효한 기록을 유지한다. 출력의 target ID는 달성을 노리는 목표이며, 실제 달성 여부는 실행 후 KPI로 판정한다.

### 내부 monolith

#### 구성 호출

Target와 Control의 원천 입력을 함께 제공한다. 이 호출에서 구성한 T를 이어서 C 구성에 사용한다. 출력 스키마는 T·C와 짧은 근거를 담는다.

#### 선택 호출

Trajectory와 같은 입력 필드와 출력 스키마를 사용한다. 해당 방식의 준비된 T·C와 현재 상태·관측 이력을 전달한다. 동일 T·C 조건의 비교에서는 외부 실행기가 실제 후보 집합의 동일성을 확인한다.

### 기본 monolith

| 위치 | 내용 |
|---|---|
| input.intents | 실제 받은 intent 전체의 ID·owner·원문·범위와 명시된 KPI 요구조건·단위 |
| input.authorization | priority, 사전 허용 완화, 최저 서비스, 공동 조건과 동일한 owner 선호 규칙 |
| input.function_catalog | 사용 가능한 xApp 기능, 지원 policy 필드·값·단위, 동작 범위와 적용 전제 |
| input.compatibility | 동시 사용 조건, 선행·종속 관계, 자원·파라미터 제약 |
| input.network_state | 현재 적용 구성, 셀·UE·부하·자원·최신 KPI와 관측 시각, 기존 xApp의 유지·비활성 규칙 |
| input.effect_evidence | 동일하게 제공 가능한 효과·상호작용 근거, 예측 모델·도구와 불확실성 |
| input.observations | 해당 방식의 실제 xApp 지시와 관측 KPI·시각·문맥·유효성. 기록이 없으면 [] |
| output_schema | 함께 실행할 xApp들과 각 policy·scope, 달성을 노리는 요구조건, 짧은 근거 |

3-agent가 외부에서 받는 원천 자료를 한 요청에 모아 제공한다. 우리 방법이 생성한 T·C, 후보 순위와 추천은 포함하지 않는다. 기본 monolith가 자체 생성한 결과는 동일한 유효성 기준으로 보관·재사용할 수 있다. 원천 정보와 도구 접근 조건은 비교 방식 간 동일하게 적용하고, 운영 중 이력은 각 방식이 실제로 얻은 관측을 사용한다.

출력 요구조건은 intent·owner·scope·KPI·비교 연산·값·단위로 원래 intent에 연결한다. 각 policy는 해당 xApp이 실행할 수 있는 형식으로 반환한다. 외부 실행기는 모든 방식에 동일한 허용 조건과 KPI 판정 기준을 적용한다.

## 변경 이력

### 2026-09-20 — Target·Control 병렬화와 프롬프트 대칭 (오너 지시)

두 가지를 한 번에 바꿨다.

**(1) 대칭 교정.** three-agent 와 internal monolith 의 대응 문장 두 곳이 어긋나 있었다 —
후보 채우기 지시의 `across the proposed T` 는 통합형에만, 대안 선택 기준의
`Use intent semantics, owner preferences, ...` 와 `Unknown control effects do not
establish infeasibility.` 는 분리형에만 있었다. 서로 다른 지시를 받은 두 방식을
비교하고 있었던 것이다. 이제 역할을 밝히는 첫 문장을 빼면 글자까지 같다.

**(2) 생성 의존성 제거.** Control 이 완성된 T 를 기다리지 않는다. 선언된 요구조건과
인가된 대안은 원본 인텐트·인가에 이미 있으므로 두 구성 호출을 동시에 시작한다.
구성 지연은 두 호출의 합(중앙값 19.3 + 52.9 = 72.2 초)이 아니라 느린 쪽(52.9 초)이 된다.
Target 입력에서 `input.effect_evidence` 를 뺐다(늘 비어 있었고, 인텐트·인가·선호가
같으면 T 를 재사용할 수 있어야 한다). `input.network_state` 의 현재 구성(소속 셀·부하)은
남긴다 — 어느 UE 가 붐비는 셀에 있는지 모르면 어떤 양보가 쓸모 있는지 고를 수 없다.
Control 이 받는 `input.authorization` 에서는 선호 순서를 뺀다.

### 2026-09-17 — 기본 monolith 에 반복 회피 문장 추가 (오너 지시)

기본 monolith 지시문에 다음을 더했다:

    Avoid repeating a configuration already listed in tried_configurations
    while its observations are still valid.

**왜**: 세 팔을 **같은 지시** 아래 비교하기 위해서다.  `three-agent`(trajectory)와
`내부 monolith 선택`은 *"Avoid repeating controls with still-valid observations."* 를
받는데 기본 monolith 에만 그 문장이 없었다.  입력은 오히려 기본 monolith 가 더 명시적이다 —
이 팔만 `input.tried_configurations`(이미 시도한 설정 전체)를 받는다.

**문구가 다른 이유**: 기본 monolith 에는 후보 ID(`controls`)라는 개념이 없으므로
그 팔이 실제로 받는 `tried_configurations` 의 어휘로 **같은 뜻을** 옮겼다.

**데이터에 미치는 영향**: 이 문장이 없던 동안 수집한 기본 monolith 판은 지시가 달랐으므로
**팔 비교에서 제외한다**(2026-09-17 21:30 이전, 깨끗한 판 1 개).

### 2026-09-17 — 여섯 프롬프트 전면 교체 (오너 드롭, **고정**)

출처: `.orca/drops/RAN_AGENT_PROMPTS_REVISED_20260917.md` — 오너가 "그대로 해라, 이건 이제
고정이다" 로 지시한 판본이다.  여섯 절 전부를 그 드롭의 `text` 블록으로 바꿨다.

- 코드와 이 문서를 **한 원본에서 생성**했다.  드롭에서 블록을 뽑아 `agents.py` 의 상수를
  만들고, 이 절은 그 상수에서 되찍었다.  손으로 옮겨 적지 않았으므로 글자 어긋남이 없다.
- `MONOLITH_SELECT_SYSTEM_PROMPT` 는 복사본이 아니라 `TRAJECTORY_SYSTEM_PROMPT` 의
  **별칭**이다.  드롭의 "Keep 3A-Trajectory and IM-Select identical" 을 구조로 강제한다.
- 길이: target 1159 · control 1237 · trajectory 1023 · monolith-form 2066 ·
  monolith-select 1023 · basic-monolith 1186 자.
- 이 판본 **이전**의 판은 세 팔 모두 지시가 달랐으므로 팔 비교에 쓰면 안 된다
  (`ops/arm_compare.py` 의 `PROMPT_EPOCH` 가 세 팔 전부에 경계를 둔다).
- 드롭의 `## Runtime alignment` 항목들은 **프롬프트 밖의 런타임 요구**다.  프롬프트 교체와
  별개로 확인해야 하며, 확인 결과는 `ops/overnight/RESULTS-20260917.md` 에 적는다.
