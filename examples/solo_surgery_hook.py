"""Minimal wiring example for solo_surgery's non-real-time orchestration thread."""

from __future__ import annotations

from typing import Any

from orchestra_surgical_display import (
    SoloSurgeryRuntimeObserver,
    start_runtime_observer_from_env,
)


def start_display_observer(
    *,
    controller: Any,
    servo: Any,
    coordinator: Any,
) -> SoloSurgeryRuntimeObserver | None:
    """Return ``None`` when display integration is not configured."""

    return start_runtime_observer_from_env(
        controller=controller,
        servo=servo,
        coordinator=coordinator,
    )


def report_voice_result(
    observer: SoloSurgeryRuntimeObserver | None,
    *,
    transcript: str,
    action: str,
    executed: bool,
) -> None:
    if observer is None:
        return
    observer.set_voice_result(transcript, action, executed=executed)


def stop_display_observer(observer: SoloSurgeryRuntimeObserver | None) -> None:
    if observer is not None:
        observer.stop()
