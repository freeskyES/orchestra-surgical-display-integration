from __future__ import annotations

import threading
import time
import unittest

from orchestra_surgical_display.client import SurgicalDisplayClient


class SurgicalDisplayClientTest(unittest.TestCase):
    def test_simple_state_method_generates_full_event(self) -> None:
        delivered: list[dict[str, object]] = []
        delivered_event = threading.Event()

        def transport(event: dict[str, object], _: float) -> None:
            delivered.append(event)
            delivered_event.set()

        with SurgicalDisplayClient(
            "http://display.invalid",
            heartbeat_interval=0,
            transport=transport,
        ) as client:
            self.assertTrue(client.state("MANUAL_MOVING", direction="cam_left"))
            self.assertTrue(delivered_event.wait(1.0))

        event = delivered[0]
        self.assertEqual("MANUAL_MOVING", event["state"])
        self.assertEqual({"direction": "cam_left"}, event["payload"])
        self.assertEqual("rby1-surgical", event["robot_id"])
        self.assertEqual(1, event["sequence"])
        self.assertIn("event_id", event)
        self.assertIn("occurred_at", event)

    def test_network_io_runs_on_worker_and_event_is_typed(self) -> None:
        caller_thread = threading.get_ident()
        delivered: list[tuple[dict[str, object], int]] = []
        delivered_event = threading.Event()

        def transport(event: dict[str, object], _: float) -> None:
            delivered.append((event, threading.get_ident()))
            delivered_event.set()

        with SurgicalDisplayClient(
            "http://display.invalid",
            heartbeat_interval=0,
            transport=transport,
        ) as client:
            self.assertTrue(
                client.publish_state(
                    "VISUAL_SERVOING",
                    {"active_arm": "scope", "visual_phase": "tracking"},
                )
            )
            self.assertTrue(delivered_event.wait(1.0))

        event, worker_thread = delivered[0]
        self.assertNotEqual(caller_thread, worker_thread)
        self.assertEqual("VISUAL_SERVOING", event["state"])
        self.assertEqual("INFO", event["severity"])

    def test_bounded_queue_drops_old_entries_without_blocking_caller(self) -> None:
        release = threading.Event()

        def blocked_transport(_: dict[str, object], __: float) -> None:
            release.wait(1.0)

        client = SurgicalDisplayClient(
            "http://display.invalid",
            queue_capacity=2,
            max_retries=0,
            heartbeat_interval=0,
            transport=blocked_transport,
        )
        started = time.monotonic()
        for _ in range(20):
            client.publish_state("COMMAND_READY", {"active_arm": "scope"})
        elapsed = time.monotonic() - started
        release.set()
        client.close()

        self.assertLess(elapsed, 0.2)
        self.assertGreater(client.stats.dropped, 0)

    def test_unknown_or_private_payload_is_rejected_before_queueing(self) -> None:
        with SurgicalDisplayClient(
            "http://display.invalid",
            heartbeat_interval=0,
            transport=lambda *_: None,
        ) as client:
            with self.assertRaises(ValueError):
                client.publish_state("COMMAND_READY", {"raw_transcript": "private"})


if __name__ == "__main__":
    unittest.main()
