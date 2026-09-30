---
goal: 기존 Agentic_Intent_Coordinator를 Operator intent 입력부터 표준 O-RAN R1/A1 경계를 통한 Near-RT RIC 정책 전달과 피드백 기반 검증까지 담당하는 rApp/Non-RT RIC 측 시스템으로 전환한다.
deliverables: ["O-RAN 책임 분리에 맞게 전환된 코드베이스", "R1 기반 rApp 경계와 표준 A1-P 연동 경계", "버전 고정 계약·schema·golden fixture·black-box conformance suite", "하드웨어 없이 실행 가능한 mock 환경", "기존 S0-S6·협상·검증·실험 기능의 회귀 검증 결과", "선택적 GUI와 독립적인 headless 실행 경로", "아키텍처·마이그레이션·운영·통합 문서"]
quality_bar: paper-grade
budget: generous
parallel: encourage
cross_check: required
verify_math: false
interview: auto
plan_critique: on
skills_hint: []
---

# Agentic Intent Coordinator의 표준 O-RAN rApp 전환 작업

## 배경

대상 저장소는 다음과 같다.

`/Users/ljb/Dropbox/LICS/정범/repos/Agentic_Intent_Coordinator`

이 작업과 함께 제공되는 다음 문서를 양측 통합 계약의 최우선 기준으로 사용한다.

- `02-rapp-xapp-backend-mandatory-contract.md`
- `shared-contract-bundle/` 전체: standalone Policy/Status/DME/Capability schema, R1/A1/O1/E2 profile, NETCONF/YANG RPC fixture, integration-values/deployment-vector schema, golden vectors, complete scenario catalog와 runner semantics
- `handoff-manifest.1.0.0.json`의 두 문서/bundle fingerprint

전달 단위는 분리하지 않는다. 상위 오케스트레이션에는 이 문서, `02`, `shared-contract-bundle/`, root manifest 전부를 주고, 하위 연구원에는 `02`, 같은 bundle, 같은 root manifest를 준다. 마지막 병합 전 추가 질의·응답이 없다는 전제로 두 release 모두 상대 측 mock/harness에 대해 독립적으로 Phase A gate를 통과해야 한다.

다음 자료는 기존 구현과 하위 시스템의 맥락을 이해하기 위한 참고 자료다. 위 통합 계약과 충돌하면 위 통합 계약을 우선한다.

- `https://github.com/brandonkims/AI-RAN_xApp`
- `agentic-coordinator-to-oran-mapping.md`
- `rapp-xapp-interface-contract.md`

현재 프로젝트는 Operator의 자연어 intent를 받아 다음 핵심 과정을 수행한다.

1. Analysis Layer, S0–S2
   - 자연어 intent를 typed intent로 변환하고 strict schema validation을 수행한다.
   - 기존 monitored intent와의 충돌을 선별한다.
   - 현재 상태와 history를 이용해 feasibility, confidence 및 대안을 평가한다.
   - 보정된 confidence와 적응형 `theta*`를 사용해 trial 또는 negotiation으로 분기한다.
2. Execution Layer, S3–S4
   - bounded trial을 수행한다.
   - fresh KPI와 readback을 이용해 전체 intent 집합을 검증한다.
   - 실패 시 rollback을 수행한다.
3. Negotiation Layer, S5
   - 대안을 생성하고 revised intent를 다시 S2부터 실행한다.
4. S6
   - 하나의 typed terminal outcome과 evidence record로 episode를 종료한다.

현재 live 경로는 Coordinator와 GUI가 다음 기능을 직접 담당하므로 O-RAN 계층 경계와 맞지 않는다.

- OAI 전용 telnet 명령을 이용한 power, PRB, scheduler priority, MCS 변경
- SSH, gNB log, UE interface counter, ping, iperf3 등을 이용한 직접 측정
- OAI Core, gNB, UE 및 USRP 환경의 직접 시작·정지
- `OAIExecutor`, `MultiUECollector`, `SystemController`에 대한 직접 의존

이 작업의 목적은 기존 agentic 판단 엔진을 폐기하는 것이 아니다. 기존 판단·협상·검증·안전·이력 기능을 보존하면서, 직접 OAI/하드웨어 경계를 표준 O-RAN 책임 경계로 교체하는 것이다.

목표 구조의 권위 있는 흐름은 다음과 같다.

```text
Operator
  → rApp: Agentic Intent Coordinator
  → R1: A1 policy management service
  → Non-RT RIC Framework: A1-P Consumer
  → A1-P
  → Near-RT RIC: A1-P Producer
  → Near-RT RIC 내부 xApp
  → E2
  → OAI RAN
```

상태와 관측 정보는 다음의 표준 책임을 따라 상위로 전달된다.

```text
Near-RT RIC A1 policy status/feedback
  → A1-P
  → Non-RT RIC Framework
  → R1 A1 policy management service
  → rApp

RAN performance/assurance data
  → O-RAN Managed Element의 O1 Performance Assurance/PM MnS
  → SMO/Non-RT RIC Framework의 O1 Consumer·assurance correlator
  → R1 RAN OAM PM service 또는 R1 DME service
  → rApp
```

Operator의 원문 RAN intent 자체를 A1 메시지로 보내지 않는다. rApp이 intent를 해석·조정한 후 지원되는 A1 policy type의 declarative policy로 변환한다. rApp은 A1을 직접 종단하거나 xApp을 직접 호출하지 않고 R1 service를 사용한다.

상위 개발자와 하위 개발자는 구현을 병렬로 완료하고 마지막 통합 시점까지 상시 소통하지 않는 것을 전제로 한다. 따라서 구현 중의 추가 합의를 기대하지 말고, 함께 제공된 계약·schema·fixture·적합성 테스트를 기준으로 독립적으로 완료해야 한다.

## 요구사항

### 1. O-RAN 책임 경계

완성된 시스템에서 다음 책임이 코드와 문서상 분리되어야 한다.

#### rApp 책임

- Operator의 natural-language 또는 structured intent 입력
- typed intent 생성과 strict validation
- intent conflict, priority, feasibility, history 및 negotiation 판단
- intent를 지원 가능한 declarative A1 policy로 변환
- R1을 통한 A1 policy type discovery, policy create/update/delete/query 및 status subscription
- R1 RAN OAM PM 또는 DME를 통한 상태·KPI 수신
- 상위 intent의 `SATISFIED / VIOLATED / UNKNOWN` 판정
- continuous assurance, evidence 및 terminal outcome 관리

#### Non-RT RIC Framework 책임

- rApp에 R1 service를 제공
- rApp 등록·service discovery·인증·권한 경계를 제공
- R1 A1 policy management 요청을 A1-P lifecycle에 연결
- Near-RT RIC의 A1 policy status를 R1 service로 rApp에 노출
- O1 `FileDataReportingMnS` subscription과 NETCONF/YANG `PerfMetricJob` desired state를 소유하고 notification 수신·SFTP 회수·normalization을 수행
- O1 Performance Assurance/PM으로 수집한 결과를 R1 RAN OAM PM 또는 DME로 rApp에 노출
- 여러 rApp·policy·Near-RT RIC가 존재해도 ownership과 identifier를 혼동하지 않음

전체 상용 SMO를 새로 구현하는 것이 목적은 아니다. 필요한 최소 로컬 framework 또는 adapter를 구현할 수 있으나 실제 R1/A1 구현과 교체 가능한 책임 경계를 유지해야 한다.

#### Near-RT RIC/xApp 책임

- A1-P Producer/termination
- 지원 A1 policy type의 schema validation과 lifecycle 관리
- A1 policy를 Near-RT RIC 내부 xApp 입력으로 전달
- 최신 E2SM-KPM 상태에 근거한 near-real-time 수학적·결정론적 action 선택
- E2SM-RC를 통한 RAN 제어, readback 및 하위 rollback
- A1으로 enforcement/type-specific execution status와 action/readback metadata 제공; KPI sample과 Operator intent fulfilment 판정을 A1에 싣지 않음
- 최종 integration profile에서 OAI RAN/O-RAN Managed Element 측의 O1 NETCONF/YANG `PerfMetricJob` Provider, `FileDataReportingMnS` subscription/notification endpoint, SFTP PM file Provider를 가능하게 하는 소스·patch·설정·measurement profile 제공
- `backend-release-manifest.1.0.0.schema.json`에 맞춘 OAI/FlexRIC full commit, ordered private patch digest, post-patch tree, binary/ASN.1/profile/vector/evidence digest와 양쪽 gNB의 동일한 rollback-capable Style 3 배포 제공
- `e2-capability-inventory.1.0.0.schema.json`에 맞춰 E2 Setup/RIC Service Update마다 node·connection-epoch별 KPM/RC OID, revision, RAN Function Definition과 exact style/action/format/parameter tree를 관측·검증하고 mismatch 시 zero write

이 영역은 하위 연구원의 구현 범위이며 이 저장소에서 FlexRIC native xApp, E2AP/E2SM encoding, OAI patch 또는 USRP 제어를 대신 구현하지 않는다. 상대 구현 없이 개발하기 위한 mock만 이 저장소 범위에 포함한다.

### 2. 기존 S0–S6 의미의 전환

기존 상태 이름과 핵심 연구 로직을 보존하되, S3와 S4의 외부 동작을 O-RAN 책임에 맞게 전환한다.

- S0: Operator intent 수신, typed intent 생성, schema validation
- S1: 기존 monitored intent와의 conflict screening 및 joint state 확인
- S2: feasibility 분석, history 사용, confidence calibration 및 `theta*` routing
- S3: 직접 RAN write가 아니라 R1 A1 policy management service를 통한 policy create/update 시작
- S4: A1 enforcement status와 R1 PM/DME의 fresh KPI를 이용한 상위 intent 검증
- S5: 대안 생성, negotiation 및 revised intent materialization
- S6: 단일 terminal outcome과 evidence record 확정

다음 기능과 불변식을 유지한다.

- natural-language entry point와 typed-intent re-entry
- model-agnostic multi-LLM backend
- strict schema validation
- episode 중 proposer/model hot-swap 경계
- calibrated confidence와 adaptive `theta*`
- budget-bounded negotiation
- episode single-flight 및 deadline
- 외부 호출별 timeout과 bounded retry
- monitored intent 전체의 joint validation
- `SATISFIED / VIOLATED / UNKNOWN` 3값 판정
- continuous-assurance violation 재진입
- history disabled/frozen/online mode
- 같은 실패 policy의 무한 반복 방지
- fail-closed 처리
- immutable evidence와 identifier chain
- 기존 terminal outcome:
  - `commit_original`
  - `commit_revised`
  - `pending_not_admitted`
  - `technical_failsafe`

기존의 snapshot–write–readback–rollback 의미를 단순히 이름만 유지하지 않는다. 전환 후에는 다음과 같이 동등한 안전 의미를 구성한다.

- A1 create/update 응답은 transport 및 policy resource 처리 결과일 뿐, RAN 목표 달성 증거가 아니다.
- A1 `ENFORCED`는 policy가 적용 상태임을 뜻하며, Operator intent 만족을 뜻하지 않는다.
- commit은 correlation 가능한 policy status와 fresh PM/DME KPI가 함께 충분할 때만 허용한다.
- stale, missing, contradictory 또는 scope가 다른 evidence는 성공으로 사용하지 않는다.
- policy withdrawal/replacement와 하위 rollback 결과를 구분한다.
- 결과를 검증할 수 없으면 기존 fail-closed 및 technical-failsafe 의미를 유지한다.

### 3. Intent에서 A1 policy로의 변환

기존 OAI 전용 action key와 명령을 새로운 외부 계약으로 승계하지 않는다.

- `power_offset`
- `prb`
- `sched_priority`
- `mcs_offset`
- `ci rfatt`
- `ci prbcap`
- `ci sched_prio`
- `ci mcs`
- `ci trigger_n2_ho`

rApp은 다음 원칙을 만족하는 policy를 생성한다.

- 지원 policy type을 R1 capability/policy-type discovery 결과로 확인한다.
- low-level E2 action 대신 scope, 목표, preference, constraint 및 허용 범위를 표현한다.
- 기존 intent priority와 conflict는 rApp에서 먼저 해소한다.
- Near-RT RIC에 서로 충돌하는 overlapping policy를 의도적으로 넘기지 않는다.
- 구체 target/action 선택은 policy가 허용한 범위 안에서 xApp에 남긴다.
- 지원되지 않는 intent를 임의의 legacy action으로 우회 변환하지 않는다.
- unsupported 또는 capability-mismatch intent는 fail-closed admission/negotiation 경로로 보낸다.

최소 통합 policy profile은 project-specific `AIC_UECellSteering_1.0.0`으로 고정한다. 이 type은 O-RAN A1 Policy Type mechanism과 A1TD identifier를 따르지만, O-RAN이 사전 정의한 표준 Policy Type이라고 표기하지 않는다. 추가 policy type은 capability-driven extension으로만 도입하고 기존 type의 의미를 바꾸지 않는다.

### 4. R1 사용과 A1 종단 분리

rApp core는 A1 endpoint, Near-RT RIC 주소 또는 xApp 주소를 직접 알지 않아야 한다. rApp 관점의 외부 기능은 R1 service로 표현한다.

최종 경로에서 다음 결과를 제공해야 한다.

- rApp 등록 및 필요한 R1 service discovery
- 지원 A1 policy type 조회
- A1 policy 생성·갱신·조회·삭제
- A1 policy enforcement status 조회와 subscription
- R1 PM/DME 데이터 request 또는 subscription
- 외부 service timeout, duplicate, restart 및 reconnection 처리

R1 DME baseline은 `data-registration/v2`, `data-discovery/v2`, `data-access/v2/data-jobs`와 data job이 협상한 HTTP push/pull URI를 사용한다. 존재하지 않는 direct `/data` endpoint를 만들지 않는다. Continuous assurance는 `CONTINUOUS + PUSH_HTTP`, 유실 복구 query는 bounded `ONE_TIME + PULL_HTTP` profile로 구현하고, callback은 durable schema/correlation validation 뒤 `204`를 반환한다.

Non-RT RIC Framework가 별도 제품인지, 이 저장소와 함께 실행되는 최소 framework인지, 외부 adapter인지에 대한 세부 선택은 오케스트레이션이 결정한다. 다만 배치 형태와 관계없이 rApp–R1–Framework–A1 책임이 코드 수준에서 식별 가능해야 한다.

### 5. 표준 A1-P 경로

최종 production/integration profile은 함께 제공된 계약이 지정한 A1-P 규격과 policy type을 따라야 한다.

현재 하위 저장소의 project-specific HTTP/JSON boundary를 재사용할 필요가 있으면 다음 조건을 만족한다.

- 개발·mock·migration adapter임을 명시한다.
- 표준 A1-P라고 명명하지 않는다.
- rApp core가 해당 endpoint 또는 payload를 직접 참조하지 않는다.
- 표준 A1-P adapter와 동일한 상위 domain 결과로 변환한다.
- 제거하거나 표준 adapter로 교체할 때 Coordinator 판단 로직이 바뀌지 않는다.
- 최종 인수 시험은 custom endpoint가 아니라 표준 A1-P 경로에서 통과한다.

### 6. 양방향 데이터와 correlation

상위 evidence record는 최소한 다음 identifier를 연결할 수 있어야 한다.

- `run_id`
- `episode_id`
- `cycle_id`
- `proposal_id`
- `trial_id`
- `intent_id`와 intent revision
- R1 request/subscription identifier
- A1 `policyTypeId`
- A1 `policyId`
- rApp이 생성한 `policyRevision`
- episode가 생성되면 `episodeId`, E2 control attempt가 있으면 `transactionId`와 `actionId`
- KPI observation window와 scope

다음 상태를 서로 구분한다.

- 요청이 R1 framework에 도착함
- A1 policy resource가 생성 또는 갱신됨
- Near-RT RIC가 policy를 `ENFORCED` 또는 `NOT_ENFORCED`로 보고함
- xApp이 action을 선택하지 않음
- action이 전송되었으나 결과가 검증되지 않음
- readback으로 action 결과가 확인됨
- KPI가 fresh하고 사용 가능함
- policy는 적용되었지만 Operator intent는 만족되지 않음
- policy 삭제/대체가 완료됨
- rollback이 전송됨
- rollback 결과가 검증됨 또는 불명확함

하위 세부 lifecycle은 `02-rapp-xapp-backend-mandatory-contract.md` 섹션 8–10의 `statusSchema`와 `aic:policy-evidence:1.0.0` schema로 수신한다. rApp의 최종 판정은 A1 enforcement/type-specific status와 O1에서 유래한 R1 DME evidence의 권위 구분을 지켜야 한다.

### 7. 무소통 병렬 개발을 위한 공유 계약

상위와 하위 구현은 마지막 통합 전까지 추가 합의를 요구하지 않아야 한다. 이를 위해 함께 제공된 계약을 저장소에 pin하고 다음 산출물을 생성한다.

- 적용한 O-RAN specification과 version manifest
- A1 OpenAPI/profile
- O1 Performance Assurance/PM profile과 measurement mapping
- 3GPP Rel-18 schema-mount와 exact `PerfMetricJob` NETCONF/YANG lifecycle profile 및 RPC fixture
- 지원 `PolicyTypeObject`, `policySchema`, `statusSchema`
- R1에서 사용하는 policy-management API version 및 assurance data schema
- schema digest와 compatibility manifest
- backend reproducible release manifest, live E2 capability inventory 및 최종 `integration-values` schema
- identifier 및 error catalogue
- 함께 제공된 `shared-contract-bundle/`을 그대로 사용하는 golden request/status/assurance fixtures와 complete machine-readable scenario catalog
- root `handoff-manifest.1.0.0.json` 검증과 release artifact manifest pinning
- black-box conformance runner
- mock A1-P Producer
- mock R1 policy-management/PM/DME service
- mock O1 PM source/collector
- deterministic test topology와 initial state

계약에 없는 field, default, enum, transition 또는 error를 한쪽만 추가하지 않는다. 구현 중 추가 요구가 발견되면 기존 `1.0.0` 동작을 보존한 additive extension과 별도 version으로 격리하고, 기본 profile은 계속 계약대로 동작하게 한다.

양측이 서로 다른 UUID, timestamp, topology 또는 expected status를 새로 만들어 별도 baseline corpus로 사용하지 않는다. 동봉 bundle의 scenario catalog가 baseline 실행 recipe와 assertion의 단일 source of truth이고, 구현별 추가 fixture는 `extension/`에 두어 baseline digest 계산에서 제외한다.

### 8. Mock과 적합성 시험

하드웨어, FlexRIC, OAI, USRP, 외부 LLM API 및 상대 저장소가 없어도 상위 구현을 **Phase A integration-ready release**까지 완성하고 검증할 수 있어야 한다. 실제 하위 endpoint와 실제 O1 Provider가 필요한 검증은 마지막 병합 시점의 **Phase B merged acceptance**로 분리한다.

mock은 실제 경계와 동일한 다음 의미를 제공한다.

- R1 service discovery와 policy management
- A1 policy type discovery
- create/update/query/delete 및 status notification
- PM/DME KPI request/subscription
- O1 PM sample ingestion과 A1 status–scope–time correlation
- deterministic policy/KPI lifecycle
- timeout, duplicate, out-of-order, stale, missing, conflict 및 restart 주입

최소 golden scenario는 다음을 포함한다.

- R1 bootstrap/service discovery, A1 policy management discovery와 DME registration/discovery/data-job/PUSH_HTTP delivery
- valid traffic-steering policy create와 `ENFORCED`
- already-satisfied 상태의 zero-action
- improvement threshold 미달과 eligible target 부재의 zero-action
- verified steering과 fresh KPI
- invalid schema
- unsupported policy type
- unknown UE/cell scope
- overlapping policy conflict
- identical PUT retry
- 동일 `policyRevision`/idempotency key의 다른 payload conflict
- policy update
- policy delete/withdraw
- response loss 후 retry
- A1 status callback loss 후 query recovery
- control timeout
- readback mismatch
- stale/missing/not-available KPI
- rollback verified/failed/unknown
- Near-RT 또는 framework restart recovery
- duplicate/out-of-order assurance event
- 필수 dependency가 준비되지 않은 health 상태
- O1 subscription `201` before job unlock, restart reuse/no-duplicate 및 definitive delete-before-recreate
- `notifyFilePreparationError` 또는 expected-window timeout 후 `fileReadyTime` 범위의 표준 `/files` recovery: unique valid file 성공과 valid file 부재/중복의 가짜 evidence 없는 fail-closed 처리
- O1 notification에서 `eventTime == fileReadyTime < fileExpirationTime` 및 `fileReadyTime <= retrievedAt < fileExpirationTime` 위반의 zero-retrieval/zero-publish 처리
- node/epoch별 E2 Setup identity·OID·revision·definition digest mismatch, unresolved NR-CGI profile 및 stale RIC Service Update의 zero-E2-write 처리
- fixed fixture와 live invariant 각각의 Operator→R1→A1→E2→O1→R1 DME→S4/S6 성공 왕복

적합성 suite를 interface별로 분리한다.

1. A1-P Producer suite: 하위 mock과 실제 Near-RT RIC에 동일하게 실행
2. R1 service suite: 상위 mock과 실제 Non-RT RIC Framework에 동일하게 실행
3. O1 Consumer normalizer suite: 동봉 notification/XML의 정확한 bytes를 상위 production collector/normalizer에 넣어 exact normalized evidence를 검증
4. O1 lifecycle contract suite: 상위 O1 lifecycle manager는 contract-faithful Provider mock을 상대로, 하위 O1 Provider는 contract-faithful Consumer harness를 상대로 동일한 subscription/job/restart/recovery/teardown 경계 시나리오를 실행
5. O1 Provider profile suite: 고정 `PerfMetricJob` profile로 하위 mock과 실제 Managed Element의 동적 notification/XML/SFTP/security/profile invariant를 검증
6. End-to-end suite: R1→A1→E2 제어와 실제 동적 O1→R1 DME assurance를 함께 검증

실제 Managed Element의 timestamp, KPI 값, file URI/size/hash를 golden fixture와 같게 요구하지 않는다. Exact fixture equality는 O1 Consumer normalizer suite에만 적용하고, live Provider suite는 capture한 동적 값 내부의 schema·identity·time·size·digest 관계를 검증한다.

상대 구현의 내부 객체를 import하는 white-box test만으로 contract conformance를 주장하지 않는다. 통합은 endpoint와 configuration을 mock에서 실제 profile로 바꾸는 작업으로 제한되어야 한다.

완료 gate는 다음처럼 분리한다.

- **Phase A — integration-ready upper release:** root handoff manifest, 권위 있는 schema/O1 profile/shared scenario catalog와 mock을 사용한 R1/A1-P 시험, fixed-byte O1 Consumer normalization, contract-faithful Provider mock을 사용한 `o1-lifecycle-contract`, 상위 mock end-to-end, 기존 회귀 및 정적 책임 경계 검사를 모두 통과한다. 상대 저장소나 하드웨어가 없어도 이 gate를 완료할 수 있다.
- **Phase B — final merged acceptance:** 마지막 병합 시 실제 Near-RT RIC A1-P Producer, 실제 O1 PM Provider 및 실제 RAN/E2 경로를 대상으로 A1/R1 real-target corpus, 동적 O1 Provider profile suite와 end-to-end trace를 통과한다. Phase A release의 Coordinator 판단 source를 수정하지 않고 endpoint, credential, deployment manifest만 교체해야 한다.

### 9. GUI와 실행 경로

GUI는 선택 가능한 Operator client로 남긴다.

보존할 GUI 역할:

- Operator intent 입력
- S0–S6 진행 상태
- LLM 판단, confidence 및 `theta*`
- active/monitored intent
- A1 policy lifecycle
- negotiation
- KPI/assurance 요약
- terminal outcome과 evidence log

새 O-RAN runtime에서 제거하거나 별도 legacy 도구로 격리할 GUI 역할:

- OAI Core/gNB/UE 직접 시작·정지
- USRP 준비와 RF 설정
- PRB/MCS/scheduler 직접 변경
- OAI/UE log 직접 접근
- SSH, tmux 또는 장비 process 관리

GUI 없이 동일 기능을 수행하는 headless entry point가 있어야 한다. GUI 표시 오류가 policy 처리나 안전 판정 결과를 변경하면 안 된다.

### 10. 직접 OAI·RAN 접근 제거

rApp 및 Non-RT 측의 권위 있는 runtime 경로에서 다음 접근이 없어야 한다.

- `OAIExecutor`, `MultiUECollector`, `SystemController`의 직접 생성·호출
- OAI telnet socket과 `ci` command
- gNB/UE SSH
- gNB stdout scraping
- UE tunnel counter 직접 읽기
- ping/iperf 기반 직접 KPI 수집
- OAI Core/gNB/UE process 관리
- USRP/RF 접근
- FlexRIC E42/native API 직접 호출
- E2AP/E2SM message 직접 생성
- naked `rrc_ue_id` 또는 lab-specific E2 identifier를 rApp domain에 노출

legacy 비교 코드가 필요하면 새 O-RAN runtime에서 기본적으로 도달할 수 없도록 격리한다. legacy path를 fallback이나 우회 제어면으로 사용하지 않는다.

### 11. 기존 실험·평가 자산 보존

가능한 범위에서 다음 실행·평가 자산을 보존한다.

- synthetic mode
- emulated mode
- live/integration mode
- rule-based baseline
- score-heuristic baseline
- trained tabular-RL baseline
- LLM without history
- LLM with history
- 공통 metric, aggregation, evidence 및 figure 생성

Coordinator의 판단 결과, A1 policy lifecycle, Near-RT action evidence 및 최종 intent satisfaction을 서로 다른 단계로 기록한다. 기존 direct-OAI action을 새 O-RAN policy와 동일한 것으로 취급하지 않는다.

### 12. 테스트와 검증

기존 테스트를 삭제하거나 assertion을 약화하는 방식으로 전환하지 않는다.

최소 검증 범위는 다음과 같다.

- 기존 S0–S6 transition과 terminal outcome
- natural-language parse와 typed re-entry
- strict schema rejection
- low-confidence/infeasible negotiation
- revised intent 재실행
- episode single-flight와 deadline
- continuous assurance
- history mode와 proposer hot-swap
- policy type discovery와 intent-to-policy mapping
- R1/A1 policy correlation
- duplicate, retry, timeout 및 out-of-order 처리
- fresh/stale/missing KPI 처리
- `ENFORCED`와 intent satisfaction의 구분
- policy conflict, update, delete 및 replacement
- rollback/withdraw 결과
- restart recovery
- GUI 없는 실행
- GUI와 headless 결과 의미의 일치
- mock과 실제 구현의 black-box conformance
- rApp core에서 direct OAI/SSH/telnet/USRP 의존이 제거되었는지에 대한 정적 검사

기존 test suite를 회귀 기준으로 사용한다. OAI 직접 제어만을 검사하는 테스트는 새 rApp core test로 위장하지 않고 legacy 또는 별도 integration scope로 재분류한다.

이 절의 "mock과 실제 구현" 검증은 한 번에 같은 환경에서 수행한다는 뜻이 아니다. Phase A에서는 mock target이, Phase B에서는 실제 target이 해당 versioned suite와 oracle을 통과한다. A1/R1은 같은 fixed corpus를 사용하지만, 실제 O1 Provider는 golden bytes equality가 아니라 같은 고정 profile의 dynamic invariant oracle을 사용한다.

### 13. 필수 최종 문서

최종 결과에 다음 문서를 포함한다.

- 목표 O-RAN component와 responsibility
- Operator intent부터 xApp policy consumption까지의 sequence
- A1 policy status와 PM/DME 기반 assurance sequence
- 기존 S0–S6와 전환 후 의미의 대응표
- R1/A1/O1/E2 책임 매트릭스
- 적용 specification/version/profile manifest
- intent-to-policy mapping과 capability admission
- shared schema와 fixture 사용법
- mock 및 conformance suite 실행법
- local, mock, integration, production profile 구분
- custom development adapter의 비표준 지위
- 기존 direct-OAI path에서 새 경로로의 migration mapping
- 격리 또는 제거된 legacy 기능
- 오류, retry, idempotency, conflict 및 restart 규칙
- O1 subscription→`PerfMetricJob` unlock→first-valid-file readiness, restart reconciliation, teardown 및 transport trust 설정
- 설정만으로 mock을 실제 endpoint로 교체하는 통합 절차
- 검증 명령과 실제 결과

문서, 코드, schema, fixture 및 테스트가 서로 다른 field·status·lifecycle 의미를 설명하면 완료로 간주하지 않는다.

### 14. 완료 판정

#### Phase A: 상위 독립 구현 완료

오케스트레이션 작업은 다음 조건을 모두 만족하면 상대 구현 없이도 `integration-ready`로 완료 판정할 수 있다.

- Operator intent가 기존 agentic pipeline을 거쳐 declarative A1 policy로 변환된다.
- rApp은 R1 A1 policy management service를 사용하고 A1을 직접 종단하지 않는다.
- Non-RT RIC Framework와 Near-RT RIC 사이의 최종 제어 경계는 표준 A1-P다.
- 최소 통합 policy type과 status schema가 하위 계약과 정확히 일치한다.
- rApp이 xApp, FlexRIC, OAI, E2 node 또는 USRP에 직접 접근하지 않는다.
- S0–S6, negotiation, adaptive `theta*`, history, assurance, fail-closed 및 terminal outcome이 보존된다.
- A1 create/update 응답이나 E2 ACK만으로 intent를 commit하지 않는다.
- A1 status와 fresh PM/DME KPI를 결합해 상위 intent satisfaction을 판단한다.
- 권위 있는 O1 fixture가 고정 O1 Performance Assurance/PM profile을 거쳐 R1 PM/DME evidence로 변환된다.
- O1 `FileDataReportingMnS` subscription, NETCONF/YANG `PerfMetricJob`, notification receiver와 SFTP retrieval의 desired-state lifecycle이 mock에서 검증된다.
- GUI가 선택 사항이며 headless 실행이 독립적으로 동작한다.
- custom HTTP는 개발 adapter로만 존재하고 최종 표준 경로를 대체하지 않는다.
- 상대 구현 없이 mock과 golden fixture로 상위 개발을 완료할 수 있다.
- A1-P mock, R1 mock, fixed-byte O1 Consumer normalizer, O1 lifecycle manager와 contract-faithful Provider mock 및 mock end-to-end suite가 shared scenario catalog의 applicable oracle로 통과한다.
- mock을 실제 endpoint로 바꿀 때 Coordinator 판단 코드 변경이 필요하지 않다.
- 기존 관련 회귀 테스트와 새 contract test가 모두 통과한다.
- architecture, contract, source, schema 및 test 간 cross-check에서 책임이나 의미 충돌이 없다.

#### Phase B: 최종 병합 완료

다음은 마지막 병합 시 양측 release를 함께 배치한 뒤 판정한다.

- Phase A에서 사용한 것과 같은 `a1p-producer-conformance` corpus가 실제 Near-RT RIC A1-P endpoint에서 통과한다.
- 실제 Managed Element의 동적 O1 PM output이 `o1-provider-profile-conformance`의 subscription/job/notification/SFTP/XML/security invariant를 통과하고, production normalizer가 그 capture를 valid DME evidence로 변환한다. 실제 KPI 값·timestamp·file digest를 fixed golden과 같게 요구하지 않는다.
- 실제 R1→A1→E2 제어와 O1→R1 DME assurance를 포함한 end-to-end success, no-action, failure/rollback, restart/recovery trace가 통과한다.
- 양측 root handoff/version/schema/profile/scenario/golden digest가 일치하고, source 수정 없이 endpoint·credential·deployment manifest 교체만으로 구동된다.
- 실제 backend capability/release manifest와 live E2 inventory가 shared schema를 통과하고, 양쪽 gNB의 post-patch Style 3/Action 1·reverse rollback profile 및 KPM readback이 exact digest/profile과 일치한다.
- RIC Control ACK만으로 성공하지 않고 같은 node/epoch의 control result와 serving-cell post-condition readback이 있어야 `APPLIED_VERIFIED`가 된다.

## 제약

- 구현 언어, library, framework, 내부 class, module 배치 및 세부 알고리즘 선택은 오케스트레이션이 결정한다.
- 기존 Agentic Intent Coordinator의 핵심 판단·협상·검증 로직을 불필요하게 새로 작성하거나 단순화하지 않는다.
- 기존 사용자 변경과 관련 없는 코드를 덮어쓰지 않는다.
- 하위 연구원의 저장소를 이 작업에서 수정하지 않는다.
- xApp 내부 알고리즘, FlexRIC native xApp, E2AP/E2SM, OAI patch 및 USRP 제어를 대신 구현하지 않는다.
- 상대 구현과 실제 하드웨어 없이도 이 저장소의 구현 및 검증을 완료할 수 있어야 한다.
- project-specific HTTP endpoint를 표준 A1-P라고 부르지 않는다.
- E2SM-KPM을 Near-RT RIC→Non-RT RIC의 임의 custom KPI uplink로 확장해 O1/R1 경계를 대체하지 않는다.
- rApp에서 xApp으로 직접 메시지를 보내는 경로를 최종 O-RAN 경로로 만들지 않는다.
- RAN intent 원문을 그대로 A1 policy로 전달하지 않는다.
- mock 성공 결과를 실제 RAN 적용 증거로 기록하지 않는다.
- unsupported, missing, stale, ambiguous 또는 uncorrelated 상태를 성공으로 취급하지 않는다.
- 계약에 없는 field, default, enum, status 또는 error 의미를 암묵적으로 추가하지 않는다.
- 하위 계약과 충돌하는 추측을 구현하지 않는다.
- 통합 시점 이전의 추가 대화를 구현 전제로 삼지 않는다.
- 테스트 통과를 위해 safety invariant, evidence requirement 또는 fail-closed 동작을 약화하지 않는다.
