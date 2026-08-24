from __future__ import annotations

from datetime import datetime, timezone
from http import HTTPStatus
from urllib import error, request
import json
import threading
import time
import unittest
import uuid

from orchestra_surgical_display import SurgicalDisplayClient
from orchestra_surgical_display.simulator import SimulatorServer


class SimulatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = SimulatorServer(("127.0.0.1", 0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_minimum_state_request_generates_missing_event_values(self) -> None:
        status, body = self._post(
            "/api/v1/state",
            {"state": "COMMAND_READY"},
        )

        self.assertEqual(HTTPStatus.ACCEPTED, status)
        self.assertTrue(body["accepted"])
        uuid.UUID(body["event_id"])

        robot = self._get("/api/v1/robots")["robots"][0]
        self.assertEqual("rby1-surgical", robot["robot_id"])
        self.assertEqual("simulator-state", robot["session_id"])
        self.assertEqual(1, robot["sequence"])
        self.assertEqual("INFO", robot["severity"])

    def test_simple_state_is_visible_with_voice_and_direction_payload(self) -> None:
        status, body = self._post(
            "/api/v1/state",
            {
                "robot_id": "rby1-surgical",
                "state": "MANUAL_MOVING",
                "payload": {
                    "active_arm": "scope",
                    "direction": "cam_up",
                    "voice_phase": "complete",
                    "recognized_text": "위쪽",
                    "recognition_result": "recognized",
                    "command_action": "CAMERA_UP",
                    "command_result": "accepted",
                },
            },
        )
        self.assertEqual(HTTPStatus.ACCEPTED, status)
        self.assertTrue(body["accepted"])

        robots = self._get("/api/v1/robots")["robots"]
        self.assertEqual("MANUAL_MOVING", robots[0]["state"])
        self.assertEqual("cam_up", robots[0]["payload"]["direction"])
        self.assertEqual("위쪽", robots[0]["payload"]["recognized_text"])

    def test_sdk_event_reaches_receiver(self) -> None:
        with SurgicalDisplayClient(
            self.base_url,
            heartbeat_interval=0,
            request_timeout=1,
        ) as client:
            self.assertTrue(
                client.publish_state(
                    "VISUAL_SERVOING",
                    {"active_arm": "scope", "visual_phase": "tracking"},
                )
            )
            deadline = time.monotonic() + 2
            while not self.server.store.events() and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertEqual("VISUAL_SERVOING", self.server.store.events()[0]["state"])
        self.assertEqual(1, client.stats.sent)

    def test_duplicate_event_id_is_idempotent(self) -> None:
        event = self._event("COMMAND_READY")
        first_status, first = self._post("/api/v1/events", event)
        second_status, second = self._post("/api/v1/events", event)

        self.assertEqual(HTTPStatus.ACCEPTED, first_status)
        self.assertEqual(HTTPStatus.ACCEPTED, second_status)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(1, len(self.server.store.events()))

    def test_heartbeat_refreshes_snapshot_without_history_row(self) -> None:
        state = self._event("HOLDING")
        self._post("/api/v1/events", state)
        heartbeat = dict(state)
        heartbeat["event_id"] = str(uuid.uuid4())
        heartbeat["event_type"] = "HEARTBEAT"
        heartbeat["sequence"] = 2
        self._post("/api/v1/events", heartbeat)

        self.assertEqual(1, len(self.server.store.events()))
        self.assertEqual("HOLDING", self._get("/api/v1/robots")["robots"][0]["state"])

    def test_private_or_unknown_payload_is_rejected(self) -> None:
        status, body = self._post(
            "/api/v1/state",
            {
                "state": "COMMAND_READY",
                "payload": {"raw_transcript": "내부 음성 데이터"},
            },
            expected_error=True,
        )

        self.assertEqual(HTTPStatus.UNPROCESSABLE_ENTITY, status)
        self.assertEqual("validation_failed", body["error"])

    def test_unknown_state_is_rejected(self) -> None:
        status, _ = self._post(
            "/api/v1/state",
            {"state": "READY"},
            expected_error=True,
        )
        self.assertEqual(HTTPStatus.UNPROCESSABLE_ENTITY, status)

    def _event(self, state: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "event_id": str(uuid.uuid4()),
            "event_type": "STATE",
            "robot_id": "rby1-surgical",
            "session_id": "test-session",
            "sequence": 1,
            "state": state,
            "severity": "WARNING" if state == "HOLDING" else "INFO",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "payload": {"voice_phase": "listening"},
        }

    def _get(self, path: str) -> dict[str, object]:
        with request.urlopen(f"{self.base_url}{path}", timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post(
        self,
        path: str,
        value: dict[str, object],
        *,
        expected_error: bool = False,
    ) -> tuple[int, dict[str, object]]:
        post = request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(value, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(post, timeout=2) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            try:
                if not expected_error:
                    raise
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()



if __name__ == "__main__":
    unittest.main()
