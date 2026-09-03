# Orchestra Surgical Display API 계약

기준 파일은 [`contract/openapi.yaml`](../contract/openapi.yaml)입니다. 이 문서는 로봇 개발자가 자주 확인하는 요청 형식과 운영 원칙을 요약합니다.

## 결론

첫 연동에서는 `POST /api/v1/state`를 사용합니다. 필수값은 `state` 하나입니다.

```json
{"state":"COMMAND_READY"}
```

방향이나 음성 인식 결과가 필요한 화면에서만 `payload`를 추가합니다. 이벤트 ID, 로봇 ID 기본값, 세션, 순번, 시간과 중요도는 태블릿이 자동 생성합니다.

`POST /api/v1/events`는 Python 연동 코드가 내부적으로 사용하는 고급 API입니다. 로봇 개발자가 직접 전체 요청을 만들 필요가 없습니다.

Python에서는 Display 1과 같은 형태로 호출할 수 있습니다.

```python
from orchestra_surgical_display import SurgicalDisplayClient

display = SurgicalDisplayClient("http://10.77.0.11:8080")
display.state("COMMAND_READY")
display.state("MANUAL_MOVING", direction="cam_left")
display.close()
```

## 주소

| 환경 | Base URL |
|---|---|
| 노트북 로컬 receiver | `http://127.0.0.1:8080` |
| Display 2 Lenovo 권장 주소 | `http://10.77.0.11:8080` |

현장 주소는 DHCP 예약 후 실제 할당값으로 확정합니다.

## Endpoint

| Method | Path | 용도 |
|---|---|---|
| `GET` | `/api/v1/health` | 수신 서버 확인 |
| `GET` | `/api/v1/robots` | 최신 State와 presence 확인 |
| `POST` | `/api/v1/state` | 처음 연동하는 간편 State 요청 |
| `POST` | `/api/v1/events` | Python 연동 코드가 내부적으로 사용하는 고급 이벤트 |

## 간편 State 요청

기본 State는 다음처럼 보냅니다.

```bash
curl -X POST http://127.0.0.1:8080/api/v1/state \
  -H 'Content-Type: application/json' \
  -d '{"state":"COMMAND_READY"}'
```

수동 이동 방향이 필요할 때만 추가합니다.

```json
{
  "state": "MANUAL_MOVING",
  "payload": {
    "direction": "cam_left"
  }
}
```

음성 인식 성공 화면이 필요할 때만 다음 값을 추가합니다.

```json
{
  "state": "REQUEST_RECEIVED",
  "payload": {
    "voice_phase": "complete",
    "recognized_text": "왼쪽",
    "recognition_result": "recognized",
    "command_result": "accepted"
  }
}
```

정상 수신은 HTTP `202 Accepted`입니다.

## 누가 값을 만드나요?

| 값 | 담당 |
|---|---|
| `state` | 로봇 개발자가 상태 전환 시 전달 |
| 방향·배율 | 해당 이동 화면에서만 선택 전달 |
| 음성 인식 단계·결과 | 해당 음성 화면에서만 선택 전달 |
| `robot_id` | 생략 시 태블릿이 `rby1-surgical` 사용 |
| 이벤트 ID·세션·순번·시간·중요도 | 태블릿 또는 Python 연동 코드가 자동 생성 |
| 연결 확인 신호 | Python 연동 코드가 자동 전송 |

## 화면 State

| State | 의미 |
|---|---|
| `STARTING` | 시스템 준비 |
| `AWAITING_START` | “스코프 시작” 명령 대기 |
| `COMMAND_READY` | 음성 명령 대기·인식 결과 |
| `REQUEST_RECEIVED` | 실행 승인 직후의 짧은 확인 |
| `MANUAL_MOVING` | 수동 이동 |
| `VISUAL_SERVOING` | 화면 추종 |
| `RETURNING` | 준비 위치 복귀 |
| `COMPLETED` | 정상 동작 종료 직후의 짧은 완료 표시 |
| `SAFE_WAIT` | 보호·복구 대기 |
| `ERROR` | 시스템 확인 필요 |

`PEDAL_MOVING`, `HOLDING`, `PROTECTIVE_RECOVERY`는 원본 진단 State로 유지하며 화면에서는 각각 `MANUAL_MOVING`, `COMMAND_READY`, `SAFE_WAIT`로 표시합니다. 안전 State는 모든 피드백·동작 State보다 우선합니다.

## 고급 이벤트 예시

아래 전체 형식은 Python 연동 코드가 내부적으로 생성합니다. 직접 HTTP 클라이언트를 새로 만드는 경우가 아니면 사용할 필요가 없습니다.

```json
{
  "schema_version": 1,
  "event_id": "9623c144-5a12-4d66-9f20-b5107cbdebd5",
  "event_type": "STATE",
  "robot_id": "rby1-surgical",
  "session_id": "session-1787216400",
  "sequence": 12,
  "state": "MANUAL_MOVING",
  "severity": "INFO",
  "occurred_at": "2026-08-21T01:00:00Z",
  "payload": {
    "active_arm": "scope",
    "direction": "cam_left",
    "direction_scale": 1.0,
    "voice_phase": "complete",
    "recognized_text": "왼쪽",
    "recognition_result": "recognized",
    "command_action": "CAMERA_LEFT",
    "command_result": "accepted"
  }
}
```

정상 수신은 HTTP `202 Accepted`입니다.

```json
{
  "accepted": true,
  "event_id": "9623c144-5a12-4d66-9f20-b5107cbdebd5",
  "duplicate": false,
  "server_received_at": "2026-08-21T01:00:00.030Z"
}
```

같은 `event_id`를 재전송하면 `duplicate=true`와 함께 `202`를 반환합니다.

## 추가 정보에 사용할 값

| 필드 | 값 |
|---|---|
| `active_arm` | `scope`, `assist` |
| `direction` | `cam_left`, `cam_right`, `cam_up`, `cam_down`, `insert`, `retract` |
| `voice_phase` | `disabled`, `listening`, `recording`, `transcribing`, `dispatching`, `complete`, `error` |
| `recognition_result` | `recognized`, `unrecognized` |
| `command_result` | `accepted`, `rejected` |
| `visual_phase` | `waiting_for_target`, `acquiring`, `tracking`, `target_loss_grace`, `target_lost`, `handoff_decelerating` |

전체 허용 필드와 enum은 [`contract/states.json`](../contract/states.json)을 함께 확인합니다. 계약에 없는 필드는 HTTP `422`로 거절합니다.

## 오류 응답

| HTTP | 의미 |
|---|---|
| `400` | 비어 있거나 잘못된 JSON |
| `413` | 64 KiB를 초과한 요청 |
| `415` | `application/json`이 아님 |
| `422` | State, enum, 필드 또는 형식 검증 실패 |

수신 오류와 timeout은 화면 전송 실패일 뿐입니다. 로봇 모션이나 명령 승인 결과로 변환하지 않습니다.
