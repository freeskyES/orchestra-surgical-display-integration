from __future__ import annotations

import unittest

from orchestra_surgical_display.state_resolver import (
    SurgicalStateSignals,
    resolve_state,
    resolve_transport_state,
)


class SurgicalStateResolverTest(unittest.TestCase):
    def test_compatibility_hold_signals_map_to_agreed_presentations(self) -> None:
        busy = SurgicalStateSignals(
            visual_servoing=True,
            pedal_moving=True,
            holding=True,
            protective_recovery=True,
            error=True,
        )
        self.assertEqual("ERROR", resolve_state(busy))
        self.assertEqual("SAFE_WAIT", resolve_state(busy.__class__(
            visual_servoing=True,
            holding=True,
            protective_recovery=True,
        )))
        self.assertEqual(
            "PROTECTIVE_RECOVERY",
            resolve_transport_state(busy.__class__(
                visual_servoing=True,
                holding=True,
                protective_recovery=True,
            )),
        )

    def test_transport_resolver_retains_compatibility_states(self) -> None:
        self.assertEqual(
            "HOLDING",
            resolve_transport_state(SurgicalStateSignals(holding=True)),
        )
        self.assertEqual(
            "PEDAL_MOVING",
            resolve_transport_state(SurgicalStateSignals(pedal_moving=True)),
        )

    def test_visual_servoing_wins_over_manual_motion(self) -> None:
        self.assertEqual(
            "VISUAL_SERVOING",
            resolve_state(SurgicalStateSignals(manual_moving=True, visual_servoing=True)),
        )

    def test_lifecycle_fallbacks(self) -> None:
        self.assertEqual("STARTING", resolve_state(SurgicalStateSignals(startup_complete=False)))
        self.assertEqual("AWAITING_START", resolve_state(SurgicalStateSignals(session_active=False)))
        self.assertEqual("COMMAND_READY", resolve_state(SurgicalStateSignals()))


if __name__ == "__main__":
    unittest.main()
