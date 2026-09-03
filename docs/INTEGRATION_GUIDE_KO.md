# solo_surgery 연동 가이드

## 적용 범위

Display 연동은 현재 로봇 상태를 읽어 Lenovo 화면에 보여주는 기능입니다. 기존 로봇 동작, 명령 승인, 안전 제한과 종료 판단은 변경하지 않습니다.

## 1. 패키지 설치

로봇 Python 환경에서 이 저장소를 설치합니다.

```bash
python3 -m pip install -e /path/to/orchestra-surgical-display-integration
```

## 2. 환경 변수

Display를 사용할 때만 URL을 설정합니다.

```bash
export ORCHESTRA_SURGICAL_DISPLAY_URL=http://10.77.0.11:8080
export ORCHESTRA_SURGICAL_DISPLAY_POLL_SECONDS=0.1
```

URL이 없으면 `start_runtime_observer_from_env()`는 `None`을 반환합니다. URL 또는 상태 확인 주기 값이 잘못되어도 로봇 시작을 중단하지 않습니다.

## 3. Display 연동 모듈 시작과 종료

`VoiceCoordinator`와 servo 연결을 마친 뒤 일반 Python 실행 코드에서 시작합니다.

```python
from orchestra_surgical_display import start_runtime_observer_from_env

display_observer = start_runtime_observer_from_env(
    controller=controller,
    servo=servo,
    coordinator=coordinator,
)
```

종료 `finally` 블록에서는 controller와 servo를 종료하기 전에 Display 연동 모듈을 먼저 닫습니다.

```python
if display_observer is not None:
    display_observer.stop()
```

## 4. 음성 인식 결과 연결

기존 음성 인식 단계가 바뀌는 지점에서 같은 값을 전달합니다.

```python
if display_observer is not None:
    display_observer.set_voice_phase(phase)
```

`dispatch_text()` 결과 생성 직후 최종 인식 문장과 실행 결과를 전달합니다.

```python
if display_observer is not None:
    display_observer.set_voice_result(
        result.intent.transcript,
        result.intent.action.value,
        executed=result.executed,
    )
```

매핑은 다음과 같습니다.

| 결과 | `recognition_result` | `command_result` |
|---|---|---|
| UNKNOWN intent | `unrecognized` | `rejected` |
| 인식했지만 실행 조건 불충족 | `recognized` | `rejected` |
| 명령 실행 승인 | `recognized` | `accepted` |

## 5. 방향과 배율

`VoiceCoordinator.arm_state()`에 현재 선택 배율을 읽기 전용 값으로 포함합니다.

```python
"selected_scale": self._operator_step_scale,
```

방향 enum은 `cam_left`, `cam_right`, `cam_up`, `cam_down`, `insert`, `retract`입니다. 배율은 `direction_scale`로 전송하며 계약 범위는 0.1~3.0입니다.

## 6. 상태 계산

별도 READY 이벤트가 없어도 Display 연동 모듈이 기존 상태 조회 결과를 조합해 `COMMAND_READY`를 계산할 수 있습니다.

```text
startup 완료
AND session_active=true
AND motion/visual/fault/protection 없음
→ COMMAND_READY
```

상태 우선순위는 오류와 보호 상태가 이동 상태보다 앞섭니다. 실행 승인 직후에는 `REQUEST_RECEIVED`, 정상 동작 종료 직후에는 `COMPLETED`를 observer가 짧게 전송합니다. `HOLDING`, `PROTECTIVE_RECOVERY`, `PEDAL_MOVING` 원본은 진단을 위해 그대로 전송되고, 태블릿이 화면 표시 시 호환 매핑합니다.

## 7. 현장 적용 전 검증

1. 환경 변수 없이 기존 로봇 시작·동작·종료가 동일한지 확인
2. PC 테스트 서버에서 State와 음성 성공·실패 값 확인
3. 닿지 않는 URL에서도 로봇 명령과 종료가 정상인지 확인
4. Lenovo API 연결 후 화면 전환과 연결 상태 복구 확인
5. 연동 전후 100 Hz·500 Hz 실시간 제어 주기가 느려지지 않는지 확인

100 Hz 또는 500 Hz 실시간 제어 함수 안에서 `poll_once()`나 HTTP 관련 함수를 직접 호출하지 않습니다.
