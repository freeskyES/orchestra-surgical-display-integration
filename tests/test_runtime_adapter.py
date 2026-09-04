from __future__ import annotations

from types import SimpleNamespace
import unittest

from orchestra_surgical_display.runtime_adapter import (
    RuntimeSnapshot,
    SoloSurgeryDisplayAdapter,
    SoloSurgeryRuntimeObserver,
    resolve_runtime_event,
    start_runtime_observer_from_env,
)


class _Publisher:
    def __init__(self, *, result: bool = True, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.events: list[tuple[str, dict[str, object], str | None]] = []

    def publish_state(self, state, payload=None, *, occurred_at=None):
        if self.error is not None:
            raise self.error
        self.events.append((state, dict(payload or {}), occurred_at))
        return self.result


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RuntimeAdapterTest(unittest.TestCase):
    def test_components_map_manual_direction_and_voice_feedback(self) -> None:
        snapshot = RuntimeSnapshot.from_components(
            SimpleNamespace(active=True, label="button_left_cam_left", fault=None),
            session_active=True,
            arm_state={
                "arm": "scope",
                "selected_direction": "cam_left",
                "selected_scale": 2.0,
                "pedal_engaged": False,
            },
            voice_phase="VOICE: command complete",
            recognized_text="왼쪽 많이",
            recognition_result="recognized",
            command_action="motion",
            command_result="accepted",
        )

        event = resolve_runtime_event(snapshot)

        self.assertEqual("MANUAL_MOVING", event.state)
        self.assertEqual("cam_left", event.payload["direction"])
        self.assertEqual(2.0, event.payload["direction_scale"])
        self.assertEqual("complete", event.payload["voice_phase"])
        self.assertEqual("왼쪽 많이", event.payload["recognized_text"])
        self.assertEqual("MOTION", event.payload["command_action"])

    def test_protective_and_pedal_raw_states_are_retained(self) -> None:
        protective = resolve_runtime_event(RuntimeSnapshot(
            motion_active=True,
            motion_label="rcm_protective_hold",
        ))
        pedal = resolve_runtime_event(RuntimeSnapshot(
            motion_active=True,
            motion_label="left_pedal_insert",
            pedal_engaged=True,
        ))

        self.assertEqual("PROTECTIVE_RECOVERY", protective.state)
        self.assertEqual("PEDAL_MOVING", pedal.state)
        self.assertEqual("engaged", pedal.payload["pedal_phase"])

    def test_persistent_runtime_state_matrix(self) -> None:
        cases = (
            (RuntimeSnapshot(startup_complete=False), "STARTING"),
            (RuntimeSnapshot(session_active=False), "AWAITING_START"),
            (RuntimeSnapshot(), "COMMAND_READY"),
            (RuntimeSnapshot(motion_active=True, motion_label="manual_left"), "MANUAL_MOVING"),
            (RuntimeSnapshot(servo_enabled=True), "VISUAL_SERVOING"),
            (RuntimeSnapshot(motion_active=True, motion_label="voice_ready"), "RETURNING"),
            (RuntimeSnapshot(motion_active=True, motion_label="left_pedal_insert"), "PEDAL_MOVING"),
            (RuntimeSnapshot(motion_active=True, motion_label="rcm_protective_hold"), "PROTECTIVE_RECOVERY"),
            (RuntimeSnapshot(fault_code="ROBOT_FAULT"), "ERROR"),
        )

        for snapshot, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(expected, resolve_runtime_event(snapshot).state)

    def test_fault_text_is_not_exposed_and_error_wins(self) -> None:
        snapshot = RuntimeSnapshot.from_components(
            SimpleNamespace(
                active=True,
                label="voice_ready",
                fault="internal path and hardware exception details",
            ),
            session_active=True,
        )

        event = resolve_runtime_event(snapshot)

        self.assertEqual("ERROR", event.state)
        self.assertEqual("ROBOT_FAULT", event.payload["safety_reason_code"])
        self.assertNotIn("internal", str(event.payload))

    def test_unchanged_snapshots_are_deduplicated(self) -> None:
        publisher = _Publisher()
        adapter = SoloSurgeryDisplayAdapter(publisher)
        snapshot = RuntimeSnapshot(session_active=False)

        self.assertTrue(adapter.observe(snapshot))
        self.assertFalse(adapter.observe(snapshot))
        self.assertEqual(1, len(publisher.events))
        self.assertEqual(1, adapter.stats.published)
        self.assertEqual(1, adapter.stats.deduplicated)

    def test_publisher_failure_never_reaches_robot_caller(self) -> None:
        adapter = SoloSurgeryDisplayAdapter(
            _Publisher(error=OSError("tablet offline"))
        )

        self.assertFalse(adapter.observe(RuntimeSnapshot()))
        self.assertEqual(1, adapter.stats.failed)

    def test_invalid_optional_values_are_omitted_or_normalized(self) -> None:
        event = resolve_runtime_event(RuntimeSnapshot(
            active_arm="unexpected",
            selected_direction="diagonal",
            direction_scale=99,
            voice_phase="VOICE: unknown",
            recognized_text="  시작   " * 30,
            recognition_result="maybe",
            visual_phase="bad",
            motion_label="unsafe label / path",
        ))

        self.assertEqual("scope", event.payload["active_arm"])
        self.assertEqual(3.0, event.payload["direction_scale"])
        self.assertEqual("error", event.payload["voice_phase"])
        self.assertLessEqual(len(event.payload["recognized_text"]), 80)
        self.assertNotIn("direction", event.payload)
        self.assertNotIn("recognition_result", event.payload)
        self.assertNotIn("visual_phase", event.payload)
        self.assertNotIn("motion_label", event.payload)

    def test_runtime_observer_reads_existing_component_snapshots(self) -> None:
        publisher = _Publisher()
        adapter = SoloSurgeryDisplayAdapter(publisher)
        controller = SimpleNamespace(
            loop_status=lambda: SimpleNamespace(
                active=False,
                label="hold",
                fault=None,
            )
        )
        servo = SimpleNamespace(is_enabled=lambda: False)
        coordinator = SimpleNamespace(
            session_active=False,
            gripper=None,
            arm_state=lambda: {
                "arm": "assist",
                "selected_direction": "insert",
                "selected_scale": 0.5,
                "pedal_engaged": False,
                "pedal_requires_release": False,
            },
        )
        observer = SoloSurgeryRuntimeObserver(
            adapter,
            controller=controller,
            servo=servo,
            coordinator=coordinator,
        )

        observer.set_voice_phase("VOICE: transcribing")
        self.assertTrue(observer.poll_once())

        state, payload, _occurred_at = publisher.events[-1]
        self.assertEqual("AWAITING_START", state)
        self.assertEqual("assist", payload["active_arm"])
        self.assertEqual(0.5, payload["direction_scale"])
        self.assertEqual("transcribing", payload["voice_phase"])

    def test_runtime_observer_exposes_real_startup_state(self) -> None:
        publisher = _Publisher()
        observer = SoloSurgeryRuntimeObserver(
            SoloSurgeryDisplayAdapter(publisher),
            controller=SimpleNamespace(
                loop_status=lambda: SimpleNamespace(
                    active=False,
                    label="hold",
                    fault=None,
                )
            ),
            servo=SimpleNamespace(is_enabled=lambda: False),
            coordinator=SimpleNamespace(
                session_active=False,
                gripper=None,
                arm_state=lambda: {},
            ),
            startup_complete=False,
        )

        self.assertTrue(observer.poll_once())
        self.assertEqual("STARTING", publisher.events[-1][0])

    def test_visual_and_gripper_details_are_read_from_existing_snapshots(self) -> None:
        publisher = _Publisher()
        observer = SoloSurgeryRuntimeObserver(
            SoloSurgeryDisplayAdapter(publisher),
            controller=SimpleNamespace(
                loop_status=lambda: SimpleNamespace(
                    active=False,
                    label="hold",
                    fault=None,
                )
            ),
            servo=SimpleNamespace(
                is_enabled=lambda: True,
                basic_controller=SimpleNamespace(state="TARGET_LOST"),
            ),
            coordinator=SimpleNamespace(
                session_active=True,
                gripper=object(),
                arm_state=lambda: {
                    "gripper_available": True,
                    "gripper_open": True,
                },
            ),
        )

        self.assertTrue(observer.poll_once())
        state, payload, _ = publisher.events[-1]
        self.assertEqual("VISUAL_SERVOING", state)
        self.assertEqual("target_lost", payload["visual_phase"])
        self.assertEqual("open", payload["gripper_requested"])

        observer.set_voice_unavailable()
        observer._servo = SimpleNamespace(is_enabled=lambda: False)
        self.assertTrue(observer.poll_once())
        _, payload, _ = publisher.events[-1]
        self.assertEqual("error", payload["voice_phase"])
        self.assertEqual("VOICE_UNAVAILABLE", payload["command_action"])

    def test_runtime_observer_voice_result_is_separate_from_main_state(self) -> None:
        publisher = _Publisher()
        adapter = SoloSurgeryDisplayAdapter(publisher)
        controller = SimpleNamespace(
            loop_status=lambda: SimpleNamespace(
                active=False,
                label="hold",
                fault=None,
            )
        )
        observer = SoloSurgeryRuntimeObserver(
            adapter,
            controller=controller,
            servo=SimpleNamespace(is_enabled=lambda: False),
            coordinator=SimpleNamespace(
                session_active=True,
                gripper=None,
                arm_state=lambda: {},
            ),
        )

        observer.set_voice_result(
            "알 수 없는 문장",
            "unknown",
            executed=False,
        )
        self.assertTrue(observer.poll_once())

        state, payload, _occurred_at = publisher.events[-1]
        self.assertEqual("COMMAND_READY", state)
        self.assertEqual("unrecognized", payload["recognition_result"])
        self.assertEqual("rejected", payload["command_result"])
        self.assertEqual("complete", payload["voice_phase"])

    def test_accepted_voice_result_emits_request_received_then_motion(self) -> None:
        publisher = _Publisher()
        clock = _Clock()
        status = SimpleNamespace(active=False, label="hold", fault=None)
        observer = SoloSurgeryRuntimeObserver(
            SoloSurgeryDisplayAdapter(publisher),
            controller=SimpleNamespace(loop_status=lambda: status),
            servo=SimpleNamespace(is_enabled=lambda: False),
            coordinator=SimpleNamespace(
                session_active=True,
                gripper=None,
                arm_state=lambda: {},
            ),
            clock=clock,
        )

        observer.set_voice_result("왼쪽", "motion", executed=True)
        self.assertTrue(observer.poll_once())
        self.assertEqual("REQUEST_RECEIVED", publisher.events[-1][0])

        clock.advance(0.7)
        status.active = True
        status.label = "button_left_cam_left"
        self.assertTrue(observer.poll_once())
        self.assertEqual("MANUAL_MOVING", publisher.events[-1][0])

    def test_motion_completion_emits_completed_then_ready(self) -> None:
        publisher = _Publisher()
        clock = _Clock()
        status = SimpleNamespace(
            active=True,
            label="button_left_cam_left",
            fault=None,
        )
        observer = SoloSurgeryRuntimeObserver(
            SoloSurgeryDisplayAdapter(publisher),
            controller=SimpleNamespace(loop_status=lambda: status),
            servo=SimpleNamespace(is_enabled=lambda: False),
            coordinator=SimpleNamespace(
                session_active=True,
                gripper=None,
                arm_state=lambda: {},
            ),
            clock=clock,
        )

        self.assertTrue(observer.poll_once())
        self.assertEqual("MANUAL_MOVING", publisher.events[-1][0])
        status.active = False
        status.label = "hold"
        clock.advance(0.1)
        self.assertTrue(observer.poll_once())
        self.assertEqual("COMPLETED", publisher.events[-1][0])
        clock.advance(0.9)
        self.assertTrue(observer.poll_once())
        self.assertEqual("COMMAND_READY", publisher.events[-1][0])

    def test_safety_state_preempts_transient_feedback(self) -> None:
        publisher = _Publisher()
        clock = _Clock()
        status = SimpleNamespace(active=False, label="hold", fault=None)
        observer = SoloSurgeryRuntimeObserver(
            SoloSurgeryDisplayAdapter(publisher),
            controller=SimpleNamespace(loop_status=lambda: status),
            servo=SimpleNamespace(is_enabled=lambda: False),
            coordinator=SimpleNamespace(
                session_active=True,
                gripper=None,
                arm_state=lambda: {},
            ),
            clock=clock,
        )

        observer.set_voice_result("왼쪽", "motion", executed=True)
        status.active = True
        status.label = "rcm_protective_hold"
        self.assertTrue(observer.poll_once())
        self.assertEqual("PROTECTIVE_RECOVERY", publisher.events[-1][0])

    def test_runtime_observer_component_failure_is_isolated(self) -> None:
        adapter = SoloSurgeryDisplayAdapter(_Publisher())
        observer = SoloSurgeryRuntimeObserver(
            adapter,
            controller=SimpleNamespace(
                loop_status=lambda: (_ for _ in ()).throw(RuntimeError("closed"))
            ),
            servo=SimpleNamespace(is_enabled=lambda: False),
            coordinator=SimpleNamespace(session_active=True, arm_state=lambda: {}),
        )

        self.assertFalse(observer.poll_once())

    def test_optional_environment_hook_is_disabled_by_default(self) -> None:
        observer = start_runtime_observer_from_env(
            controller=SimpleNamespace(),
            servo=SimpleNamespace(),
            coordinator=SimpleNamespace(),
            environ={},
        )

        self.assertIsNone(observer)

    def test_invalid_environment_hook_never_blocks_robot_startup(self) -> None:
        observer = start_runtime_observer_from_env(
            controller=SimpleNamespace(),
            servo=SimpleNamespace(),
            coordinator=SimpleNamespace(),
            environ={
                "ORCHESTRA_SURGICAL_DISPLAY_URL": "http://tablet:8080",
                "ORCHESTRA_SURGICAL_DISPLAY_POLL_SECONDS": "invalid",
            },
        )

        self.assertIsNone(observer)


if __name__ == "__main__":
    unittest.main()
