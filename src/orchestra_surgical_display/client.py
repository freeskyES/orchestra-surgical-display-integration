from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from queue import Empty, Full, Queue
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping
from urllib import request
from uuid import uuid4


PUBLIC_STATES = frozenset(
    {
        "STARTING",
        "AWAITING_START",
        "COMMAND_READY",
        "MANUAL_MOVING",
        "VISUAL_SERVOING",
        "RETURNING",
        "ERROR",
        "PEDAL_MOVING",
        "HOLDING",
        "PROTECTIVE_RECOVERY",
    }
)

ALLOWED_PAYLOAD_FIELDS = frozenset(
    {
        "step",
        "session_active",
        "active_arm",
        "direction",
        "direction_scale",
        "pedal_pressed",
        "pedal_phase",
        "voice_phase",
        "recognized_text",
        "recognition_result",
        "command_action",
        "command_result",
        "visual_phase",
        "gripper_available",
        "gripper_requested",
        "gripper_health",
        "safety_reason_code",
        "motion_label",
    }
)


@dataclass(frozen=True, slots=True)
class ClientStats:
    queued: int = 0
    sent: int = 0
    failed: int = 0
    dropped: int = 0
    retries: int = 0


@dataclass(frozen=True, slots=True)
class _Envelope:
    schema_version: int
    event_id: str
    event_type: str
    robot_id: str
    session_id: str
    sequence: int
    state: str
    severity: str
    occurred_at: str
    payload: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "robot_id": self.robot_id,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "state": self.state,
            "severity": self.severity,
            "occurred_at": self.occurred_at,
            "payload": dict(self.payload),
        }


Transport = Callable[[Mapping[str, Any], float], None]


class SurgicalDisplayClient:
    """Best-effort sender whose caller path never performs network I/O."""

    def __init__(
        self,
        base_url: str,
        *,
        robot_id: str = "rby1-surgical",
        session_id: str | None = None,
        queue_capacity: int = 128,
        request_timeout: float = 0.35,
        max_retries: int = 2,
        heartbeat_interval: float = 1.0,
        transport: Transport | None = None,
    ) -> None:
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be positive")
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        self._endpoint = f"{base_url.rstrip('/')}/api/v1/events"
        self._robot_id = robot_id
        self._session_id = session_id or f"session-{int(time.time())}"
        self._request_timeout = request_timeout
        self._max_retries = max(0, max_retries)
        self._heartbeat_interval = max(0.0, heartbeat_interval)
        self._transport = transport or self._post_json
        self._queue: Queue[_Envelope | None] = Queue(maxsize=queue_capacity)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._latest_state: _Envelope | None = None
        self._sequence = 0
        self._stats = ClientStats()
        self._worker = threading.Thread(target=self._worker_loop, name="surgical-display-sender", daemon=True)
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            name="surgical-display-heartbeat",
            daemon=True,
        )
        self._worker.start()
        if self._heartbeat_interval > 0:
            self._heartbeat.start()

    @property
    def stats(self) -> ClientStats:
        with self._lock:
            return self._stats

    def publish_state(
        self,
        state: str,
        payload: Mapping[str, Any] | None = None,
        *,
        occurred_at: str | None = None,
    ) -> bool:
        normalized_state = state.upper()
        if normalized_state not in PUBLIC_STATES:
            raise ValueError(f"unknown surgical display state: {state}")
        safe_payload = dict(payload or {})
        unknown = set(safe_payload) - ALLOWED_PAYLOAD_FIELDS
        if unknown:
            raise ValueError(f"unsupported payload fields: {sorted(unknown)}")

        with self._lock:
            self._sequence += 1
            envelope = _Envelope(
                schema_version=1,
                event_id=str(uuid4()),
                event_type="STATE",
                robot_id=self._robot_id,
                session_id=self._session_id,
                sequence=self._sequence,
                state=normalized_state,
                severity=_severity_for(normalized_state),
                occurred_at=occurred_at or _now_iso(),
                payload=MappingProxyType(safe_payload),
            )
            self._latest_state = envelope
        return self._enqueue_latest(envelope)

    def close(self, drain_timeout: float = 1.0) -> None:
        deadline = time.monotonic() + max(0.0, drain_timeout)
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        self._stop.set()
        self._put_sentinel()
        self._worker.join(timeout=max(0.1, drain_timeout))
        if self._heartbeat.is_alive():
            self._heartbeat.join(timeout=0.2)

    def __enter__(self) -> "SurgicalDisplayClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _enqueue_latest(self, envelope: _Envelope) -> bool:
        try:
            self._queue.put_nowait(envelope)
            self._update_stats(queued=1)
            return True
        except Full:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except Empty:
                pass
            self._update_stats(dropped=1)
            try:
                self._queue.put_nowait(envelope)
                self._update_stats(queued=1)
                return True
            except Full:
                self._update_stats(dropped=1)
                return False

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                envelope = self._queue.get(timeout=0.1)
            except Empty:
                continue
            try:
                if envelope is None:
                    return
                self._send_with_retry(envelope)
            finally:
                self._queue.task_done()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self._heartbeat_interval):
            with self._lock:
                latest = self._latest_state
            if latest is None:
                continue
            heartbeat = replace(
                latest,
                event_id=str(uuid4()),
                event_type="HEARTBEAT",
                occurred_at=_now_iso(),
            )
            self._enqueue_latest(heartbeat)

    def _send_with_retry(self, envelope: _Envelope) -> None:
        for attempt in range(self._max_retries + 1):
            try:
                self._transport(envelope.as_dict(), self._request_timeout)
                self._update_stats(sent=1)
                return
            except Exception:
                if attempt >= self._max_retries:
                    self._update_stats(failed=1)
                    return
                self._update_stats(retries=1)
                time.sleep(min(0.05 * (2**attempt), 0.2))

    def _post_json(self, event: Mapping[str, Any], timeout: float) -> None:
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        http_request = request.Request(
            self._endpoint,
            data=encoded,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(http_request, timeout=timeout) as response:
            if response.status != 202:
                raise RuntimeError(f"display receiver returned HTTP {response.status}")
            response.read()

    def _put_sentinel(self) -> None:
        try:
            self._queue.put_nowait(None)
        except Full:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except Empty:
                return
            try:
                self._queue.put_nowait(None)
            except Full:
                pass

    def _update_stats(
        self,
        *,
        queued: int = 0,
        sent: int = 0,
        failed: int = 0,
        dropped: int = 0,
        retries: int = 0,
    ) -> None:
        with self._lock:
            self._stats = ClientStats(
                queued=self._stats.queued + queued,
                sent=self._stats.sent + sent,
                failed=self._stats.failed + failed,
                dropped=self._stats.dropped + dropped,
                retries=self._stats.retries + retries,
            )


def _severity_for(state: str) -> str:
    if state == "ERROR":
        return "ERROR"
    if state in {"HOLDING", "PROTECTIVE_RECOVERY"}:
        return "WARNING"
    return "INFO"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
