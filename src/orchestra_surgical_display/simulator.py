"""Local API receiver used when a Lenovo tablet is not available."""

from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading
import time
from typing import Any
from urllib.parse import urlparse
import uuid

from .client import ALLOWED_PAYLOAD_FIELDS, PUBLIC_STATES


MAX_BODY_BYTES = 64 * 1024
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_LABEL = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_ENUM_FIELDS: dict[str, frozenset[str]] = {
    "active_arm": frozenset({"scope", "assist"}),
    "direction": frozenset(
        {"cam_left", "cam_right", "cam_up", "cam_down", "insert", "retract"}
    ),
    "pedal_phase": frozenset(
        {"idle", "engaged", "decelerating", "release_required"}
    ),
    "voice_phase": frozenset(
        {
            "disabled",
            "listening",
            "recording",
            "transcribing",
            "dispatching",
            "complete",
            "error",
        }
    ),
    "recognition_result": frozenset({"recognized", "unrecognized"}),
    "command_result": frozenset({"accepted", "rejected"}),
    "visual_phase": frozenset(
        {
            "waiting_for_target",
            "acquiring",
            "tracking",
            "target_loss_grace",
            "target_lost",
            "handoff_decelerating",
        }
    ),
    "gripper_requested": frozenset({"open", "close"}),
    "gripper_health": frozenset({"ok", "unavailable", "error"}),
}

_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "event_type",
        "robot_id",
        "session_id",
        "sequence",
        "state",
        "severity",
        "occurred_at",
        "payload",
    }
)
_REQUIRED_EVENT_FIELDS = _EVENT_FIELDS - {"payload"}


ADMIN_PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Orchestra Surgical Display Receiver</title><style>
:root{color-scheme:dark}body{font-family:system-ui,sans-serif;margin:0;background:#061421;color:#e8f5fa}
main{max-width:1180px;margin:auto;padding:32px}h1{margin:0 0 6px}.note{color:#8faab6;margin:0 0 24px}
.status{display:inline-flex;gap:9px;align-items:center;padding:9px 14px;border-radius:99px;background:#102b36;color:#78ead6}
.dot{width:8px;height:8px;border-radius:50%;background:#62e5cf}table{width:100%;margin-top:18px;border-collapse:collapse;background:#0b1f2d;border-radius:12px;overflow:hidden}
th,td{padding:12px 14px;text-align:left;border-bottom:1px solid #193544}th{color:#82d8fa;background:#102937;font-size:13px}
td{vertical-align:top}code{color:#b8e8fa}.payload{max-width:420px;white-space:pre-wrap;word-break:break-word;color:#9bb0ba;font-size:12px}
</style></head><body><main><h1>Orchestra Surgical Display Receiver</h1>
<p class="note">Lenovo 없이 수술로봇 State·음성·방향 payload 수신을 확인하는 개발용 화면입니다.</p>
<div class="status"><span class="dot"></span><span id="status">수신 대기 중</span></div>
<table><thead><tr><th>수신 시각</th><th>로봇</th><th>State</th><th>음성</th><th>방향</th><th>Payload</th></tr></thead>
<tbody id="events"></tbody></table></main><script>
const esc=v=>String(v??'-').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function refresh(){try{const r=await fetch('/debug/events');const d=await r.json();
document.querySelector('#status').textContent=`State 이벤트 ${d.events.length}건`;
document.querySelector('#events').innerHTML=d.events.slice().reverse().map(e=>{const p=e.payload||{};return `<tr><td>${esc(e.server_received_at)}</td><td><code>${esc(e.robot_id)}</code></td><td><code>${esc(e.state)}</code></td><td>${esc(p.recognized_text||p.voice_phase)}</td><td>${esc(p.direction)}</td><td class="payload">${esc(JSON.stringify(p,null,2))}</td></tr>`}).join('');
}catch(e){document.querySelector('#status').textContent='수신 서버 확인 필요';}}
refresh();setInterval(refresh,700);</script></body></html>"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class EventStore:
    """Thread-safe in-memory model of the tablet's receive behavior."""

    def __init__(self, max_events: int = 200) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._latest: dict[str, dict[str, Any]] = {}
        self._last_seen_at: dict[str, str] = {}
        self._last_seen_clock: dict[str, float] = {}
        self._event_ids: set[str] = set()
        self._event_id_order: deque[str] = deque()
        self._max_event_ids = max(32, max_events * 4)
        self._lock = threading.Lock()

    def accept(self, event: dict[str, Any]) -> tuple[bool, str]:
        received_at = utc_now()
        event_id = str(event["event_id"])
        robot_id = str(event["robot_id"])
        with self._lock:
            duplicate = event_id in self._event_ids
            if duplicate:
                return True, received_at

            self._remember_event_id(event_id)
            stored = _copy_event(event)
            stored["server_received_at"] = received_at
            self._last_seen_at[robot_id] = received_at
            self._last_seen_clock[robot_id] = time.monotonic()

            # Heartbeats refresh presence but do not create visible history rows.
            if event["event_type"] == "STATE":
                self._events.append(stored)
                self._latest[robot_id] = stored
            elif robot_id not in self._latest:
                self._latest[robot_id] = stored
        return False, received_at

    def next_sequence(self, robot_id: str) -> int:
        with self._lock:
            latest = self._latest.get(robot_id)
            return int(latest["sequence"]) + 1 if latest else 1

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [_copy_event(event) for event in self._events]

    def robots(self) -> list[dict[str, Any]]:
        with self._lock:
            now = time.monotonic()
            snapshots = []
            for robot_id, event in self._latest.items():
                elapsed = now - self._last_seen_clock[robot_id]
                presence = "CONNECTED" if elapsed <= 3 else "STALE" if elapsed <= 10 else "OFFLINE"
                snapshots.append(
                    {
                        "robot_id": robot_id,
                        "session_id": event["session_id"],
                        "sequence": event["sequence"],
                        "state": event["state"],
                        "severity": event["severity"],
                        "presence": presence,
                        "last_seen_at": self._last_seen_at[robot_id],
                        "payload": dict(event.get("payload", {})),
                    }
                )
            return snapshots

    def _remember_event_id(self, event_id: str) -> None:
        self._event_ids.add(event_id)
        self._event_id_order.append(event_id)
        while len(self._event_id_order) > self._max_event_ids:
            self._event_ids.discard(self._event_id_order.popleft())


class SimulatorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int]) -> None:
        self.store = EventStore()
        self.started_at = utc_now()
        super().__init__(address, SimulatorHandler)


class HttpProblem(ValueError):
    def __init__(self, status: HTTPStatus, error: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error = error


class SimulatorHandler(BaseHTTPRequestHandler):
    server: SimulatorServer

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/admin"):
            self._send(
                HTTPStatus.OK,
                ADMIN_PAGE.encode("utf-8"),
                "text/html; charset=utf-8",
            )
        elif path == "/api/v1/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "api_version": "v1",
                    "server_time": utc_now(),
                    "started_at": self.server.started_at,
                },
            )
        elif path == "/api/v1/robots":
            self._send_json(
                HTTPStatus.OK,
                {"server_time": utc_now(), "robots": self.server.store.robots()},
            )
        elif path == "/debug/events":
            self._send_json(HTTPStatus.OK, {"events": self.server.store.events()})
        else:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "not_found", "message": "route not found", "fields": []},
            )

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path not in ("/api/v1/events", "/api/v1/state"):
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "not_found", "message": "route not found", "fields": []},
            )
            return
        try:
            body = self._read_json()
            event = body if path.endswith("/events") else self._expand_state(body)
            validate_event(event)
        except HttpProblem as exc:
            self._send_json(
                exc.status,
                {"error": exc.error, "message": str(exc), "fields": []},
            )
            return
        except ValueError as exc:
            self._send_json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {"error": "validation_failed", "message": str(exc), "fields": []},
            )
            return

        duplicate, received_at = self.server.store.accept(event)
        if event["event_type"] == "STATE" and not duplicate:
            print(f"received {event['robot_id']} {event['state']} seq={event['sequence']}")
        self._send_json(
            HTTPStatus.ACCEPTED,
            {
                "accepted": True,
                "event_id": event["event_id"],
                "duplicate": duplicate,
                "server_received_at": received_at,
            },
        )

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _read_json(self) -> dict[str, Any]:
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise HttpProblem(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "unsupported_media_type",
                "Content-Type must be application/json",
            )
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise HttpProblem(HTTPStatus.BAD_REQUEST, "invalid_json", "invalid Content-Length") from exc
        if length < 1:
            raise HttpProblem(HTTPStatus.BAD_REQUEST, "invalid_json", "request body is empty")
        if length > MAX_BODY_BYTES:
            raise HttpProblem(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "payload_too_large",
                "request body exceeds 64 KiB",
            )
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpProblem(HTTPStatus.BAD_REQUEST, "invalid_json", "invalid JSON body") from exc
        if not isinstance(value, dict):
            raise HttpProblem(HTTPStatus.BAD_REQUEST, "invalid_json", "request body must be a JSON object")
        return value

    def _expand_state(self, body: dict[str, Any]) -> dict[str, Any]:
        unknown = set(body) - {"robot_id", "state", "payload"}
        if unknown:
            raise ValueError(f"unsupported request fields: {sorted(unknown)}")
        state = body.get("state")
        if not isinstance(state, str):
            raise ValueError("state must be a string")
        robot_id = body.get("robot_id", "rby1-surgical")
        severity = (
            "ERROR"
            if state == "ERROR"
            else "WARNING"
            if state in {"SAFE_WAIT", "HOLDING", "PROTECTIVE_RECOVERY"}
            else "INFO"
        )
        return {
            "schema_version": 1,
            "event_id": str(uuid.uuid4()),
            "event_type": "STATE",
            "robot_id": robot_id,
            "session_id": "simulator-state",
            "sequence": self.server.store.next_sequence(str(robot_id)),
            "state": state,
            "severity": severity,
            "occurred_at": utc_now(),
            "payload": body.get("payload", {}),
        }

    def _send_json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
        self._send(
            status,
            json.dumps(value, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def validate_event(event: dict[str, Any]) -> None:
    unknown = set(event) - _EVENT_FIELDS
    if unknown:
        raise ValueError(f"unsupported event fields: {sorted(unknown)}")
    missing = sorted(_REQUIRED_EVENT_FIELDS - set(event))
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")
    if event["schema_version"] != 1:
        raise ValueError("schema_version must be 1")
    try:
        uuid.UUID(str(event["event_id"]))
    except (ValueError, AttributeError) as exc:
        raise ValueError("event_id must be a UUID") from exc
    if event["event_type"] not in {"STATE", "HEARTBEAT"}:
        raise ValueError("event_type must be STATE or HEARTBEAT")
    for field in ("robot_id", "session_id"):
        if not isinstance(event[field], str) or not _IDENTIFIER.fullmatch(event[field]):
            raise ValueError(f"{field} has an invalid format")
    sequence = event["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ValueError("sequence must be a non-negative integer")
    if event["state"] not in PUBLIC_STATES:
        raise ValueError(f"unknown state: {event['state']}")
    if event["severity"] not in {"INFO", "WARNING", "ERROR"}:
        raise ValueError("severity must be INFO, WARNING, or ERROR")
    if not isinstance(event["occurred_at"], str):
        raise ValueError("occurred_at must be an ISO-8601 string")
    try:
        datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("occurred_at must be an ISO-8601 date-time") from exc
    validate_payload(event.get("payload", {}))


def validate_payload(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    unknown = set(payload) - ALLOWED_PAYLOAD_FIELDS
    if unknown:
        raise ValueError(f"unsupported payload fields: {sorted(unknown)}")
    for field, choices in _ENUM_FIELDS.items():
        if field in payload and payload[field] not in choices:
            raise ValueError(f"{field} must be one of {sorted(choices)}")
    for field in ("session_active", "pedal_pressed", "gripper_available"):
        if field in payload and not isinstance(payload[field], bool):
            raise ValueError(f"{field} must be boolean")
    if "direction_scale" in payload:
        value = payload["direction_scale"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.1 <= value <= 3.0:
            raise ValueError("direction_scale must be between 0.1 and 3.0")
    if "recognized_text" in payload:
        value = payload["recognized_text"]
        if not isinstance(value, str) or not 1 <= len(value) <= 80:
            raise ValueError("recognized_text must contain 1 to 80 characters")
    for field in ("step", "command_action", "safety_reason_code"):
        if field in payload and (
            not isinstance(payload[field], str) or not _CODE.fullmatch(payload[field])
        ):
            raise ValueError(f"{field} must be a normalized uppercase code")
    if "motion_label" in payload and (
        not isinstance(payload["motion_label"], str)
        or not _LABEL.fullmatch(payload["motion_label"])
    ):
        raise ValueError("motion_label must be a normalized lowercase label")


def _copy_event(event: dict[str, Any]) -> dict[str, Any]:
    copied = dict(event)
    copied["payload"] = dict(event.get("payload", {}))
    return copied


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a local Orchestra Surgical Display API receiver"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8080, type=int)
    args = parser.parse_args()
    server = SimulatorServer((args.host, args.port))
    print(f"receiver: http://{args.host}:{server.server_port}")
    print(f"admin:    http://{args.host}:{server.server_port}/admin")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
