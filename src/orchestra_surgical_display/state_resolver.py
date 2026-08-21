from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SurgicalStateSignals:
    startup_complete: bool = True
    session_active: bool = True
    manual_moving: bool = False
    visual_servoing: bool = False
    pedal_moving: bool = False
    returning: bool = False
    holding: bool = False
    protective_recovery: bool = False
    error: bool = False


def resolve_state(signals: SurgicalStateSignals) -> str:
    """Resolve robot signals into the seven public presentation states."""
    transport_state = resolve_transport_state(signals)
    return {
        "PEDAL_MOVING": "MANUAL_MOVING",
        "HOLDING": "COMMAND_READY",
        "PROTECTIVE_RECOVERY": "COMMAND_READY",
    }.get(transport_state, transport_state)


def resolve_transport_state(signals: SurgicalStateSignals) -> str:
    """Resolve the raw event state while retaining compatibility signals.

    Android stores this state for diagnostics and normalizes it only when it
    is presented.  Keeping transport and presentation resolution separate
    prevents HOLDING or protective recovery evidence from being discarded.
    """

    if signals.error:
        return "ERROR"
    if signals.protective_recovery:
        return "PROTECTIVE_RECOVERY"
    if signals.holding:
        return "HOLDING"
    if signals.returning:
        return "RETURNING"
    if signals.pedal_moving:
        return "PEDAL_MOVING"
    if signals.visual_servoing:
        return "VISUAL_SERVOING"
    if signals.manual_moving:
        return "MANUAL_MOVING"
    if not signals.startup_complete:
        return "STARTING"
    if not signals.session_active:
        return "AWAITING_START"
    return "COMMAND_READY"
