# Orchestra Surgical Display API 계약

기준 파일은 [`contract/openapi.yaml`](../contract/openapi.yaml)입니다. 이 문서는 로봇 개발자가 자주 확인하는 요청 형식과 운영 원칙을 요약합니다.

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
| `POST` | `/api/v1/state` | 수동 시험용 축약 요청 |
| `POST` | `/api/v1/events` | SDK가 사용하는 운영 이벤트 |

로봇을 제어하거나 명령을 반환하는 endpoint는 없습니다.

## 운영 이벤트 예시

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

## 축약 State 요청

수동 테스트에서만 사용합니다. 수신기가 envelope을 생성합니다.

```bash
curl -X POST http://127.0.0.1:8080/api/v1/state \
  -H 'Content-Type: application/json' \
  -d '{"robot_id":"rby1-surgical","state":"COMMAND_READY","payload":{"voice_phase":"listening"}}'
```

운영 코드에서는 SDK와 `/api/v1/events`를 사용합니다.

## Payload 핵심 enum

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
