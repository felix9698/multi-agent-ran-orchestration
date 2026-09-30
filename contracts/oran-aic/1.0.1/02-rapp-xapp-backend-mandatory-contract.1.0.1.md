# rApp–Near-RT RIC 병렬 개발 및 최종 O-RAN 통합 필수 계약

**계약 기준선:** `oran-aic/1.0.1`  
**대체 대상:** `oran-aic/1.0.0` (frozen, byte 그대로 보존; in-place 수정 금지)  
**문서 상태:** 양측 독립 개발을 위한 구속적 기준  
**적용 대상:** rApp/Non-RT RIC 측과 Near-RT RIC/xApp/E2/OAI 측  
**최소 Policy Type:** `AIC_UECellSteering_1.0.0` (변경 없음)  
**A1 API:** `A1-P/v2`  
**작성 기준일:** 2026-08-04  
**1.0.1 개정일:** 2026-08-07

## 0. 이 문서의 사용법

이 문서는 두 연구자가 마지막 통합 시점까지 상시 연락하지 않아도 독립적으로 구현한 결과가 맞물리도록 외부 계약을 선고정한다.

이 문서와 같은 directory의 `shared-contract-bundle/`은 문서와 동등한 규범 산출물이다. 전달할 때 문서만 떼어 보내지 않고 directory 전체와 root의 `handoff-manifest.1.0.1.json`을 함께 전달한다. Root manifest가 고정한 두 문서의 byte SHA-256, bundle manifest byte SHA-256 및 bundle manifest JCS SHA-256을 양측 release가 검증한다. Appendix A schema는 양측 release의 `contracts/oran-aic/1.0.1/`으로 추출하고, bundle의 O1 profile·raw fixture·golden vectors·scenario catalog·execution-profile assignment는 byte-for-byte 복사한다.

하위 연구원에게는 최소한 이 문서, `shared-contract-bundle/` 전체와 root manifest를 하나의 변경 불가 handoff로 전달한다. 상위 오케스트레이션에는 여기에 `01-agentic-intent-coordinator-oran-refactor-task.md`를 더해 전달한다. 마지막 병합까지 상대에게 질문할 수 없다고 가정하며, 미정/TBD/fixture placeholder를 실제 release 값으로 간주하거나 상대 구현의 내부 source를 import해 모호성을 메우지 않는다.

이 handoff에는 `shared-contract-bundle/execution-profile-assignment.1.0.1.json`과 그 schema가 반드시 포함된다. 하위 측은 어떤 scenario를 어떤 실행 profile로 돌릴지 **추론하거나 로컬 default를 만들지 않는다**. 배정은 이 문서와 함께 전달된 assignment 문서만이 권위이며, 그 규칙은 §17.3에 있다.

이 문서는 frozen `oran-aic/1.0.0`을 in-place 수정한 결과가 아니라 corrected `1.0.1` full package의 규범 문서다. `1.0.0`의 파일·manifest·digest와 그것으로 만든 기존 Phase A 증거는 historical evidence로 그대로 보존하며 삭제·소급 수정하지 않는다. Runtime과 conformance는 선택한 `1.0.1` authority와 digest를 fail-closed로 검증하고, `1.0.1` 검증이 실패했을 때 `1.0.0`으로 조용히 fallback하지 않는다. `1.0.0` 기준으로 만든 결과를 `1.0.1` 또는 통합 Phase A 완료 증거로 재사용하지 않는다.

`MUST`, `MUST NOT`, `SHOULD`, `MAY`는 각각 필수, 금지, 권고, 선택을 의미한다. 표와 예시에서 **고정**으로 표시한 identifier, enum, 의미 및 버전은 양측이 임의로 바꿀 수 없다.

내부 구현 언어, framework, process 수, database, build system, 수학적 알고리즘 및 directory 구조는 각 구현자의 자유다. 그러나 다음 항목은 구현 편의를 이유로 변경할 수 없다.

- O-RAN component의 책임과 배치
- R1/A1/O1/E2 경계
- A1-P 표준 API semantics
- project-specific A1 Policy Type의 field와 의미
- policy/status/KPI identifier와 version
- retry, duplicate, conflict, recovery 및 rollback 의미
- schema validation과 fail-closed 동작
- black-box acceptance criteria

충돌은 단일 순위로 표준을 덮어쓰지 않고 적용 영역으로 판정한다.

1. A1/R1/O1/E2의 표준 field, procedure, URI, status code, role 및 security semantics에는 본 문서가 pin한 O-RAN/ETSI 규격이 최우선이다. 본 문서로 규격을 변경하거나 축소해서는 안 된다.
2. `AIC_UECellSteering_1.0.0`의 project-specific field, lifecycle extension, safety invariant 및 규격이 선택지로 남겨 둔 부분은 본 문서가 권위 있다.
3. 본 문서와 오케스트레이션 작업 문서가 충돌하면 외부 계약은 본 문서, 구현 범위·산출물은 오케스트레이션 작업 문서를 따른다.
4. 기존 `rapp-xapp-interface-contract.md`, 기존 구현 및 code comment는 위 세 기준과 충돌하지 않는 범위에서만 참고한다.

규격과 본 문서의 project-specific requirement가 실제로 양립할 수 없으면 표준 동작을 보존하고 `compatibility-report.json`에 deviation을 기록한다. 그 구현을 `oran-aic/1.0.1` conformance 성공으로 표시하지 않는다.

기존 custom HTTP 계약에서 유용한 lifecycle, readback, KPI 및 안전 요구는 재사용하되, custom endpoint 자체는 최종 O-RAN 경계로 승계하지 않는다.

## 1. 선고정된 결정

| 항목 | 고정 결정 |
|---|---|
| rApp 외부 경계 | rApp은 R1 service를 사용한다. |
| A1-P Consumer | Non-RT RIC Framework가 담당한다. |
| A1-P Producer | Near-RT RIC가 담당한다. |
| xApp 위치 | Near-RT RIC 내부 application이다. A1 endpoint가 아니다. |
| 최종 제어 경계 | 표준 `A1-P/v2`다. |
| 최소 Policy Type | project-specific `AIC_UECellSteering_1.0.0`이다. |
| Policy Type 성격 | O-RAN A1 type mechanism을 따르지만 O-RAN이 사전 정의한 표준 Policy Type이라고 주장하지 않는다. |
| 정상 자율 objective | `BALANCE_PRB_LOAD`다. |
| 시험·명시적 복구 objective | `PIN_TO_CELL`이다. |
| concrete action 결정 | policy envelope 안에서 xApp이 수행한다. |
| intent conflict/priority | rApp이 A1 제출 전에 해소한다. |
| 동일 UE 물리 write 안전 | Near-RT RIC/xApp이 보장한다. |
| A1 status | enforcement와 type-specific execution status를 전달한다. |
| KPI/intent assurance | RAN에서 O1 Performance Assurance/PM으로 수집하고, SMO/Non-RT RIC이 R1 RAN OAM PM 또는 DME로 rApp에 제공한다. |
| 최종 intent 만족 판정 | rApp이 수행한다. |
| 개발용 custom HTTP | mock/migration adapter로만 허용한다. |
| 최종 merge 방식 | mock endpoint를 실제 endpoint/configuration으로 교체하며 Coordinator 판단 코드는 수정하지 않는다. |

## 2. 고정 O-RAN 아키텍처

```text
Operator
  │
  ▼
rApp: Agentic Intent Coordinator
  │  R1 services
  ▼
Non-RT RIC Framework
  ├─ R1 A1 Policy Management Service Producer
  ├─ R1 PM/DME Service Producer
  └─ A1-P Consumer
       │  Standard A1-P v2
       ▼
Near-RT RIC
  ├─ A1-P Producer / A1 termination
  ├─ policy routing and lifecycle
  └─ xApp
       │  E2AP + E2SM-KPM / E2SM-RC
       ▼
OAI E2 Node / RAN / USRP
```

성능·assurance 경로는 제어 경로와 별도로 다음과 같이 고정한다.

```text
OAI/O-RAN Managed Element: O-CU/O-DU/other applicable MF
  │  O1 Performance Assurance/PM MnS
  ▼
SMO/Non-RT RIC Framework: O1 Consumer + Assurance Correlator
  │  R1 RAN OAM PM service or R1 DME
  ▼
rApp
```

다음 배치는 `MUST`다.

- rApp은 R1을 통해 Non-RT RIC Framework의 서비스를 사용한다.
- rApp 자체를 A1-P Consumer로 구현하지 않는다.
- Non-RT RIC Framework가 A1-P Consumer 역할을 가진다.
- Near-RT RIC가 A1-P Producer와 A1 termination을 제공한다.
- xApp은 Near-RT RIC 내부에서 policy를 전달받는다.
- A1 `PolicyObject`는 특정 xApp process, binary 또는 내부 routing 주소를 포함하지 않는다.
- xApp과 OAI E2 node 사이의 near-real-time 제어·관측은 E2를 통해 수행한다.
- final profile의 non-real-time RAN performance data는 O1 Performance Assurance/PM으로 SMO에 수집한다.
- rApp은 FlexRIC, E42, E2SM payload, OAI 내부 API, gNB telnet, SSH 또는 USRP를 직접 다루지 않는다.

양측의 직접 병합 경계는 “xApp 전용 메시지”가 아니라 다음 두 O-RAN interface다.

1. 제어와 policy feedback: Non-RT RIC ↔ Near-RT RIC의 A1-P
2. non-real-time RAN performance: O-RAN Managed Element ↔ SMO의 O1 Performance Assurance/PM

R1은 상위 저장소 내의 rApp↔Non-RT RIC Framework 경계이다. SMO/Non-RT RIC은 A1 status의 policy/action scope·time과 O1 PM data를 correlation한 후 R1으로 rApp에 제공한다. E2SM-KPM은 Near-RT RIC/xApp의 결정과 readback에 사용하지만, Near-RT→Non-RT의 임의 custom KPI uplink로 확장하지 않는다.

Near-RT RIC 내부의 A1 policy→xApp message 형식은 하위 구현자의 내부 계약이다. 단, 그 내부 계약 때문에 본 문서의 외부 semantics가 달라져서는 안 된다.

### 2.1 팀별 소유 경계

| 영역 | 상위 연구자/저장소 | 하위 연구자/저장소 |
|---|---|---|
| rApp·S0–S6·Operator API/GUI | 구현·검증 | 비소유 |
| R1 service consumer | rApp에 구현 | 비소유 |
| 최소 Non-RT RIC Framework | R1 producer, A1-P Consumer, desired-state reconciliation 구현 | A1-P로만 접속 |
| A1-P Producer/termination | 소비·conformance 검증 | 구현·운영 |
| Policy Type·status schema | 계약 복사본을 validation에 사용 | 동일 계약 복사본을 discovery에서 제공 |
| A1 policy→xApp adapter | 비소유 | 구현 |
| xApp·E2·OAI·USRP | mock만 제공 | 구현·검증 |
| O1 NETCONF/YANG `PerfMetricJob` | Consumer로서 create/reconcile/delete하고 subscription 성공 뒤에만 `UNLOCKED`로 전환 | Provider로서 IOC/configuration을 구현하고 PM file을 생성 |
| O1 `FileDataReportingMnS` | subscription create/reconcile/delete, HTTPS notification 수신·영속화, SFTP 회수·검증 | subscription endpoint, `notifyFileReady`/`notifyFilePreparationError` 발행, SFTP file provider 구현 |
| O1 Consumer·PM normalization·assurance correlation | 구현 | O1 fixture와 실제 provider로 검증 지원 |
| R1 PM/DME exposure | 구현·DME type 등록 | 동일 schema의 source fixture 제공 |

최종 병합 전에 상대 측 소스를 import하거나 내부 class/API에 의존하지 않는다. 공유하는 것은 본 계약의 wire schema, fixture, manifest와 O-RAN interface뿐이다.

## 3. 규격 기준선

다음 version을 구현 기준으로 pin한다.

| 영역 | 규격 |
|---|---|
| A1 General | ETSI TS 103 983 V4.0.0 |
| A1 Transport | ETSI TS 103 986 V3.3.0 |
| A1 Application Protocol | ETSI TS 103 987 V4.3.0 |
| A1 Type Definitions | ETSI TS 103 988 V9.0.0 |
| A1 Test | ETSI TS 103 989 V4.2.0 |
| R1 General | ETSI TS 104 228 V11.0.0 |
| R1 Application Protocol | ETSI TS 104 231 V8.0.0 |
| O1 Interface | ETSI TS 104 043 V11.0.0 |
| O-RAN Security Requirements | ETSI TS 104 104 V9.1.0 |
| O-RAN Security Protocols | ETSI TS 104 107 V9.0.0 |
| 3GPP Generic NRM/PerfMetricJob | ETSI TS 128 622 V18.7.0 |
| 3GPP Generic NRM YANG | ETSI TS 128 623 V18.7.0, 3GPP Forge `Tag_Rel18_SA104` commit `dfada043e1a54453af779367615ad71e2a631268` |
| 3GPP Generic Management Services | ETSI TS 128 532 V18.3.0 |
| 3GPP NR Performance Measurements | ETSI TS 128 552 V18.11.0 |
| 3GPP Performance Measurement File Format | ETSI TS 132 432 V17.0.0 |
| 3GPP Performance Data XML Schema | ETSI TS 132 435 V10.0.0 |

구현 환경상 더 최신 version을 함께 지원할 수는 있지만, `oran-aic/1.0.1` compatibility profile은 위 version의 동작을 그대로 제공해야 한다. 조용히 다른 major version으로 대체하지 않는다.

## 4. R1 측 필수 동작

rApp은 R1에서 다음 기능을 소비한다.

- rApp registration, authentication 및 authorization
- service discovery
- A1 Policy Type discovery와 availability notification
- A1 Policy create, query, update 및 delete
- A1 Policy enforcement status query와 subscription
- R1 PM 또는 DME type discovery
- KPI/assurance data request 또는 subscription
- cell/topology/capability/operational data 조회

R1 API와 procedure를 project-specific `/policies` API로 다시 정의하지 않는다. ETSI TS 104 228과 ETSI TS 104 231의 service와 resource semantics를 따른다.

`oran-aic/1.0.1`에서 사용하는 R1 API version은 다음과 같이 pin한다. `1.0.0`에서 변경된 값은 없다.

| R1 API | version |
|---|---|
| Service registration | `1.2.0` |
| Service discovery | `1.2.0` |
| Service events subscription | `1.2.0` |
| Bootstrap | `1.0.0` |
| A1 policy management | `1.0.0` |
| DME data registration | `2.0.0-alpha.2` |
| DME data discovery | `2.0.0` |
| DME data access | `2.0.0-alpha.2` |
| HTTP push/pull data delivery | `1.0.0` |

R1AP V8.0.0에서 구체 REST API가 정의된 DME를 baseline delivery mechanism으로 사용한다. R1 RAN OAM PM service가 제품에 구현되어 있으면 함께 제공할 수 있으나, 그 여부가 `aic:policy-evidence:1.0.0` DME path를 변경하지 않는다.

R1 bootstrap/service/DME의 기준 resource는 다음과 같다. `apiVersion`의 semantic version 전체는 discovery/manifest에서 검증하고 URI에는 규격이 정한 major segment를 사용한다.

```text
GET    {r1ApiRoot}/bootstrap/v1/bootstrap-info
POST   {r1ApiRoot}/published-apis/v1/{rAppId}/service-apis
GET    {r1ApiRoot}/service-apis/v1/allServiceAPIs
POST   {r1ApiRoot}/data-registration/v2/production-capabilities
GET    {r1ApiRoot}/data-discovery/v2/dme-types
GET    {r1ApiRoot}/data-discovery/v2/dme-types/{dmeTypeId}
POST   {r1ApiRoot}/data-access/v2/data-jobs
GET    {r1ApiRoot}/data-access/v2/data-jobs/{dataJobId}
PUT    {r1ApiRoot}/data-access/v2/data-jobs/{dataJobId}
DELETE {r1ApiRoot}/data-access/v2/data-jobs/{dataJobId}
GET    {r1ApiRoot}/data-access/v2/data-jobs/{dataJobId}/status
POST   {dataPushUri}
GET    {dataPullUri}
```

- Service registration POST는 rApp이 **일반 request/response R1 service API**를 publish하는 배치에서만 사용한다. 단순히 API를 소비하는 rApp의 onboarding identity 및 DME push callback과 이 service registration을 혼동하지 않는다. Request의 `ServiceAPIDescription`에는 client-assigned `apiId`를 넣지 않고 `apiName`, `apiVersion="v1"`, `aefProfiles[]`, `communicationType="REQUEST_RESPONSE"` 및 `vendorSpecific-o-ran.org.fullApiVersions=["1.0.0"]`을 넣는다. Server가 응답/`Location`으로 할당한 API ID를 영속화한다.
- Service discovery GET은 canonical resource `/allServiceAPIs`와 `api-invoker-id={rAppId}`를 필수로 사용하고 필요한 경우 `api-name` 및 URI-major filter `api-version=v1`을 사용한다. 발견된 `vendorSpecific-o-ran.org.fullApiVersions`에 정확히 `1.0.0`이 있어야 한다. 상대 구현이 표준 문서의 대소문자 충돌을 위해 lowercase alias를 추가 제공할 수는 있지만, 본 profile의 conformance request는 `/allServiceAPIs`로 고정한다.
- DME Data Producer는 `data-registration/v2/production-capabilities`로 `aic:policy-evidence:1.0.0`의 production/delivery schema를 등록하고, rApp은 discovery 후 `data-access/v2/data-jobs`에서 standard `DataJobInfo`를 생성한다.
- ETSI TS 104 231 V8.0.0 본문 7.3.2는 Data access URI major를 `v2`로 규정한다. Annex A.3.3.2 server example의 `v1`과 충돌할 때 본 compatibility profile은 본문의 explicit `shall`과 `2.0.0-alpha.2` API version에 맞춰 `v2`를 사용하며, 상대 implementation이 이 profile을 discovery로 광고한 경우 같은 URI를 제공해야 한다.
- `/data-access/v2/data` 같은 direct record endpoint는 규격 resource가 아니므로 만들거나 호출하지 않는다. 실제 payload는 data job이 협상한 `dataPushUri` 또는 `dataPullUri`로 전달한다.

A1 policy management의 기준 URI와 version signaling은 다음과 같다.

```text
GET    {apiRoot}/a1-policy-management/v1/policy-types
GET    {apiRoot}/a1-policy-management/v1/policy-types/{policyTypeId}
POST   {apiRoot}/a1-policy-management/v1/policies
GET    {apiRoot}/a1-policy-management/v1/policies
PUT    {apiRoot}/a1-policy-management/v1/policies/{policyId}
GET    {apiRoot}/a1-policy-management/v1/policies/{policyId}
DELETE {apiRoot}/a1-policy-management/v1/policies/{policyId}
GET    {apiRoot}/a1-policy-management/v1/policies/{policyId}/status
POST   {apiRoot}/a1-policy-management/v1/policies/subscriptions
PUT    {apiRoot}/a1-policy-management/v1/policies/subscriptions/{subscriptionId}
GET    {apiRoot}/a1-policy-management/v1/policies/subscriptions/{subscriptionId}
DELETE {apiRoot}/a1-policy-management/v1/policies/subscriptions/{subscriptionId}
POST   {notificationDestination}
```

- ETSI TS 104 231 V8.0.0 본문 9.1.3의 `a1-policy-managment` 표기와 달리, 같은 규격의 각 resource definition과 규범 OpenAPI Annex A.5.1.2가 고정한 `a1-policy-management/v1` server path를 사용한다. 양측은 이 profile에서 오타 형태의 alias를 상대가 지원할 것으로 가정하지 않는다.
- rApp은 request `Version: 1.0.0`을 사용하고 Producer의 response `Version` header를 검증한다.
- create POST body는 표준 `PolicyObjectInformation = policyObject + nearRtRicId + optional policyTypeId`를 사용하되, 이 compatibility profile은 `policyTypeId=AIC_UECellSteering_1.0.0`을 필수로 전송한다.
- item UPDATE `PUT .../policies/{policyId}` body는 wrapper가 아닌 bare `PolicyObject`다. 따라서 JSON Patch/assertion 경로도 `/trace/...`, `/policyRevision`처럼 PolicyObject root에서 시작하며 존재하지 않는 `/policyObject/...` wrapper를 가정하지 않는다.
- `nearRtRicId`는 하위 release의 digest-pinned `backend-capability-manifest.json`에서 가져온 값을 그대로 사용한다. R1/A1 discovery에서 관측한 실제 deployment 식별자와 다르면 상위는 policy create 전에 fail closed하며 추측·alias 치환을 하지 않는다.
- `policyId`는 R1 A1 policy management Producer가 할당하고, 그 Producer가 동일 ID로 A1-P PUT을 수행한다.
- R1 status subscription은 `PolicyStatusSubscription`을 사용한다. Callback body는 `A1PolicyStatusChangeNotification` 래퍼이며 `subscriptionId`와 1개 이상의 `policyStates[]` entry를 포함하고, 각 entry는 `policyId`와 해당 `PolicyStatusObject`를 포함한다. A1-P에서 수신한 bare `PolicyStatusObject`를 R1 callback body로 직접 전송하지 않는다.

### 4.1 최초 R1 create의 응답 유실·중복 방지

R1 create는 POST이고 rApp이 사전에 `policyId`를 모르므로, A1 PUT retry와 별도의 first-create deduplication을 보장한다.

- 멱등성 key는 `(authenticated rAppId, nearRtRicId, policyTypeId, policyObject.trace.idempotencyKey)`다. `authenticated rAppId`는 R1 등록·인증 context에서 얻으며 client가 임의로 body에 삽입한 문자열을 신뢰하지 않는다.
- Non-RT RIC Framework는 A1-P PUT 전에 key→assigned `policyId`→canonical payload digest를 write-ahead ledger에 영속화한다.
- 최초 성공은 표준 `201 Created`, `Location`, `PolicyObjectInformation`을 반환한다.
- 응답 유실 후 동일 key·payload POST가 반복되면 새 `policyId`나 A1 PUT/E2 write를 만들지 않고 최초 `201`, 같은 `Location`, 같은 `PolicyObjectInformation`을 재사용한다.
- 같은 key에 다른 canonical payload가 오면 `409` 및 zero downstream write다.
- ledger 상태가 불확실하면 rApp/Framework는 `GET /policies?nearRtRicId=...&policyTypeId=...`와 individual policy query로 `trace.idempotencyKey`를 reconciliation한 후에만 재시도한다.
- CI는 “R1 POST 성공 후 response loss”와 “Framework crash after ledger/before A1 PUT”을 별도 scenario로 검증한다.

Non-RT RIC Framework는 다음을 보장해야 한다.

- rApp 요청과 A1 request/response/status의 identifier mapping을 영속화한다.
- A1 status notification이 유실되어도 status query로 복구한다.
- Near-RT RIC restart 후 desired policy를 표준 A1 query와 PUT으로 reconcile한다.
- A1 `PolicyStatusObject`를 R1 policy status service로 그대로 추적 가능하게 제공한다.
- PM/DME data와 policy identifier의 상위 correlation이 가능하도록 time, scope 및 source metadata를 보존한다.

## 5. A1-P v2 필수 동작

Near-RT RIC 측은 ETSI TS 103 987 V4.3.0의 A1-P v2를 구현한다.

기준 URI는 다음 형식이다.

```text
{apiRoot}/A1-P/v2/<ResourceUriPart>
```

최소 지원 resource와 operation은 다음과 같다.

```text
GET    .../policytypes
GET    .../policytypes/{policyTypeId}
GET    .../policytypes/{policyTypeId}/policies
PUT    .../policytypes/{policyTypeId}/policies/{policyId}
GET    .../policytypes/{policyTypeId}/policies/{policyId}
DELETE .../policytypes/{policyTypeId}/policies/{policyId}
GET    .../policytypes/{policyTypeId}/policies/{policyId}/status
POST   {notificationDestination}
```

Policy status subscription은 create/update `PUT`의 표준 query parameter인 `notificationDestination`을 사용한다. PolicyObject 본문에 callback field를 넣지 않는다. Update PUT에서 해당 parameter를 생략하면 표준 절차에 따라 기존 notification을 해제한다.

표준 응답 의미를 바꾸지 않는다.

| 동작 | 성공 응답 |
|---|---:|
| Policy 생성 | `201 Created`, 생성된 `PolicyObject`, `Location` header |
| Policy 갱신 | `200 OK`, 갱신된 `PolicyObject` |
| Policy 조회 | `200 OK` |
| Policy 삭제 | `204 No Content` |
| Policy status 조회 | `200 OK` |
| status notification 수신 | `204 No Content` |

오류는 표준 `ProblemDetails`와 함께 다음 의미를 지킨다.

| 상황 | 응답 | side effect |
|---|---:|---|
| malformed JSON 또는 schema 불일치 | `400` | Policy 미생성, zero write |
| 존재하지 않는 `policyTypeId` 또는 `policyId` | `404` | zero write |
| 동일·중첩·충돌 policy 또는 stale revision | `409` | 기존 Policy 유지, zero new write |
| 인증 실패 | 적용 security profile의 `401/403` | zero write |

`202 receipt`, `/events`, `?rollback=true` 등 기존 custom API 의미를 A1-P 표준 resource에 추가하지 않는다.

## 6. 개발용 custom adapter

기존 `ts-northbound` 또는 다음 endpoint는 개발·mock·migration adapter로 남길 수 있다.

```text
/capability
/health
/policies
/policies/{id}/events
/kpi
/ran/ues
/ran/cells
```

다음 조건을 모두 만족해야 한다.

- custom API를 A1 또는 표준 O-RAN interface라고 표기하지 않는다.
- final rApp–Near-RT RIC 경계로 사용하지 않는다.
- A1-P adapter 뒤의 내부 compatibility layer로만 둔다.
- 같은 policy schema, decision rule, zero-write rule 및 readback rule을 사용한다.
- 표준 A1-P 경로와 custom adapter의 상태가 서로 다른 source of truth가 되지 않는다.
- final conformance와 end-to-end acceptance는 custom endpoint 없이 통과한다.

## 7. 고정 A1 Policy Type

### 7.1 Identifier와 성격

고정 identifier는 다음과 같다.

```text
AIC_UECellSteering_1.0.0
```

이는 O-RAN A1 type mechanism과 SemVer를 따르는 **project-specific Policy Type**이다. `ORAN_TrafficSteeringPreference_*`처럼 O-RAN이 사전 정의한 Policy Type이라고 주장하지 않는다.

Near-RT RIC는 표준 Policy Type discovery에서 이 identifier를 반환하고, 조회 시 다음을 포함한 `PolicyTypeObject`를 반환해야 한다.

- `policySchema`
- `statusSchema`

Schema dialect는 JSON Schema Draft 2020-12로 고정한다. Schema는 다음을 만족한다.

- `$schema`와 `$id` 포함
- 모든 level에서 `additionalProperties: false`
- behavior-critical field에 암묵적 default 사용 금지
- unknown field 거절
- O-RAN A1TD `common_1.0.0`의 `UeId`/`CellId` 중 본 profile이 허용하는 `guAmfUeNgapId`/NR `ncI` 구조와 범위를 그대로 사용
- Appendix A의 관련 definition을 포함한 standalone schema 제공; 외부 network `$ref`에 의존하지 않음
- RFC 8785 JSON Canonicalization 후 SHA-256 digest 제공
- validator는 Draft 2020-12의 `format-assertion` vocabulary를 활성화해야 하며 `uuid`, `uri`, `date-time`을 annotation으로만 처리해서는 안 됨
- 모든 wire timestamp는 offset 형태가 아니라 RFC 3339 UTC `Z` 형식만 허용하며, Appendix A의 `UtcDateTime` pattern도 함께 검증

### 7.2 Canonical PolicyObject

다음 object의 field, type 및 의미가 `1.0.0` 기준선이다.

```json
{
  "scope": {
    "ueId": {
      "guAmfUeNgapId": {
        "guAmI": {
          "plmnId": { "mcc": "208", "mnc": "95" },
          "amfRegionId": "00",
          "amfSetId": "001",
          "amfPointer": "00"
        },
        "amfUeNgapId": 2
      }
    }
  },
  "steeringObjective": {
    "kind": "BALANCE_PRB_LOAD",
    "actionEnvelope": {
      "allowedCells": [
        {
          "plmnId": { "mcc": "208", "mnc": "95" },
          "cId": { "ncI": 12345678 }
        },
        {
          "plmnId": { "mcc": "208", "mnc": "95" },
          "cId": { "ncI": 87654321 }
        }
      ],
      "forbiddenCells": []
    },
    "improvementThresholdPrb": 10
  },
  "constraints": {
    "maxActuationsPerEpisode": 1,
    "minSecondsBetweenActuations": 30,
    "requiredKpiFreshnessMs": 3000,
    "actionDeadlineMs": 10000
  },
  "validity": {
    "notBefore": "2026-08-04T00:00:00Z",
    "expiresAt": "2026-08-04T00:30:00Z"
  },
  "priority": 75,
  "rollbackPolicy": {
    "on": ["READBACK_MISMATCH", "APPLY_FAILED"],
    "timeoutMs": 10000
  },
  "trace": {
    "intentId": "768f56d8-2d45-4c05-b00b-7b76e8e2ef61",
    "intentRevision": 1,
    "policyRevision": 1,
    "idempotencyKey": "768f56d8-2d45-4c05-b00b-7b76e8e2ef61:1",
    "correlationId": "f993a697-778f-4551-b88c-5a5c07a09b1d",
    "producerId": "agentic-intent-coordinator"
  }
}
```

모든 top-level field는 필수다. Nested field의 필수 여부·type·bound는 다음과 같이 고정하며 암묵적 default는 없다.

| field | 고정 의미 |
|---|---|
| `scope.ueId` | O-RAN A1TD 형식의 대상 UE |
| `steeringObjective.kind` | xApp이 사용할 objective profile |
| `allowedCells` | xApp이 선택할 수 있는 cell의 완전한 집합 |
| `forbiddenCells` | 어떤 내부 경로에서도 선택할 수 없는 cell |
| `improvementThresholdPrb` | `BALANCE_PRB_LOAD`의 최소 개선 조건, percentage point |
| `constraints` | 한 episode의 write 횟수·간격·freshness·deadline 제한 |
| `validity` | UTC RFC 3339 policy 유효 구간 |
| `priority` | 상위 의사결정 provenance; Near-RT의 임의 preemption 권한이 아님 |
| `rollbackPolicy` | xApp이 자동 rollback할 수 있는 사전 승인 조건 |
| `trace` | intent–policy correlation과 retry/revision 정보 |

| field | type/bound | presence |
|---|---|---|
| `scope` | object, `additionalProperties: false` | required; `ueId` exactly one |
| `scope.ueId` | A1TD `common_1.0.0#/$defs/UeId` | required |
| `steeringObjective.kind` | `BALANCE_PRB_LOAD \| PIN_TO_CELL` | required |
| `actionEnvelope.allowedCells` | unique A1TD `CellId` array, 1..64 | required |
| `actionEnvelope.forbiddenCells` | unique A1TD `CellId` array, 0..64 | required |
| `improvementThresholdPrb` | number, 0..100 percentage points | `BALANCE_PRB_LOAD`에서 required, `PIN_TO_CELL`에서 forbidden |
| `maxActuationsPerEpisode` | integer, constant `1` | required |
| `minSecondsBetweenActuations` | integer, 0..86,400 | required |
| `requiredKpiFreshnessMs` | integer, 1..60,000 | required |
| `actionDeadlineMs` | integer, 1..120,000 | required |
| `validity.notBefore`, `validity.expiresAt` | RFC 3339 UTC `date-time` | both required; `expiresAt > notBefore` runtime validation |
| `priority` | integer, 0..100 | required |
| `rollbackPolicy.on` | unique array of `READBACK_MISMATCH \| APPLY_FAILED`, 0..2 | required; `APPLY_FAILED` rollback은 status가 `writeMayHaveOccurred=true`일 때만 가능 |
| `rollbackPolicy.timeoutMs` | integer, 1..120,000 | required |
| `trace.intentId`, `trace.correlationId` | UUID string | required |
| `trace.intentRevision`, `trace.policyRevision` | integer, minimum 1 | required |
| `trace.idempotencyKey`, `trace.producerId` | ASCII `[A-Za-z0-9._:/-]`, 1..128 bytes | required |

모든 object에 `additionalProperties: false`를 적용한다. Array uniqueness는 RFC 8785 canonicalized item을 기준으로 판정한다. Schema가 표현하지 못하는 cross-field 규칙도 runtime에서 동일하게 validation하며, 위반은 `400` 및 zero write다.

### 7.3 Identity 규칙

- UE는 ETSI TS 103 988의 완전한 `UeId`를 사용한다.
- 현재 profile의 최소 지원 UE 형식은 `guAmfUeNgapId`다.
- naked `amfUeNgapId`, IMSI, RNTI 또는 `rrc_ue_id`를 A1 scope로 사용하지 않는다.
- `rrc_ue_id` 등 E2/RRC 내부 identifier의 resolution은 Near-RT RIC/xApp 책임이다.
- Cell은 `CellId = plmnId + cId.ncI`를 사용한다.
- lab alias, PCI, process name 또는 E2 node address를 `CellId` 대신 사용하지 않는다.
- 구조적으로 잘못된 identity는 schema `400`이다. 구조는 유효하지만 현재 topology에서 unknown 또는 ambiguous한 identity는 추측하지 않고 policy를 zero write의 `NOT_ENFORCED + SCOPE_NOT_APPLICABLE + AIC_SCOPE_NOT_FOUND`로 보고한다.

### 7.4 Objective 규칙

v1은 다음 objective를 지원한다.

| 값 | 용도 | xApp 자유도 |
|---|---|---|
| `BALANCE_PRB_LOAD` | 정상 autonomous path | allowed cell 중 fresh PRB/KPI에 따라 target 선택 |
| `PIN_TO_CELL` | 시험, 명시적 override, 복구 | allowed cell은 정확히 하나이며 write 필요 여부만 판단 |

`BALANCE_PRB_LOAD`가 기본 연구 경로다. `PIN_TO_CELL`을 정상 agentic path의 기본 출력으로 사용하지 않는다.

다음 semantics는 필수다.

- `allowedCells`는 최소 1개, 중복 금지다.
- 하나의 PolicyObject 내에서 `forbiddenCells`와 `allowedCells`가 겹치면 `400`, zero write다. 이미 존재하는 다른 policy와 scope/control axis가 overlap하면 `409`다.
- `PIN_TO_CELL`은 `allowedCells`가 정확히 1개여야 한다.
- `BALANCE_PRB_LOAD`는 fresh KPI가 없으면 target을 추측하지 않는다.
- already-on-target이면 `NO_ACTION + noAction.reason=ALREADY_ON_TARGET`, zero write다.
- 예상 개선이 `improvementThresholdPrb` 미만이면 `NO_ACTION + noAction.reason=IMPROVEMENT_BELOW_THRESHOLD`, zero write다.
- fresh coherent snapshot에는 도달했지만 envelope 안에 eligible target이 없으면 `NO_ACTION + noAction.reason=NO_ELIGIBLE_TARGET`, zero write다. KPI freshness나 dependency가 상실된 경우는 이 reason으로 숨기지 않고 `NOT_ENFORCED` 또는 `ABORTED_NO_WRITE`로 처리한다.
- action envelope 밖의 target은 어떤 internal code path에서도 실행할 수 없다.
- Operator intent 만족 여부를 xApp이 판정하지 않는다.

### 7.5 Priority 규칙

기본 mapping은 다음과 같이 고정한다.

| rApp priority | A1 policy `priority` |
|---|---:|
| `CRITICAL` | 100 |
| `HIGH` | 75 |
| `MEDIUM` | 50 |
| `LOW` | 25 |

rApp은 의미적 충돌을 해소한 뒤 하나의 effective policy를 제출한다. Near-RT RIC는 다른 `policyId`를 priority만으로 자동 삭제·수정·supersede하지 않는다.

## 8. A1 Policy lifecycle과 xApp episode lifecycle

A1 policy resource lifecycle과 개별 actuation episode는 서로 다른 상태 축이다.

### 8.1 A1 enforcement

표준 field는 다음 값을 사용한다.

- `ENFORCED`
- `NOT_ENFORCED`

`NOT_ENFORCED`일 때 표준 `enforceReason`을 제공한다.

- `SCOPE_NOT_APPLICABLE`
- `STATEMENT_NOT_APPLICABLE`
- `OTHER_REASON`

`201`, `200` 또는 `ENFORCED`는 Operator intent 만족이나 concrete RAN action 성공을 의미하지 않는다.

매핑은 다음과 같이 고정한다.

- policy가 유효하고 Near-RT RIC가 그 statements를 실행 중이거나 지속적으로 적용할 수 있으면 `ENFORCED`다. Episode가 `SCHEDULED`, `COMPUTING`, `NO_ACTION` 또는 verified action 상태인 것은 이 판정과 모순되지 않는다.
- scope가 적용되지 않거나 statement를 지원할 수 없거나 E2/control dependency 상실로 policy를 적용할 수 없으면 `NOT_ENFORCED`다.
- `APPLIED_VERIFIED`는 action effect의 evidence일 뿐, Operator intent satisfaction 판정은 아니다.

### 8.2 Project policy state

`aicStatus.policyState`는 다음 enum으로 고정한다.

```text
ACTIVE
NOT_ENFORCED
EXPIRED
CANCELLED
SUPERSEDED
RECOVERY_PENDING
ERROR
```

`policyTerminal`은 A1 resource 전체가 다시는 변경될 수 없다는 뜻이 아니라, **해당 `policyRevision`의 lifecycle이 종료**되었음을 뜻한다. 더 높은 revision은 같은 A1 policy resource에 새 lifecycle을 시작할 수 있다.

Policy revision state transition은 다음으로 고정한다.

| 현재 policy state | event/condition | 다음 state | `policyTerminal` |
|---|---|---|---:|
| resource 없음 | create/update accepted, `now < notBefore` | `NOT_ENFORCED` | false |
| resource 없음/이전 revision | validity open, dependencies ready | `ACTIVE` | false |
| resource 없음/이전 revision | validity open, dependency/scope not applicable | `NOT_ENFORCED` | false |
| `NOT_ENFORCED` | `notBefore` 도달 또는 dependency 회복, validity open | `ACTIVE` | false |
| `ACTIVE` | scope/dependency/KPI readiness 상실 | `NOT_ENFORCED` | false |
| `ACTIVE` | in-flight write 결과 불확실 | `RECOVERY_PENDING` | false |
| `RECOVERY_PENDING` | fresh readback으로 안전 상태 확정, validity open | `ACTIVE` | false |
| `RECOVERY_PENDING` | bounded recovery 실패/quarantine | `ERROR` | true |
| any non-terminal | `expiresAt` 도달 | `EXPIRED` | true |
| any non-terminal | A1 DELETE 직전 최종 notification | `CANCELLED` | true |
| old revision | higher revision accepted | `SUPERSEDED` | true |
| any non-terminal | unrecoverable invariant/internal failure | `ERROR` | true |

- `notBefore` 이전은 `NOT_ENFORCED + OTHER_REASON`이며 episode를 만들지 않는다.
- `ACTIVE ↔ NOT_ENFORCED`는 validity 구간 안에서 복구 가능한 transition이다. `NOT_ENFORCED`를 항상 terminal failure로 해석하지 않는다.
- `EXPIRED`, `CANCELLED`, `SUPERSEDED`, `ERROR`는 해당 revision에 대해 terminal이며 새 normal episode를 만들지 않는다.
- higher revision acceptance은 old revision의 final `SUPERSEDED` snapshot을 영속·통지한 후 new revision을 `ACTIVE` 또는 `NOT_ENFORCED`로 시작한다. `statusSeq`는 같은 producer epoch에서 계속 증가한다.

### 8.3 Episode state

`aicStatus.episodeState`는 다음 enum으로 고정한다.

```text
SCHEDULED
COMPUTING
NO_ACTION
ABORTED_NO_WRITE
APPLYING
APPLIED_UNVERIFIED
APPLIED_VERIFIED
READBACK_MISMATCH
APPLY_FAILED
ROLLING_BACK
ROLLED_BACK_VERIFIED
ROLLBACK_FAILED
ROLLBACK_UNKNOWN
RECOVERY_PENDING
QUARANTINED
```

다음 의미는 변경할 수 없다.

- `NO_ACTION`: 계산은 성공했으나 `noAction.reason`으로 명시한 이유 때문에 RAN write가 필요하지 않았음. 이는 Operator intent 만족을 뜻하지 않는다.
- `ABORTED_NO_WRITE`: episode 시작 후 E2 send 전에 freshness, readiness, lock, validity 또는 deadline precondition이 깨져 zero write로 종료함
- `APPLYING`: E2 control attempt가 시작됨
- `APPLIED_UNVERIFIED`: control은 전달되었으나 RAN effect가 확인되지 않음
- `APPLIED_VERIFIED`: KPM/readback으로 의도한 serving cell이 확인됨
- `READBACK_MISMATCH`: 관측 serving cell이 의도한 target과 다름
- `APPLY_FAILED`: E2 control이 NACK였고, zero-effect가 확정되었거나 부분 적용 가능성에 대해 사전 승인 rollback을 즉시 요청한 상태
- `RECOVERY_PENDING`: restart/timeout으로 기존 write 결과가 불확실함
- `ROLLED_BACK_VERIFIED`: restore 결과가 readback으로 확인됨
- `ROLLBACK_UNKNOWN`: rollback 전송 이후 결과 확인 불가
- `QUARANTINED`: 명시적 상위 revision 전까지 추가 write 금지

E2 ACK만으로 `APPLIED_VERIFIED`에 진입하지 않는다.

`episodeTerminal=true`는 `NO_ACTION`, `ABORTED_NO_WRITE`, `APPLIED_VERIFIED`, rollback을 시작하지 않은 `READBACK_MISMATCH`, zero-effect가 확정된 `APPLY_FAILED`, `ROLLED_BACK_VERIFIED`, `QUARANTINED`에서만 허용한다. `READBACK_MISMATCH` 또는 `writeMayHaveOccurred=true`인 `APPLY_FAILED`에서 automatic rollback을 시작할 경우 같은 status snapshot에 `rollback.state=REQUESTED`, `episodeTerminal=false`를 기록한 후 `ROLLING_BACK`으로 전이한다. `writeMayHaveOccurred=false`인 `APPLY_FAILED`만 rollback write 없이 terminal로 끝난다. NACK인데 `writeMayHaveOccurred=true`이고 rollback이 사전 승인되지 않았거나 restore snapshot이 유효하지 않으면 terminal `APPLY_FAILED`로 끝내지 않고 persisted UE/policy fence와 함께 즉시 `NOT_ENFORCED + RECOVERY_PENDING`으로 전이해 fresh readback만 수행한다. `ROLLBACK_FAILED`/`ROLLBACK_UNKNOWN`은 이유를 영속화한 뒤 zero-write `QUARANTINED`로 전이하는 비종료 상태다. `RECOVERY_PENDING`은 결과를 확정할 수 없는 비종료 상태다.

### 8.4 허용 transition과 불변식

| 현재 episode state | event/condition | 다음 state | 물리 write |
|---|---|---|---:|
| 없음 | policy accepted, valid, dependencies ready | `SCHEDULED` | 0 |
| `SCHEDULED` | decision worker starts | `COMPUTING` | 0 |
| `COMPUTING` | already target/threshold 미달/eligible target 없음 | 해당 `noAction.reason`의 `NO_ACTION` | 0 |
| `SCHEDULED`/`COMPUTING` | send 전 precondition 상실 | `ABORTED_NO_WRITE` | 0 |
| `COMPUTING` | target selected and every precondition true | `APPLYING` | 최대 1 |
| `APPLYING` | E2 receipt, effect not yet observed | `APPLIED_UNVERIFIED` | 추가 0 |
| `APPLYING` | E2 NACK, zero-effect 확정 | terminal `APPLY_FAILED` | 새 write 0; rollback 금지 |
| `APPLYING` | E2 NACK, 부분 적용 가능, valid restore와 rollback 사전 승인 | nonterminal `APPLY_FAILED + rollback.REQUESTED` | recovery write는 다음 transition에서 최대 1 |
| `APPLYING` | E2 NACK, 부분 적용 가능, rollback 미승인/불가 | `RECOVERY_PENDING` | 새 normal write 0; fresh readback only |
| `APPLYING` | timeout/crash and write occurrence uncertain | `RECOVERY_PENDING` | 추가 0 |
| `APPLIED_UNVERIFIED` | fresh readback equals target | `APPLIED_VERIFIED` | 0 |
| `APPLIED_UNVERIFIED` | fresh readback differs from target | `READBACK_MISMATCH` | 0 |
| `APPLIED_UNVERIFIED` | readback deadline/crash | `RECOVERY_PENDING` | 0 |
| `READBACK_MISMATCH`/`APPLY_FAILED` (`episodeTerminal=false`, `rollback.state=REQUESTED`, `writeMayHaveOccurred=true`) | rollback pre-authorized and snapshot valid | `ROLLING_BACK` | rollback 최대 1 |
| `ROLLING_BACK` | restore readback equals snapshot | `ROLLED_BACK_VERIFIED` | 0 |
| `ROLLING_BACK` | definite rollback failure | `ROLLBACK_FAILED` | 0 |
| `ROLLING_BACK` | result cannot be determined | `ROLLBACK_UNKNOWN` | 0 |
| `ROLLBACK_FAILED`/`ROLLBACK_UNKNOWN` | reason and ledger persisted | `QUARANTINED` | 0 |
| `RECOVERY_PENDING` | fresh readback equals selected target | `APPLIED_VERIFIED` | 0 |
| `RECOVERY_PENDING` | fresh readback resolves a different known cell | `READBACK_MISMATCH` | 0 |
| `RECOVERY_PENDING` | recovery deadline expires | `QUARANTINED` | 0 |

- 위 표에 없는 transition은 금지한다.
- episode의 최초 상태는 `SCHEDULED`다. Policy가 적용 불가하면 episode를 만들지 않고 `NOT_ENFORCED` status만 제공한다.
- `ENFORCED + ACTIVE` 조합은 `SCHEDULED`, `COMPUTING`, `NO_ACTION`, `APPLYING`, `APPLIED_UNVERIFIED`, `APPLIED_VERIFIED`, `READBACK_MISMATCH`, `APPLY_FAILED`, `ROLLING_BACK`, `ROLLED_BACK_VERIFIED`와 함께 사용할 수 있다.
- `NOT_ENFORCED`는 `policyState=NOT_ENFORCED|EXPIRED|CANCELLED|SUPERSEDED|RECOVERY_PENDING|ERROR`와만 사용한다. `ABORTED_NO_WRITE`는 원인에 따라 `NOT_ENFORCED` policy state와 함께 사용한다. 비종료 `ROLLBACK_FAILED`/`ROLLBACK_UNKNOWN`은 `NOT_ENFORCED + RECOVERY_PENDING + policyTerminal=false`이고, 다음 zero-write transition의 `QUARANTINED`에서만 `NOT_ENFORCED + ERROR + policyTerminal=true`가 된다.
- `enforceReason`은 `NOT_ENFORCED`에서만 필수이고 `ENFORCED`에서는 금지한다.
- higher revision update는 기존 episode가 terminal이거나 fresh readback으로 recovery된 후에만 새 `SCHEDULED` episode를 만든다. 새 revision에서 `statusSeq`는 계속 증가한다.
- policy expiry 이후에는 새 normal write를 시작하지 않는다. Expiry 전에 이미 전송된 write는 readback/recovery를 완료하되, 사전 승인된 rollback 외의 추가 write를 금지한다.

### 8.5 `BALANCE_PRB_LOAD` recurring evaluation trigger

유효한 장기 policy가 최초 1회만 계산하고 멈추지 않도록 trigger를 다음과 같이 고정한다.

- 최초 trigger는 `notBefore` 이후 policy가 `ACTIVE`가 되고 요구 freshness 내의 coherent KPM snapshot이 존재할 때다.
- 이후 trigger는 **직전 evaluation에 사용하지 않은 새 coherent KPM snapshot**이 도착하고, 같은 policy/UE의 이전 episode가 terminal이며, fence/quarantine가 없을 때다.
- Snapshot identity는 `(policyId, policyRevision, observationWindowEnd, RFC8785-canonical KPI payload digest)`로 고정한다. 같은 identity의 duplicate/out-of-order sample은 새 episode를 만들지 않는다.
- `minSecondsBetweenActuations`는 **normal write** 간 cooldown이다. Cooldown 중 도착한 snapshot은 최신 1개로 coalesce하고, cooldown 종료 시 아직 fresh한 경우에만 evaluation한다. Cooldown을 이유로 E2 write를 먼저 수행하지 않는다.
- `NO_ACTION` 또는 `ABORTED_NO_WRITE` 종료 후에도 더 새로운 snapshot은 새 episode를 trigger할 수 있다. 같은 snapshot을 tight loop로 재계산하지 않는다.
- Snapshot이 없거나 stale이면 episode 시작 전에는 `NOT_ENFORCED`/episode 없음으로 보고한다. `SCHEDULED`/`COMPUTING` 후 stale/readiness/lock/deadline 상실은 `ABORTED_NO_WRITE`로 종료한다. `APPLYING` 이후에는 write 가능성 때문에 `RECOVERY_PENDING` 규칙을 적용한다.
- Higher revision은 old revision의 pending snapshot/trigger를 폐기하고 자신의 새 trigger sequence를 시작한다.
- `expiresAt`, DELETE, terminal policy revision 또는 quarantine 이후에는 새 evaluation/normal write를 금지한다.

Golden fixture의 KPM input은 `observationWindowEnd`와 canonical payload digest를 포함하고, `expectation.json`은 expected episode count와 write count를 각각 검증한다.

## 9. Canonical PolicyStatusObject

`statusSchema`는 표준 enforcement field와 project-specific `aicStatus`를 함께 정의한다.

```json
{
  "enforceStatus": "ENFORCED",
  "aicStatus": {
    "policyId": "42c56544-f2d1-40a5-ab1d-3de8c5ee92dc",
    "policyRevision": 1,
    "producerEpoch": "0eb265c6-6303-49de-96bd-02eceb4bddf3",
    "statusSeq": 7,
    "policyState": "ACTIVE",
    "policyTerminal": false,
    "episodeId": "9972b940-15a1-463d-81b2-f68df04e0e91",
    "episodeState": "APPLIED_VERIFIED",
    "episodeTerminal": true,
    "occurredAt": "2026-08-04T00:00:07Z",
    "selectedCell": {
      "plmnId": { "mcc": "208", "mnc": "95" },
      "cId": { "ncI": 87654321 }
    },
    "control": {
      "transactionId": "530491ab-9033-484d-b045-01a2f8e81e3d",
      "actionId": "5addcb2e-eef4-4803-940d-b7698bc9d662",
      "result": "ACK",
      "resultIsEffectEvidence": false,
      "writeMayHaveOccurred": true
    },
    "readback": {
      "result": "VERIFIED",
      "observedServingCell": {
        "plmnId": { "mcc": "208", "mnc": "95" },
        "cId": { "ncI": 87654321 }
      },
      "observedAt": "2026-08-04T00:00:07Z",
      "latencyMs": 1260
    },
    "rollback": {
      "state": "NOT_REQUESTED"
    },
    "trace": {
      "intentId": "768f56d8-2d45-4c05-b00b-7b76e8e2ef61",
      "intentRevision": 1,
      "correlationId": "f993a697-778f-4551-b88c-5a5c07a09b1d"
    }
  }
}
```

다음 규칙을 적용한다.

- 항상 필수인 `aicStatus` field는 `policyId`, `policyRevision`, `producerEpoch`, `statusSeq`, `policyState`, `policyTerminal`, `occurredAt`, `trace`다.
- `episodeId`, `episodeState`, `episodeTerminal`은 첫 episode가 생성된 후 필수다. `noAction`은 `NO_ACTION`에서만 필수이고, `selectedCell`, `control`, `readback`, `rollback`, `error`는 해당 사건이 실제로 발생한 경우에만 존재한다.
- `statusSeq`는 `policyId + producerEpoch` 범위에서 1부터 단조 증가한다.
- `producerEpoch`은 Near-RT status producer restart마다 새 UUID가 된다.
- notification은 at-least-once일 수 있다.
- Consumer는 `policyId + producerEpoch + statusSeq`로 중복 제거한다.
- 같은 epoch에서 낮은 sequence는 late event로 기록하되 current state를 되돌리지 않는다.
- 처음 보는 `producerEpoch`의 notification은 즉시 current state로 승격하지 않고 A1 status query를 수행한다. 성공한 query response가 현재 active epoch과 snapshot의 권위 있는 기준이다.
- active epoch이 query로 확정된 후 이전 epoch에서 도착한 notification은 audit로만 남기고 state에 적용하지 않는다.
- A1 status query는 최신 snapshot을 반환한다.
- `notificationDestination`이 제공되면 Near-RT RIC는 표준 status notification을 실제로 전송한다.
- callback 실패 시 baseline은 최대 6회 attempt, 대기 `[0, 250, 500, 1000, 2000, 4000] ms`, 총 `10000 ms` 이내다. 모든 실패를 기록하고 query recovery가 항상 가능해야 한다.
- PolicyObject 안에 별도 `callback_url`을 추가하지 않는다.
- error가 있으면 `code`, `stage`, `retryable`, `writeMayHaveOccurred`, `detail`을 포함한다.
- `control.result=PENDING`은 `APPLYING`에서만 허용한다. `PENDING`, `ACK`, `TIMEOUT`, `UNKNOWN`은 보수적으로 `control.writeMayHaveOccurred=true`다. `NACK`은 zero-effect가 증명되면 `false`, 부분 적용 가능성을 배제할 수 없으면 `true`다. 후자의 `APPLY_FAILED`는 valid restore와 사전 승인이 있을 때 `rollback.state=REQUESTED`를 반드시 동반하며, 없으면 즉시 `RECOVERY_PENDING`으로 전이한다.
- `rollback.state=REQUESTED`부터 persisted snapshot의 `restoreCell`이 필수다. `SENT|VERIFIED|FAILED|UNKNOWN`에는 별도 rollback `transactionId`, `actionId`, `writeMayHaveOccurred=true`도 필수다. `NOT_REQUESTED`에는 이 필드를 넣지 않는다.
- `APPLIED_VERIFIED`의 `readback.observedServingCell`은 `selectedCell`과 같아야 하고, `READBACK_MISMATCH`에서는 달라야 한다. `ROLLED_BACK_VERIFIED`의 observed cell은 `rollback.restoreCell`과 같아야 한다.
- `NO_ACTION`은 `reason`, E2/RAN에서 관측한 `observedServingCell`, `observedAt`, 사용한 KPM `observationWindowEnd`와 canonical `snapshotSha256`을 반드시 제공한다. `ALREADY_ON_TARGET`은 objective가 선택한 최적/sole target과 observed cell이 같을 때만, `IMPROVEMENT_BELOW_THRESHOLD`는 `BALANCE_PRB_LOAD` 계산이 threshold 미만일 때만 사용한다. rApp은 이 metadata만으로 intent satisfaction을 확정하지 않고 O1/R1 assurance를 계속 사용한다.
- status의 `policyId`는 A1 URI의 `policyId`, `policyRevision`과 trace는 저장된 PolicyObject의 값과 정확히 같아야 한다. `selectedCell`은 해당 revision의 `allowedCells`에 속하고 `forbiddenCells`에는 속하지 않아야 한다. 이 값 간 equality/membership은 runtime validation과 negative golden fixture로 강제한다.
- `CANCELLED` 또는 `SUPERSEDED`는 resource가 존재하는 동안의 최종 status notification에서만 관측될 수 있다. A1 `DELETE` 204 후에는 policy/status query의 `404`가 권위 있는 결과이며, 삭제된 status resource를 별도 custom endpoint로 유지하지 않는다.

고정 error stage:

```text
ADMISSION
DECISION
CONTROL
READBACK
ROLLBACK
RECOVERY
TELEMETRY
INTERNAL
```

최소 error code catalogue:

```text
AIC_SCHEMA_INVALID
AIC_RESOURCE_NOT_FOUND
AIC_UNSUPPORTED_OBJECTIVE
AIC_SCOPE_NOT_FOUND
AIC_CELL_NOT_ALLOWED
AIC_CELL_NOT_NEIGHBOR
AIC_POLICY_CONFLICT
AIC_STALE_REVISION
AIC_IDEMPOTENCY_CONFLICT
AIC_VALIDITY_INVALID
AIC_CAPABILITY_MISMATCH
AIC_KPI_STALE
AIC_KPI_MISSING
AIC_E2_NOT_READY
AIC_LOCK_CONFLICT
AIC_ENVELOPE_VIOLATION
AIC_DEADLINE_EXCEEDED
AIC_CONTROL_TIMEOUT
AIC_APPLY_FAILED
AIC_READBACK_MISMATCH
AIC_ROLLBACK_FAILED
AIC_ROLLBACK_UNKNOWN
AIC_RECOVERY_PENDING
AIC_INTERNAL_ERROR
```

동기 HTTP 오류는 표준 `ProblemDetails`의 `type`을 machine-readable AIC code의 유일한 위치로 사용한다. `type`은 정확히 `urn:oran-aic:problem:{AIC_CODE}`이고 `title`은 같은 `{AIC_CODE}`, `status`는 실제 HTTP status, `detail`은 사람이 읽는 설명, `instance`는 요청별 URI다. 예를 들어 schema 오류는 다음과 같다.

```json
{
  "type": "urn:oran-aic:problem:AIC_SCHEMA_INVALID",
  "title": "AIC_SCHEMA_INVALID",
  "status": 400,
  "detail": "PolicyObject does not satisfy AIC_UECellSteering_1.0.0.",
  "instance": "urn:uuid:3f8d3295-b13f-44c8-bf67-76df0e0864f5"
}
```

비동기 오류는 `aicStatus.error.code`에 같은 code를 사용한다. Consumer는 자유문인 `detail`을 parse하지 않는다. `retryable=true`는 **같은 E2 write 재전송 허가가 아니라** 새 snapshot 평가 또는 fresh readback/recovery probe를 수행할 수 있다는 뜻이다. `writeMayHaveOccurred` 값을 모르면 `false`로 추정하지 않고 `true`로 보고한다.

| code | surface/result | enforcement / episode | retryable | write may have occurred |
|---|---|---|---:|---:|
| `AIC_SCHEMA_INVALID` | PUT/POST `400` | resource/episode 없음 | false | false |
| `AIC_RESOURCE_NOT_FOUND` | GET/PUT/DELETE `404` | resource/episode 없음 | false | false |
| `AIC_UNSUPPORTED_OBJECTIVE` | PUT/POST `400` | resource/episode 없음 | false | false |
| `AIC_VALIDITY_INVALID` | PUT/POST `400` | resource/episode 없음 | false | false |
| `AIC_STALE_REVISION` | PUT `409` | 기존 resource 불변 | false | false |
| `AIC_IDEMPOTENCY_CONFLICT` | POST/PUT `409` | 기존 resource 불변 | false | false |
| `AIC_POLICY_CONFLICT` | POST/PUT `409` | 기존 resource 불변 | false | false |
| `AIC_SCOPE_NOT_FOUND` | accepted resource status | `NOT_ENFORCED + SCOPE_NOT_APPLICABLE`, episode 없음 | true | false |
| `AIC_CELL_NOT_ALLOWED` | accepted resource status | `NOT_ENFORCED + STATEMENT_NOT_APPLICABLE`, episode 없음 | false | false |
| `AIC_CELL_NOT_NEIGHBOR` | accepted resource status | `NOT_ENFORCED + STATEMENT_NOT_APPLICABLE`, episode 없음 | false | false |
| `AIC_CAPABILITY_MISMATCH` | accepted resource status | `NOT_ENFORCED + STATEMENT_NOT_APPLICABLE`, episode 없음 | false | false |
| `AIC_KPI_STALE` | status | 시작 전: episode 없음; 시작 후: `ABORTED_NO_WRITE` | true | false |
| `AIC_KPI_MISSING` | status | 시작 전: episode 없음; 시작 후: `ABORTED_NO_WRITE` | true | false |
| `AIC_E2_NOT_READY` | status | 시작 전: episode 없음; 시작 후: `ABORTED_NO_WRITE` | true | false |
| `AIC_LOCK_CONFLICT` | status | 시작 전: episode 없음; 시작 후: `ABORTED_NO_WRITE` | true | false |
| `AIC_ENVELOPE_VIOLATION` | status | `NOT_ENFORCED + ERROR`, `QUARANTINED` | false | false |
| `AIC_DEADLINE_EXCEEDED` (send 전) | status | 시작 전: episode 없음; episode 시작 후: `ABORTED_NO_WRITE` | true | false |
| `AIC_DEADLINE_EXCEEDED` (send 후) | status | `RECOVERY_PENDING` | true | true |
| `AIC_CONTROL_TIMEOUT` | status | `NOT_ENFORCED + RECOVERY_PENDING`, `RECOVERY_PENDING` | true | true |
| `AIC_APPLY_FAILED` (zero-effect 확정) | status | terminal `APPLY_FAILED`, rollback 금지 | false | false |
| `AIC_APPLY_FAILED` (부분 적용 가능, rollback 승인) | status | nonterminal `APPLY_FAILED + rollback.REQUESTED` | false | true |
| `AIC_APPLY_FAILED` (부분 적용 가능, rollback 미승인/불가) | status | `NOT_ENFORCED + RECOVERY_PENDING`; fresh readback only | true | true |
| `AIC_READBACK_MISMATCH` | status | `ENFORCED + ACTIVE`, `READBACK_MISMATCH` | false | true |
| `AIC_ROLLBACK_FAILED` | status | 비종료 `NOT_ENFORCED + RECOVERY_PENDING`, `ROLLBACK_FAILED`; 다음 zero-write transition에서 `ERROR + QUARANTINED` | false | true |
| `AIC_ROLLBACK_UNKNOWN` | status | 비종료 `NOT_ENFORCED + RECOVERY_PENDING`, `ROLLBACK_UNKNOWN`; 다음 zero-write transition에서 `ERROR + QUARANTINED` | false | true |
| `AIC_RECOVERY_PENDING` | status | `NOT_ENFORCED + RECOVERY_PENDING`, `RECOVERY_PENDING` | true | true |
| `AIC_INTERNAL_ERROR` | status 또는 applicable `500` | fail closed; terminal success 금지 | false | true |

Valid A1 scope나 statement가 현재 topology/dependency에 적용되지 않는 경우를 policy conflict `409`로 위장하지 않는다. Resource를 정상 create/update한 후 표준 `NOT_ENFORCED`/`enforceReason`으로 보고한다. 이는 문법/schema 오류 `400`과 다르다.

## 10. KPI, performance와 intent assurance

KPI sample을 A1 `PolicyStatusObject`에 넣지 않는다. A1-P는 policy enforcement와 type-specific execution status를 전달한다. Final profile의 non-real-time performance sample은 O1 Performance Assurance/PM으로 SMO가 수집하고, Non-RT RIC Framework의 assurance correlator가 A1 status의 scope·time·identifier와 결합해 R1 DME로 제공한다. Operator intent fulfilment의 최종 판정은 rApp이 수행한다.

고정 DME type identifier는 다음과 같다.

```text
aic:policy-evidence:1.0.0
```

이는 R1 `DmeTypeIdStruct`에서 다음과 같이 표현한다.

```json
{
  "namespace": "aic",
  "name": "policy-evidence",
  "version": "1.0.0"
}
```

이 DME의 R1 delivery profile은 다음으로 고정한다.

- Data Producer의 Data registration `2.0.0-alpha.2` POST body는 정확히 outer `{dmeTypeDefinition,dataAccessEndpoint,dataDeliveryModes}`다. `dmeTypeDefinition`은 `{dmeTypeId,metadata,dataProductionSchema,dataDeliverySchemas,dataDeliveryMechanisms}`를 포함하고, `dataProductionSchema`에는 Appendix A.5 JSON Schema object를 **inline**으로 넣는다. `dataDeliverySchemas[]` entry는 `{type:"JSON_SCHEMA",deliverySchemaId:"aic.policy-evidence.record.schema.1.0.0",schema:"<Appendix A.3의 RFC 8785 canonical JSON text>"}`이며 `dataDeliveryMechanisms=[{dataDeliveryMethod:"PUSH_HTTP"}]`다. Outer field는 표준의 plural `dataDeliveryModes`를 사용하고 임의 `dataDeliveryMode`/`data-registration` 변형을 만들지 않는다. Appendix A.5의 `$id`는 schema 내부 identifier이며 R1 wire에 별도 production-schema-ID field를 만들지 않는다.
- rApp은 policy resource가 생성된 뒤 Data access `2.0.0-alpha.2`의 `POST .../data-access/v2/data-jobs`로 `CONTINUOUS + PUSH_HTTP` job을 만든다. Create 전에 cryptographically random한 최소 128-bit opaque `deliveryBindingId`를 생성·영속화하고, Request `DataJobInfo`에는 `dmeTypeId=aic:policy-evidence:1.0.0`, delivery schema ID, `pushDeliveryDetailsHttp.dataPushUri={r1.dme.policyEvidencePushBaseUri}/{deliveryBindingId}`와 아래 project-specific `productionJobDefinition`을 포함한다. Callback URI의 binding segment 이외에 비표준 `dataJobId` body/header를 추가하지 않는다.
- `productionJobDefinition`은 unknown field를 거절하는 `{policyTypeId, policyId, minimumPolicyRevision, nearRtRicId}` object다. `policyTypeId`는 `AIC_UECellSteering_1.0.0`, `minimumPolicyRevision`은 1 이상의 정수이며 나머지 두 identifier는 현재 R1 policy/capability mapping과 byte-for-byte 같아야 한다.
- Create data job 성공은 `201 Created`, `Location`, `DataJobInfo`다. rApp은 사전에 저장한 `deliveryBindingId`를 `Location`의 assigned `dataJobId`, DME type, policy/revision, canonical job definition과 원자적으로 bind하고 status를 확인한 후에만 그 callback의 payload를 S4 evidence로 사용한다. Response 유실 또는 restart에도 새 binding으로 성급히 재생성하지 않고 standard data-job query/reconciliation으로 수렴한다.
- DME Data Producer는 schema-valid evidence record 하나를 정확한 `Content-Type: application/json`으로 그 job에 협상된 job-specific `dataPushUri`에 POST한다. rApp callback은 URI의 opaque binding, mTLS/OAuth sender, active `dataJobId` mapping, DME type, schema, policy·revision·scope·time 및 dedupe ledger를 durable하게 검증한 뒤 `204 No Content`를 반환한다. Payload 자체나 비표준 header에서 job identity를 추측하지 않는다. 잘못된 payload에는 `400`, 알 수 없거나 만료된 binding에는 `404`, 인증·권한 실패에는 `401/403`이며 commit side effect가 없다.
- Push response가 유실되면 같은 callback binding과 delivery identity 및 canonical payload digest로 retry하고 rApp은 한 번만 적용한다. Status callback/continuous delivery가 유실된 recovery에는 별도 binding의 `ONE_TIME + PULL_HTTP` data job을 만들고 response의 `dataPullUri`를 GET한다. `200`은 payload, 아직 준비되지 않은 `202`는 bounded `Retry-After`를 사용한다.
- Data job update/query/status/delete는 표준 item resource를 사용한다. Policy delete/expiry 뒤에는 data job을 DELETE `204`하고 이후 residual push가 와도 새 commit을 만들지 않는다.
- DME discovery의 authoritative lookup은 collection query가 아니라 `GET .../data-discovery/v2/dme-types/{percent-encoded dmeTypeId}` item resource다. `aic:policy-evidence:1.0.0`의 colon을 path segment에서 percent-encode하며 임의 `?dme-type-id=` query로 대체하지 않는다.
- 임의 `/data` store/query endpoint, bare message broker topic 또는 xApp custom callback을 R1 DME라고 부르지 않는다.

각 evidence record는 최소한 다음 정보를 포함한다.

```json
{
  "dmeTypeId": "aic:policy-evidence:1.0.0",
  "observationId": "4f1429e6-f053-4fbb-a4ac-4c67672a90ea",
  "observedAt": "2026-08-04T00:02:00Z",
  "window": {
    "start": "2026-08-04T00:01:00Z",
    "end": "2026-08-04T00:02:00Z"
  },
  "policyScope": {
    "ueId": {
      "guAmfUeNgapId": {
        "guAmI": {
          "plmnId": { "mcc": "208", "mnc": "95" },
          "amfRegionId": "00",
          "amfSetId": "001",
          "amfPointer": "00"
        },
        "amfUeNgapId": 2
      }
    }
  },
  "measurementScope": {
    "managedObjectClass": "NRCellDU",
    "managedObjectDn": "SubNetwork=oran-lab,ManagedElement=oai-gnb,GNBDUFunction=oai-du,NRCellDU=1",
    "cellId": {
      "plmnId": { "mcc": "208", "mnc": "95" },
      "cId": { "ncI": 87654321 }
    }
  },
  "source": {
    "interface": "O1",
    "managementService": "PerformanceAssurance",
    "managedFunction": "O-DU",
    "profileId": "oran-aic-o1-pa-file/1.0.0",
    "specification": "ETSI_TS_128_552_V18.11.0",
    "collectionMode": "FILE",
    "perfMetricJobId": "oran-aic-nrcelldu-60s",
    "file": {
      "name": "B20260804.0001+0000-0002+0000_-oran-aic-nrcelldu-60s_oai-gnb.xml",
      "sha256": "beaab08dc59fa26489ebed1e6a9725bed0d5171c1773b2e9d416b649e906ab5f",
      "readyAt": "2026-08-04T00:02:02Z",
      "retrievedAt": "2026-08-04T00:02:04Z"
    },
    "pmRecord": {
      "measuredEntityDn": "SubNetwork=oran-lab,ManagedElement=oai-gnb",
      "measInfoId": "oran-aic-pm",
      "measObjLdn": "GNBDUFunction=oai-du,NRCellDU=1"
    }
  },
  "phase": "AFTER",
  "quality": "OK",
  "samples": [
    {
      "name": "RRU.PrbDl",
      "value": 41,
      "unit": "percent",
      "standard": "3GPP_TS_28.552_V18.11.0",
      "clause": "5.1.1.2.1",
      "valueKind": "INTEGER",
      "measTypeIndex": 1,
      "suspect": false,
      "samplingPeriodMs": 60000,
      "aggregation": "PERIOD_MEAN",
      "measurementAgeMs": 8000,
      "ingestLatencyMs": 4000,
      "quality": "OK"
    }
  ],
  "correlation": {
    "policyTypeId": "AIC_UECellSteering_1.0.0",
    "policyId": "42c56544-f2d1-40a5-ab1d-3de8c5ee92dc",
    "policyRevision": 1,
    "episodeId": "9972b940-15a1-463d-81b2-f68df04e0e91",
    "transactionId": "530491ab-9033-484d-b045-01a2f8e81e3d",
    "actionId": "5addcb2e-eef4-4803-940d-b7698bc9d662"
  }
}
```

고정 enum은 다음과 같다.

```text
phase: BEFORE | AFTER | STEADY | OVERLAPS_ACTION
quality: OK | SUSPECT | STALE | MISSING | NOT_AVAILABLE | AMBIGUOUS
aggregation: PERIOD_MEAN | DERIVED_RATIO
ambiguityReason: ACTION_WINDOW_OVERLAP | IDENTITY_CORRELATION_AMBIGUOUS | CLOCK_SKEW_EXCEEDED | CONTRADICTORY_SOURCE
```

다음 semantics는 필수다.

- `MISSING`과 `NOT_AVAILABLE`을 숫자 `0`으로 표현하지 않는다.
- 모든 sample은 `value` field를 가진다. `MISSING` 또는 `NOT_AVAILABLE`이면 정확히 `null`, 그 밖의 quality이면 schema가 허용한 숫자여야 한다.
- `observedAt`, window, sampling period, aggregation, unit, measurement age, ingest latency 및 raw source provenance를 생략하지 않는다.
- before/after sample은 같은 `measurementScope`와 같은 measurement definition을 사용한다.
- stale 또는 uncorrelated evidence로 intent 만족을 선언하지 않는다.
- Near-RT RIC/xApp은 A1 type-specific status에 action·readback metadata를 제공하고, RAN Managed Element는 O1 PM sample을 제공한다.
- SMO/Non-RT RIC assurance correlator가 `policyScope.ueId`, `measurementScope.cellId`/`managedObjectDn`, observation window, `policyId` 및 가능한 경우 `episodeId`로 결합한다. 결합을 유일하게 확정할 수 없으면 `quality`를 `MISSING` 또는 `NOT_AVAILABLE`로 보고하고 commit evidence로 사용하지 않는다.
- `source.interface` final value는 `O1`이다. `E2`는 lab/mock profile의 비표준 source로만 표시할 수 있으며 final conformance를 만족하지 않는다.
- A1-EI를 KPI의 Near-RT→Non-RT 역방향 전송 수단으로 사용하지 않는다.
- `RRU.PrbDl`은 `NRCellDU` 범위의 필수 assurance measurement이고 단위는 `percent`, 값 범위는 `0..100`, aggregation은 해당 60초 granularity period의 `PERIOD_MEAN`이다.
- `DRB.UEThpDl`은 같은 `NRCellDU` 범위의 optional measurement이고 단위는 `kbit/s`다. 이 PM record만으로 특정 `policyScope.ueId` 한 명의 throughput이라고 추론하지 않는다. UE별 throughput intent를 검증하려면 별도 versioned UE-level O1 profile이 필요하며 v1 baseline에서는 unsupported/fail-closed다.
- 하나의 evidence record는 하나의 `NRCellDU` measurement scope만 가진다. source/target 두 cell을 비교하려면 같은 window의 record 두 개를 사용한다.
- `quality=OK`이면 `samples`가 비어 있지 않고 포함된 모든 sample이 `OK`이며 필수 `RRU.PrbDl` sample이 정확히 하나 존재해야 한다. `STALE`, `MISSING`, `NOT_AVAILABLE`은 각각 **어느 sample이든** 같은 quality가 존재할 때만 사용하며, record quality는 아래 고정 precedence에 따른 전체 sample의 최악값이다. 따라서 optional `DRB.UEThpDl=NOT_AVAILABLE`과 valid `RRU.PrbDl=OK`가 함께 있으면 record는 `NOT_AVAILABLE`이다. 서로 모순된 record/sample quality는 schema 또는 normalization error다.
- 상위 Coordinator가 intent assurance를 commit하려면 record `quality=OK`이고 필수 `RRU.PrbDl` sample이 `quality=OK`이며 freshness·identity·action-window 조건도 모두 만족해야 한다. Optional sample의 열화가 있는 record는 숫자 PRB가 있더라도 commit evidence가 아니다.
- action-window overlap, identity correlation 실패, clock-skew 초과 또는 contradictory source와 같은 record-level ambiguity가 있으면 sample의 intrinsic quality보다 먼저 record `quality=AMBIGUOUS`를 사용하고 `ambiguityReason`을 반드시 기록한다. `ambiguityReason`은 `AMBIGUOUS`에서만 허용하며, `ACTION_WINDOW_OVERLAP`이면 `phase=OVERLAPS_ACTION`이어야 한다.
- record-level ambiguity가 없으면 record quality는 `NOT_AVAILABLE > MISSING > STALE > SUSPECT > OK` 순서에서 포함 sample의 가장 왼쪽 값을 사용한다. Sample 자체가 `AMBIGUOUS`이면 record도 `AMBIGUOUS`다. 이 순서는 상태의 심각도가 아니라 commit 가능성을 보수적으로 집계하기 위한 고정 precedence다.
- PM file의 `suspect=true`는 숫자가 있더라도 sample/record `SUSPECT`이며 commit evidence가 아니다. PM null/noValue는 `NOT_AVAILABLE`, 기대한 필수 measType/record 부재는 `MISSING`이다. 숫자 `0`은 valid `OK` 값이며 missing으로 바꾸지 않는다.
- `measTypes`와 `measResults`는 PM XML의 position index로 대응하며 이름 정렬을 가정하지 않는다. `(file.sha256, perfMetricJobId, measurementScope.managedObjectDn, window, name)` 중복은 하나만 normalize한다.
- action 시각이 PM window 내부에 있으면 `phase=OVERLAPS_ACTION`, `quality=AMBIGUOUS`, `ambiguityReason=ACTION_WINDOW_OVERLAP`이며 before/after 판정이나 commit에 사용하지 않는다. Sample의 원래 `OK`/`SUSPECT`/availability quality는 보존한다. 파일 도착 시각이 아니라 PM window를 사용한다.
- episode가 아직 생성되지 않은 steady/before observation에는 `correlation.episodeId`가 없을 수 있다. `transactionId` 또는 `actionId`가 있으면 `episodeId`와 두 ID가 모두 필수다.

### 10.1 고정 O1 Performance Assurance profile

양측의 O1 wire boundary는 Appendix B의 `oran-aic-o1-pa-file.1.0.0.json`으로 고정한다. 요약은 다음과 같다.

- ETSI TS 104 043 V11.0.0의 file-based bulk PM 경로를 사용한다.
- SDO O1 `notifyFileReady`를 HTTPS/mTLS로 전송하고, `fileInfoList[].fileLocation`에서 SFTP로 PM file을 회수한다. `href`는 file을 만든 `PerfMetricJob` managed-object URI이며 다운로드 주소가 아니다. `systemDN`은 notification을 emit한 MnS Agent의 DN이다. VES와 streaming은 v1 baseline이 아니다.
- PM file 문법은 하나로 고정한다. ETSI TS 132 432의 legacy `measCollecFile` branch와 ETSI TS 132 435 V10.0.0의 `measCollec.xsd`, root `measCollecFile`, namespace `http://www.3gpp.org/ftp/specs/archive/32_series/32.435#measCollec`, `fileFormat="32.435 V10.0 XML-schema"`를 사용한다. XML 선언 바로 뒤에는 `<?xml-stylesheet type="text/xsl" href="MeasDataCollection.xsl"?>`를 둔다. TS 128 532의 newer `measDataFile` branch를 이 v1 fixture의 대안으로 허용하지 않는다.
- `PerfMetricJob`은 `shared-contract-bundle/o1-netconf-yang-profile.1.0.0.json`과 `golden/o1/netconf/` fixture대로 NETCONF/YANG으로 구성한다. 3GPP Forge `Tag_Rel18_SA104`의 schema mount를 확인하고 `SubNetwork/ManagedElement/PerfMetricJob` 실제 instance path를 사용한다. `children-of-SubNetwork`는 mount label이지 XML instance wrapper가 아니다. `PerfMetricJob`과 그 하위 instance node의 namespace는 grouping 원본 measurements module이 아니라 최종 `uses` 지점인 `urn:3gpp:sa5:_3gpp-common-managed-element`다. `granularityPeriod=60`은 초, direct `fileReportingPeriod=1`은 분이며 비존재하는 `reportingCtrl` wrapper를 만들지 않는다. `fileLocation`과 PerfMetricJob의 `notificationRecipientAddress`는 모두 생략한다.
- NETCONF hello에는 `:writable-running`, `:validate:1.1`, `:rollback-on-error`와 `:xpath:1.0`이 모두 있어야 한다. `get-schema-mount.xml`은 전역 mount metadata와 실제 `SubNetwork` mount instance 아래의 `ietf-yang-library:yang-library`를 함께 조회하며, 둘 중 하나만 확인해서는 schema verification을 통과하지 못한다.
- NETCONF profile의 `moduleSet`은 허용 가능한 전체 module 집합이 아니라 **최소 필수 module 집합**이다. 하위 release는 `backend-release-manifest.json#/o1/yangModuleClosure`에 실제 배포에 사용한 전체 transitive import closure를 vendored relative path와 byte SHA-256으로 기록하고, 모든 import가 offline에서 해소된다는 validation report, schema-mount configuration 및 mounted YANG Library snapshot도 path+digest로 제공한다. 3GPP import가 revision을 고정하지 않은 `ietf-inet-types`와 `ietf-yang-types`는 임의 revision을 공통 profile에 박지 않고 release가 vendoring한 revision을 mounted YANG Library와 정확히 대조한다. 최소 집합 외의 import가 발견되면 생략하지 않고 같은 closure에 추가한다.
- 5G measurement definition은 ETSI TS 128 552 V18.11.0이며 필수 measurement는 `RRU.PrbDl`이다.
- `managedObjectDn ↔ CellId`는 하위 release의 static capability manifest에서 제공하는 bijective mapping만 사용한다. alias, PCI 또는 배열 순서로 추측하지 않는다.
- 외부 timestamp는 UTC `Z`, duration은 monotonic clock으로 계산한다. 허용 wall-clock skew는 `250 ms`, DME assurance freshness limit은 `120000 ms`다. 초과하면 `OK`가 될 수 없다.
- raw PM filename과 SHA-256을 evidence에 보존한다. Appendix C의 raw XML과 normalized JSON pair가 양측 공통 parser oracle이다.

### 10.2 O1 소유권과 고정 startup/teardown 순서

상위 SMO/Non-RT 측 O1 Consumer가 subscription과 job의 desired state, notification receiver, SFTP retrieval, raw-file 보관 및 normalization을 소유한다. 하위 Managed Element 측은 NETCONF/YANG `PerfMetricJob` Provider, `FileDataReportingMnS` Provider, notification sender, SFTP file Provider 및 `fileExpirationTime`까지의 file 보존을 소유한다. rApp은 이 O1 wire lifecycle을 직접 수행하지 않고 R1 PM/DME만 소비한다. 양측은 다음 순서와 상태를 source 변경 없이 실행 가능하게 구현한다.

1. `PRECHECK`: 상위는 `handoff-manifest.1.0.1.json`, bundle, O1 profile, `backend-capability-manifest.json` digest와 backend release manifest의 `o1` provenance, 실제 `nearRtRicId`·cell/DN mapping과 양측 clock skew를 검증한다. `o1.yangModuleClosure`의 vendored module path/digest·offline closure report·schema-mount configuration·mounted YANG Library snapshot, NETCONF/PA profile digest 또는 각 O1 Provider artifact 중 하나라도 누락·불일치하면 실패하고 다음 상태로 진행하지 않는다.
2. `TRUST_READY`: 상위 notification receiver와 NETCONF/SFTP trust·credential reference를 준비한다. 하위는 NETCONF, `FileDataReportingMnS`, HTTPS notification 송신 및 SFTP endpoint를 시작하고 자신의 `MnsAgent` DN, server certificate와 SFTP host key를 배포 설정에 고정한다.
3. `SUBSCRIBED`: 상위가 `POST {MnSRoot}/FileDataReportingMnS/{MnSVersion}/subscriptions`로 ETSI TS 128 532의 `Subscription`을 생성한다. Canonical request는 `{"consumerReference":"{o1.fileDataReporting.consumerReference}","timeTick":0}`이며, `consumerReference`에는 외부 설정의 HTTPS callback URI가 들어가고 `timeTick=0`은 이 compatibility profile에서 무기한 subscription을 뜻한다. `notificationRecipientAddress`는 이 operation의 wire field가 아니므로 전송하지 않는다. Provider의 `201 Created`, `Location`, 반환 Subscription representation과 subscription identifier를 durable state에 영속화한다. Project-specific `expectedNotifications` field도 wire request에 추가하지 않는다.
4. `JOB_ACTIVE`: 상위는 먼저 `running` datastore를 lock하고 NETCONF profile의 exact payload로 `administrativeState=LOCKED`인 `PerfMetricJob`을 create/readback한다. 위 `201` 결과와 subscription identifier가 durable하다는 전제까지 확인된 뒤에만 별도 `edit-config`로 `UNLOCKED` 전환하고 `<get>`에서 `administrativeState=UNLOCKED`, eventual `operationalState=ENABLED`를 확인한 다음 datastore를 unlock한다. Subscription 성공 전 job unlock, `operationalState`/`_linkToFiles`의 client write, `children-of-SubNetwork` wrapper 및 measurements-module namespace 사용은 모두 금지한다.
5. `ASSURANCE_READY`: 하위는 60초 granularity마다 고정 XML grammar의 PM file을 생성하고 `notifyFileReady` 또는 준비 실패 시 `notifyFilePreparationError`를 subscription recipient로 전송한다. 상위 callback은 notification을 durable하게 수락한 뒤 `204 No Content`를 반환한다. 첫 정상 notification→SFTP retrieval→size/digest/schema/profile 검증→normalization이 모두 성공해야 이 상태가 된다.
6. 상위는 `notifyFileReady.fileInfoList[].fileLocation`만을 다운로드 위치로 사용하고 SFTP known-host pinning과 별도 secret reference로 인증한 뒤 byte SHA-256, size, format, job, DN, period 및 measurement를 검증한다. 이 profile에서는 `eventTime == fileReadyTime`, `fileReadyTime < fileExpirationTime`, `fileReadyTime <= retrievedAt < fileExpirationTime`도 필수다. 미래 ready time, 역전된 expiration 또는 만료 뒤 retrieval은 격리하며, 검증 완료 전 evidence를 publish하지 않는다.
7. `DEGRADED`: expected measurement-window notification timeout이면 상위는 먼저 `GET {MnSRoot}/FileDataReportingMnS/{MnSVersion}/files`를 호출한다. 이 operation의 `beginTime`/`endTime`은 PM measurement time이 아니라 ETSI TS 128 532의 **`fileReadyTime` filter**다. `fileDataType=PERFORMANCE`, `beginTime=expectedMeasurementWindow.end+0 ms`, `endTime=expectedMeasurementWindow.end+120000 ms`의 `[beginTime,endTime)` 범위를 사용한다. 반환 `FileInfo` 중 profile/job과 ready-time 범위가 맞는 후보를 SFTP로 회수한 뒤에만 raw PM의 DN과 measurement window를 파싱하며, 그 window가 원래 expected measurement window와 정확히 일치하는 단 하나의 file만 복구에 사용한다. 후보가 없거나 최종 일치 file이 여러 개면 `UNKNOWN/MISSING`으로 판정한다. `notifyFilePreparationError`, notification/profile mismatch, SFTP host-key mismatch, retrieval failure, digest mismatch 또는 parsing failure는 해당 file/window를 격리한다. 실제 valid file이 없으면 가짜 `source.file`/`pmRecord`를 채운 DME evidence를 만들지 않고 rApp은 fail closed한다. O1 assurance 장애만으로 Near-RT의 A1 enforcement status를 임의로 `NOT_ENFORCED`로 바꾸지 않는다.
8. `RECONCILING`: pinned REST profile은 subscription POST/DELETE만 제공하고 subscription GET을 제공하지 않는다. 양측은 subscription과 identifier를 restart-safe하게 영속화하고, 정상 재시작에서는 상위가 저장된 identifier를 재사용하며 job만 NETCONF readback/reconcile한다. Subscription 상태가 불확실하면 상위는 먼저 job을 `LOCKED`로 만들고 저장된 identifier에 대한 DELETE가 `204` 또는 이미 없음을 뜻하는 `404`로 끝난 뒤에만 새 POST를 한 번 수행한다. DELETE 결과가 transport상 불명확하면 `DEGRADED`에 머물고 중복 가능성이 있는 POST를 하지 않는다. 마지막 정상 window를 freshness limit 이후까지 재사용하지 않는다.
9. `DRAINING`: teardown은 새 file 생성을 먼저 막도록 job을 `LOCKED`/delete하고 in-flight notification/retrieval을 drain한 뒤 subscription을 delete한다. 상대가 이미 없는 경우 desired-state reconciliation으로 수렴하되 임의 custom callback/API로 우회하지 않는다.

필수 O1 외부 설정 key는 다음으로 고정한다.

- endpoint/profile: `o1.netconf.endpoint`, `o1.netconf.knownHostsRef`, `o1.netconf.credentialRef`, `o1.fileDataReporting.mnsRoot`, `o1.fileDataReporting.mnsVersion`, `o1.fileDataReporting.mnsAgentDn`, `o1.fileDataReporting.consumerReference`, `o1.sftp.allowedAuthorities`, `o1.sftp.knownHostsRef`, `o1.sftp.credentialRef`, `o1.perfMetricJob.managedObjectDn`, `o1.perfMetricJob.managedObjectUri`
- shared trust: `o1.https.truststoreRef`
- Consumer→Provider subscription request: `o1.https.consumerClientCertificateRef`, `o1.https.consumerClientPrivateKeyRef`, `o1.https.consumerOauthClientId`, `o1.https.consumerOauthCredentialRef`, `o1.https.consumerOauthTokenEndpoint`, `o1.https.consumerOauthAudience`, `o1.https.consumerOauthScope`
- Provider→Consumer notification: `o1.https.notificationSenderClientCertificateRef`, `o1.https.notificationSenderClientPrivateKeyRef`, `o1.https.notificationSenderOauthClientId`, `o1.https.notificationSenderOauthCredentialRef`, `o1.https.notificationSenderOauthTokenEndpoint`, `o1.https.notificationSenderOauthAudience`, `o1.https.notificationSenderOauthScope`
- inbound servers: `o1.https.providerServerCertificateRef`, `o1.https.providerServerPrivateKeyRef`, `o1.https.notificationReceiverServerCertificateRef`, `o1.https.notificationReceiverServerPrivateKeyRef`

`mnsRoot`는 version/service suffix를 포함하지 않고 `mnsVersion`은 상대가 제공하는 실제 version segment다. Secret, OAuth client secret, certificate private key와 SFTP password의 실제 값은 capability manifest/shared bundle/non-secret values file에 넣지 않고 외부 secret reference만 둔다.

교차 팀 A1 배포 설정 key는 다음으로 고정한다.

- endpoint/identity: `a1.apiRoot`, `a1.notificationDestination`, `backend.capabilityManifestPath`, `backend.capabilityManifestSha256`
- shared trust: `a1.https.truststoreRef`
- Non-RT Consumer→Near-RT Producer: `a1.https.consumerClientCertificateRef`, `a1.https.consumerClientPrivateKeyRef`, `a1.https.consumerOauthClientId`, `a1.https.consumerOauthCredentialRef`, `a1.https.consumerOauthTokenEndpoint`, `a1.https.consumerOauthAudience`, `a1.https.consumerOauthScope`
- Near-RT Producer→Non-RT status notification: `a1.https.notificationSenderClientCertificateRef`, `a1.https.notificationSenderClientPrivateKeyRef`, `a1.https.notificationSenderOauthClientId`, `a1.https.notificationSenderOauthCredentialRef`, `a1.https.notificationSenderOauthTokenEndpoint`, `a1.https.notificationSenderOauthAudience`, `a1.https.notificationSenderOauthScope`
- inbound servers: `a1.https.producerServerCertificateRef`, `a1.https.producerServerPrivateKeyRef`, `a1.https.notificationReceiverServerCertificateRef`, `a1.https.notificationReceiverServerPrivateKeyRef`

`nearRtRicId`는 별도 수기 key로 중복 입력하지 않고 digest-pinned capability manifest에서 읽는다. 하위 `integration-values.yaml`과 상위 loader가 이 이름을 그대로 사용한다.

상위 R1 설정 key는 `r1.apiRoot`, `r1.rAppId`, `r1.dme.policyEvidencePushBaseUri`, `r1.https.truststoreRef`, `r1.https.clientCertificateRef`, `r1.https.clientPrivateKeyRef`, `r1.https.oauthClientId`, `r1.https.oauthCredentialRef`, `r1.https.oauthTokenEndpoint`, `r1.https.oauthAudience`, `r1.https.oauthScope`, `r1.https.pushServerCertificateRef`, `r1.https.pushServerPrivateKeyRef`, `r1.https.pushProducerClientCertificateRef`, `r1.https.pushProducerClientPrivateKeyRef`, `r1.https.pushProducerOauthClientId`, `r1.https.pushProducerOauthCredentialRef`, `r1.https.pushProducerOauthTokenEndpoint`, `r1.https.pushProducerOauthAudience`, `r1.https.pushProducerOauthScope`로 고정한다. `policyEvidencePushBaseUri`는 R1 DME Producer가 접근 가능한 HTTPS callback collection base이고 trailing slash 없이 설정한다. rApp은 source 변경 없이 각 data job에 대해 `{base}/{deliveryBindingId}`를 생성한다.

마지막 병합에서 양측이 공유하는 설정 object는 `shared-contract-bundle/integration-values.1.0.0.schema.json`을 만족해야 한다. 이 object는 backend capability/release manifest와 live E2 capability inventory뿐 아니라 deployment test vector도 각각 path와 SHA-256의 쌍으로 pin한다. 이 object의 값만 교체 가능하며, endpoint·ID·credential을 맞추기 위한 source edit는 merge gate 실패다. NETCONF/SFTP authority의 명시적 port는 `1..65535`만 허용한다. `*Ref`에는 schema가 허용한 secret reference만 넣고 certificate, private key, password, OAuth client secret 자체는 넣지 않는다.

최종 standard profile에서 rApp은 `/kpi` custom endpoint를 직접 호출하지 않는다. O1 PM은 SMO/Non-RT RIC가 수집하고 R1 DME로 제공한다. E2SM-KPM은 xApp의 near-real-time 판단과 readback에 계속 사용하되 R1/O1 data path를 대체하지 않는다.

## 11. Static RAN capability와 runtime readiness

Policy capability는 A1-P Policy Type discovery로 확인한다. A1 namespace에 별도 `/capability` endpoint를 만들지 않는다.

Deployment capability용 DME type은 다음으로 고정한다.

```text
aic:ran-capability:1.0.0
```

이 DME는 **동적 health API가 아니라 pin된 deployment capability manifest**다. 권위 경로를 다음과 같이 고정한다.

1. 하위 release는 Appendix A.4 schema를 만족하는 `backend-capability-manifest.json`, `backend-release-manifest.1.0.0.schema.json`을 만족하는 source/build manifest, `e2-capability-inventory.1.0.0.schema.json`을 만족하는 live E2 Setup inventory를 release artifact로 제공한다. Static capability/release manifest는 기대값, E2 Setup/RIC Service Update inventory는 관측값이며 둘이 모두 일치해야 한다.
   Backend release manifest의 `o1`은 NETCONF/YANG Provider build manifest·binary와 configuration, complete vendored YANG closure, NETCONF/YANG 및 PA profile, `FileDataReportingMnS` Provider, PM file generator와 measurement mapping, notification sender, SFTP Provider의 artifact path와 SHA-256을 모두 포함한다. 여러 component가 같은 process/image를 공유해도 각 역할에서 같은 artifact tuple을 명시적으로 참조하며 암묵적인 파일명 추측을 허용하지 않는다. 모든 `BuildArtifact`는 `artifactManifestPath`와 `artifactManifestSha256`을 함께 제공한다.
2. 상위 deployment는 이 파일과 release manifest의 SHA-256을 configuration으로 import하고 검증한 뒤 `aic:ran-capability:1.0.0` R1 DME로 노출한다. runtime custom `/capability` 호출은 사용하지 않는다.
3. A1 Policy Type discovery 결과와 schema digest가 static manifest보다 권위 있다. 서로 다르면 fail closed하고 policy를 생성하지 않는다.
4. v1 baseline의 Cell/DN authority는 digest-pinned static manifest이고, 실제 `PerfMetricJob`/PM record의 DN을 그 manifest와 cross-check한다. 제품이 O1 CM/inventory를 제공하면 추가 검증에 사용할 수 있으나, 별도 O1 CM Provider/Consumer는 이 profile의 필수 병합 경계가 아니다. 실제 PM record가 manifest와 다르면 fail closed한다.
5. E2 association, KPM subscription, xApp handler 및 control reachability 같은 **동적 Near-RT readiness의 유일한 상위 관측**은 policy create/update 뒤의 표준 A1 `NOT_ENFORCED`/type-specific status다. 이를 pre-admission DME나 custom 역방향 API로 추측하지 않는다.

최소 static capability record는 다음을 포함한다.

- immutable `manifestId`, `effectiveAt`, lower release `sourceRevision` 및 digest-pinned release-artifact authority
- contract/profile version
- 이 deployment가 R1/A1에서 사용하는 단일 `nearRtRicId`
- supported `policyTypeId`
- supported objective와 control axis
- 지원 `UeId`와 `CellId` 형식
- cell topology와 neighbour relation
- E2SM-KPM 기반 decision KPI와 O1 PM 기반 assurance KPI를 분리한 이름, unit, sampling period 및 source
- KPM/RC service model version과 style/action
- node별 canonical structured `globalE2NodeId`와 `(connectionEpoch, ranFunctionId)` binding, E2 Setup에서 관측한 OID/revision/raw·canonical definition digest
- OAI/FlexRIC full commit, 순서가 고정된 private patch byte digest, post-patch source tree, running binary/build manifest, RC profile·APER vector·OTA evidence digest
- 최대 active policy 수
- 동일 UE당 허용 policy 수
- freshness/deadline 범위
- hardware profile identifier
- schema digest와 O1 profile identifier

완전한 canonical example은 `shared-contract-bundle/golden/golden-vectors.1.0.0.json`의 `canonicalObjects.capabilityManifest`다. 상위 mock과 하위 release validator는 이 object를 그대로 accept해야 한다. 실제 하위 release는 topology, `sourceRevision`, hardware profile 및 E2 service-model 값만 자신의 digest-pinned deployment artifact에 맞게 채우며 field 의미나 schema를 변경하지 않는다. 이 baseline에서 `authority.kind=DIGEST_PINNED_RELEASE_ARTIFACT`는 별도 미정 서명 포맷을 뜻하지 않는다. RFC 8785 canonical manifest의 SHA-256을 release version manifest에 고정하고, 인증·무결성 보호된 release 전달 경로와 접근 통제로 신뢰한다.

현재 뒷단 구현과의 compatibility baseline은 OAI `2026.w30` commit `42bf80e9b25dbf521cc692fa6338cbbbebfbcd1d`, FlexRIC commit `ef6d722f22191eea74089966983da1f5ec1fedd4`, E2AP `2.03`/APER, KPM `2.03` RAN Function ID `2` revision `2` OID `1.3.6.1.4.1.53148.1.2.2.2` report style `1,4`, RC `1.03` RAN Function ID `3` revision `1` OID `1.3.6.1.4.1.53148.1.1.2.3`, Control Style `3`/Action `1`/Header·Message·Outcome Format `1`, RAN parameter path `1→2→3→4`다. ID는 node-local expected value이며 매 connection epoch의 E2 Setup에서 다시 bind한다. Measurement 이름과 RC parameter tree/type/cardinality는 decoded RAN Function Definition 및 digest-pinned profile에서 얻고 수기 추측하지 않는다.

E2 inventory의 `status=READY`는 단순 표기가 아니다. Static capability와 live inventory는 동일한 `GlobalE2NodeId` object를 사용하며, `hex`는 lowercase이고 `bitLength`에 필요한 정확한 nibble 수와 zero high-bit padding을 가져야 한다. 정확히 두 개의 서로 다른 RFC 8785-canonical `globalE2NodeId`가 각각 하나의 active connection epoch만 가져야 하고, 두 connection과 그 안의 KPM function 2 및 RC function 3이 모두 `active=true`여야 한다. 동일 node identity 중복, 이전 epoch 동시 활성, inactive connection/function 또는 static capability와의 identity/function/provenance 불일치가 하나라도 있으면 inventory는 `NOT_READY`, A1 readiness는 false, E2 write count는 0이다. JSON Schema가 표현할 수 없는 canonical bit-string encoding, cross-item uniqueness, active epoch uniqueness 및 static↔live equality는 schema의 `CANONICAL_GLOBAL_E2_NODE_ID_ENCODING`, `READY_DISTINCT_ACTIVE_NODE_IDS`, `ONE_ACTIVE_EPOCH_PER_NODE`, `READY_STATIC_CAPABILITY_EXACT_MATCH` machine semantic assertion이 강제한다.

`objectives[]`는 실제 deployment에서 끝까지 검증된 subset만 광고한다. `BALANCE_PRB_LOAD`를 광고하려면 양쪽 node의 decoded KPM capability와 live indication에서 cell-scope `RRU.PrbDl`을 정확한 이름·unit·scope로 제공해야 한다. 현재 관측되는 per-UE `RRU.PrbTotDl` 또는 다른 이름을 alias/합산해 같은 KPI로 취급하지 않는다. 이 조건이 없으면 `PIN_TO_CELL`만 광고하거나 해당 policy를 `NOT_ENFORCED + AIC_KPI_MISSING`으로 처리하며 추측값으로 action을 선택하지 않는다. 여기서 사용하는 error token은 §9 error code catalogue와 `AIC_UECellSteering_1.0.0.status.schema.json`의 `Error.code` enum에 있는 `AIC_KPI_MISSING` 하나뿐이다. `AIC_KPI_UNAVAILABLE`은 이 계약의 token이 아니며 enum 추가, alias, scenario-specific 변환 또는 production adapter의 token 치환으로 만들어 내지 않는다. `PIN_TO_CELL`도 Style 3/Action 1과 target serving-cell KPM readback이 모두 검증된 경우에만 광고한다.

Stock OAI pin은 실제 RAN 제어를 수행하는 RC Style 3 handover를 제공하지 않으므로 base commit만 적은 release는 무조건 실패다. Style 3을 추가한 private patch는 양쪽 gNB 모두에 재현 가능하게 적용하고 patch byte digest, post-patch tree, binary digest, complete RC parameter/NR-CGI wire profile, APER encode→decode→re-encode vector 및 OTA/readback evidence를 고정한다. Backend release의 `oai.patches[]`와 `flexric.patches[]`는 **JSON array 위치만** 적용 순서의 권위이며 각 Patch object에는 별도 `order` field를 두지 않는다. Appendix A.4의 `softwareProvenance.orderedPatchSet[]`도 backend release의 OAI patch `(path, sha256)` sequence와 같은 순서여야 한다. 정렬, 중복 제거 또는 숫자 order 재해석은 금지한다. Target gNB도 reverse handover/rollback 때 control source가 되므로 unpatched target은 Phase B 대상이 아니다. NR-CGI encoding이 미해결이거나 OID/revision/definition/epoch가 빠지면 `PIN_TO_CELL` capability를 publish하지 않고 E2 write count는 0이어야 한다.

RIC Control ACK와 transport send 성공은 delivery evidence일 뿐 실행 성공이 아니다. `APPLIED_VERIFIED`는 같은 policy/episode/transaction/action과 active `(globalE2NodeId, connectionEpoch)`에 대해 profile-defined control result를 decode하고, E2SM-KPM serving-cell post-condition readback까지 일치할 때만 허용한다. Epoch rollover, late ACK, generic unconditional ACK 또는 HTTP 2xx만으로 성공 상태로 올리지 않는다.

Operational readiness는 static DME의 boolean으로 저장하지 않는다. Near-RT RIC가 policy를 `ENFORCED`로 유지하려면 내부적으로 다음 조건이 모두 참이어야 한다.

- A1 termination ready
- `AIC_UECellSteering_1.0.0` registered
- policy handler/xApp ready
- 계약상 필요한 모든 E2 node associated
- 필요한 service model advertised
- KPM subscription active
- 필요한 KPI가 freshness bound 안에 존재
- E2 control path reachable

필수 node가 disconnect되면 즉시, 필수 KPM이 policy의 `requiredKpiFreshnessMs`를 넘으면 age-out된 것으로 본다. active policy가 있을 때 readiness가 깨지면 새 write 없이 `NOT_ENFORCED + OTHER_REASON` status notification을 보낸다. 복구되면 동일 revision을 `ACTIVE`로 되돌리고 새로운 coherent snapshot에서만 다음 episode를 시작한다.

## 12. Revision, idempotency와 retry

| identifier | 생성 주체 | 규칙 |
|---|---|---|
| `policyTypeId` | 계약 | `AIC_UECellSteering_1.0.0` |
| `nearRtRicId` | 하위 deployment/release | `backend-capability-manifest.json`에 고정하고 해당 release 동안 안정적; 상위 R1 create가 그대로 사용 |
| `policyId` | Non-RT RIC의 A1-P Consumer | policy resource 동안 안정적 |
| `intentId` | rApp | Operator intent 동안 안정적 |
| `intentRevision` | rApp | intent 재판단 시 증가 |
| `policyRevision` | rApp | 같은 policy update마다 단조 증가; sole authority |
| `idempotencyKey` | rApp | 같은 logical revision retry 동안 동일; Non-RT RIC은 변경 없이 보존 |
| `episodeId` | Near-RT/xApp | 계산→제어→검증 cycle마다 생성 |
| `transactionId` | Near-RT/xApp | E2 control attempt마다 생성 |
| `actionId` | Near-RT/xApp | concrete action마다 생성 |
| `correlationId` | rApp | end-to-end trace 동안 안정적 |

A1 PolicyObject에는 전송 시도마다 달라지는 `messageId`를 넣지 않는다.

`episodeId`는 `NO_ACTION`을 포함해 Near-RT decision episode가 생성되면 필수다. `transactionId`와 `actionId`는 E2 control attempt가 있을 때만 필수이며 `NO_ACTION`에서는 생성하지 않는다. Evidence와 status에서도 동일 requiredness를 사용한다.

다음 동작을 보장한다.

- 같은 `policyId + policyRevision + idempotencyKey + canonical payload` 재전송은 기존 결과를 재사용하며 새 episode/E2 write를 만들지 않는다. Resource가 이미 존재하므로 재시도 PUT은 표준 update response인 `200 OK`와 현재 PolicyObject를 반환한다.
- 최초 create만 `201 Created` 및 `Location` header를 반환한다. 다른 `policyId`로 동일·overlapping policy를 생성하려면 `409`다.
- 같은 revision 또는 idempotency key에 다른 canonical payload가 오면 `409`와 zero write다.
- 낮은 `policyRevision`은 `409`와 zero write다.
- 높은 revision은 같은 resource의 update다.
- HTTP response가 유실된 뒤 동일 PUT을 재전송해도 actuation은 최대 한 번이다.
- canonical digest에서 transport retry metadata를 제외한다.
- idempotency/action ledger는 최소 policy validity 종료 후 24시간 동안 또는 configuration된 더 긴 기간 동안 보존한다.
- restart 후에도 ledger semantics가 유지된다.

## 13. Conflict와 concurrency

rApp은 Operator intent의 의미적 conflict와 priority를 판단한다. Near-RT RIC/xApp은 물리 resource conflict와 write serialization을 책임진다.

- 다른 `policyId`가 동일 UE의 `serving_cell` axis에 overlap하면 `409`다.
- Near-RT RIC가 priority만으로 incumbent policy를 임의 삭제·수정하지 않는다.
- 우선순위를 바꾸려면 Non-RT RIC가 기존 policy를 update/delete한 후 새 desired state를 제출한다.
- 동일 UE actuation lock은 admission/scheduling 시점에 획득 또는 예약한다.
- 같은 UE에 두 E2 control을 동시에 보내지 않는다.
- lock conflict는 zero write로 끝나고 외부에서 식별 가능한 상태/오류를 제공한다.
- advertised `maxActivePolicies`와 `maxPoliciesPerUe`를 실제로 강제한다.

Update/delete fencing은 다음과 같이 고정한다.

- 같은 `policyId`의 higher revision으로 `scope.ueId`를 변경하지 않는다. Scope 변경은 기존 resource DELETE 204 후 새 policy create로만 수행한다.
- higher revision PUT이 `APPLYING`, `APPLIED_UNVERIFIED`, `ROLLING_BACK`, `RECOVERY_PENDING`중 도착하면 UE/policy fence를 설정하고 기존 episode를 fresh readback으로 종료/recovery한다. Baseline `controlDrainTimeoutMs=15000`안에 안전 상태가 되지 않으면 PUT `409`, old PolicyObject 유지, zero new write다.
- DELETE도 같은 fence를 사용한다. E2 send 전이면 episode를 cancel하고 `204`로 삭제한다. Write가 발생했거나 불확실하면 readback/사전 승인 rollback을 완료한 후에만 `204`를 반환한다. Drain timeout이면 `409`와 기존 resource를 유지한다.
- fence가 설정된 동안 새 normal episode/write를 시작하지 않는다.
- 모든 fence, ledger 및 pending update/delete intent는 process restart를 넘어 영속한다.

## 14. Control, readback와 zero-write 조건

E2 control은 다음 precondition을 모두 만족할 때만 허용한다.

- policy가 유효 시간 안에 있음
- scope identity가 현재 topology에서 유일하게 resolve됨
- target candidate가 neighbour이며 envelope 안에 있음
- 필요한 E2 node와 service model이 ready임
- KPI가 요구 freshness 안에 있음
- snapshot serving cell이 존재함
- 동일 UE lock을 보유함
- action deadline 안에 완료 가능함

다음 경우에는 zero write가 필수다.

- schema 또는 capability 불일치
- unknown/ambiguous UE 또는 cell
- stale revision 또는 idempotency conflict
- overlapping policy conflict
- stale, missing 또는 not-available 필수 KPI
- E2 node/service model not ready
- already-on-target
- improvement threshold 미달
- empty candidate set
- envelope violation
- lock 미획득
- policy expiry 또는 deadline 초과

E2 ACK는 transport/control receipt일 뿐 effect evidence가 아니다. `APPLIED_VERIFIED`는 fresh KPM/readback이 실제 serving cell을 확인한 경우에만 사용한다.

### 14.1 시간 기준

- 외부 wire timestamp는 UTC `Z`이고 policy validity와 cross-system correlation에만 사용한다. 각 process 내부 timeout과 cooldown은 wall clock 변경의 영향을 받지 않는 monotonic clock으로 측정한다.
- `actionDeadlineMs`는 최초 `SCHEDULED` snapshot을 ledger에 commit한 monotonic instant부터 `APPLYING` 전환, 즉 E2 send attempt 시작 전까지다. 만료 후에는 `ABORTED_NO_WRITE`이며 send하지 않는다.
- `rollbackPolicy.timeoutMs`는 `rollback.state=REQUESTED`를 ledger에 commit한 instant부터 rollback readback 종료까지다.
- `controlDrainTimeoutMs=15000`은 update/delete fence를 ledger에 commit한 instant부터다.
- `recoveryWindowMs=30000`은 같은 incident의 최초 `RECOVERY_PENDING` snapshot을 commit한 instant부터다. 새 callback/restart로 timer를 다시 시작하지 않는다.
- E2 node association/control channel loss는 즉시 not-ready다. KPM readiness는 필수 sample age가 해당 policy의 `requiredKpiFreshnessMs`를 초과한 순간 상실한다.
- 양측 clock skew 허용치는 `250 ms`다. 이를 초과하면 A1 status 순서는 `producerEpoch/statusSeq`로만 처리하고, O1/action time correlation은 `AMBIGUOUS`로 fail closed한다.

## 15. Restart, reconciliation와 rollback

Non-RT RIC Framework가 desired policy의 source of truth다. A1 policy가 Near-RT RIC restart를 넘어 유지된다고 가정하지 않는다.

Near-RT RIC/xApp은 다음을 보장한다.

- E2 send 전에 `policyId`, revision, episode/action/transaction ID, snapshot, target, canonical action digest를 write-ahead ledger에 `PREPARED`로 영속화한다.
- E2 send 후 `SENT` 표시를 영속화하기 전에 crash한 경우 write 발생 가능성을 `true`로 취급하고, fresh readback 없이 action을 replay하지 않는다.
- restart 직전 write 결과가 불확실하면 새 write 전에 fresh readback을 수행한다.
- 불확실 상태를 `RECOVERY_PENDING`으로 보고한다.
- 이미 target 상태면 replay된 policy로 두 번째 write를 하지 않는다.
- bounded recovery window 안에 verified, failed, unknown 또는 quarantined 상태로 수렴한다.
- `APPLYING`/`APPLIED_UNVERIFIED` 중 restart된 case를 복구한다.
- policy, status sequence, idempotency/action ledger, lock/quarantine 정보를 필요한 만큼 영속화한다.
- 결과를 확인할 수 없으면 성공으로 추정하지 않는다.

A1 `DELETE`는 policy resource 삭제이며 rollback 명령이 아니다. `?rollback=true` 같은 custom query parameter를 추가하지 않는다.

명시적 restore가 필요하면 다음 순서를 사용한다.

1. 기존 `policyId`를 higher `policyRevision`의 `PIN_TO_CELL`로 update하고 snapshot cell을 유일한 allowed cell로 설정한다. 별도 recovery `policyId`를 사용하려면 기존 policy를 먼저 `DELETE` 204로 제거해 overlap이 없음을 확인해야 한다.
2. `APPLIED_VERIFIED`와 fresh readback을 확인한다.
3. 복구 후 상위 desired state에 따라 더 높은 revision으로 정상 policy를 재설정하거나 표준 A1 `DELETE`로 policy를 삭제한다.

자동 rollback은 PolicyObject의 `rollbackPolicy.on`에 사전 승인된 경우만 수행한다. 다음을 구분해 보고한다.

자동 rollback write는 정상 action의 `maxActuationsPerEpisode`와 별도인 recovery write로 계수하며, 한 episode에서 최대 1회만 허용한다. Snapshot serving cell이 없거나 유일하게 resolve되지 않으면 최초 action 자체를 금지하고 rollback target을 추측하지 않는다.

- rollback requested
- rollback control sent
- rollback verified
- rollback failed
- rollback unknown

`ROLLBACK_FAILED` 또는 `ROLLBACK_UNKNOWN`이면 UE를 quarantine하고 새 higher revision/명시적 recovery 없이는 추가 write를 하지 않는다.

## 16. Security와 configuration

최종 integration profile은 적용 O-RAN security specification에 따라 다음을 제공한다.

- TLS 1.2 이상과 TLS 1.3 지원
- mutual TLS
- OAuth 2.0 기반 authorization
- replay protection
- 외부 설정 가능한 certificate, credential 및 token
- secret, IMSI 및 subscriber identity의 log redaction
- least-privilege service account

O1 transport별 최소 security/configuration profile은 다음과 같다.

| 경로 | 고정 baseline | fail-closed 검증 |
|---|---|---|
| `FileDataReportingMnS` request와 HTTPS notification | TLS 1.2+와 mutual TLS, OAuth 2.0 client-credentials token, 외부 truststore/client-certificate/token endpoint·audience·scope 설정 | certificate chain/hostname, token audience/scope, authenticated peer role; 실패 시 subscription/notification 거절 |
| NETCONF | NETCONF over SSH, pinned server host key, least-privilege client public-key credential | configured Managed Element authority와 host key가 다르면 job create/read/unlock 금지 |
| SFTP | `sftp` scheme만 허용, pinned SSH host key, least-privilege read-only client credential | `fileLocation` authority가 `o1.sftp.allowedAuthorities` 밖이거나 host key가 다르면 다운로드 금지 |

R1/A1 HTTPS 경계도 별도로 검증한다.

| 경로 | 고정 baseline | fail-closed 검증 |
|---|---|---|
| rApp↔R1 bootstrap/discovery/policy/DME data-job | TLS 1.2+, mutual TLS, OAuth 2.0 client credentials와 API별 least-privilege scope | server identity, token audience/scope, authenticated `rAppId`; 실패 시 resource/data-job side effect 0 |
| R1 DME Producer→rApp job-specific `dataPushUri` | TLS 1.2+, mutual TLS, OAuth 2.0, configured callback authority allowlist | certificate/token, URI의 opaque `deliveryBindingId`와 durable active `dataJobId`·DME type binding; 실패 시 evidence commit 0 |
| Non-RT RIC↔Near-RT RIC A1-P | TLS 1.2+, mutual TLS, OAuth 2.0 client credentials와 A1 policy least-privilege scope | peer role, policy type/near-RT RIC authorization; 실패 시 policy resource와 E2 write 0 |

SFTP retrieval의 acceptance baseline은 connect timeout `5000 ms`, read timeout `30000 ms`, 최대 file size `16777216 bytes`, 최대 3회 attempt와 backoff `[0, 1000, 3000] ms`다. Consumer는 partial content를 임시 격리 위치에 받고, EOF 뒤 실제 byte count가 `fileSize`와 같을 때만 원자적으로 보관한다. `fileExpirationTime`이 지났거나, 최대 크기를 초과하거나, size/format/profile 검증에 실패하면 parse·publish하지 않는다. URI userinfo/password와 path traversal은 거절한다.

plaintext는 loopback 또는 격리된 lab에서 명시적 insecure development flag로만 허용한다. insecure mode는 release default가 될 수 없다.

다음 값을 source에 hard-code하지 않는다.

- host/IP/port
- cell/UE identity
- E2 node ID
- USRP serial/frequency
- token/certificate path
- 개인 home/worktree path

## 17. 무소통 병렬 개발용 필수 산출물

본 문서의 field, enum, identifier, version, transition과 error semantics가 규범적이다. `shared-contract-bundle/`은 양측이 따로 재생성하지 않는 공통 seed/oracle이다. Machine-readable schema file의 whitespace와 object key order만 달라질 수 있다. RFC 8785 canonical object에는 `description`, `$defs` 배치와 모든 annotation도 포함되므로 baseline에서 이를 바꾸면 digest 불일치이며 계약 위반이다. 양측은 자신의 release에 Appendix A를 추출한 schema, 동봉 bundle의 byte-for-byte copy 및 digest를 포함하고, 상대 측이 discovery로 얻은 schema를 runtime validation에 사용할 수 있게 한다.

### 17.1 하위 연구자 release

하위 연구원은 다음을 하나의 pin 가능한 branch/tag/commit에서 제공해야 한다.

1. 표준 A1-P Producer/termination
2. `AIC_UECellSteering_1.0.0` `PolicyTypeObject`
3. `policySchema`와 standalone compound schema
4. `statusSchema`
5. A1 policy→xApp internal adapter
6. xApp와 필요한 E2SM-KPM/E2SM-RC implementation
7. 양쪽 gNB에 적용 가능한 OAI patch array-order sequence, post-patch tree, pinned OAI/FlexRIC build·ASN.1 module/profile/vector digest 및 hardware profile
8. OAI/O-RAN Managed Element의 O1 NETCONF/YANG `PerfMetricJob` Provider, `FileDataReportingMnS` subscription endpoint, PM generator/measurement mapping, notification sender, SFTP file Provider와 이를 가능하게 하는 build/config/profile artifact. 이 provenance는 backend release manifest의 `o1` object에 기록하고 모든 manifest·binary·configuration·profile·module·validation evidence를 relative path+SHA-256으로 pin한다.
9. 실제 topology/DN/E2/O1 mapping과 service model OID/version/revision/style/action/format을 채운 Appendix A.4 형식의 digest-pinned `backend-capability-manifest.json`
10. 동봉 `backend-release-manifest.1.0.0.schema.json`을 만족하는 재현 가능 release manifest와 `e2-capability-inventory.1.0.0.schema.json`을 만족하는 node/connection-epoch별 live E2 Setup/RIC Service Update inventory
11. 하드웨어 없는 deterministic Near-RT/E2/KPM/O1-PM mock
12. A1-P Producer simulator
13. 실제 HTTP/O1 boundary를 검증하는 black-box conformance runner
14. A1 status, E2 readback, raw O1 PM과 expected normalized evidence를 포함한 동봉 golden corpus 및 dynamic live-O1 invariant runner adapter
15. Appendix A schema, `shared-contract-bundle/` 및 root `handoff-manifest.1.0.1.json`의 동일 계약 복사본
16. version/digest/compatibility manifest와 `integration-values.1.0.0.schema.json`을 만족하는 non-secret integration values(실제 endpoint, `MnsAgent` DN, allowed authority, certificate/secret reference; secret value 금지)
17. A1/O1/E2 startup, O1 file-retention, readiness, shutdown 및 desired-state recovery runbook
18. A1/O1/E2 end-to-end evidence bundle 생성 기능

### 17.2 상위 연구자 release

상위 저장소는 다음을 하나의 pin 가능한 release에서 제공해야 한다.

1. rApp R1 Consumer와 headless Operator entry point
2. R1 registration/discovery, A1 policy management 1.0.0 및 DME API baseline
3. Non-RT RIC Framework의 A1-P Consumer와 desired-state reconciliation
4. O1 `FileDataReportingMnS` subscription·NETCONF `PerfMetricJob` desired-state manager, HTTPS notification receiver, pinned-host SFTP client, PM collector/normalizer 또는 실제 SMO 연동 profile
5. A1 status–O1 PM assurance correlator
6. `aic:policy-evidence:1.0.0` 및 `aic:ran-capability:1.0.0` R1 DME 등록·discovery·access
7. digest-pinned backend capability/release manifest와 live E2 inventory import·digest verification 및 A1/O1 discovery cross-check; E2 observed identity는 직접 추측하지 않고 하위 A1 readiness status로 fail closed
8. 하위 release와 동일 Policy Type/status/DME schema, 동봉 bundle 및 root `handoff-manifest.1.0.1.json`의 vendored copy와 digest configuration
9. mock R1/A1-P/O1 PM environment
10. 실제 service boundary를 호출하는 black-box conformance runner와 동봉 golden corpus adapter
11. version/digest/compatibility manifest, `integration-values.1.0.0.schema.json` exact key의 external configuration loader 및 통합 runbook

상위 연구자가 하드웨어 없이 사용할 수 있는 mock release도 같은 계약과 schema를 사용해야 한다.

Release는 다른 branch/worktree나 개인 home의 source, binary, config 또는 artifact를 참조하지 않는다. 필요한 모든 source, patch, profile, launcher와 test는 해당 release에서 재현 가능해야 한다.

### 17.3 실행 profile 배정

Scenario를 어떤 환경에서 실행해야 하는지는 각 팀이 판단하지 않는다. `shared-contract-bundle/execution-profile-assignment.1.0.1.json`이 그 유일한 권위이며, 형식은 같은 directory의 `execution-profile-assignment.1.0.1.schema.json`(`$id`: `urn:oran-aic:schema:execution-profile-assignment:1.0.1`, JSON Schema Draft 2020-12, `additionalProperties: false`)이 고정한다. 두 파일 모두 `bundle-manifest.1.0.1.json`의 `files[]`에 등재되고 그 byte SHA-256이 handoff에서 고정된다. Manifest에 등재되지 않았거나 digest가 맞지 않는 assignment 문서는 사용하지 않는다.

전달 규칙은 다음과 같다.

- 하위 측은 profile을 추론하거나 로컬 default를 생성하지 않는다. Assignment 문서에 배정이 없는 scenario는 실행하지 않고 blocker로 보고한다.
- Runner는 assignment 문서 digest 검증과 배정 조회를 preflight 안에서 끝낸다. **profile 검증 이전 target call은 0이어야 한다.** 검증 실패는 target call 없이 fail-closed다.
- Assignment 문서는 catalog scenario를 정확히 두 group으로 나눈다. 하위 실행 대상 `assignments[]` 74개와 하위 비적용 `notApplicable[]` 27개이며 두 group의 합집합은 catalog의 101개와 정확히 같다. 미배정 0, 중복 배정 0이다.
- `assignments[]`의 `scenarioId`가 권위 좌표다. `catalogPointer`는 편의 좌표이며 가리키는 항목의 `scenarioId`가 다르면 fail-closed한다.

Profile은 셋뿐이다.

| profile | 실행 대상 | 실행 환경 | counterpart 공급 |
|---|---:|---|---|
| `lower-local` | 69 | 하위 소유 구현 + deterministic local adapter/stub/fault harness | 하위가 shared bundle로 구성 |
| `bilateral-mock` | 4 | 양측이 합의한 mock endpoint와 합의된 capture | 상위가 contract mock endpoint 제공 |
| `live-O1` | 1 | 별도 live-O1 authority가 승인한 Provider/Consumer harness | live-O1 authority 승인 harness |

각 배정 항목은 아래를 기계적으로 결정한다. 이 값을 읽어 그대로 실행하면 되고 별도 해석이 필요 없다.

- `executedBy` — 실행 주체
- `targetOwner`, `suiteTargetToken`, `selectedTargetRole` — target 소유자와 실행하는 target role
- `counterpartRole`, `counterpartProvisioning` — counterpart와 그 공급 주체
- `permittedTargetKinds`, `prohibitedTargetKinds` — 허용·금지 target 종류
- `fixtureSource`, `oracleSource` — fixture와 oracle의 출처 및 catalog pointer
- `evidenceProducer`, `evidenceLabel` — evidence 생성 주체와 결과에 붙일 profile label

`notApplicable[]`의 27개는 target을 상위가 소유하므로 하위 Phase A 실행 대상이 아니다. 각 항목은 `reasonCode`, `selectedTargetRole`, `targetOwner`, 하위 기여 범위 `lowerContribution`을 명시한다. §2.1의 소유 경계상 `r1-service-conformance`의 target `NON_RT_RIC_FRAMEWORK_R1_SERVICE`와 `o1-consumer-normalizer-conformance`의 target `O1_CONSUMER_NORMALIZER`는 모두 상위 소유다.

`live-O1`은 live O1 Provider의 동적 output을 검증하는 profile일 뿐이다. **USRP, OTA 전송, live E2 control 또는 live RAN write 승인이 아니며** assignment 문서의 `hardwareAuthorization`이 이를 `NOT_AUTHORIZED`로 명시한다. `live-O1` scenario에서도 E2 control과 RAN write 경로는 deterministic stub을 유지한다. 어떤 profile도 상대 구현의 내부 source import를 허용하지 않는다.

Profile별 실행 결과에는 contract/profile digest, scenario ID, injected fault, target call count, normal/rollback RAN write count와 raw stdout/stderr digest를 기록하고 `evidenceLabel`을 붙인다. 세 profile 중 하나라도 실행되지 않았으면 74개 전체 통과를 주장하지 않는다.

## 18. Golden fixture와 black-box test

적합성 프로그램은 interface·role별로 분리한다.

| suite | 적용 대상 | 주요 경계 |
|---|---|---|
| `a1p-producer-conformance` | 하위 A1 mock ↔ 실제 Near-RT RIC | A1-P v2, PolicyType, status callback |
| `r1-service-conformance` | 상위 R1 mock ↔ 실제 Non-RT RIC Framework | R1 registration/discovery/A1 policy mgmt/DME |
| `o1-consumer-normalizer-conformance` | 상위 O1 collector/normalizer와 양측 fixture harness | 고정 notification/XML bytes → 정확한 normalized DME evidence |
| `o1-lifecycle-contract` | 상위 O1 lifecycle manager ↔ Provider mock, 하위 O1 Provider ↔ Consumer harness | subscription/job/startup/restart/files recovery/teardown의 양측 wire 동작 |
| `o1-provider-profile-conformance` | 하위 O1 PM mock ↔ 실제 Managed Element | 동적 notification/SFTP/XML/security/profile invariant |
| `end-to-end-contract` | 양측 mock 조합 ↔ 실제 통합 deployment | R1→A1→E2 및 O1→R1 DME |

서로 다른 API를 노출하는 R1 service와 A1-P Producer에 하나의 runner/corpus를 강제하지 않는다. A1/R1 suite에서는 각 suite의 mock↔real target에 같은 입력·oracle을 사용한다. O1은 고정-byte consumer normalization과 동적 live provider 검증을 의도적으로 분리한다.

`o1-lifecycle-contract`는 한 팀의 내부 구현을 상대 팀에 강제하는 white-box suite가 아니다. 같은 wire scenario를 상위 release에서는 실제 Consumer/lifecycle manager와 contract-faithful Provider mock에, 하위 release에서는 contract-faithful Consumer harness와 실제 Provider에 실행한다. 따라서 각 팀은 자신이 소유한 절반의 요청·응답·영속·순서·failure semantics를 상대 구현 없이 검증한다.

Baseline seed와 object oracle은 `shared-contract-bundle/golden/golden-vectors.1.0.0.json`, 모든 필수 case의 실행 recipe와 assertion은 `shared-contract-bundle/scenario-catalog.1.0.1.json`, 실행 profile 배정은 `shared-contract-bundle/execution-profile-assignment.1.0.1.json`, O1 normalization profile은 `shared-contract-bundle/oran-aic-o1-pa-file.1.0.0.json`이다. Exact O1 input은 `golden/o1/`의 notification, valid/null/suspect XML 및 overlap context다. Runner가 catalog에서 suite별 directory artifact를 materialize할 때 fixed mode의 UUID, timestamp, topology, payload 및 expected result를 바꾸지 않는다. 추가 test는 `extension/`에서 실행하며 baseline bundle digest에 포함하지 않는다.

`scenario-catalog.1.0.1.json`이 두 팀 사이의 완전한 portable machine oracle이다. 각 atomic scenario는 Section 18 requirement reference, suite, execution mode, seed reference, deterministic operation/fault schedule 및 적용 가능한 모든 expected result를 가진다. 한쪽이 별도 baseline request/status/오류 의미를 발명하지 않는다.

Runner는 catalog recipe를 실행하면서 scenario에 적용 가능한 다음 evidence artifact를 결과 directory에 materialize한다. 적용되지 않는 종류는 빈 가짜 JSON으로 만들지 않고 result manifest에 `NOT_APPLICABLE`로 기록한다.

```text
request.json
captured-response.json
expected-http-response.json
expected-policy-status.json
expected-status-notifications.json
o1-notification.json 및 normative PM file fixture 또는 live capture
expected-evidence.json
expectation.json
execution-result.json
```

`expectation.json`에는 최소한 다음이 있어야 한다.

- expected HTTP status
- expected A1 resource existence
- expected policy/enforcement/episode state
- expected RAN write count
- expected rollback write count
- expected terminal/non-terminal 구분
- expected error code
- expected evidence quality

`o1-consumer-normalizer-conformance`의 `O1-001`은 `evaluationNow=2026-08-04T00:02:08Z`에서 정확히 두 evidence record와 네 sample을 만든다. 각 record의 `measurementAgeMs`는 `evaluationNow - window.end = 8000`, `ingestLatencyMs`는 `retrievedAt - window.end = 4000`이며, 값·DN·position index·raw-file SHA-256은 golden의 `afterEvidence`와 `afterEvidenceCell2`와 정확히 같아야 한다. `O1-002`, `O1-003`, `O1-004`는 각각 `null-prb.xml`, `suspect-prb.xml`, `valid-prb.xml + overlap-context.json`을 사용한다.

`o1-provider-profile-conformance`는 실제 `PerfMetricJob`으로 생성된 live output을 capture한다. 실제 timestamp, KPI 값, file URI, size 및 byte digest를 golden fixture와 같다고 요구하지 않는다. 대신 subscription→job unlock 순서, notification/XML schema, `systemDN`/`href` 의미, SFTP retrievability/security, filename/content cardinality, metric 이름·unit·period·position mapping, DN↔CellId, clock/freshness 및 live file 내부의 size/digest 일관성을 검증한다. Phase B end-to-end는 이 동적 output을 production normalizer에 넣어 evidence schema와 관계 invariant를 검증한다.

필수 scenario:

- R1 bootstrap/service discovery, DME registration/discovery, data-job create/status, HTTP push `204`와 teardown
- Policy Type discovery와 schema digest 확인
- valid policy create `201`
- valid policy update `200`
- policy query와 status query
- status notification과 `204`
- policy delete `204`
- malformed policy `400`
- unknown Policy Type/Policy `404`
- schema-valid unknown UE scope의 `NOT_ENFORCED + SCOPE_NOT_APPLICABLE`와 unsupported cell statement의 `NOT_ENFORCED + STATEMENT_NOT_APPLICABLE`
- overlapping policy `409`
- stale revision `409`
- 같은 key의 다른 payload `409`
- identical PUT retry와 zero duplicate write
- response loss 후 retry와 single write
- R1 first-create POST response loss 후 same `policyId`/Location recovery
- Framework ledger 영속화 후 A1 PUT 전 restart reconciliation
- already-on-target `NO_ACTION`
- improvement threshold 미달과 eligible target 부재의 `NO_ACTION`
- episode 시작 후 freshness/readiness/lock/deadline 상실의 `ABORTED_NO_WRITE`
- `APPLYING + control.result=PENDING`
- PRB decision 후 `APPLIED_VERIFIED`
- ACK 후 readback 전 `APPLIED_UNVERIFIED`
- control timeout
- zero-effect NACK의 terminal `APPLY_FAILED`와 rollback zero
- partial-effect 가능 NACK의 conditional rollback fencing
- readback mismatch
- stale/missing/not-available KPI와 zero write
- E2 disconnect 후 `NOT_ENFORCED`
- automatic rollback verified
- rollback failed/unknown과 quarantine
- same-UE concurrent request serialization
- `APPLYING` 중 higher revision PUT의 drain/fence/409 semantics
- `APPLYING` 중 DELETE의 drain/fence/204-or-409 semantics
- scope-changing update `409`
- policy expiry during in-flight episode
- callback duplicate/loss와 query recovery
- 같은 producer epoch의 낮은 `statusSeq`와 이전 `producerEpoch` replay 무시
- `APPLYING` 또는 `APPLIED_UNVERIFIED` 중 restart
- Non-RT reconciliation 후 duplicate actuation 없음
- R1 DME evidence delivery와 correlation
- O1 PM ingestion, identity/time correlation 및 R1 DME normalization
- O1 `measType` positional mapping, null-vs-zero, `suspect=true`, duplicate/replay 및 digest mismatch
- 미등록/중복/ambiguous `managedObjectDn ↔ CellId` mapping 격리
- action과 겹치는 PM window의 `OVERLAPS_ACTION + AMBIGUOUS`
- O1 sample이 없고 E2 KPM만 있을 때 final profile fail-closed
- O1 subscription `201`이 job unlock보다 먼저이고, restart에서 저장된 identifier를 재사용하며, 불확실 subscription은 job lock→확정 DELETE 뒤에만 재생성하고 teardown은 `204`
- `notifyFilePreparationError` 또는 expected-window notification timeout 후 `fileReadyTime` 범위의 표준 `/files` recovery에서 unique valid file은 SFTP/raw PM window 검증으로 복구하고, valid file이 없거나 중복이면 가짜 file provenance/DME evidence 없이 assurance `UNKNOWN/MISSING`
- mTLS/authentication/authorization failure
- fixed bundle과 live O1 invariant 각각의 R1→A1→E2→O1→R1 data-job delivery→rApp S4/S6 success trace

Test runner는 실제 HTTP/service boundary를 호출한다. 구현 내부 Python/C++ object import로 conformance를 대체하지 않는다. Schema validation이 없거나 건너뛰면 test runner는 non-zero로 실패한다.

A1/R1 suite의 같은 corpus는 해당 mock과 real target에 실행하고, O1 suite는 위 fixed/dynamic mode 구분을 지킨다. `BALANCE_PRB_LOAD`의 내부 수학 알고리즘은 하위 연구자 소유이므로, fixture가 유일한 최적 cell을 만들지 않는 경우 상위 suite는 특정 cell이 아니라 envelope·freshness·threshold·zero-write 불변식을 검증한다.

## 19. Version과 변경 동결

양측은 다음 manifest structure를 사용한다.

```yaml
architecture_baseline: oran-aic/1.0.1
supersedes: oran-aic/1.0.0
handoff:
  root_manifest: handoff-manifest.1.0.1.json
  contract_document: 02-rapp-xapp-backend-mandatory-contract.1.0.1.md
  digest_authority: handoff-manifest.1.0.1.json
distribution_archive:
  digest_authority: dist/handoff-1.0.1/provenance.json
a1_api:
  specification: ETSI-TS-103-987
  specification_version: 4.3.0
  api_name: A1-P
  api_major: 2
policy_type:
  id: AIC_UECellSteering_1.0.0
  wire_schema_ids_unchanged_from_1_0_0: true
  policy_schema_id: urn:oran-aic:schema:AIC_UECellSteering:policy:1.0.0
  status_schema_id: urn:oran-aic:schema:AIC_UECellSteering:status:1.0.0
  policy_schema_jcs_sha256: "3c48abaefd1c213ef78e3cad1e3483f512e9675bcb5936dadaada47782c02c90"
  status_schema_jcs_sha256: "9ec81ea297806d2816611b634b3d0163de830574dde9d91227ca6f309aca115a"
r1:
  gap: ETSI-TS-104-228-v11.0.0
  application_protocol: ETSI-TS-104-231-v8.0.0
  service_registration: 1.2.0
  service_discovery: 1.2.0
  a1_policy_management: 1.0.0
  dme_data_registration: 2.0.0-alpha.2
  dme_data_discovery: 2.0.0
  dme_data_access: 2.0.0-alpha.2
  dme_http_push_pull: 1.0.0
o1:
  specification: ETSI-TS-104-043
  specification_version: 11.0.0
  performance_assurance_profile: oran-aic-o1-pa-file/1.0.0
  performance_assurance_profile_jcs_sha256: "8e20a04899d4486695d38b4b6edbe52bedd78d768d68be370c3cb88012569ef9"
  netconf_yang_profile: oran-aic-o1-netconf-perfmetricjob/1.0.0
  netconf_yang_profile_jcs_sha256: "1828395178acc2c0515921d67831e6cbfc49dcf6536c20fb006e44d976d39bbf"
dme_types:
  policy_evidence: aic:policy-evidence:1.0.0
  policy_evidence_schema_jcs_sha256: "a707fabbbdbbb944a15d1e1fab2bb73042db689ae2b6ee50b3ad97ea5dce6f98"
  policy_evidence_filter_schema_jcs_sha256: "beb9956f241b1a4ca00f0d528f5984153c07e23f0322d90ba330f6277e16499a"
  ran_capability: aic:ran-capability:1.0.0
  ran_capability_schema_jcs_sha256: "b356f1f6a183cf2438cbbfb89b5c7de08276cef0067588effa2aa344649eacf3"
merge_contracts:
  backend_release_manifest_schema_jcs_sha256: "2b02ad831fad2cac57e5a2e9d9ddccb02748809d05f7e772299f3538bddc04cd"
  e2_capability_inventory_schema_jcs_sha256: "b203a4b131346f149a3f48b887fb89e8748566c2175664b11823c85910af5e56"
  integration_values_schema_jcs_sha256: "ef2422cc5ccab9f4165d95bbc04b9915523e4eb1d8d771b09709e4f77dbb0e30"
scenario_program:
  catalog: scenario-catalog.1.0.1.json
  catalog_byte_sha256: "0f28720007cba46722aee4f13fb6ce67c3d61ba27c2f264106e63b5649fbfc26"
  runner_contract: scenario-runner-contract.1.0.1.json
  runner_contract_byte_sha256: "4e9a2f8c802594490c2c6f4cb71f34723b0698bf9fefc6d36eefb163fb480390"
  execution_profile_assignment: execution-profile-assignment.1.0.1.json
  execution_profile_assignment_byte_sha256: "3091b8afcad362c2dffaaa97acb1a551786ca8f2d41b70edef38fcb7ee9fde2c"
  execution_profile_assignment_schema: execution-profile-assignment.1.0.1.schema.json
  execution_profile_assignment_schema_id: urn:oran-aic:schema:execution-profile-assignment:1.0.1
  execution_profile_assignment_schema_byte_sha256: "3647f5898cd97cd78ce7f2180afc9a4ae8efa1ae415df7b711f64248f84c60f6"
shared_bundle:
  version: oran-aic-shared-contract-bundle/1.0.1
  manifest_byte_sha256: "c01dfb46518af0e6f2687e073158ae3e408f199ecd09a1405b1f162c98d7c0b1"
  manifest_jcs_sha256: "6d18be509950a17efa136971af788b607f0d67778b2a83d9f4e8f22c470867ed"
backend_release:
  git_commit: "<release commit>"
  artifact_manifest_sha256: "<digest>"
```

`a1_api`, `policy_type`, `r1`, `o1`, `dme_types`, `merge_contracts`의 고정 digest는 `1.0.0`에서 바뀌지 않은 wire artifact의 값이며 변경할 수 없다. `scenario_program`과 `shared_bundle`의 값은 package build가 실제 artifact에서 계산한 값이다. `backend_release` 아래의 angle-bracket 값은 각 release build가 채우는 output slot이다. 이 release slot 중 하나라도 빈 값, 예시 문자열, angle-bracket placeholder 또는 `TBD`로 남은 backend release manifest는 release/merge 대상이 아니다.

Contract document, handoff manifest와 archive digest는 이 문서에 값으로 내장하지 않는다. Contract document digest를 담는 handoff manifest의 digest를 다시 contract document에 넣으면 `contract → handoff → contract` 순환이 생기고, archive digest를 archive member인 contract document에 넣으면 archive가 자기 digest를 포함하게 된다. Package 발행 순서는 bundle member digest 계산, bundle manifest 생성, 이 문서의 비순환 digest pin 확정, contract document digest 계산, handoff manifest 생성, handoff manifest digest 계산, archive 생성, archive digest 계산 순이다. Contract document digest는 root manifest의 `documents[].byteSha256`, handoff manifest와 archive byte digest는 `dist/handoff-1.0.1/provenance.json`에서 외부 고정한다.

두 저장소는 startup과 CI에서 상대가 제공한 schema를 RFC 8785로 canonicalize한 뒤 local expected digest와 비교한다. 불일치하면 fail closed한다.

변경 규칙:

- field 이름, type, required 여부 또는 의미 변경은 Policy Type major bump다.
- enum 제거 또는 기존 enum 의미 변경은 major bump다.
- `additionalProperties: false`인 object의 optional field 추가, enum/objective 추가는 기존 schema가 새 object를 거절하므로 기본적으로 major bump다.
- minor bump은 old/new schema가 서로의 전체 허용 object 집합을 상호 validation한다는 자동 증거가 있는 변경에만 허용한다. Golden scenario, documentation 및 implementation-only capability 추가는 PolicyType schema가 변하지 않으면 project release minor로 관리할 수 있다.
- patch bump는 허용/거절 object 집합과 field semantics을 전혀 바꾸지 않는 설명·검증 수정에만 사용한다.
- hardware profile 변경은 Policy Type version을 바꾸지 않는다.
- official A1/R1 API는 project versioning 대상으로 fork하지 않는다.
- 병렬 개발 중 `1.0.0` baseline을 바꾸지 않는다.
- 새 요구가 생기면 기존 profile을 유지한 additive version으로 구현한다.
- 불가피한 deviation은 `compatibility-report.json`에 machine-readable하게 기록하되 `1.0.0` 성공으로 위장하지 않는다.
- unknown field 허용으로 양측 차이를 숨기지 않는다.
- `1.0.0` baseline은 in-place로 고치지 않는다. 계약 무결성 결함은 `1.0.0`을 frozen으로 보존한 채 별도의 corrected full package(`1.0.1`)를 발행해 해소한다. 이 규칙은 위의 "병렬 개발 중 `1.0.0` baseline을 바꾸지 않는다"와 충돌하지 않고 그 실행 방법이다.

### 19.1 Digest 이름과 검증 순서

서로 다른 대상의 digest를 같은 이름으로 부르지 않는다. 다음 다섯 항목은 **서로 다른 필드이자 서로 다른 문서 항목**이며 값이 우연히 같아도 교환해 사용하지 않는다.

| 이름 | hashing 대상 | 입력 | 어디에 pin되는가 |
|---|---|---|---|
| `archive_sha256` | 배포 archive 파일 자체 | raw byte | handoff 문서의 `distribution_archive` |
| `handoff_manifest_sha256` | `handoff-manifest.1.0.1.json` 파일 | raw byte | handoff/root authority |
| `bundle_manifest_sha256` | `bundle-manifest.1.0.1.json` 파일 | raw byte | root manifest의 `bundle.manifestByteSha256` |
| `contract_document_sha256` | 이 문서 파일 | raw byte | root manifest의 `documents[].byteSha256` |
| manifest-listed per-file SHA-256 | bundle의 각 regular file | raw byte | `bundle-manifest.1.0.1.json`의 `files[]` |

Raw byte digest와 RFC 8785 JCS canonical digest를 함께 쓰는 대상은 이름에 각각 `byte` 또는 `jcs`를 명시한다. 예를 들어 bundle manifest는 `bundle_manifest_byte_sha256`과 `bundle_manifest_jcs_sha256`을 모두 가지며, 두 값을 하나의 이름으로 합치지 않는다. Appendix A schema와 O1 profile처럼 JCS digest만 pin하는 대상에는 byte digest를 그 이름으로 기록하지 않는다.

검증 순서는 다음으로 고정한다.

1. Archive를 추출하기 **전에** `archive_sha256`을 raw byte로 검증한다.
2. 새로 만든 빈 directory에 추출한다.
3. `contract_document_sha256`과 `handoff_manifest_sha256`을 검증한다.
4. Root manifest가 가리키는 `bundle_manifest_byte_sha256`과 `bundle_manifest_jcs_sha256`을 검증한다.
5. `files[]`의 manifest-listed per-file SHA-256을 전부 검증하고, manifest에 없는 파일이 0인지 확인한다.
6. §17.3의 execution-profile assignment 문서와 그 schema의 digest를 검증한다.

Verifier 인자는 다음과 같이 대응한다. `verify_frozen_handoff.py --expected-handoff-sha256`에는 **handoff/root authority가 정의한 handoff manifest byte digest만** 전달한다. 이 인자에 archive digest를 넣지 않는다. Archive digest는 추출 전 단계의 별도 검증이며 verifier의 handoff 인자와 다른 대상이다.

어느 단계든 실패하면 다음 단계로 진행하지 않고 fail-closed한다. 실패 상태에서 target call은 0이며, 이전 baseline으로 조용히 fallback하지 않는다.

### 19.2 1.0.0 → 1.0.1 규범 변경 요지

이 절은 규범 변경만 적는다. 상세 release note와 파일별 diff는 별도의 `1.0.0 → 1.0.1` release/change note가 담당한다.

각 변경의 좌표는 frozen `1.0.0`에서 재현한 mismatch matrix(`diagnosis/1.0.0/mismatch-matrix.json`)의 `scenarioId`와 `jsonPointer`를 그대로 쓴다.

문서 수준 규범 변경:

1. §11의 KPI 미가용 처리 token을 `AIC_KPI_UNAVAILABLE`에서 §9 error code catalogue 및 status schema `Error.code` enum과 일치하는 `AIC_KPI_MISSING`으로 정정했다. Enum 추가, alias, scenario-specific 변환, adapter token 치환은 모두 금지다. Status schema는 바뀌지 않았다. (matrix `SC-032-ENUM`, `/scenarios/31/expected/errorCode`)
2. §17.3 실행 profile 배정 절을 신설하고, §0의 전달 규칙과 §18의 oracle 참조에 편입했다. 하위 측이 profile을 추론할 필요가 없어졌다. (matrix `EXECUTION-PROFILE`, `/files`)
3. §19 version manifest를 `1.0.1` 명칭과 package build digest slot 구조로 갱신하고, §19.1에서 archive/handoff manifest/bundle manifest/contract document/per-file digest를 분리하며 byte와 JCS 의미를 명시했다. (matrix `HANDOFF-DIGEST`, `/handoff-manifest.1.0.0.json`)
4. §19의 변경 규칙에 "frozen baseline은 in-place 수정하지 않고 corrected full package로 대체한다"를 명문화했다. (matrix `PKG-METADATA`, `/`)

Bundle 수준 규범 변경의 범위(구체 내용은 `1.0.1` bundle artifact와 release note가 확정한다):

- SC-004/013/031/032/033/057/094의 runner-required field 정합 (matrix `RUNNER-SC-004`, `RUNNER-SC-013`, `RUNNER-SC-031`, `RUNNER-SC-032`, `RUNNER-SC-033`, `RUNNER-SC-057`, `RUNNER-SC-094`)
- SC-033의 historical 성공 status와 현재 `AIC_E2_NOT_READY` status 분리 — 동일 current snapshot에 `APPLIED_VERIFIED`와 `AIC_E2_NOT_READY`가 공존하지 않는다 (matrix `SC-033-SCHEMA`, `/scenarios/32/expected`)
- SC-063~069 및 SC-072~073의 O1 required-rule 정합 (matrix `O1-SC-063`~`O1-SC-069`, `O1-SC-072`, `O1-SC-073`; 규칙 이름만 추가한 vacuous PASS 금지, SC-051·SC-084·SC-097의 기존 PM invariant 약화 0)
- `deliveryBindingId` 21건을 runner contract의 canonical step-level binding으로 표현 (matrix `DELIVERY-BINDING`, `/endpointTemplates/r1DmePushDestination`; top-level alias·이중 해석 0)
- manifest-listed execution-profile assignment 문서와 schema 추가

Version 경계:

- `1.0.1`로 올라가는 것: handoff, package, 이 문서, `scenario-catalog`, `scenario-runner-contract`, `bundle-manifest`, execution-profile assignment와 그 schema
- `1.0.0`을 그대로 유지하는 것(의도적 retained identifier): Policy Type id `AIC_UECellSteering_1.0.0`, Appendix A schema의 모든 `$id`(`urn:oran-aic:schema:AIC_UECellSteering:policy:1.0.0` 등)와 그 JCS digest, DME type identifier `aic:policy-evidence:1.0.0`·`aic:ran-capability:1.0.0`, O1 PA/NETCONF profile identifier와 digest, `aic.ran-capability.1.0.0.schema.json`이 강제하는 `contractProfile` const `oran-aic/1.0.0`, bundle 내 golden fixture 파일명
- Wire payload semantics는 바뀌지 않았다. Wire schema 자체를 바꿔야 하는 변경이 생기면 patch release를 계속하지 않고 compatibility를 분석해 적절한 version bump를 별도로 판정한다.

## 20. 최종 통합 절차

통합은 다음 순서로 수행한다.

1. 양측 root handoff manifest, version manifest와 schema/profile/scenario/golden digest를 비교한다.
2. 양측 Phase A release가 자신의 실제 구현과 contract-faithful 상대 mock/harness로 만든 conformance 결과를 다시 검증한다.
3. 실제 Near-RT RIC의 Policy Type discovery, schema digest와 `nearRtRicId`를 확인한다.
4. 실제 R1 service에서 A1 Policy Management와 DME type을 discovery한다.
5. 실제 O1 endpoint, MnS Agent DN, capability manifest의 DN↔CellId, certificate chain과 SFTP host key를 profile/configuration과 대조한다.
6. source를 변경하지 않고 mock endpoint·credential reference·deployment value를 실제 값으로 교체한다.
7. 실제 Near-RT RIC에 `a1p-producer-conformance`를 실행한다.
8. 실제 Non-RT RIC Framework에 `r1-service-conformance`를 실행한다.
9. 고정 O1 fixture bytes에 상위 production collector/normalizer의 `o1-consumer-normalizer-conformance`를 다시 실행한다.
10. 상위 actual Consumer↔하위 actual Provider에 `o1-lifecycle-contract`를 실행한다.
11. 실제 Managed Element의 동적 output에 `o1-provider-profile-conformance`를 실행한다.
12. live O1 capture를 포함한 `end-to-end-contract` success, no-action, failure/rollback, retry/recovery trace를 실행한다.
13. 위 과정 전체가 Coordinator 판단 source 변경 없이 configuration 교체만으로 동작했는지 확인한다.

필수 end-to-end success trace:

```text
Operator intent
→ rApp S0–S2
→ R1 A1 Policy create
→ Non-RT RIC A1-P Consumer
→ A1-P PUT
→ Near-RT RIC Policy validation/status
→ xApp deterministic decision
→ E2SM-RC control
→ E2SM-KPM readback 및 A1 type-specific action status
→ A1 PolicyStatus notification/query
→ R1 policy status
→ O-RAN Managed Element O1 Performance Assurance/PM
→ SMO assurance correlation
→ R1 DME evidence
→ rApp S4 satisfaction judgement
→ S6 terminal outcome
```

필수 failure/restore trace:

```text
valid Policy
→ control failure 또는 readback mismatch
→ explicit failure status
→ pre-authorized rollback/recovery
→ rollback verified 또는 unknown
→ A1/R1 status와 DME evidence
→ rApp fail-closed negotiation/failsafe
```

## 21. Definition of Done

### 21.1 Phase A — 각 팀의 무소통 독립 release gate

상위와 하위는 상대 저장소·상대 프로세스·하드웨어 없이도 각자 다음을 만족한 pin 가능한 release를 만든다.

- Root `handoff-manifest.1.0.1.json`, 이 문서, Appendix A schema 및 `shared-contract-bundle/` 전체의 digest가 일치한다.
- 자신이 소유한 실제 구현과 상대 경계의 contract-faithful mock에 applicable baseline black-box corpus가 통과한다. 상위 release는 고정 bytes의 `o1-consumer-normalizer-conformance`와 Provider mock을 사용한 `o1-lifecycle-contract`를, 하위 release는 Consumer harness를 사용한 `o1-lifecycle-contract`와 고정 profile의 mock `o1-provider-profile-conformance`를 통과한다.
- endpoint, certificate/secret reference, topology deployment value 및 artifact location은 외부 설정으로 교체할 수 있고 계약 의미는 source 변경 없이 유지된다.
- release manifest의 commit과 artifact digest가 실제 산출물로 채워져 있으며 재현 가능한 build/test/runbook을 포함한다.
- backend release manifest의 `o1` provenance가 실제 NETCONF/YANG·FileDataReporting·PM generation·notification·SFTP 구현 및 configuration과 일치하고, vendored YANG closure가 offline import validation과 mounted YANG Library exact cross-check를 통과한다.
- 하위 release는 backend release manifest와 synthetic E2 inventory fixture를 shared schema로 검증하고, 실제 hardware 없이도 E2 Setup/function-definition drift·epoch rollover·unresolved NR-CGI가 zero write임을 증명한다.
- 상대 구현이 문서의 계약을 지키면 별도의 field 재해석, 임시 변환기 또는 협의 없이 연결될 수 있어야 한다.

### 21.2 Phase B — 최종 병합 acceptance gate

다음 조건을 모두 충족해야 최종 통합 완료로 간주한다.

- rApp은 R1 service를 사용하고 A1/xApp을 직접 호출하지 않는다.
- Non-RT RIC Framework가 A1-P Consumer다.
- Near-RT RIC가 표준 A1-P v2 Producer/termination을 제공한다.
- `AIC_UECellSteering_1.0.0`이 discovery되고 schema digest가 일치한다.
- Policy가 A1 표준 status code와 resource semantics로 처리된다.
- xApp은 envelope 안에서만 concrete target을 선택한다.
- xApp은 agentic/LLM intent 해석을 다시 수행하지 않는다.
- 제어와 telemetry가 E2SM-RC/KPM 경로를 사용한다.
- 양쪽 gNB의 exact patch/build가 release manifest와 일치하고, active E2 inventory의 node/epoch별 E2AP 2.03, KPM 2.03 ID 2/revision 2/OID/style 1·4 및 RC 1.03 ID 3/revision 1/OID/Style 3 Action 1/format·parameter tree가 static capability와 일치한다.
- RC profile의 NR-CGI wire encoding과 APER byte vector가 완전히 해결·검증되어 있고 reverse rollback control도 target node에서 같은 gate를 통과한다.
- E2 ACK만으로 action 성공을 선언하지 않는다.
- KPM/readback으로 `APPLIED_VERIFIED`를 확인한다.
- A1 status가 Non-RT RIC로 돌아오고 R1로 rApp에 제공된다.
- non-real-time KPI가 O1 Performance Assurance/PM으로 SMO에 수집되고, A1 payload가 아닌 R1 DME data path로 rApp에 도달한다.
- `intentId → policyId → episodeId → transactionId → actionId → KPI` provenance가 연결된다.
- duplicate/lost-response/restart case에서 duplicate E2 write가 없다.
- envelope violation, stale KPI 및 disconnected 상태에서 zero write가 입증된다.
- rollback sent와 verified/failed/unknown이 구분된다.
- A1/R1은 mock과 실제 implementation에 같은 black-box corpus가 통과하고, O1은 고정-byte consumer normalizer suite와 동적 live-provider profile suite가 각각 통과한다.
- official A1 conformance test와 project golden suite가 통과한다.
- custom HTTP adapter 없이 final end-to-end flow가 동작한다.
- E2SM-KPM만을 임의 상향 API로 올려 O1/R1을 대체하지 않는다.
- 통합 시 Coordinator 판단 source 변경이 필요하지 않다.
- 모든 source/config/patch/test/runbook이 하나의 pin 가능한 release에 존재한다.

## 22. 명시적 비요구사항

다음 내부 선택은 본 계약이 강제하지 않는다.

- xApp 내부 언어
- Near-RT adapter의 framework
- process/container 수
- database 종류
- 수학적 PRB load-balancing 알고리즘 구현
- FlexRIC 내부 routing 방식
- OAI patch 구현 방식
- log/metric backend 제품

단, 내부 선택으로 인해 본 문서의 외부 semantics, 표준 경계 또는 acceptance result가 달라져서는 안 된다.

## 23. 규범 참조

- [ETSI TS 103 983 V4.0.0 — A1 General Aspects and Principles](https://www.etsi.org/deliver/etsi_ts/103900_103999/103983/04.00.00_60/ts_103983v040000p.pdf)
- [ETSI TS 103 986 V3.3.0 — A1 Transport Protocol](https://www.etsi.org/deliver/etsi_ts/103900_103999/103986/03.03.00_60/ts_103986v030300p.pdf)
- [ETSI TS 103 987 V4.3.0 — A1 Application Protocol](https://www.etsi.org/deliver/etsi_ts/103900_103999/103987/04.03.00_60/ts_103987v040300p.pdf)
- [ETSI TS 103 988 V9.0.0 — A1 Type Definitions](https://www.etsi.org/deliver/etsi_ts/103900_103999/103988/09.00.00_60/ts_103988v090000p.pdf)
- [ETSI TS 103 989 V4.2.0 — A1 Test Specification](https://www.etsi.org/deliver/etsi_ts/103900_103999/103989/04.02.00_60/ts_103989v040200p.pdf)
- [ETSI TS 104 228 V11.0.0 — R1 General Aspects and Principles](https://www.etsi.org/deliver/etsi_ts/104200_104299/104228/11.00.00_60/ts_104228v110000p.pdf)
- [ETSI TS 104 231 V8.0.0 — R1 Application Protocols for R1 Services](https://www.etsi.org/deliver/etsi_ts/104200_104299/104231/08.00.00_60/ts_104231v080000p.pdf)
- [ETSI TS 104 043 V11.0.0 — O-RAN Operations and Maintenance Interface](https://www.etsi.org/deliver/etsi_ts/104000_104099/104043/11.00.00_60/ts_104043v110000p.pdf)
- [ETSI TS 104 104 V9.1.0 — O-RAN Security Requirements and Controls](https://www.etsi.org/deliver/etsi_TS/104100_104199/104104/09.01.00_60/ts_104104v090100p.pdf)
- [ETSI TS 104 107 V9.0.0 — O-RAN Security Protocols](https://www.etsi.org/deliver/etsi_ts/104100_104199/104107/09.00.00_60/ts_104107v090000p.pdf)
- [ETSI TS 128 622 V18.7.0 — Generic Network Resource Model](https://www.etsi.org/deliver/etsi_ts/128600_128699/128622/18.07.00_60/ts_128622v180700p.pdf)
- [ETSI TS 128 623 V18.7.0 — Generic NRM YANG](https://www.etsi.org/deliver/etsi_ts/128600_128699/128623/18.07.00_60/ts_128623v180700p.pdf)
- [3GPP SA5 MnS `Tag_Rel18_SA104` — pinned Release 18 YANG snapshot](https://forge.3gpp.org/rep/sa5/MnS/-/tags/Tag_Rel18_SA104)
- [ETSI TS 128 532 V18.3.0 — Generic Management Services](https://www.etsi.org/deliver/etsi_ts/128500_128599/128532/18.03.00_60/ts_128532v180300p.pdf)
- [ETSI TS 128 552 V18.11.0 — 5G Performance Measurements](https://www.etsi.org/deliver/etsi_ts/128500_128599/128552/18.11.00_60/ts_128552v181100p.pdf)
- [ETSI TS 132 432 V17.0.0 — Performance Measurement File Format](https://www.etsi.org/deliver/etsi_ts/132400_132499/132432/17.00.00_60/ts_132432v170000p.pdf)
- [ETSI TS 132 435 V10.0.0 — Performance Measurement XML Schema](https://www.etsi.org/deliver/etsi_ts/132400_132499/132435/10.00.00_60/ts_132435v100000p.pdf)
- [RFC 7950 — YANG 1.1](https://www.rfc-editor.org/rfc/rfc7950.html)
- [RFC 8525 — YANG Library](https://www.rfc-editor.org/rfc/rfc8525.html)
- [RFC 8528 — YANG Schema Mount](https://www.rfc-editor.org/rfc/rfc8528.html)

## Appendix A. 규범 machine-readable schema

본 appendix의 JSON object가 `oran-aic/1.0.1` package가 배포하는 권위 있는 schema다. 내용, `$id`와 JCS digest는 `oran-aic/1.0.0`과 동일하며 `1.0.1`에서 wire schema를 바꾸지 않았다(§19.2). 양측은 code fence 내 JSON을 변경 없이 각 schema file로 추출한다. 공백·key order와 관계없이 RFC 8785 canonical JSON의 SHA-256을 manifest에 기록한다. 설명 문구를 포함한 schema object 전체가 digest 대상이다.

### A.1 `AIC_UECellSteering_1.0.0.policy.schema.json`

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:oran-aic:schema:AIC_UECellSteering:policy:1.0.0",
  "title": "AIC UE Cell Steering Policy",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "scope",
    "steeringObjective",
    "constraints",
    "validity",
    "priority",
    "rollbackPolicy",
    "trace"
  ],
  "properties": {
    "scope": { "$ref": "#/$defs/Scope" },
    "steeringObjective": { "$ref": "#/$defs/SteeringObjective" },
    "constraints": { "$ref": "#/$defs/Constraints" },
    "validity": { "$ref": "#/$defs/Validity" },
    "priority": { "type": "integer", "minimum": 0, "maximum": 100 },
    "rollbackPolicy": { "$ref": "#/$defs/RollbackPolicy" },
    "trace": { "$ref": "#/$defs/Trace" }
  },
  "allOf": [
    {
      "if": {
        "properties": {
          "steeringObjective": {
            "properties": { "kind": { "const": "BALANCE_PRB_LOAD" } },
            "required": ["kind"]
          }
        }
      },
      "then": {
        "properties": {
          "steeringObjective": { "required": ["improvementThresholdPrb"] }
        }
      },
      "else": {
        "properties": {
          "steeringObjective": {
            "not": { "required": ["improvementThresholdPrb"] },
            "properties": {
              "actionEnvelope": {
                "properties": {
                  "allowedCells": { "maxItems": 1 }
                }
              }
            }
          }
        }
      }
    }
  ],
  "$defs": {
    "UtcDateTime": {
      "type": "string",
      "format": "date-time",
      "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]{1,9})?Z$"
    },
    "PlmnId": {
      "type": "object",
      "additionalProperties": false,
      "required": ["mcc", "mnc"],
      "properties": {
        "mcc": { "type": "string", "pattern": "^[0-9]{3}$" },
        "mnc": { "type": "string", "pattern": "^[0-9]{2,3}$" }
      }
    },
    "GuAmI": {
      "type": "object",
      "additionalProperties": false,
      "required": ["plmnId", "amfRegionId", "amfSetId", "amfPointer"],
      "properties": {
        "plmnId": { "$ref": "#/$defs/PlmnId" },
        "amfRegionId": { "type": "string", "pattern": "^[A-Fa-f0-9]{2}$" },
        "amfSetId": { "type": "string", "pattern": "^[0-3][A-Fa-f0-9]{2}$" },
        "amfPointer": { "type": "string", "pattern": "^[0-3][A-Fa-f0-9]{1}$" }
      }
    },
    "GuAmfUeNgapId": {
      "type": "object",
      "additionalProperties": false,
      "required": ["guAmI", "amfUeNgapId"],
      "properties": {
        "guAmI": { "$ref": "#/$defs/GuAmI" },
        "amfUeNgapId": {
          "type": "integer",
          "minimum": 0,
          "maximum": 1099511627775
        }
      }
    },
    "UeId": {
      "type": "object",
      "additionalProperties": false,
      "required": ["guAmfUeNgapId"],
      "properties": {
        "guAmfUeNgapId": { "$ref": "#/$defs/GuAmfUeNgapId" }
      }
    },
    "CId": {
      "type": "object",
      "additionalProperties": false,
      "required": ["ncI"],
      "properties": {
        "ncI": {
          "type": "integer",
          "minimum": 0,
          "maximum": 68719476735
        }
      }
    },
    "CellId": {
      "type": "object",
      "additionalProperties": false,
      "required": ["plmnId", "cId"],
      "properties": {
        "plmnId": { "$ref": "#/$defs/PlmnId" },
        "cId": { "$ref": "#/$defs/CId" }
      }
    },
    "Scope": {
      "type": "object",
      "additionalProperties": false,
      "required": ["ueId"],
      "properties": {
        "ueId": { "$ref": "#/$defs/UeId" }
      }
    },
    "ActionEnvelope": {
      "type": "object",
      "additionalProperties": false,
      "required": ["allowedCells", "forbiddenCells"],
      "properties": {
        "allowedCells": {
          "type": "array",
          "minItems": 1,
          "maxItems": 64,
          "uniqueItems": true,
          "items": { "$ref": "#/$defs/CellId" }
        },
        "forbiddenCells": {
          "type": "array",
          "maxItems": 64,
          "uniqueItems": true,
          "items": { "$ref": "#/$defs/CellId" }
        }
      }
    },
    "SteeringObjective": {
      "type": "object",
      "additionalProperties": false,
      "required": ["kind", "actionEnvelope"],
      "properties": {
        "kind": {
          "type": "string",
          "enum": ["BALANCE_PRB_LOAD", "PIN_TO_CELL"]
        },
        "actionEnvelope": { "$ref": "#/$defs/ActionEnvelope" },
        "improvementThresholdPrb": {
          "type": "number",
          "minimum": 0,
          "maximum": 100
        }
      }
    },
    "Constraints": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "maxActuationsPerEpisode",
        "minSecondsBetweenActuations",
        "requiredKpiFreshnessMs",
        "actionDeadlineMs"
      ],
      "properties": {
        "maxActuationsPerEpisode": { "const": 1 },
        "minSecondsBetweenActuations": {
          "type": "integer",
          "minimum": 0,
          "maximum": 86400
        },
        "requiredKpiFreshnessMs": {
          "type": "integer",
          "minimum": 1,
          "maximum": 60000
        },
        "actionDeadlineMs": {
          "type": "integer",
          "minimum": 1,
          "maximum": 120000
        }
      }
    },
    "Validity": {
      "type": "object",
      "additionalProperties": false,
      "required": ["notBefore", "expiresAt"],
      "properties": {
        "notBefore": { "$ref": "#/$defs/UtcDateTime" },
        "expiresAt": { "$ref": "#/$defs/UtcDateTime" }
      }
    },
    "RollbackPolicy": {
      "type": "object",
      "additionalProperties": false,
      "required": ["on", "timeoutMs"],
      "properties": {
        "on": {
          "type": "array",
          "maxItems": 2,
          "uniqueItems": true,
          "items": {
            "type": "string",
            "enum": ["READBACK_MISMATCH", "APPLY_FAILED"]
          }
        },
        "timeoutMs": {
          "type": "integer",
          "minimum": 1,
          "maximum": 120000
        }
      }
    },
    "Trace": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "intentId",
        "intentRevision",
        "policyRevision",
        "idempotencyKey",
        "correlationId",
        "producerId"
      ],
      "properties": {
        "intentId": { "type": "string", "format": "uuid" },
        "intentRevision": { "type": "integer", "minimum": 1 },
        "policyRevision": { "type": "integer", "minimum": 1 },
        "idempotencyKey": {
          "type": "string",
          "minLength": 1,
          "maxLength": 128,
          "pattern": "^[A-Za-z0-9._:/-]+$"
        },
        "correlationId": { "type": "string", "format": "uuid" },
        "producerId": {
          "type": "string",
          "minLength": 1,
          "maxLength": 128,
          "pattern": "^[A-Za-z0-9._:/-]+$"
        }
      }
    }
  }
}
```

### A.2 `AIC_UECellSteering_1.0.0.status.schema.json`

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:oran-aic:schema:AIC_UECellSteering:status:1.0.0",
  "title": "AIC UE Cell Steering Policy Status",
  "type": "object",
  "additionalProperties": false,
  "required": ["enforceStatus", "aicStatus"],
  "properties": {
    "enforceStatus": {
      "type": "string",
      "enum": ["ENFORCED", "NOT_ENFORCED"]
    },
    "enforceReason": {
      "type": "string",
      "enum": [
        "SCOPE_NOT_APPLICABLE",
        "STATEMENT_NOT_APPLICABLE",
        "OTHER_REASON"
      ]
    },
    "aicStatus": { "$ref": "#/$defs/AicStatus" }
  },
  "allOf": [
    {
      "if": {
        "properties": { "enforceStatus": { "const": "NOT_ENFORCED" } },
        "required": ["enforceStatus"]
      },
      "then": {
        "required": ["enforceReason"],
        "properties": {
          "aicStatus": {
            "properties": {
              "policyState": {
                "enum": [
                  "NOT_ENFORCED",
                  "EXPIRED",
                  "CANCELLED",
                  "SUPERSEDED",
                  "RECOVERY_PENDING",
                  "ERROR"
                ]
              }
            }
          }
        }
      },
      "else": {
        "not": { "required": ["enforceReason"] },
        "properties": {
          "aicStatus": {
            "properties": { "policyState": { "const": "ACTIVE" } }
          }
        }
      }
    },
    {
      "if": {
        "properties": {
          "aicStatus": {
            "properties": { "episodeState": { "const": "RECOVERY_PENDING" } },
            "required": ["episodeState"]
          }
        }
      },
      "then": {
        "properties": {
          "enforceStatus": { "const": "NOT_ENFORCED" },
          "enforceReason": { "const": "OTHER_REASON" },
          "aicStatus": {
            "properties": { "policyState": { "const": "RECOVERY_PENDING" } }
          }
        }
      }
    },
    {
      "if": {
        "properties": {
          "aicStatus": {
            "properties": {
              "episodeState": {
                "enum": ["ROLLBACK_FAILED", "ROLLBACK_UNKNOWN"]
              }
            },
            "required": ["episodeState"]
          }
        }
      },
      "then": {
        "properties": {
          "enforceStatus": { "const": "NOT_ENFORCED" },
          "enforceReason": { "const": "OTHER_REASON" },
          "aicStatus": {
            "properties": { "policyState": { "const": "RECOVERY_PENDING" } }
          }
        }
      }
    },
    {
      "if": {
        "properties": {
          "aicStatus": {
            "properties": { "episodeState": { "const": "QUARANTINED" } },
            "required": ["episodeState"]
          }
        }
      },
      "then": {
        "properties": {
          "enforceStatus": { "const": "NOT_ENFORCED" },
          "enforceReason": { "const": "OTHER_REASON" },
          "aicStatus": {
            "properties": { "policyState": { "const": "ERROR" } }
          }
        }
      }
    },
    {
      "if": {
        "properties": {
          "aicStatus": {
            "properties": {
              "policyState": {
                "enum": [
                  "EXPIRED",
                  "CANCELLED",
                  "SUPERSEDED",
                  "RECOVERY_PENDING",
                  "ERROR"
                ]
              }
            },
            "required": ["policyState"]
          }
        }
      },
      "then": {
        "properties": {
          "enforceStatus": { "const": "NOT_ENFORCED" },
          "enforceReason": { "const": "OTHER_REASON" }
        }
      }
    }
  ],
  "$defs": {
    "UtcDateTime": {
      "type": "string",
      "format": "date-time",
      "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]{1,9})?Z$"
    },
    "PlmnId": {
      "type": "object",
      "additionalProperties": false,
      "required": ["mcc", "mnc"],
      "properties": {
        "mcc": { "type": "string", "pattern": "^[0-9]{3}$" },
        "mnc": { "type": "string", "pattern": "^[0-9]{2,3}$" }
      }
    },
    "CId": {
      "type": "object",
      "additionalProperties": false,
      "required": ["ncI"],
      "properties": {
        "ncI": {
          "type": "integer",
          "minimum": 0,
          "maximum": 68719476735
        }
      }
    },
    "CellId": {
      "type": "object",
      "additionalProperties": false,
      "required": ["plmnId", "cId"],
      "properties": {
        "plmnId": { "$ref": "#/$defs/PlmnId" },
        "cId": { "$ref": "#/$defs/CId" }
      }
    },
    "NoAction": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "reason",
        "observedServingCell",
        "observedAt",
        "observationWindowEnd",
        "snapshotSha256"
      ],
      "properties": {
        "reason": {
          "type": "string",
          "enum": [
            "ALREADY_ON_TARGET",
            "IMPROVEMENT_BELOW_THRESHOLD",
            "NO_ELIGIBLE_TARGET"
          ]
        },
        "observedServingCell": { "$ref": "#/$defs/CellId" },
        "observedAt": { "$ref": "#/$defs/UtcDateTime" },
        "observationWindowEnd": { "$ref": "#/$defs/UtcDateTime" },
        "snapshotSha256": {
          "type": "string",
          "pattern": "^[a-f0-9]{64}$"
        }
      }
    },
    "Control": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "transactionId",
        "actionId",
        "result",
        "resultIsEffectEvidence",
        "writeMayHaveOccurred"
      ],
      "properties": {
        "transactionId": { "type": "string", "format": "uuid" },
        "actionId": { "type": "string", "format": "uuid" },
        "result": {
          "type": "string",
          "enum": ["PENDING", "ACK", "NACK", "TIMEOUT", "UNKNOWN"]
        },
        "resultIsEffectEvidence": { "const": false },
        "writeMayHaveOccurred": { "type": "boolean" }
      }
    },
    "Readback": {
      "type": "object",
      "additionalProperties": false,
      "required": ["result", "observedAt", "latencyMs"],
      "properties": {
        "result": {
          "type": "string",
          "enum": ["VERIFIED", "MISMATCH", "MISSING", "STALE", "NOT_AVAILABLE"]
        },
        "observedServingCell": { "$ref": "#/$defs/CellId" },
        "observedAt": { "$ref": "#/$defs/UtcDateTime" },
        "latencyMs": { "type": "integer", "minimum": 0 }
      },
      "allOf": [
        {
          "if": {
            "properties": {
              "result": { "enum": ["VERIFIED", "MISMATCH"] }
            },
            "required": ["result"]
          },
          "then": { "required": ["observedServingCell"] },
          "else": { "not": { "required": ["observedServingCell"] } }
        }
      ]
    },
    "Rollback": {
      "type": "object",
      "additionalProperties": false,
      "required": ["state"],
      "properties": {
        "state": {
          "type": "string",
          "enum": [
            "NOT_REQUESTED",
            "REQUESTED",
            "SENT",
            "VERIFIED",
            "FAILED",
            "UNKNOWN"
          ]
        },
        "restoreCell": { "$ref": "#/$defs/CellId" },
        "transactionId": { "type": "string", "format": "uuid" },
        "actionId": { "type": "string", "format": "uuid" },
        "writeMayHaveOccurred": { "type": "boolean" }
      },
      "dependentRequired": {
        "transactionId": ["actionId", "writeMayHaveOccurred"],
        "actionId": ["transactionId", "writeMayHaveOccurred"]
      },
      "allOf": [
        {
          "if": {
            "properties": {
              "state": { "enum": ["NOT_REQUESTED", "REQUESTED"] }
            },
            "required": ["state"]
          },
          "then": {
            "not": {
              "anyOf": [
                { "required": ["transactionId"] },
                { "required": ["actionId"] },
                { "required": ["writeMayHaveOccurred"] }
              ]
            }
          },
          "else": {
            "required": [
              "restoreCell",
              "transactionId",
              "actionId",
              "writeMayHaveOccurred"
            ],
            "properties": {
              "writeMayHaveOccurred": { "const": true }
            }
          }
        },
        {
          "if": {
            "properties": { "state": { "const": "REQUESTED" } },
            "required": ["state"]
          },
          "then": { "required": ["restoreCell"] },
          "else": {
            "if": {
              "properties": { "state": { "const": "NOT_REQUESTED" } },
              "required": ["state"]
            },
            "then": { "not": { "required": ["restoreCell"] } }
          }
        }
      ]
    },
    "Error": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "code",
        "stage",
        "retryable",
        "writeMayHaveOccurred",
        "detail"
      ],
      "properties": {
        "code": {
          "type": "string",
          "enum": [
            "AIC_SCHEMA_INVALID",
            "AIC_RESOURCE_NOT_FOUND",
            "AIC_UNSUPPORTED_OBJECTIVE",
            "AIC_SCOPE_NOT_FOUND",
            "AIC_CELL_NOT_ALLOWED",
            "AIC_CELL_NOT_NEIGHBOR",
            "AIC_POLICY_CONFLICT",
            "AIC_STALE_REVISION",
            "AIC_IDEMPOTENCY_CONFLICT",
            "AIC_VALIDITY_INVALID",
            "AIC_CAPABILITY_MISMATCH",
            "AIC_KPI_STALE",
            "AIC_KPI_MISSING",
            "AIC_E2_NOT_READY",
            "AIC_LOCK_CONFLICT",
            "AIC_ENVELOPE_VIOLATION",
            "AIC_DEADLINE_EXCEEDED",
            "AIC_CONTROL_TIMEOUT",
            "AIC_APPLY_FAILED",
            "AIC_READBACK_MISMATCH",
            "AIC_ROLLBACK_FAILED",
            "AIC_ROLLBACK_UNKNOWN",
            "AIC_RECOVERY_PENDING",
            "AIC_INTERNAL_ERROR"
          ]
        },
        "stage": {
          "type": "string",
          "enum": [
            "ADMISSION",
            "DECISION",
            "CONTROL",
            "READBACK",
            "ROLLBACK",
            "RECOVERY",
            "TELEMETRY",
            "INTERNAL"
          ]
        },
        "retryable": { "type": "boolean" },
        "writeMayHaveOccurred": { "type": "boolean" },
        "detail": { "type": "string", "minLength": 1, "maxLength": 1024 }
      }
    },
    "Trace": {
      "type": "object",
      "additionalProperties": false,
      "required": ["intentId", "intentRevision", "correlationId"],
      "properties": {
        "intentId": { "type": "string", "format": "uuid" },
        "intentRevision": { "type": "integer", "minimum": 1 },
        "correlationId": { "type": "string", "format": "uuid" }
      }
    },
    "AicStatus": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "policyId",
        "policyRevision",
        "producerEpoch",
        "statusSeq",
        "policyState",
        "policyTerminal",
        "occurredAt",
        "trace"
      ],
      "properties": {
        "policyId": { "type": "string", "minLength": 1, "maxLength": 255 },
        "policyRevision": { "type": "integer", "minimum": 1 },
        "producerEpoch": { "type": "string", "format": "uuid" },
        "statusSeq": { "type": "integer", "minimum": 1 },
        "policyState": {
          "type": "string",
          "enum": [
            "ACTIVE",
            "NOT_ENFORCED",
            "EXPIRED",
            "CANCELLED",
            "SUPERSEDED",
            "RECOVERY_PENDING",
            "ERROR"
          ]
        },
        "policyTerminal": { "type": "boolean" },
        "episodeId": { "type": "string", "format": "uuid" },
        "episodeState": {
          "type": "string",
          "enum": [
            "SCHEDULED",
            "COMPUTING",
            "NO_ACTION",
            "ABORTED_NO_WRITE",
            "APPLYING",
            "APPLIED_UNVERIFIED",
            "APPLIED_VERIFIED",
            "READBACK_MISMATCH",
            "APPLY_FAILED",
            "ROLLING_BACK",
            "ROLLED_BACK_VERIFIED",
            "ROLLBACK_FAILED",
            "ROLLBACK_UNKNOWN",
            "RECOVERY_PENDING",
            "QUARANTINED"
          ]
        },
        "episodeTerminal": { "type": "boolean" },
        "occurredAt": { "$ref": "#/$defs/UtcDateTime" },
        "noAction": { "$ref": "#/$defs/NoAction" },
        "selectedCell": { "$ref": "#/$defs/CellId" },
        "control": { "$ref": "#/$defs/Control" },
        "readback": { "$ref": "#/$defs/Readback" },
        "rollback": { "$ref": "#/$defs/Rollback" },
        "error": { "$ref": "#/$defs/Error" },
        "trace": { "$ref": "#/$defs/Trace" }
      },
      "dependentRequired": {
        "episodeId": ["episodeState", "episodeTerminal"],
        "episodeState": ["episodeId", "episodeTerminal"],
        "episodeTerminal": ["episodeId", "episodeState"]
      },
      "allOf": [
        {
          "if": {
            "properties": {
              "policyState": {
                "enum": ["EXPIRED", "CANCELLED", "SUPERSEDED", "ERROR"]
              }
            },
            "required": ["policyState"]
          },
          "then": { "properties": { "policyTerminal": { "const": true } } },
          "else": { "properties": { "policyTerminal": { "const": false } } }
        },
        {
          "if": {
            "properties": {
              "episodeState": {
                "enum": [
                  "NO_ACTION",
                  "ABORTED_NO_WRITE",
                  "APPLIED_VERIFIED",
                  "ROLLED_BACK_VERIFIED",
                  "QUARANTINED"
                ]
              }
            },
            "required": ["episodeState"]
          },
          "then": { "properties": { "episodeTerminal": { "const": true } } }
        },
        {
          "if": {
            "properties": {
              "episodeState": {
                "enum": [
                  "SCHEDULED",
                  "COMPUTING",
                  "APPLYING",
                  "APPLIED_UNVERIFIED",
                  "ROLLING_BACK",
                  "ROLLBACK_FAILED",
                  "ROLLBACK_UNKNOWN",
                  "RECOVERY_PENDING"
                ]
              }
            },
            "required": ["episodeState"]
          },
          "then": { "properties": { "episodeTerminal": { "const": false } } }
        },
        {
          "if": {
            "properties": { "episodeState": { "const": "READBACK_MISMATCH" } },
            "required": ["episodeState"]
          },
          "then": {
            "properties": {
              "rollback": {
                "properties": {
                  "state": { "enum": ["NOT_REQUESTED", "REQUESTED"] }
                },
                "required": ["state"]
              }
            },
            "allOf": [
              {
                "if": {
                  "required": ["rollback"],
                  "properties": {
                    "rollback": {
                      "properties": { "state": { "const": "REQUESTED" } },
                      "required": ["state"]
                    }
                  }
                },
                "then": {
                  "properties": { "episodeTerminal": { "const": false } }
                },
                "else": {
                  "properties": { "episodeTerminal": { "const": true } }
                }
              }
            ]
          }
        },
        {
          "if": {
            "properties": { "episodeState": { "const": "APPLY_FAILED" } },
            "required": ["episodeState"]
          },
          "then": {
            "oneOf": [
              {
                "properties": {
                  "control": {
                    "properties": { "writeMayHaveOccurred": { "const": false } }
                  },
                  "episodeTerminal": { "const": true }
                }
              },
              {
                "required": ["rollback"],
                "properties": {
                  "control": {
                    "properties": { "writeMayHaveOccurred": { "const": true } }
                  },
                  "rollback": {
                    "properties": { "state": { "const": "REQUESTED" } },
                    "required": ["state"]
                  },
                  "episodeTerminal": { "const": false }
                }
              }
            ]
          }
        },
        {
          "if": {
            "properties": {
              "episodeState": {
                "enum": [
                  "ABORTED_NO_WRITE",
                  "READBACK_MISMATCH",
                  "APPLY_FAILED",
                  "ROLLBACK_FAILED",
                  "ROLLBACK_UNKNOWN",
                  "RECOVERY_PENDING",
                  "QUARANTINED"
                ]
              }
            },
            "required": ["episodeState"]
          },
          "then": { "required": ["error"] }
        },
        {
          "if": {
            "properties": {
              "episodeState": {
                "enum": [
                  "APPLYING",
                  "APPLIED_UNVERIFIED",
                  "APPLIED_VERIFIED",
                  "READBACK_MISMATCH",
                  "APPLY_FAILED",
                  "ROLLING_BACK",
                  "ROLLED_BACK_VERIFIED",
                  "ROLLBACK_FAILED",
                  "ROLLBACK_UNKNOWN",
                  "RECOVERY_PENDING"
                ]
              }
            },
            "required": ["episodeState"]
          },
          "then": { "required": ["selectedCell", "control"] }
        },
        {
          "if": {
            "properties": { "episodeState": { "const": "APPLIED_VERIFIED" } },
            "required": ["episodeState"]
          },
          "then": {
            "required": ["readback"],
            "properties": {
              "readback": { "properties": { "result": { "const": "VERIFIED" } } }
            }
          }
        },
        {
          "if": {
            "properties": { "episodeState": { "const": "READBACK_MISMATCH" } },
            "required": ["episodeState"]
          },
          "then": {
            "required": ["readback"],
            "properties": {
              "readback": { "properties": { "result": { "const": "MISMATCH" } } }
            }
          }
        },
        {
          "if": {
            "properties": { "episodeState": { "const": "ROLLING_BACK" } },
            "required": ["episodeState"]
          },
          "then": {
            "required": ["rollback"],
            "properties": {
              "rollback": {
                "properties": { "state": { "enum": ["REQUESTED", "SENT"] } }
              }
            }
          }
        },
        {
          "if": {
            "properties": { "episodeState": { "const": "ROLLED_BACK_VERIFIED" } },
            "required": ["episodeState"]
          },
          "then": {
            "required": ["rollback", "readback"],
            "properties": {
              "rollback": { "properties": { "state": { "const": "VERIFIED" } } },
              "readback": { "properties": { "result": { "const": "VERIFIED" } } }
            }
          }
        },
        {
          "if": {
            "properties": { "episodeState": { "const": "ROLLBACK_FAILED" } },
            "required": ["episodeState"]
          },
          "then": {
            "required": ["rollback"],
            "properties": {
              "rollback": { "properties": { "state": { "const": "FAILED" } } }
            }
          }
        },
        {
          "if": {
            "properties": { "episodeState": { "const": "ROLLBACK_UNKNOWN" } },
            "required": ["episodeState"]
          },
          "then": {
            "required": ["rollback"],
            "properties": {
              "rollback": { "properties": { "state": { "const": "UNKNOWN" } } }
            }
          }
        },
        {
          "if": {
            "properties": { "episodeState": { "const": "NO_ACTION" } },
            "required": ["episodeState"]
          },
          "then": {
            "required": ["noAction"],
            "not": {
              "anyOf": [
                { "required": ["selectedCell"] },
                { "required": ["control"] },
                { "required": ["readback"] },
                { "required": ["rollback"] },
                { "required": ["error"] }
              ]
            }
          },
          "else": { "not": { "required": ["noAction"] } }
        },
        {
          "if": { "not": { "required": ["episodeId"] } },
          "then": {
            "not": {
              "anyOf": [
                { "required": ["selectedCell"] },
                { "required": ["control"] },
                { "required": ["readback"] },
                { "required": ["rollback"] }
              ]
            }
          }
        },
        {
          "if": {
            "properties": {
              "episodeState": { "enum": ["SCHEDULED", "COMPUTING"] }
            },
            "required": ["episodeState"]
          },
          "then": {
            "not": {
              "anyOf": [
                { "required": ["selectedCell"] },
                { "required": ["control"] },
                { "required": ["readback"] },
                { "required": ["rollback"] },
                { "required": ["error"] }
              ]
            }
          }
        },
        {
          "if": {
            "properties": { "episodeState": { "const": "ABORTED_NO_WRITE" } },
            "required": ["episodeState"]
          },
          "then": {
            "required": ["error"],
            "properties": {
              "error": {
                "properties": { "writeMayHaveOccurred": { "const": false } }
              }
            },
            "not": {
              "anyOf": [
                { "required": ["control"] },
                { "required": ["readback"] },
                { "required": ["rollback"] }
              ]
            }
          }
        },
        {
          "if": {
            "properties": { "episodeState": { "const": "APPLYING" } },
            "required": ["episodeState"]
          },
          "then": {
            "required": ["selectedCell", "control"],
            "properties": {
              "control": {
                "properties": {
                  "result": { "const": "PENDING" },
                  "writeMayHaveOccurred": { "const": true }
                }
              }
            },
            "not": {
              "anyOf": [
                { "required": ["readback"] },
                { "required": ["rollback"] },
                { "required": ["error"] }
              ]
            }
          }
        },
        {
          "if": {
            "properties": { "episodeState": { "const": "APPLIED_UNVERIFIED" } },
            "required": ["episodeState"]
          },
          "then": {
            "required": ["selectedCell", "control"],
            "properties": {
              "control": {
                "properties": {
                  "result": { "const": "ACK" },
                  "writeMayHaveOccurred": { "const": true }
                }
              },
              "readback": {
                "properties": {
                  "result": { "enum": ["MISSING", "STALE", "NOT_AVAILABLE"] }
                }
              }
            },
            "not": {
              "anyOf": [
                { "required": ["rollback"] },
                { "required": ["error"] }
              ]
            }
          }
        },
        {
          "if": {
            "properties": { "episodeState": { "const": "APPLIED_VERIFIED" } },
            "required": ["episodeState"]
          },
          "then": {
            "required": ["selectedCell", "control", "readback"],
            "properties": {
              "control": {
                "properties": {
                  "result": { "enum": ["ACK", "NACK", "TIMEOUT", "UNKNOWN"] },
                  "writeMayHaveOccurred": { "const": true }
                }
              },
              "readback": { "properties": { "result": { "const": "VERIFIED" } } },
              "rollback": {
                "properties": { "state": { "const": "NOT_REQUESTED" } }
              }
            },
            "not": { "required": ["error"] }
          }
        },
        {
          "if": {
            "properties": { "episodeState": { "const": "READBACK_MISMATCH" } },
            "required": ["episodeState"]
          },
          "then": {
            "required": ["selectedCell", "control", "readback", "error"],
            "properties": {
              "control": {
                "properties": {
                  "result": { "enum": ["ACK", "NACK", "TIMEOUT", "UNKNOWN"] },
                  "writeMayHaveOccurred": { "const": true }
                }
              },
              "readback": { "properties": { "result": { "const": "MISMATCH" } } },
              "error": {
                "properties": {
                  "code": { "const": "AIC_READBACK_MISMATCH" },
                  "stage": { "const": "READBACK" },
                  "writeMayHaveOccurred": { "const": true }
                }
              }
            }
          }
        },
        {
          "if": {
            "properties": { "episodeState": { "const": "APPLY_FAILED" } },
            "required": ["episodeState"]
          },
          "then": {
            "required": ["selectedCell", "control", "error"],
            "properties": {
              "control": { "properties": { "result": { "const": "NACK" } } },
              "error": {
                "properties": {
                  "code": { "const": "AIC_APPLY_FAILED" },
                  "stage": { "const": "CONTROL" }
                }
              }
            },
            "not": { "required": ["readback"] },
            "oneOf": [
              {
                "properties": {
                  "control": { "properties": { "writeMayHaveOccurred": { "const": false } } },
                  "error": { "properties": { "writeMayHaveOccurred": { "const": false } } }
                },
                "not": { "required": ["rollback"] }
              },
              {
                "required": ["rollback"],
                "properties": {
                  "control": { "properties": { "writeMayHaveOccurred": { "const": false } } },
                  "error": { "properties": { "writeMayHaveOccurred": { "const": false } } },
                  "rollback": { "properties": { "state": { "const": "NOT_REQUESTED" } } }
                }
              },
              {
                "required": ["rollback"],
                "properties": {
                  "control": { "properties": { "writeMayHaveOccurred": { "const": true } } },
                  "error": { "properties": { "writeMayHaveOccurred": { "const": true } } },
                  "rollback": {
                    "properties": { "state": { "const": "REQUESTED" } },
                    "required": ["state"]
                  }
                }
              }
            ]
          }
        },
        {
          "if": {
            "properties": { "episodeState": { "const": "ROLLING_BACK" } },
            "required": ["episodeState"]
          },
          "then": {
            "required": ["selectedCell", "control", "rollback"],
            "properties": {
              "control": {
                "properties": {
                  "result": { "enum": ["ACK", "NACK", "TIMEOUT", "UNKNOWN"] }
                }
              },
              "rollback": {
                "properties": { "state": { "enum": ["REQUESTED", "SENT"] } }
              }
            }
          }
        },
        {
          "if": {
            "properties": { "episodeState": { "const": "ROLLED_BACK_VERIFIED" } },
            "required": ["episodeState"]
          },
          "then": {
            "required": ["selectedCell", "control", "rollback", "readback"],
            "properties": {
              "control": {
                "properties": {
                  "result": { "enum": ["ACK", "NACK", "TIMEOUT", "UNKNOWN"] }
                }
              },
              "rollback": { "properties": { "state": { "const": "VERIFIED" } } },
              "readback": { "properties": { "result": { "const": "VERIFIED" } } }
            }
          }
        },
        {
          "if": {
            "properties": { "episodeState": { "const": "ROLLBACK_FAILED" } },
            "required": ["episodeState"]
          },
          "then": {
            "required": ["selectedCell", "control", "rollback", "error"],
            "properties": {
              "control": {
                "properties": {
                  "result": { "enum": ["ACK", "NACK", "TIMEOUT", "UNKNOWN"] }
                }
              },
              "rollback": { "properties": { "state": { "const": "FAILED" } } },
              "error": {
                "properties": {
                  "code": { "const": "AIC_ROLLBACK_FAILED" },
                  "stage": { "const": "ROLLBACK" },
                  "writeMayHaveOccurred": { "const": true }
                }
              }
            }
          }
        },
        {
          "if": {
            "properties": { "episodeState": { "const": "ROLLBACK_UNKNOWN" } },
            "required": ["episodeState"]
          },
          "then": {
            "required": ["selectedCell", "control", "rollback", "error"],
            "properties": {
              "control": {
                "properties": {
                  "result": { "enum": ["ACK", "NACK", "TIMEOUT", "UNKNOWN"] }
                }
              },
              "rollback": { "properties": { "state": { "const": "UNKNOWN" } } },
              "error": {
                "properties": {
                  "code": { "const": "AIC_ROLLBACK_UNKNOWN" },
                  "stage": { "const": "ROLLBACK" },
                  "writeMayHaveOccurred": { "const": true }
                }
              }
            }
          }
        },
        {
          "if": {
            "properties": { "episodeState": { "const": "RECOVERY_PENDING" } },
            "required": ["episodeState"]
          },
          "then": {
            "required": ["selectedCell", "control", "error"],
            "properties": {
              "control": {
                "properties": {
                  "result": { "enum": ["ACK", "NACK", "TIMEOUT", "UNKNOWN"] },
                  "writeMayHaveOccurred": { "const": true }
                }
              },
              "readback": {
                "properties": {
                  "result": { "enum": ["MISSING", "STALE", "NOT_AVAILABLE"] }
                }
              },
              "rollback": { "properties": { "state": { "const": "SENT" } } },
              "error": {
                "properties": { "writeMayHaveOccurred": { "const": true } }
              }
            }
          }
        },
        {
          "if": {
            "properties": { "episodeState": { "const": "QUARANTINED" } },
            "required": ["episodeState"]
          },
          "then": {
            "required": ["error"],
            "oneOf": [
              {
                "properties": {
                  "error": {
                    "properties": {
                      "code": { "const": "AIC_ENVELOPE_VIOLATION" },
                      "writeMayHaveOccurred": { "const": false }
                    }
                  }
                },
                "not": {
                  "anyOf": [
                    { "required": ["control"] },
                    { "required": ["readback"] },
                    { "required": ["rollback"] }
                  ]
                }
              },
              {
                "required": ["selectedCell", "control"],
                "properties": {
                  "control": {
                    "properties": {
                      "result": { "enum": ["ACK", "NACK", "TIMEOUT", "UNKNOWN"] },
                      "writeMayHaveOccurred": { "const": true }
                    }
                  },
                  "error": {
                    "properties": { "writeMayHaveOccurred": { "const": true } }
                  }
                },
                "not": { "required": ["rollback"] }
              },
              {
                "required": ["selectedCell", "control", "rollback"],
                "properties": {
                  "control": {
                    "properties": {
                      "result": { "enum": ["ACK", "NACK", "TIMEOUT", "UNKNOWN"] }
                    }
                  },
                  "rollback": { "properties": { "state": { "const": "FAILED" } } },
                  "error": {
                    "properties": {
                      "code": { "const": "AIC_ROLLBACK_FAILED" },
                      "stage": { "const": "ROLLBACK" },
                      "writeMayHaveOccurred": { "const": true }
                    }
                  }
                }
              },
              {
                "required": ["selectedCell", "control", "rollback"],
                "properties": {
                  "control": {
                    "properties": {
                      "result": { "enum": ["ACK", "NACK", "TIMEOUT", "UNKNOWN"] }
                    }
                  },
                  "rollback": { "properties": { "state": { "const": "UNKNOWN" } } },
                  "error": {
                    "properties": {
                      "code": { "const": "AIC_ROLLBACK_UNKNOWN" },
                      "stage": { "const": "ROLLBACK" },
                      "writeMayHaveOccurred": { "const": true }
                    }
                  }
                }
              }
            ]
          }
        }
      ]
    }
  }
}
```

### A.3 `aic.policy-evidence.1.0.0.schema.json`

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:oran-aic:schema:policy-evidence:1.0.0",
  "title": "AIC Policy Evidence DME Record",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "dmeTypeId",
    "observationId",
    "observedAt",
    "window",
    "policyScope",
    "measurementScope",
    "source",
    "phase",
    "quality",
    "samples",
    "correlation"
  ],
  "properties": {
    "dmeTypeId": { "const": "aic:policy-evidence:1.0.0" },
    "observationId": { "type": "string", "format": "uuid" },
    "observedAt": { "$ref": "#/$defs/UtcDateTime" },
    "window": { "$ref": "#/$defs/Window" },
    "policyScope": { "$ref": "#/$defs/PolicyScope" },
    "measurementScope": { "$ref": "#/$defs/MeasurementScope" },
    "source": { "$ref": "#/$defs/Source" },
    "phase": {
      "type": "string",
      "enum": ["BEFORE", "AFTER", "STEADY", "OVERLAPS_ACTION"]
    },
    "quality": {
      "type": "string",
      "enum": ["OK", "SUSPECT", "STALE", "MISSING", "NOT_AVAILABLE", "AMBIGUOUS"]
    },
    "ambiguityReason": {
      "type": "string",
      "enum": [
        "ACTION_WINDOW_OVERLAP",
        "IDENTITY_CORRELATION_AMBIGUOUS",
        "CLOCK_SKEW_EXCEEDED",
        "CONTRADICTORY_SOURCE"
      ]
    },
    "samples": {
      "type": "array",
      "minItems": 1,
      "maxItems": 2,
      "items": { "$ref": "#/$defs/Sample" },
      "contains": {
        "properties": { "name": { "const": "RRU.PrbDl" } },
        "required": ["name"]
      },
      "minContains": 1,
      "maxContains": 1
    },
    "correlation": { "$ref": "#/$defs/Correlation" }
  },
  "allOf": [
    {
      "if": { "properties": { "quality": { "const": "OK" } }, "required": ["quality"] },
      "then": {
        "properties": {
          "samples": {
            "items": { "properties": { "quality": { "const": "OK" } } }
          }
        }
      }
    },
    {
      "if": {
        "properties": { "phase": { "const": "OVERLAPS_ACTION" } },
        "required": ["phase"]
      },
      "then": {
        "required": ["ambiguityReason"],
        "properties": {
          "quality": { "const": "AMBIGUOUS" },
          "ambiguityReason": { "const": "ACTION_WINDOW_OVERLAP" }
        }
      }
    },
    {
      "if": { "properties": { "quality": { "const": "SUSPECT" } }, "required": ["quality"] },
      "then": {
        "properties": {
          "samples": {
            "items": { "properties": { "quality": { "enum": ["SUSPECT", "OK"] } } },
            "contains": { "properties": { "quality": { "const": "SUSPECT" } }, "required": ["quality"] }
          }
        }
      }
    },
    {
      "if": { "properties": { "quality": { "const": "STALE" } }, "required": ["quality"] },
      "then": {
        "properties": {
          "samples": {
            "items": { "properties": { "quality": { "enum": ["STALE", "SUSPECT", "OK"] } } },
            "contains": {
              "properties": { "quality": { "const": "STALE" } },
              "required": ["quality"]
            }
          }
        }
      }
    },
    {
      "if": { "properties": { "quality": { "const": "MISSING" } }, "required": ["quality"] },
      "then": {
        "properties": {
          "samples": {
            "items": { "properties": { "quality": { "enum": ["MISSING", "STALE", "SUSPECT", "OK"] } } },
            "contains": {
              "properties": { "quality": { "const": "MISSING" } },
              "required": ["quality"]
            }
          }
        }
      }
    },
    {
      "if": { "properties": { "quality": { "const": "NOT_AVAILABLE" } }, "required": ["quality"] },
      "then": {
        "properties": {
          "samples": {
            "items": { "properties": { "quality": { "enum": ["NOT_AVAILABLE", "MISSING", "STALE", "SUSPECT", "OK"] } } },
            "contains": {
              "properties": { "quality": { "const": "NOT_AVAILABLE" } },
              "required": ["quality"]
            }
          }
        }
      }
    },
    {
      "if": { "properties": { "quality": { "const": "AMBIGUOUS" } }, "required": ["quality"] },
      "then": {
        "required": ["ambiguityReason"]
      },
      "else": {
        "not": { "required": ["ambiguityReason"] }
      }
    },
    {
      "if": {
        "properties": { "ambiguityReason": { "const": "ACTION_WINDOW_OVERLAP" } },
        "required": ["ambiguityReason"]
      },
      "then": {
        "properties": { "phase": { "const": "OVERLAPS_ACTION" } }
      }
    }
  ],
  "$defs": {
    "UtcDateTime": {
      "type": "string",
      "format": "date-time",
      "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]{1,9})?Z$"
    },
    "PlmnId": {
      "type": "object",
      "additionalProperties": false,
      "required": ["mcc", "mnc"],
      "properties": {
        "mcc": { "type": "string", "pattern": "^[0-9]{3}$" },
        "mnc": { "type": "string", "pattern": "^[0-9]{2,3}$" }
      }
    },
    "GuAmI": {
      "type": "object",
      "additionalProperties": false,
      "required": ["plmnId", "amfRegionId", "amfSetId", "amfPointer"],
      "properties": {
        "plmnId": { "$ref": "#/$defs/PlmnId" },
        "amfRegionId": { "type": "string", "pattern": "^[A-Fa-f0-9]{2}$" },
        "amfSetId": { "type": "string", "pattern": "^[0-3][A-Fa-f0-9]{2}$" },
        "amfPointer": { "type": "string", "pattern": "^[0-3][A-Fa-f0-9]{1}$" }
      }
    },
    "GuAmfUeNgapId": {
      "type": "object",
      "additionalProperties": false,
      "required": ["guAmI", "amfUeNgapId"],
      "properties": {
        "guAmI": { "$ref": "#/$defs/GuAmI" },
        "amfUeNgapId": {
          "type": "integer",
          "minimum": 0,
          "maximum": 1099511627775
        }
      }
    },
    "UeId": {
      "type": "object",
      "additionalProperties": false,
      "required": ["guAmfUeNgapId"],
      "properties": {
        "guAmfUeNgapId": { "$ref": "#/$defs/GuAmfUeNgapId" }
      }
    },
    "PolicyScope": {
      "type": "object",
      "additionalProperties": false,
      "required": ["ueId"],
      "properties": {
        "ueId": { "$ref": "#/$defs/UeId" }
      }
    },
    "CId": {
      "type": "object",
      "additionalProperties": false,
      "required": ["ncI"],
      "properties": {
        "ncI": { "type": "integer", "minimum": 0, "maximum": 68719476735 }
      }
    },
    "CellId": {
      "type": "object",
      "additionalProperties": false,
      "required": ["plmnId", "cId"],
      "properties": {
        "plmnId": { "$ref": "#/$defs/PlmnId" },
        "cId": { "$ref": "#/$defs/CId" }
      }
    },
    "MeasurementScope": {
      "type": "object",
      "additionalProperties": false,
      "required": ["managedObjectClass", "managedObjectDn", "cellId"],
      "properties": {
        "managedObjectClass": { "const": "NRCellDU" },
        "managedObjectDn": { "type": "string", "minLength": 1, "maxLength": 1024 },
        "cellId": { "$ref": "#/$defs/CellId" }
      }
    },
    "Window": {
      "type": "object",
      "additionalProperties": false,
      "required": ["start", "end"],
      "properties": {
        "start": { "$ref": "#/$defs/UtcDateTime" },
        "end": { "$ref": "#/$defs/UtcDateTime" }
      }
    },
    "Source": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "interface",
        "managementService",
        "managedFunction",
        "profileId",
        "specification",
        "collectionMode",
        "perfMetricJobId",
        "file",
        "pmRecord"
      ],
      "properties": {
        "interface": { "const": "O1" },
        "managementService": { "const": "PerformanceAssurance" },
        "managedFunction": { "const": "O-DU" },
        "profileId": { "const": "oran-aic-o1-pa-file/1.0.0" },
        "specification": { "const": "ETSI_TS_128_552_V18.11.0" },
        "collectionMode": { "const": "FILE" },
        "perfMetricJobId": {
          "type": "string",
          "minLength": 1,
          "maxLength": 128,
          "pattern": "^[A-Za-z0-9._:/-]+$"
        },
        "file": { "$ref": "#/$defs/PmFile" },
        "pmRecord": { "$ref": "#/$defs/PmRecord" }
      }
    },
    "PmFile": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "sha256", "readyAt", "retrievedAt"],
      "properties": {
        "name": { "type": "string", "minLength": 1, "maxLength": 255 },
        "sha256": { "type": "string", "pattern": "^[a-f0-9]{64}$" },
        "readyAt": { "$ref": "#/$defs/UtcDateTime" },
        "retrievedAt": { "$ref": "#/$defs/UtcDateTime" }
      }
    },
    "PmRecord": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "measuredEntityDn",
        "measInfoId",
        "measObjLdn"
      ],
      "properties": {
        "measuredEntityDn": { "type": "string", "minLength": 1, "maxLength": 1024 },
        "measInfoId": { "type": "string", "minLength": 1, "maxLength": 255 },
        "measObjLdn": { "type": "string", "minLength": 1, "maxLength": 1024 }
      }
    },
    "Sample": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "name",
        "standard",
        "clause",
        "valueKind",
        "measTypeIndex",
        "suspect",
        "unit",
        "samplingPeriodMs",
        "aggregation",
        "measurementAgeMs",
        "ingestLatencyMs",
        "quality"
      ],
      "properties": {
        "name": { "type": "string", "enum": ["RRU.PrbDl", "DRB.UEThpDl"] },
        "value": { "type": ["number", "null"] },
        "standard": { "const": "3GPP_TS_28.552_V18.11.0" },
        "clause": { "type": "string", "enum": ["5.1.1.2.1", "5.1.1.3.1"] },
        "valueKind": { "type": "string", "enum": ["INTEGER", "REAL"] },
        "measTypeIndex": { "type": "integer", "minimum": 1 },
        "suspect": { "type": "boolean" },
        "unit": { "type": "string", "enum": ["percent", "kbit/s"] },
        "samplingPeriodMs": { "const": 60000 },
        "aggregation": {
          "type": "string",
          "enum": ["PERIOD_MEAN", "DERIVED_RATIO"]
        },
        "measurementAgeMs": { "type": "integer", "minimum": 0 },
        "ingestLatencyMs": { "type": "integer", "minimum": 0 },
        "quality": {
          "type": "string",
          "enum": ["OK", "SUSPECT", "STALE", "MISSING", "NOT_AVAILABLE", "AMBIGUOUS"]
        }
      },
      "allOf": [
        {
          "if": {
            "properties": { "suspect": { "const": true } },
            "required": ["suspect"]
          },
          "then": { "properties": { "quality": { "const": "SUSPECT" } } },
          "else": { "properties": { "quality": { "not": { "const": "SUSPECT" } } } }
        },
        {
          "if": {
            "properties": { "quality": { "enum": ["MISSING", "NOT_AVAILABLE"] } },
            "required": ["quality"]
          },
          "then": {
            "required": ["value"],
            "properties": { "value": { "type": "null" } }
          },
          "else": {
            "required": ["value"],
            "properties": { "value": { "type": "number" } }
          }
        },
        {
          "if": {
            "properties": { "name": { "const": "RRU.PrbDl" } },
            "required": ["name"]
          },
          "then": {
            "properties": {
              "clause": { "const": "5.1.1.2.1" },
              "valueKind": { "const": "INTEGER" },
              "unit": { "const": "percent" },
              "aggregation": { "const": "PERIOD_MEAN" },
              "value": { "type": ["integer", "null"], "minimum": 0, "maximum": 100 }
            }
          },
          "else": {
            "properties": {
              "clause": { "const": "5.1.1.3.1" },
              "valueKind": { "const": "REAL" },
              "unit": { "const": "kbit/s" },
              "aggregation": { "const": "DERIVED_RATIO" },
              "value": { "type": ["number", "null"], "minimum": 0 }
            }
          }
        },
        {
          "if": {
            "properties": { "quality": { "const": "OK" } },
            "required": ["quality"]
          },
          "then": {
            "properties": { "measurementAgeMs": { "maximum": 120000 } }
          }
        }
      ]
    },
    "Correlation": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "policyTypeId",
        "policyId",
        "policyRevision"
      ],
      "properties": {
        "policyTypeId": { "const": "AIC_UECellSteering_1.0.0" },
        "policyId": { "type": "string", "minLength": 1, "maxLength": 255 },
        "policyRevision": { "type": "integer", "minimum": 1 },
        "episodeId": { "type": "string", "format": "uuid" },
        "transactionId": { "type": "string", "format": "uuid" },
        "actionId": { "type": "string", "format": "uuid" }
      },
      "dependentRequired": {
        "transactionId": ["episodeId", "actionId"],
        "actionId": ["episodeId", "transactionId"]
      }
    }
  }
}
```

### A.4 `aic.ran-capability.1.0.0.schema.json`

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:oran-aic:schema:ran-capability:1.0.0",
  "title": "AIC RAN Capability DME Record",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "dmeTypeId",
    "contractProfile",
    "nearRtRicId",
    "manifestId",
    "effectiveAt",
    "sourceRevision",
    "authority",
    "policyTypes",
    "objectives",
    "controlAxes",
    "ueIdFormats",
    "cellIdFormats",
    "topology",
    "decisionKpis",
    "assuranceKpis",
    "softwareProvenance",
    "e2Deployment",
    "serviceModels",
    "limits",
    "timing",
    "hardwareProfileId",
    "o1PerformanceProfileId",
    "schemaDigests"
  ],
  "properties": {
    "dmeTypeId": { "const": "aic:ran-capability:1.0.0" },
    "contractProfile": { "const": "oran-aic/1.0.0" },
    "nearRtRicId": {
      "type": "string",
      "minLength": 1,
      "maxLength": 255,
      "pattern": "^[A-Za-z0-9._:/-]+$"
    },
    "manifestId": { "type": "string", "format": "uuid" },
    "effectiveAt": { "$ref": "#/$defs/UtcDateTime" },
    "sourceRevision": {
      "type": "string",
      "minLength": 7,
      "maxLength": 128,
      "pattern": "^[A-Za-z0-9._:/-]+$"
    },
    "authority": { "$ref": "#/$defs/Authority" },
    "policyTypes": {
      "const": ["AIC_UECellSteering_1.0.0"]
    },
    "objectives": {
      "type": "array",
      "minItems": 1,
      "uniqueItems": true,
      "items": { "type": "string", "enum": ["BALANCE_PRB_LOAD", "PIN_TO_CELL"] }
    },
    "controlAxes": { "const": ["serving_cell"] },
    "ueIdFormats": { "const": ["guAmfUeNgapId"] },
    "cellIdFormats": { "const": ["plmnId+cId.ncI"] },
    "topology": { "$ref": "#/$defs/Topology" },
    "decisionKpis": {
      "type": "array",
      "minItems": 1,
      "items": { "$ref": "#/$defs/DecisionKpiCapability" }
    },
    "assuranceKpis": {
      "type": "array",
      "minItems": 1,
      "items": { "$ref": "#/$defs/AssuranceKpiCapability" },
      "contains": {
        "properties": { "name": { "const": "RRU.PrbDl" }, "required": { "const": true } },
        "required": ["name", "required"]
      },
      "minContains": 1,
      "maxContains": 1
    },
    "softwareProvenance": { "$ref": "#/$defs/SoftwareProvenance" },
    "e2Deployment": { "$ref": "#/$defs/E2Deployment" },
    "serviceModels": { "$ref": "#/$defs/ServiceModels" },
    "limits": { "$ref": "#/$defs/Limits" },
    "timing": { "$ref": "#/$defs/Timing" },
    "hardwareProfileId": {
      "type": "string",
      "minLength": 1,
      "maxLength": 128
    },
    "o1PerformanceProfileId": { "const": "oran-aic-o1-pa-file/1.0.0" },
    "schemaDigests": { "$ref": "#/$defs/SchemaDigests" }
  },
  "x-oran-aic-semanticConstraints": [
    {
      "id": "CANONICAL_GLOBAL_E2_NODE_ID_ENCODING",
      "assertion": "every topology.cells[].globalE2NodeId and e2Deployment.nodes[].globalE2NodeId BitStringIdentity.hex is lowercase 0x followed by exactly ceil(bitLength/4) hexadecimal digits, unused high-order padding bits are zero, and plmn.mnc character length equals plmn.mncDigitLength; identity equality is byte equality of RFC8785 canonical JSON",
      "failureMode": "CAPABILITY_REJECT_AND_ZERO_WRITE"
    },
    {
      "id": "STATIC_E2_NODE_IDENTITY_BIJECTION",
      "assertion": "the set of RFC8785-canonical topology.cells[].globalE2NodeId values equals the set of RFC8785-canonical e2Deployment.nodes[].globalE2NodeId values; each topology cell resolves to exactly one deployment node and each deployment node resolves to at least one topology cell",
      "failureMode": "CAPABILITY_REJECT_AND_ZERO_WRITE"
    }
  ],
  "$defs": {
    "UtcDateTime": {
      "type": "string",
      "format": "date-time",
      "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]{1,9})?Z$"
    },
    "Authority": {
      "type": "object",
      "additionalProperties": false,
      "required": ["kind", "producerId"],
      "properties": {
        "kind": { "const": "DIGEST_PINNED_RELEASE_ARTIFACT" },
        "producerId": {
          "type": "string",
          "minLength": 1,
          "maxLength": 128,
          "pattern": "^[A-Za-z0-9._:/-]+$"
        }
      }
    },
    "Plmn": {
      "type": "object",
      "additionalProperties": false,
      "required": ["mcc", "mnc", "mncDigitLength"],
      "properties": {
        "mcc": { "type": "string", "pattern": "^[0-9]{3}$" },
        "mnc": { "type": "string", "pattern": "^[0-9]{2,3}$" },
        "mncDigitLength": { "type": "integer", "enum": [2, 3] }
      },
      "allOf": [
        {
          "if": { "properties": { "mncDigitLength": { "const": 2 } }, "required": ["mncDigitLength"] },
          "then": { "properties": { "mnc": { "pattern": "^[0-9]{2}$" } } },
          "else": { "properties": { "mnc": { "pattern": "^[0-9]{3}$" } } }
        }
      ]
    },
    "BitStringIdentity": {
      "type": "object",
      "additionalProperties": false,
      "required": ["hex", "bitLength"],
      "properties": {
        "hex": { "type": "string", "pattern": "^0x[0-9a-f]+$" },
        "bitLength": { "type": "integer", "minimum": 1, "maximum": 64 }
      }
    },
    "GlobalE2NodeId": {
      "type": "object",
      "additionalProperties": false,
      "required": ["nodeType", "plmn", "nodeId"],
      "properties": {
        "nodeType": { "type": "string", "enum": ["GNB", "EN_GNB", "NG_ENB", "ENB"] },
        "plmn": { "$ref": "#/$defs/Plmn" },
        "nodeId": { "$ref": "#/$defs/BitStringIdentity" },
        "cuDuComponentId": { "$ref": "#/$defs/BitStringIdentity" }
      }
    },
    "PlmnId": {
      "type": "object",
      "additionalProperties": false,
      "required": ["mcc", "mnc"],
      "properties": {
        "mcc": { "type": "string", "pattern": "^[0-9]{3}$" },
        "mnc": { "type": "string", "pattern": "^[0-9]{2,3}$" }
      }
    },
    "CId": {
      "type": "object",
      "additionalProperties": false,
      "required": ["ncI"],
      "properties": {
        "ncI": {
          "type": "integer",
          "minimum": 0,
          "maximum": 68719476735
        }
      }
    },
    "CellId": {
      "type": "object",
      "additionalProperties": false,
      "required": ["plmnId", "cId"],
      "properties": {
        "plmnId": { "$ref": "#/$defs/PlmnId" },
        "cId": { "$ref": "#/$defs/CId" }
      }
    },
    "CellEntry": {
      "type": "object",
      "additionalProperties": false,
      "required": ["cellId", "managedObjectDn", "globalE2NodeId"],
      "properties": {
        "cellId": { "$ref": "#/$defs/CellId" },
        "managedObjectDn": { "type": "string", "minLength": 1, "maxLength": 1024 },
        "globalE2NodeId": { "$ref": "#/$defs/GlobalE2NodeId" }
      }
    },
    "Neighbour": {
      "type": "object",
      "additionalProperties": false,
      "required": ["sourceCell", "targetCell"],
      "properties": {
        "sourceCell": { "$ref": "#/$defs/CellId" },
        "targetCell": { "$ref": "#/$defs/CellId" }
      }
    },
    "Topology": {
      "type": "object",
      "additionalProperties": false,
      "required": ["cells", "neighbours"],
      "properties": {
        "cells": {
          "type": "array",
          "minItems": 1,
          "uniqueItems": true,
          "items": { "$ref": "#/$defs/CellEntry" }
        },
        "neighbours": {
          "type": "array",
          "uniqueItems": true,
          "items": { "$ref": "#/$defs/Neighbour" }
        }
      }
    },
    "DecisionKpiCapability": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "name",
        "unit",
        "samplingPeriodMs",
        "sourceInterface",
        "serviceModel",
        "measurementDefinition",
        "requiredFor"
      ],
      "properties": {
        "name": { "type": "string", "minLength": 1, "maxLength": 128 },
        "unit": { "type": "string", "minLength": 1, "maxLength": 32 },
        "samplingPeriodMs": { "type": "integer", "minimum": 1 },
        "sourceInterface": { "const": "E2" },
        "serviceModel": { "const": "E2SM-KPM" },
        "measurementDefinition": {
          "type": "string",
          "minLength": 1,
          "maxLength": 128
        },
        "requiredFor": {
          "type": "array",
          "minItems": 1,
          "uniqueItems": true,
          "items": {
            "type": "string",
            "enum": ["BALANCE_PRB_LOAD", "PIN_TO_CELL", "READBACK"]
          }
        }
      }
    },
    "AssuranceKpiCapability": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "name",
        "unit",
        "samplingPeriodMs",
        "sourceInterface",
        "measurementDefinition",
        "managedObjectClass",
        "required"
      ],
      "properties": {
        "name": { "type": "string", "enum": ["RRU.PrbDl", "DRB.UEThpDl"] },
        "unit": { "type": "string", "enum": ["percent", "kbit/s"] },
        "samplingPeriodMs": { "const": 60000 },
        "sourceInterface": { "const": "O1" },
        "measurementDefinition": { "const": "3GPP_TS_28.552_V18.11.0" },
        "managedObjectClass": { "const": "NRCellDU" },
        "required": { "type": "boolean" }
      },
      "allOf": [
        {
          "if": {
            "properties": { "name": { "const": "RRU.PrbDl" } },
            "required": ["name"]
          },
          "then": {
            "properties": { "unit": { "const": "percent" }, "required": { "const": true } }
          },
          "else": {
            "properties": { "unit": { "const": "kbit/s" }, "required": { "const": false } }
          }
        }
      ]
    },
    "DigestPinnedArtifact": {
      "type": "object",
      "additionalProperties": false,
      "required": ["path", "byteSha256"],
      "properties": {
        "path": { "type": "string", "minLength": 1, "maxLength": 1024 },
        "byteSha256": { "type": "string", "pattern": "^[a-f0-9]{64}$" }
      }
    },
    "SoftwareProvenance": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "flexRicCommitSha1",
        "oaiBaseTag",
        "oaiBaseCommitSha1",
        "postPatchSourceTreeSha1",
        "orderedPatchSet",
        "buildArtifactManifestSha256",
        "authoritativeRcArtifactSha256",
        "rcProfileArtifact",
        "aperVectorManifest",
        "otaEvidenceManifest"
      ],
      "properties": {
        "flexRicCommitSha1": { "const": "ef6d722f22191eea74089966983da1f5ec1fedd4" },
        "oaiBaseTag": { "const": "2026.w30" },
        "oaiBaseCommitSha1": { "const": "42bf80e9b25dbf521cc692fa6338cbbbebfbcd1d" },
        "postPatchSourceTreeSha1": { "type": "string", "pattern": "^[a-f0-9]{40}$" },
        "orderedPatchSet": {
          "type": "array",
          "minItems": 1,
          "uniqueItems": true,
          "items": { "$ref": "#/$defs/DigestPinnedArtifact" }
        },
        "buildArtifactManifestSha256": { "type": "string", "pattern": "^[a-f0-9]{64}$" },
        "authoritativeRcArtifactSha256": { "const": "f83f996d2d8e635ab5a2c699e6b3cea2657733ac72bebd53cf470e1c7d4e1fa0" },
        "rcProfileArtifact": { "$ref": "#/$defs/DigestPinnedArtifact" },
        "aperVectorManifest": { "$ref": "#/$defs/DigestPinnedArtifact" },
        "otaEvidenceManifest": { "$ref": "#/$defs/DigestPinnedArtifact" }
      }
    },
    "RanFunctionBinding": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "serviceModel",
        "ranFunctionId",
        "ranFunctionOid",
        "observedRanFunctionRevision",
        "rawDefinitionSha256",
        "canonicalDecodedDefinitionSha256"
      ],
      "properties": {
        "serviceModel": { "type": "string", "enum": ["E2SM-KPM", "E2SM-RC"] },
        "ranFunctionId": { "type": "integer", "minimum": 1, "maximum": 4095 },
        "ranFunctionOid": { "type": "string", "pattern": "^[0-9]+(\\.[0-9]+)+$" },
        "observedRanFunctionRevision": { "type": "integer", "minimum": 0 },
        "rawDefinitionSha256": { "type": "string", "pattern": "^[a-f0-9]{64}$" },
        "canonicalDecodedDefinitionSha256": { "type": "string", "pattern": "^[a-f0-9]{64}$" }
      },
      "allOf": [
        {
          "if": { "properties": { "serviceModel": { "const": "E2SM-KPM" } }, "required": ["serviceModel"] },
          "then": { "properties": { "ranFunctionId": { "const": 2 } } },
          "else": { "properties": { "ranFunctionId": { "const": 3 } } }
        }
      ]
    },
    "E2NodeBinding": {
      "type": "object",
      "additionalProperties": false,
      "required": ["globalE2NodeId", "role", "requiredRanFunctions"],
      "properties": {
        "globalE2NodeId": { "$ref": "#/$defs/GlobalE2NodeId" },
        "role": { "type": "string", "enum": ["SOURCE_AND_ROLLBACK_TARGET", "TARGET_AND_ROLLBACK_SOURCE"] },
        "requiredRanFunctions": {
          "type": "array",
          "minItems": 2,
          "maxItems": 2,
          "items": { "$ref": "#/$defs/RanFunctionBinding" },
          "allOf": [
            { "contains": { "properties": { "serviceModel": { "const": "E2SM-KPM" } }, "required": ["serviceModel"] }, "minContains": 1, "maxContains": 1 },
            { "contains": { "properties": { "serviceModel": { "const": "E2SM-RC" } }, "required": ["serviceModel"] }, "minContains": 1, "maxContains": 1 }
          ]
        }
      }
    },
    "E2Deployment": {
      "type": "object",
      "additionalProperties": false,
      "required": ["e2apVersion", "nodes", "runtimeBindingRule", "ackSuccessSemantics"],
      "properties": {
        "e2apVersion": { "const": "2.03" },
        "nodes": {
          "type": "array",
          "minItems": 2,
          "maxItems": 2,
          "items": { "$ref": "#/$defs/E2NodeBinding" },
          "allOf": [
            { "contains": { "properties": { "role": { "const": "SOURCE_AND_ROLLBACK_TARGET" } }, "required": ["role"] }, "minContains": 1, "maxContains": 1 },
            { "contains": { "properties": { "role": { "const": "TARGET_AND_ROLLBACK_SOURCE" } }, "required": ["role"] }, "minContains": 1, "maxContains": 1 }
          ]
        },
        "runtimeBindingRule": { "const": "E2_SETUP_AND_RIC_SERVICE_UPDATE_EXACT_MATCH_PER_NODE_CONNECTION_EPOCH" },
        "ackSuccessSemantics": { "const": "ACK_IS_DELIVERY_ONLY_APPLIED_VERIFIED_REQUIRES_KPM_READBACK" }
      }
    },
    "ServiceModels": {
      "type": "object",
      "additionalProperties": false,
      "required": ["e2ap", "e2smKpm", "e2smRc"],
      "properties": {
        "e2ap": {
          "type": "object",
          "additionalProperties": false,
          "required": ["version", "buildSelector", "encoding"],
          "properties": {
            "version": { "const": "2.03" },
            "buildSelector": { "const": "E2AP_V2" },
            "encoding": { "const": "APER" }
          }
        },
        "e2smKpm": {
          "type": "object",
          "additionalProperties": false,
          "required": ["oid", "version", "ranFunctionId", "ranFunctionRevision", "buildSelector", "reportStyles"],
          "properties": {
            "oid": { "type": "string", "pattern": "^[0-9]+(\\.[0-9]+)+$" },
            "version": { "const": "2.03" },
            "ranFunctionId": { "const": 2 },
            "ranFunctionRevision": { "const": 2 },
            "buildSelector": { "const": "KPM_V2_03" },
            "reportStyles": { "const": [1, 4] }
          }
        },
        "e2smRc": {
          "type": "object",
          "additionalProperties": false,
          "required": ["oid", "version", "ranFunctionId", "ranFunctionRevision", "encoding", "controlStyles", "ranParameterProfile"],
          "properties": {
            "oid": { "type": "string", "pattern": "^[0-9]+(\\.[0-9]+)+$" },
            "version": { "const": "1.03" },
            "ranFunctionId": { "const": 3 },
            "ranFunctionRevision": { "const": 1 },
            "encoding": { "const": "ASN" },
            "controlStyles": {
              "const": [
                {
                  "styleType": 3,
                  "actionIds": [1],
                  "headerFormat": 1,
                  "messageFormat": 1,
                  "outcomeFormat": 1
                }
              ]
            },
            "ranParameterProfile": {
              "const": {
                "targetPrimaryCellId": 1,
                "choiceTargetCell": 2,
                "nrCell": 3,
                "nrCgi": 4
              }
            }
          }
        }
      }
    },
    "Limits": {
      "type": "object",
      "additionalProperties": false,
      "required": ["maxActivePolicies", "maxPoliciesPerUe"],
      "properties": {
        "maxActivePolicies": { "type": "integer", "minimum": 1 },
        "maxPoliciesPerUe": { "const": 1 }
      }
    },
    "Timing": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "maxKpiFreshnessMs",
        "maxActionDeadlineMs",
        "controlDrainTimeoutMs",
        "recoveryWindowMs",
        "clockSkewToleranceMs",
        "assuranceFreshnessLimitMs"
      ],
      "properties": {
        "maxKpiFreshnessMs": { "type": "integer", "minimum": 1, "maximum": 60000 },
        "maxActionDeadlineMs": { "type": "integer", "minimum": 1, "maximum": 120000 },
        "controlDrainTimeoutMs": { "const": 15000 },
        "recoveryWindowMs": { "const": 30000 },
        "clockSkewToleranceMs": { "const": 250 },
        "assuranceFreshnessLimitMs": { "const": 120000 }
      }
    },
    "SchemaDigests": {
      "type": "object",
      "additionalProperties": false,
      "required": ["policy", "status", "evidence", "o1Profile"],
      "properties": {
        "policy": { "type": "string", "pattern": "^[a-f0-9]{64}$" },
        "status": { "type": "string", "pattern": "^[a-f0-9]{64}$" },
        "evidence": { "type": "string", "pattern": "^[a-f0-9]{64}$" },
        "o1Profile": { "type": "string", "pattern": "^[a-f0-9]{64}$" }
      }
    }
  }
}
```

`topology.neighbours`는 `sourceCell → targetCell`의 directed relation이다. 모든 endpoint는 `topology.cells[].cellId`에 존재해야 하고, `cellId`와 `managedObjectDn`은 각각 중복 없이 일대일이어야 한다. JSON Schema가 표현하지 못하는 이 membership/uniqueness는 runtime와 negative golden fixture에서 강제한다. Static manifest의 새 version은 새로운 `manifestId`와 더 늦은 `effectiveAt`을 사용한다. 동일 `manifestId`의 다른 payload와 out-of-order older manifest는 fail closed한다.

`topology.cells[].globalE2NodeId`는 RFC 8785 canonical value 기준으로 `e2Deployment.nodes[].globalE2NodeId`에 정확히 하나 존재하고 두 role은 서로 다른 node여야 한다. 두 위치와 live inventory의 `connections[].globalE2NodeId`는 같은 `GlobalE2NodeId` schema를 사용하며 display string, association ID 또는 epoch를 identity alias로 사용하지 않는다. 각 node에는 KPM과 RC binding이 정확히 하나씩 있어야 하며 `ranFunctionId`는 node-local이다. 이 JSON Schema가 직접 표현하지 못하는 topology↔deployment set equality와 canonical bit-string 규칙은 `STATIC_E2_NODE_IDENTITY_BIJECTION` 및 `CANONICAL_GLOBAL_E2_NODE_ID_ENCODING` machine semantic assertion이 강제한다. Golden의 fixture digest/path는 schema positive oracle일 뿐 실제 release provenance가 아니다. 실제 하위 release가 `fixture`, `example`, `TBD`, 짧은 commit 또는 존재하지 않는 artifact를 그대로 제출하면 schema가 우연히 통과하더라도 runtime release gate가 거절한다. `backend-release-manifest`와 active E2 inventory의 patch/build/module/profile/definition digest가 모두 일치할 때만 capability를 R1/A1에 publish한다.

### A.5 `aic.policy-evidence-filter.1.0.0.schema.json`

이 schema object는 R1 `DmeTypeDefinition.dataProductionSchema`에 inline으로 등록하고, `DataJobInfo.productionJobDefinition`을 검증한다. `$id`는 schema 내부 identifier일 뿐 R1 wire에 존재하지 않는 별도 `productionSchemaId` field가 아니다. 권위 있는 machine artifact는 `shared-contract-bundle/aic.policy-evidence-filter.1.0.0.schema.json`이며 다음 JSON과 RFC 8785 canonical value가 같아야 한다.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:oran-aic:schema:policy-evidence-filter:1.0.0",
  "title": "AIC Policy Evidence Data Job Filter",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "policyTypeId",
    "policyId",
    "minimumPolicyRevision",
    "nearRtRicId"
  ],
  "properties": {
    "policyTypeId": {
      "const": "AIC_UECellSteering_1.0.0"
    },
    "policyId": {
      "type": "string",
      "minLength": 1,
      "maxLength": 255
    },
    "minimumPolicyRevision": {
      "type": "integer",
      "minimum": 1
    },
    "nearRtRicId": {
      "type": "string",
      "minLength": 1,
      "maxLength": 255,
      "pattern": "^[A-Za-z0-9._:/-]+$"
    }
  }
}
```

## Appendix B. 규범 O1 Performance Assurance profile

권위 있는 machine artifact는 `shared-contract-bundle/oran-aic-o1-pa-file.1.0.0.json`이다. 다음 JSON과 artifact는 RFC 8785 canonical value가 같아야 한다.

```json
{
  "profileId": "oran-aic-o1-pa-file/1.0.0",
  "interfaceSpecification": {
    "id": "ETSI-TS-104-043",
    "version": "11.0.0"
  },
  "genericManagementSpecification": {
    "id": "ETSI-TS-128-532",
    "version": "18.3.0"
  },
  "genericNrmSpecification": {
    "id": "ETSI-TS-128-622",
    "version": "18.7.0"
  },
  "genericNrmYangSpecification": {
    "id": "ETSI-TS-128-623",
    "version": "18.7.0",
    "profileArtifact": "o1-netconf-yang-profile.1.0.0.json"
  },
  "measurementSpecification": {
    "id": "ETSI-TS-128-552",
    "version": "18.11.0"
  },
  "performanceDataFileSpecification": {
    "id": "ETSI-TS-132-435",
    "version": "10.0.0",
    "grammarBranch": "LEGACY_MEASCOLLECFILE_TS_32_432_32_435"
  },
  "delivery": {
    "mode": "FILE",
    "fileReadyFormat": "SDO",
    "notificationType": "notifyFileReady",
    "notificationTransport": "HTTPS",
    "mutualTls": true,
    "retrievalScheme": "SFTP",
    "compression": "NONE",
    "fileEncoding": "XML",
    "fileContentProfile": "ETSI-TS-128-532-11.3.2.1.2",
    "xmlProfile": "ETSI-TS-132-435-V10.0.0-measCollec.xsd",
    "xmlRoot": "measCollecFile",
    "xmlNamespace": "http://www.3gpp.org/ftp/specs/archive/32_series/32.435#measCollec",
    "fileFormat": "32.435 V10.0 XML-schema",
    "xmlStylesheetProcessingInstruction": "<?xml-stylesheet type=\"text/xsl\" href=\"MeasDataCollection.xsl\"?>",
    "subscription": {
      "service": "FileDataReportingMnS",
      "operation": "subscribe",
      "collectionResource": "{MnSRoot}/FileDataReportingMnS/{MnSVersion}/subscriptions",
      "createHttpMethod": "POST",
      "createSuccessStatus": 201,
      "itemResource": "{MnSRoot}/FileDataReportingMnS/{MnSVersion}/subscriptions/{subscriptionId}",
      "deleteHttpMethod": "DELETE",
      "deleteSuccessStatus": 204,
      "deleteAlreadyAbsentStatus": 404,
      "notificationSuccessStatus": 204,
      "filesResource": "{MnSRoot}/FileDataReportingMnS/{MnSVersion}/files",
      "listFilesHttpMethod": "GET",
      "listFilesRequiredQuery": [
        "fileDataType",
        "beginTime",
        "endTime"
      ],
      "listFilesTimeFilterField": "fileReadyTime",
      "beginOffsetFromMeasurementWindowEndMs": 0,
      "endOffsetFromMeasurementWindowEndMs": 120000,
      "listFilesTimeInterval": "BEGIN_INCLUSIVE_END_EXCLUSIVE",
      "rawPmWindowFinalMatchRequired": true,
      "listFilesSuccessStatus": 200,
      "notificationLossRecovery": "LIST_AVAILABLE_FILES_THEN_MISSING",
      "consumerCreatesBeforeJobUnlock": true,
      "getOperationAvailable": false,
      "consumerPersistsSubscriptionId": true,
      "providerRestartBehavior": "PERSIST_OR_DELETE_RETURNS_404",
      "uncertainStateRecovery": "LOCK_JOB_DELETE_STORED_ID_BEFORE_RECREATE",
      "consumerReferenceWireField": "consumerReference",
      "consumerReferenceConfigurationKey": "o1.fileDataReporting.consumerReference",
      "consumerReferenceFixture": "https://smo.example/o1/file-data-reporting/notifications",
      "timeTick": 0,
      "timeTickSemantics": "INFINITE",
      "expectedNotifications": [
        "notifyFileReady",
        "notifyFilePreparationError"
      ]
    }
  },
  "perfMetricJob": {
    "ioc": "PerfMetricJob",
    "configurationProtocol": "NETCONF",
    "jobId": "oran-aic-nrcelldu-60s",
    "administrativeState": "UNLOCKED",
    "performanceMetrics": [
      "RRU.PrbDl",
      "DRB.UEThpDl"
    ],
    "granularityPeriodSeconds": 60,
    "reportingCtrl": {
      "case": "file-based-reporting",
      "fileReportingPeriodMinutes": 1,
      "fileLocationPresence": "ABSENT_PRODUCER_SELECTED"
    }
  },
  "measurements": [
    {
      "name": "RRU.PrbDl",
      "clause": "5.1.1.2.1",
      "managedObjectClass": "NRCellDU",
      "unit": "percent",
      "valueKind": "INTEGER",
      "minimum": 0,
      "maximum": 100,
      "aggregation": "PERIOD_MEAN",
      "required": true
    },
    {
      "name": "DRB.UEThpDl",
      "clause": "5.1.1.3.1",
      "managedObjectClass": "NRCellDU",
      "unit": "kbit/s",
      "valueKind": "REAL",
      "minimum": 0,
      "aggregation": "DERIVED_RATIO",
      "required": false,
      "identitySemantics": "CELL_AGGREGATE_NOT_UNIQUE_UE"
    }
  ],
  "identity": {
    "mappingSource": "backend-capability-manifest.json/topology.cells",
    "managedObjectDnField": "managedObjectDn",
    "cellIdField": "cellId",
    "mappingCardinality": "BIJECTIVE",
    "forbidUeInferenceFromNrCellDuPm": true
  },
  "time": {
    "timestampFormat": "RFC3339_UTC_Z",
    "durationClock": "MONOTONIC",
    "clockSource": "NTP_OR_PTP",
    "maxClockSkewMs": 250,
    "evidenceFreshnessLimitMs": 120000,
    "windowInterval": "HALF_OPEN_START_INCLUSIVE_END_EXCLUSIVE",
    "notificationEventTimeEqualsFileReadyTime": true,
    "fileReadyBeforeExpirationRequired": true,
    "retrievalInterval": "fileReadyTime <= retrievedAt < fileExpirationTime"
  },
  "normalization": {
    "dmeTypeId": "aic:policy-evidence:1.0.0",
    "missingValueEncoding": "NULL_WITH_QUALITY",
    "rawFileSha256Required": true,
    "positionalMeasTypeMappingRequired": true,
    "duplicateKey": "fileSha256+perfMetricJobId+managedObjectDn+window+measurementName"
  }
}
```

다음 runtime invariant를 추가로 강제한다.

- `fileReportingPeriodMinutes * 60 % granularityPeriodSeconds == 0`이다. Profile의 `fileLocationPresence=ABSENT_PRODUCER_SELECTED`는 NETCONF/YANG payload에서 `fileLocation` leaf를 실제로 생략한다는 뜻이다.
- `o1-netconf-yang-profile.1.0.0.json`의 schema-mount, module revision, namespace, exact RPC fixture와 lifecycle을 모두 통과해야 하며, generic NETCONF 성공만으로 profile 적합성을 주장하지 않는다.
- Pinned notification profile은 `eventTime == fileReadyTime`, `fileReadyTime < fileExpirationTime`, `fileReadyTime <= retrievedAt < fileExpirationTime`을 강제한다. 하나라도 위반하면 SFTP retrieval과 DME publish를 수행하지 않는다.
- `/files`의 `beginTime`은 `fileReadyTime >= beginTime`, `endTime`은 `fileReadyTime < endTime`을 뜻한다. 본 profile은 expected PM window end부터 120초 뒤까지를 ready-time 검색 범위로 사용하고, HTTP `200` body는 wrapper가 아닌 raw `array(FileInfo)`다. 검색 결과를 file metadata만으로 최종 채택하지 않고 SFTP로 회수한 raw PM의 DN·measurement window가 원래 기대값과 일치하는지 확인한다.
- notification의 `href`는 callback 주소가 아니라 file을 만든 `PerfMetricJob` managed-object URI이고, `systemDN`은 notification을 emit한 MnS Agent의 DN이다. `jobId`, 대상 DN, measurement, period가 profile/static manifest와 다르면 파일을 격리하고 evidence를 publish하지 않는다.
- profile의 `compression=NONE`은 SDO `FileInfo.fileCompression`의 빈 문자열과 대응한다. 다른 값은 실제 압축 알고리즘 이름이어야 하며 해당 알고리즘으로 안전하게 해제하지 못하면 파일을 격리한다.
- Canonical PM file은 두 `NRCellDU` measured object와 한 granularity period를 포함하므로 ETSI TS 128 532 naming convention의 type `B`를 사용한다. Producer가 실제 content cardinality와 맞지 않는 `A/B/C/D` type을 보고하면 conformance 실패다.
- PM local DN은 raw `measuredEntityDn`과 `measObjLdn`을 보존하면서 comma 하나로 결합한다. `NRCellDU=<RDN value>` 자체를 NCI로 해석하지 않는다.
- duplicate notification/file replay는 file byte SHA-256과 normalization duplicate key로 제거한다.

## Appendix C. 규범 shared golden bundle

권위 있는 범위는 `bundle-manifest.1.0.1.json`의 `files[]`에 기록된 **모든 파일**이다. 일부 파일만 복사하거나 아래 group 중 하나를 생략한 handoff는 무효다.

```text
shared-contract-bundle/*.schema.json
shared-contract-bundle/oran-aic-o1-pa-file.1.0.0.json
shared-contract-bundle/o1-netconf-yang-profile.1.0.0.json
shared-contract-bundle/scenario-catalog.1.0.1.json
shared-contract-bundle/scenario-runner-contract.1.0.1.json
shared-contract-bundle/execution-profile-assignment.1.0.1.json
shared-contract-bundle/execution-profile-assignment.1.0.1.schema.json
shared-contract-bundle/golden/golden-vectors.1.0.0.json
shared-contract-bundle/golden/o1/*.{json,xml}
shared-contract-bundle/golden/o1/netconf/*.xml
shared-contract-bundle/bundle-manifest.1.0.1.json
```

`golden-vectors.1.0.0.json`의 `canonicalObjects`가 policy/status/evidence/capability positive fixture이며 `scenarioExpectations`가 compact oracle이다. 모든 필수 실행 recipe와 expectation은 `scenario-catalog.1.0.1.json`이 완성하고, interpolation·output binding·fault·initial-state·SKIPPED semantics는 `scenario-runner-contract.1.0.1.json`이 고정한다. 각 scenario를 어떤 환경에서 실행하는지는 §17.3의 `execution-profile-assignment.1.0.1.json`이 고정한다. Live case는 `deployment-test-vector.1.0.0.schema.json`의 실제 deployment object에서만 materialize한다. Fixed-fixture harness는 notification의 `fileLocation`을 조회했을 때 로컬 alias `valid-prb.xml`의 정확한 bytes를 반환해야 하며, normalized evidence의 `source.file.name`은 alias가 아니라 `fileLocation`의 canonical basename을 보존한다. 이 exact-byte 규칙을 실제 Managed Element의 동적 output에 적용하지 않는다.

Canonical `notifyFileReady` fixture는 다음 JSON과 같은 value이며 `fileSize`는 아래 raw XML의 정확한 byte 수다.

```json
{
  "href": "https://oai-gnb.example/o1/managed-objects/SubNetwork=oran-lab,ManagedElement=oai-gnb,PerfMetricJob=oran-aic-nrcelldu-60s",
  "notificationId": 1,
  "notificationType": "notifyFileReady",
  "eventTime": "2026-08-04T00:02:02Z",
  "systemDN": "SubNetwork=oran-lab,ManagedElement=oai-gnb,MnsAgent=o1-agent",
  "fileInfoList": [
    {
      "fileLocation": "sftp://oai-gnb.example/pm/B20260804.0001+0000-0002+0000_-oran-aic-nrcelldu-60s_oai-gnb.xml",
      "fileSize": 1297,
      "fileReadyTime": "2026-08-04T00:02:02Z",
      "fileExpirationTime": "2026-08-05T00:02:02Z",
      "fileCompression": "",
      "fileFormat": "32.435 V10.0 XML-schema",
      "fileDataType": "PERFORMANCE",
      "jobId": "oran-aic-nrcelldu-60s"
    }
  ]
}
```

Canonical raw O1 fixture는 다음 XML과 byte-for-byte 같다.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<?xml-stylesheet type="text/xsl" href="MeasDataCollection.xsl"?>
<measCollecFile xmlns="http://www.3gpp.org/ftp/specs/archive/32_series/32.435#measCollec">
  <fileHeader fileFormatVersion="32.435 V10.0" vendorName="OAI">
    <fileSender localDn="SubNetwork=oran-lab,ManagedElement=oai-gnb" elementType="O-DU"/>
    <measCollec beginTime="2026-08-04T00:01:00Z"/>
  </fileHeader>
  <measData>
    <managedElement localDn="SubNetwork=oran-lab,ManagedElement=oai-gnb" userLabel="oran-aic-oai-gnb" swVersion="fixture-1.0.0"/>
    <measInfo measInfoId="oran-aic-pm">
      <job jobId="oran-aic-nrcelldu-60s"/>
      <granPeriod duration="PT60S" endTime="2026-08-04T00:02:00Z"/>
      <repPeriod duration="PT60S"/>
      <measType p="1">RRU.PrbDl</measType>
      <measType p="2">DRB.UEThpDl</measType>
      <measValue measObjLdn="GNBDUFunction=oai-du,NRCellDU=1">
        <r p="1">41</r>
        <r p="2">4310.2</r>
        <suspect>false</suspect>
      </measValue>
      <measValue measObjLdn="GNBDUFunction=oai-du,NRCellDU=2">
        <r p="1">67</r>
        <r p="2">2980.5</r>
        <suspect>false</suspect>
      </measValue>
    </measInfo>
  </measData>
  <fileFooter>
    <measCollec endTime="2026-08-04T00:02:00Z"/>
  </fileFooter>
</measCollecFile>
```

Bundle manifest는 자기 자신을 제외한 bundle의 모든 regular file을 relative path 기준 lexicographic order로 열거하고 각 file의 byte count와 **byte SHA-256**을 기록한다. Bundle digest는 이 manifest object를 RFC 8785 canonicalize한 SHA-256이다. ZIP/TAR metadata, file mtime, directory order는 digest 대상이 아니다.

`oran-aic/1.0.0`의 고정 bundle-manifest JCS SHA-256은 `6f9908ca9cee29ca5fa7b629f4daa0f502b5b8ce244519a76b9c88c6c1710ce3`이며 frozen baseline의 값으로 보존한다. `oran-aic/1.0.1`의 고정 bundle-manifest JCS SHA-256은 실제 `bundle-manifest.1.0.1.json`을 RFC 8785 canonicalize해 계산한 `6d18be509950a17efa136971af788b607f0d67778b2a83d9f4e8f22c470867ed`다.
