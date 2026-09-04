from __future__ import annotations

from dataclasses import dataclass, replace
import os
import re
import threading
import time
from typing import Any, Mapping, Protocol

from .client import SurgicalDisplayClient
from .state_resolver import SurgicalStateSignals, resolve_transport_state


_DIRECTIONS = frozenset(
    {"cam_left", "cam_right", "cam_up", "cam_down", "insert", "retract"}
)
_VOICE_PHASES = frozenset(
    {
        "disabled",
        "listening",
        "recording",
        "transcribing",
        "dispatching",
        "complete",
        "error",
    }
)
_VISUAL_PHASES = frozenset(
    {
        "waiting_for_target",
        "acquiring",
        "tracking",
        "target_loss_grace",
        "target_lost",
        "handoff_decelerating",
    }
)
_COMMAND_RESULTS = frozenset({"accepted", "rejected"})
_RECOGNITION_RESULTS = frozenset({"recognized", "unrecognized"})
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SAFE_LABEL = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class StatePublisher(Protocol):
    def publish_state(
        self,
        state: str,
        payload: Mapping[str, Any] | None = None,
        *,
        occurred_at: str | None = None,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """Small, immutable snapshot copied outside the robot control loop."""

    startup_complete: bool = True
    session_active: bool = True
    motion_active: bool = False
    motion_label: str = "hold"
    servo_enabled: bool = False
    holding: bool = False
    protective_recovery: bool = False
    fault_code: str | None = None
    active_arm: str = "scope"
    selected_direction: str | None = None
    direction_scale: float = 1.0
    pedal_engaged: bool = False
    pedal_requires_release: bool = False
    voice_phase: str = "listening"
    recognized_text: str | None = None
    recognition_result: str | None = None
    command_action: str | None = None
    command_result: str | None = None
    visual_phase: str | None = None
    gripper_available: bool | None = None
    gripper_requested: str | None = None
    gripper_health: str | None = None

    @classmethod
    def from_components(
        cls,
        controller_status: object,
        *,
        session_active: bool,
        startup_complete: bool = True,
        servo_enabled: bool = False,
        arm_state: Mapping[str, Any] | None = None,
        **updates: Any,
    ) -> "RuntimeSnapshot":
        """Build a snapshot from solo_surgery's existing public read APIs.

        ``controller_status`` is the result of ``loop_status()`` and
        ``arm_state`` is the result of ``VoiceCoordinator.arm_state()``.
        Additional voice and visual fields come from their event hooks.
        """

        arm = dict(arm_state or {})
        raw_fault = getattr(controller_status, "fault", None)
        fault_code = updates.pop("fault_code", None)
        if raw_fault is not None and fault_code is None:
            # Do not place arbitrary internal exception text on the public API.
            fault_code = "ROBOT_FAULT"
        return cls(
            startup_complete=bool(startup_complete),
            session_active=bool(session_active),
            motion_active=bool(getattr(controller_status, "active", False)),
            motion_label=str(getattr(controller_status, "label", "hold") or "hold"),
            servo_enabled=bool(servo_enabled),
            fault_code=fault_code,
            active_arm=str(arm.get("arm", "scope")),
            selected_direction=arm.get("selected_direction"),
            direction_scale=float(arm.get("selected_scale", 1.0)),
            pedal_engaged=bool(arm.get("pedal_engaged", False)),
            pedal_requires_release=bool(arm.get("pedal_requires_release", False)),
            **updates,
        )


@dataclass(frozen=True, slots=True)
class ResolvedRuntimeEvent:
    state: str
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AdapterStats:
    observed: int = 0
    published: int = 0
    deduplicated: int = 0
    failed: int = 0


def resolve_runtime_event(snapshot: RuntimeSnapshot) -> ResolvedRuntimeEvent:
    label = _normalize_label(snapshot.motion_label)
    pedal_moving = bool(
        snapshot.pedal_engaged
        or (snapshot.motion_active and "pedal" in label)
    )
    returning = bool(snapshot.motion_active and label == "voice_ready")
    visual_servoing = bool(
        snapshot.servo_enabled
        or label in {"continuous_visual_velocity", "visual_handoff_decelerating"}
    )
    manual_moving = bool(
        snapshot.motion_active
        and not pedal_moving
        and not returning
        and not visual_servoing
        and label != "rcm_protective_hold"
    )
    protective_recovery = bool(
        snapshot.protective_recovery or label == "rcm_protective_hold"
    )
    signals = SurgicalStateSignals(
        startup_complete=snapshot.startup_complete,
        session_active=snapshot.session_active,
        manual_moving=manual_moving,
        visual_servoing=visual_servoing,
        pedal_moving=pedal_moving,
        returning=returning,
        holding=snapshot.holding,
        protective_recovery=protective_recovery,
        error=snapshot.fault_code is not None,
    )
    state = resolve_transport_state(signals)

    payload: dict[str, Any] = {
        "session_active": bool(snapshot.session_active),
        "active_arm": _one_of(snapshot.active_arm, {"scope", "assist"}, "scope"),
        "direction_scale": _direction_scale(snapshot.direction_scale),
        "pedal_pressed": bool(snapshot.pedal_engaged),
        "pedal_phase": (
            "engaged"
            if snapshot.pedal_engaged
            else "release_required"
            if snapshot.pedal_requires_release
            else "decelerating"
            if label.endswith("pedal_decelerating")
            else "idle"
        ),
        "voice_phase": _normalize_voice_phase(snapshot.voice_phase),
    }
    _put_one_of(payload, "direction", snapshot.selected_direction, _DIRECTIONS)
    _put_short_text(payload, "recognized_text", snapshot.recognized_text)
    _put_one_of(
        payload,
        "recognition_result",
        snapshot.recognition_result,
        _RECOGNITION_RESULTS,
    )
    _put_code(payload, "command_action", snapshot.command_action)
    _put_one_of(payload, "command_result", snapshot.command_result, _COMMAND_RESULTS)
    _put_one_of(payload, "visual_phase", snapshot.visual_phase, _VISUAL_PHASES)
    if snapshot.gripper_available is not None:
        payload["gripper_available"] = bool(snapshot.gripper_available)
    _put_one_of(
        payload,
        "gripper_requested",
        snapshot.gripper_requested,
        {"open", "close"},
    )
    _put_one_of(
        payload,
        "gripper_health",
        snapshot.gripper_health,
        {"ok", "unavailable", "error"},
    )
    _put_code(payload, "safety_reason_code", snapshot.fault_code)
    if _SAFE_LABEL.fullmatch(label):
        payload["motion_label"] = label
    return ResolvedRuntimeEvent(state=state, payload=payload)


class SoloSurgeryDisplayAdapter:
    """Deduplicating, fail-isolated bridge into the non-blocking sender."""

    def __init__(
        self,
        publisher: StatePublisher,
        *,
        close_publisher: bool = False,
    ) -> None:
        self._publisher = publisher
        self._close_publisher = bool(close_publisher)
        self._lock = threading.Lock()
        self._last_event: ResolvedRuntimeEvent | None = None
        self._stats = AdapterStats()

    @classmethod
    def connect(
        cls,
        base_url: str,
        **client_options: Any,
    ) -> "SoloSurgeryDisplayAdapter":
        return cls(
            SurgicalDisplayClient(base_url, **client_options),
            close_publisher=True,
        )

    @property
    def stats(self) -> AdapterStats:
        with self._lock:
            return self._stats

    def observe(
        self,
        snapshot: RuntimeSnapshot,
        *,
        force: bool = False,
        occurred_at: str | None = None,
    ) -> bool:
        """Queue a changed snapshot and never leak Display errors to the robot."""

        try:
            event = resolve_runtime_event(snapshot)
            return self.publish_event(
                event,
                force=force,
                occurred_at=occurred_at,
            )
        except Exception:
            with self._lock:
                self._stats = replace(self._stats, failed=self._stats.failed + 1)
            return False

    def publish_event(
        self,
        event: ResolvedRuntimeEvent,
        *,
        force: bool = False,
        occurred_at: str | None = None,
    ) -> bool:
        """Queue one already-resolved event without blocking the robot caller."""

        with self._lock:
            self._stats = replace(self._stats, observed=self._stats.observed + 1)
            if not force and event == self._last_event:
                self._stats = replace(
                    self._stats,
                    deduplicated=self._stats.deduplicated + 1,
                )
                return False
        try:
            accepted = bool(
                self._publisher.publish_state(
                    event.state,
                    event.payload,
                    occurred_at=occurred_at,
                )
            )
        except Exception:
            accepted = False
        with self._lock:
            if accepted:
                self._last_event = event
                self._stats = replace(
                    self._stats,
                    published=self._stats.published + 1,
                )
            else:
                self._stats = replace(
                    self._stats,
                    failed=self._stats.failed + 1,
                )
        return accepted

    def close(self) -> None:
        if not self._close_publisher:
            return
        closer = getattr(self._publisher, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass

    def __enter__(self) -> "SoloSurgeryDisplayAdapter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class SoloSurgeryRuntimeObserver:
    """Poll existing runtime snapshots away from real-time control callbacks."""

    def __init__(
        self,
        adapter: SoloSurgeryDisplayAdapter,
        *,
        controller: object,
        servo: object,
        coordinator: object,
        poll_interval: float = 0.1,
        startup_complete: bool = True,
        close_adapter: bool = False,
        request_dwell: float = 0.6,
        result_dwell: float = 1.2,
        completed_dwell: float = 0.8,
        clock: Any = time.monotonic,
    ) -> None:
        if poll_interval < 0.02:
            raise ValueError("poll_interval must be at least 0.02 seconds")
        self._adapter = adapter
        self._controller = controller
        self._servo = servo
        self._coordinator = coordinator
        self._poll_interval = float(poll_interval)
        self._startup_complete = bool(startup_complete)
        self._close_adapter = bool(close_adapter)
        self._request_dwell = max(0.0, float(request_dwell))
        self._result_dwell = max(0.0, float(result_dwell))
        self._completed_dwell = max(0.0, float(completed_dwell))
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._voice_phase = "listening"
        self._recognized_text: str | None = None
        self._recognition_result: str | None = None
        self._command_action: str | None = None
        self._command_result: str | None = None
        self._visual_phase: str | None = None
        self._gripper_requested: str | None = None
        self._request_until = 0.0
        self._result_until = 0.0
        self._completed_until = 0.0
        self._last_persistent_state: str | None = None

    def start(self) -> "SoloSurgeryRuntimeObserver":
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="surgical-display-observer",
                daemon=True,
            )
            self._thread.start()
        return self

    def set_startup_complete(self, complete: bool) -> None:
        with self._lock:
            self._startup_complete = bool(complete)

    def set_voice_phase(self, phase: str) -> None:
        with self._lock:
            self._voice_phase = str(phase)

    def set_voice_unavailable(self) -> None:
        with self._lock:
            self._voice_phase = "error"
            self._recognized_text = None
            self._recognition_result = None
            self._command_action = "VOICE_UNAVAILABLE"
            self._command_result = None
            self._request_until = 0.0
            self._result_until = 0.0

    def set_voice_result(
        self,
        transcript: str,
        action: str,
        *,
        executed: bool,
        recognized: bool | None = None,
    ) -> None:
        action_code = re.sub(
            r"[^A-Z0-9]+",
            "_",
            str(action).strip().upper(),
        ).strip("_")
        was_recognized = (
            action_code not in {"", "UNKNOWN"}
            if recognized is None
            else bool(recognized)
        )
        with self._lock:
            self._voice_phase = "complete"
            self._recognized_text = str(transcript)
            self._recognition_result = (
                "recognized" if was_recognized else "unrecognized"
            )
            self._command_action = action_code or None
            self._command_result = "accepted" if executed else "rejected"
            now = float(self._clock())
            if executed and action_code != "SESSION_END":
                self._request_until = now + self._request_dwell
                self._result_until = 0.0
            else:
                self._request_until = 0.0
                self._result_until = now + self._result_dwell

    def set_visual_phase(self, phase: str | None) -> None:
        with self._lock:
            self._visual_phase = None if phase is None else str(phase)

    def set_gripper_request(self, request: str | None) -> None:
        with self._lock:
            self._gripper_requested = None if request is None else str(request)

    def poll_once(self, *, force: bool = False) -> bool:
        """Read only public component snapshots and queue one display update."""

        try:
            status = self._controller.loop_status()
            arm_state = dict(self._coordinator.arm_state() or {})
            session_active = bool(getattr(self._coordinator, "session_active"))
            servo_enabled = bool(self._servo.is_enabled())
            now = float(self._clock())
            with self._lock:
                voice_phase = self._voice_phase
                recognized_text = self._recognized_text
                recognition_result = self._recognition_result
                command_action = self._command_action
                command_result = self._command_result
                visual_phase = self._visual_phase
                startup_complete = self._startup_complete
                gripper_requested = self._gripper_requested
                request_until = self._request_until
                result_until = self._result_until
                completed_until = self._completed_until
            if visual_phase is None:
                visual_phase = _read_visual_phase(self._servo, status)
            gripper = getattr(self._coordinator, "gripper", None)
            gripper_available = bool(
                arm_state.get("gripper_available", gripper is not None)
            )
            if gripper_requested is None:
                gripper_open = arm_state.get("gripper_open")
                if isinstance(gripper_open, bool):
                    gripper_requested = "open" if gripper_open else "close"
            snapshot = RuntimeSnapshot.from_components(
                status,
                startup_complete=startup_complete,
                session_active=session_active,
                servo_enabled=servo_enabled,
                arm_state=arm_state,
                voice_phase=voice_phase,
                recognized_text=recognized_text,
                recognition_result=recognition_result,
                command_action=command_action,
                command_result=command_result,
                visual_phase=visual_phase,
                gripper_available=gripper_available,
                gripper_requested=gripper_requested,
                gripper_health="ok" if gripper_available else "unavailable",
            )
            persistent = resolve_runtime_event(snapshot)
            with self._lock:
                previous_state = self._last_persistent_state
                self._last_persistent_state = persistent.state
                completed_transition = (
                    previous_state in {
                        "MANUAL_MOVING",
                        "RETURNING",
                    }
                    and persistent.state in {"COMMAND_READY", "HOLDING"}
                    and command_action not in {"STOP", "SERVO_OFF", "SESSION_END"}
                )
                if completed_transition:
                    start_at = max(now, self._request_until)
                    self._completed_until = start_at + self._completed_dwell
                completed_until = self._completed_until

            if persistent.state in {"ERROR", "PROTECTIVE_RECOVERY", "SAFE_WAIT"}:
                with self._lock:
                    self._request_until = 0.0
                    self._result_until = 0.0
                    self._completed_until = 0.0
                event = persistent
            elif now < request_until:
                event = ResolvedRuntimeEvent("REQUEST_RECEIVED", persistent.payload)
            elif now < result_until:
                event = persistent
            elif persistent.state in {
                "MANUAL_MOVING",
                "PEDAL_MOVING",
                "VISUAL_SERVOING",
                "RETURNING",
            }:
                event = persistent
            elif now < completed_until:
                event = ResolvedRuntimeEvent("COMPLETED", persistent.payload)
            else:
                event = persistent
            published = self._adapter.publish_event(event, force=force)
            if (
                persistent.state in {"COMMAND_READY", "HOLDING"}
                and now >= request_until
                and now >= result_until
                and now >= completed_until
                and command_action != "SESSION_END"
            ):
                with self._lock:
                    self._recognized_text = None
                    self._recognition_result = None
                    self._command_action = None
                    self._command_result = None
            return published
        except Exception:
            # The observer is diagnostics-only. Component teardown races or a
            # malformed optional field must never affect robot operation.
            return False

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout)))
        if self._close_adapter:
            self._adapter.close()

    def _run(self) -> None:
        self.poll_once(force=True)
        while not self._stop.wait(self._poll_interval):
            self.poll_once()

    def __enter__(self) -> "SoloSurgeryRuntimeObserver":
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()


def start_runtime_observer_from_env(
    *,
    controller: object,
    servo: object,
    coordinator: object,
    environ: Mapping[str, str] | None = None,
    startup_complete: bool = True,
) -> SoloSurgeryRuntimeObserver | None:
    """Start the optional bridge without making Display a robot dependency.

    When ``ORCHESTRA_SURGICAL_DISPLAY_URL`` is absent or invalid this returns
    ``None``. Even explicit Display configuration therefore cannot prevent
    the robot application from starting.
    """

    values = os.environ if environ is None else environ
    base_url = str(values.get("ORCHESTRA_SURGICAL_DISPLAY_URL", "")).strip()
    if not base_url:
        return None
    adapter: SoloSurgeryDisplayAdapter | None = None
    try:
        interval = float(
            values.get("ORCHESTRA_SURGICAL_DISPLAY_POLL_SECONDS", "0.1")
        )
        if interval < 0.02:
            return None
        adapter = SoloSurgeryDisplayAdapter.connect(
            base_url,
            request_timeout=0.35,
            max_retries=1,
            heartbeat_interval=1.0,
        )
        return SoloSurgeryRuntimeObserver(
            adapter,
            controller=controller,
            servo=servo,
            coordinator=coordinator,
            poll_interval=interval,
            startup_complete=startup_complete,
            close_adapter=True,
        ).start()
    except Exception:
        if adapter is not None:
            adapter.close()
        return None


def _normalize_label(value: str) -> str:
    return str(value).strip().lower().replace("-", "_") or "hold"


def _read_visual_phase(servo: object, status: object) -> str | None:
    label = _normalize_label(getattr(status, "label", ""))
    if label == "visual_handoff_decelerating":
        return "handoff_decelerating"
    controller = getattr(servo, "basic_controller", None)
    raw = str(getattr(controller, "state", "")).strip().upper()
    return {
        "WAITING_FOR_TARGET": "waiting_for_target",
        "ACQUIRING_TARGET": "acquiring",
        "TRACKING": "tracking",
        "WAITING_COMMAND_PERIOD": "tracking",
        "TARGET_LOSS_GRACE": "target_loss_grace",
        "TARGET_LOST": "target_lost",
    }.get(raw)


def _normalize_voice_phase(value: str) -> str:
    phase = str(value).strip().lower()
    if phase.startswith("voice:"):
        phase = phase.split(":", 1)[1].strip()
    aliases = {"ready": "listening", "command complete": "complete"}
    phase = aliases.get(phase, phase)
    return phase if phase in _VOICE_PHASES else "error"


def _one_of(value: Any, allowed: set[str] | frozenset[str], default: str) -> str:
    normalized = str(value).strip().lower()
    return normalized if normalized in allowed else default


def _put_one_of(
    payload: dict[str, Any],
    key: str,
    value: Any,
    allowed: set[str] | frozenset[str],
) -> None:
    if value is None:
        return
    normalized = str(value).strip().lower()
    if normalized in allowed:
        payload[key] = normalized


def _put_short_text(payload: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    text = " ".join(str(value).split())[:80]
    if text:
        payload[key] = text


def _put_code(payload: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    code = re.sub(r"[^A-Z0-9]+", "_", str(value).strip().upper()).strip("_")[:64]
    if _SAFE_CODE.fullmatch(code):
        payload[key] = code


def _direction_scale(value: float) -> float:
    try:
        scale = float(value)
    except (TypeError, ValueError):
        return 1.0
    return min(3.0, max(0.1, scale))


__all__ = [
    "AdapterStats",
    "ResolvedRuntimeEvent",
    "RuntimeSnapshot",
    "SoloSurgeryDisplayAdapter",
    "SoloSurgeryRuntimeObserver",
    "resolve_runtime_event",
    "start_runtime_observer_from_env",
]
