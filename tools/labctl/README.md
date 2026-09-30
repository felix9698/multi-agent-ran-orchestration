# labctl — 실험 장비 사전 준비 도구

`labctl`은 O-RAN Operator GUI와 분리된 Ubuntu 운영 도구입니다. 5G Core, Near-RT RIC,
두 gNB, 두 UE의 상태 확인·순차 기동·소유권 기반 종료를 담당합니다. GUI나 rApp에서
이 도구를 호출하지 않습니다.

## 현재 장비 프로파일

| 요소 | 접속 위치 | 기본 역할 |
|---|---|---|
| PC1 | local (`ran-node1`, 192.168.0.50) | 5G Core, Near-RT RIC, gNB1 |
| PC2 | `ran-node2@192.168.0.51` | gNB2 |
| UE1 | `ran-ue1@192.168.0.52` | UE1 |
| UE2 | `ran-ue2@192.168.0.53` | UE2 |

현재 목적은 `PIN_TO_CELL`, 24-PRB dual-cell입니다. 장비 수는 JSON 인벤토리로
결정되므로 UE3 도입 시 Python 코드를 고치지 않고 host/component 항목만 추가합니다.

## 최초 1회 준비

- PC1에서 PC2·UE1·UE2로 공개키 SSH가 동작해야 합니다.
- 원격 `sudo -n` helper가 사전에 설치·승인돼 있어야 합니다.
- 이 저장소나 프로파일에 SSH 비밀번호, token, private key 값을 넣지 마십시오.
- 모든 명령은 저장소 루트에서 실행하거나 `bin/labctl`을 사용하십시오.

## 안전한 사용 순서

1. 모든 장비의 전원·케이블·안테나·감쇠기 상태를 사람이 확인합니다.
2. 아직 gNB USRP 전원은 켜지 않은 상태에서 읽기 전용 점검을 실행합니다.

   ```bash
   bin/labctl status
   bin/labctl preflight
   ```

3. 실제로 어떤 명령이 예정되는지만 확인합니다. 아래 명령은 시작·종료 명령을
   실행하지 않습니다.

   ```bash
   bin/labctl prepare
   ```

4. 실험자가 gNB1·gNB2 USRP 전원을 켜고 RF 연결이 안전함을 직접 확인합니다.
5. 정적 config를 배포하지 않고 repository에서 stage/validate한 뒤, 간단한 `YES` 확인으로 준비를 실행합니다.

   ```bash
   bin/labctl apply-config --execute --yes
   bin/labctl prepare --execute --yes
   ```

6. JSON 결과가 `COMPLETED`인지 확인한 뒤 O-RAN GUI의 Live binding/preflight를
   진행합니다. `labctl`은 GUI의 Live profile이나 intent를 대신 설정하지 않습니다.

## 종료

기본 종료는 가장 최근 성공 실행에서 `labctl`이 직접 시작한 요소만 역순으로
종료하며 5G Core는 유지합니다.

```bash
bin/labctl stop
bin/labctl stop --execute --yes
```

5G Core까지 내리는 것은 의도적으로 별도 선택입니다.

```bash
bin/labctl stop --execute --yes --include-core
```

`SIGKILL`은 자동 사용하지 않습니다. 정상 종료가 제한 시간 안에 완료되지 않으면
도구는 중단하고 수동 조치 필요 상태를 남깁니다.

## 증거와 문제 확인

각 실행은 profile의 `stateRoot/<run-id>/` 아래에 mode 0600 로그와
원자적으로 기록한 `run.json`을 남깁니다. 최신 위치는 다음으로 확인합니다.

```bash
bin/labctl logs
```

## Readiness receipt

`labctl receipt --output <path>` writes a secret-free, unsigned Lab Setup
readiness receipt. Tests inject probes; a real status probe requires the
explicit `--live-probes` option. The receipt is not a candidate, objective
effect, or OTA evidence, and a Cockpit reader may only display it.

`cleanup` and `recover` both return only the components owned by the latest
run to their stopped safe state. Recovery deliberately does not restart RF
equipment; the operator must review the report and start a new preparation.

USRP와 network의 실제 관측은 live probe에서만 `READY`가 될 수 있습니다. 이 배포물의
hardware-free 기본 script는 점검이 없음을 `UNKNOWN`으로 명시하며, 환경변수 설정만으로
ready를 주장하지 않습니다.

이미 떠 있던 프로세스는 `PREEXISTING`으로 기록되며 기본 종료·실패 롤백 대상이
아닙니다. 실행 도중 실패하면 이번 실행이 시작한 비-Core 요소만 역순으로 종료합니다.

## 향후 A1-P/O1 및 UE3 추가

확정된 최종 통합 endpoint는 별도 overlay에 probe-only component로 추가할 수 있습니다.
실제 endpoint나 `secretRef`가 확정되기 전에 placeholder를 만들지 마십시오. UE3는 새
SSH host와 UE component를 추가하고 의존성·helper 경로를 명시합니다. 실제 암호나 key
material은 어떤 overlay에도 포함하지 않습니다.
