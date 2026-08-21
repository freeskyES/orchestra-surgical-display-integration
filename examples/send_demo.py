#!/usr/bin/env python3
"""Send a short surgical-display scenario to a local receiver or Lenovo."""

from __future__ import annotations

import argparse
import time
from typing import Any

from orchestra_surgical_display import SurgicalDisplayClient
from orchestra_surgical_display.client import PUBLIC_STATES


SCENARIO_PAYLOADS: dict[str, dict[str, Any]] = {
    "STARTING": {"step": "VISION_STARTUP", "voice_phase": "disabled"},
    "AWAITING_START": {"session_active": False, "voice_phase": "listening"},
    "COMMAND_READY": {
        "session_active": True,
        "active_arm": "scope",
        "voice_phase": "complete",
        "recognized_text": "시작",
        "recognition_result": "recognized",
        "command_action": "SESSION_START",
        "command_result": "accepted",
    },
    "MANUAL_MOVING": {
        "active_arm": "scope",
        "direction": "cam_left",
        "direction_scale": 1.0,
        "voice_phase": "complete",
        "recognized_text": "왼쪽",
        "recognition_result": "recognized",
        "command_action": "CAMERA_LEFT",
        "command_result": "accepted",
    },
    "VISUAL_SERVOING": {
        "active_arm": "scope",
        "visual_phase": "tracking",
        "voice_phase": "listening",
    },
    "RETURNING": {
        "active_arm": "scope",
        "motion_label": "voice_ready",
        "voice_phase": "complete",
    },
    "ERROR": {
        "active_arm": "scope",
        "voice_phase": "error",
        "safety_reason_code": "ROBOT_CONTROL_FAULT",
    },
    "PEDAL_MOVING": {
        "active_arm": "assist",
        "direction": "cam_right",
        "direction_scale": 1.0,
        "pedal_pressed": True,
        "pedal_phase": "engaged",
    },
    "HOLDING": {"active_arm": "scope", "motion_label": "safe_hold"},
    "PROTECTIVE_RECOVERY": {
        "active_arm": "scope",
        "voice_phase": "disabled",
        "safety_reason_code": "RCM_LIMIT_RECOVERY",
        "motion_label": "rcm_protective_hold",
    },
}

DEFAULT_SEQUENCE = (
    "STARTING",
    "AWAITING_START",
    "COMMAND_READY",
    "MANUAL_MOVING",
    "COMMAND_READY",
    "VISUAL_SERVOING",
    "RETURNING",
    "COMMAND_READY",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay surgical display states")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--robot-id", default="rby1-surgical")
    parser.add_argument("--state", choices=["DEMO", "ALL", *sorted(PUBLIC_STATES)], default="DEMO")
    parser.add_argument("--interval", type=float, default=0.7)
    args = parser.parse_args()

    if args.state == "DEMO":
        states = DEFAULT_SEQUENCE
    elif args.state == "ALL":
        states = tuple(SCENARIO_PAYLOADS)
    else:
        states = (args.state,)

    with SurgicalDisplayClient(
        args.url,
        robot_id=args.robot_id,
        heartbeat_interval=0,
    ) as client:
        for state in states:
            queued = client.publish_state(state, SCENARIO_PAYLOADS[state])
            print(f"queued={queued} state={state}")
            time.sleep(max(0.0, args.interval))
    print(client.stats)


if __name__ == "__main__":
    main()
