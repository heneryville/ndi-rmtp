"""State machine for the streamer. No subprocess concerns here."""
from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


class State(enum.Enum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    STREAMING = "STREAMING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


VALID_TRANSITIONS: dict[State, set[State]] = {
    State.IDLE: {State.STARTING},
    State.STARTING: {State.STREAMING, State.ERROR, State.STOPPING},
    State.STREAMING: {State.STOPPING, State.ERROR},
    State.STOPPING: {State.IDLE, State.ERROR},
    State.ERROR: {State.IDLE, State.STARTING},
}


@dataclass
class Transition:
    frm: State
    to: State
    reason: str
    at: float


class StateMachine:
    def __init__(self) -> None:
        self._state = State.IDLE
        self._history: list[Transition] = []

    @property
    def state(self) -> State:
        return self._state

    def transition(self, to: State, reason: str) -> None:
        if to not in VALID_TRANSITIONS[self._state]:
            raise ValueError(
                f"Invalid transition: {self._state.value} -> {to.value}"
            )
        frm = self._state
        self._state = to
        self._history.append(Transition(frm=frm, to=to, reason=reason, at=time.time()))
        logger.info("state %s -> %s (%s)", frm.value, to.value, reason)

    def history(self) -> list[Transition]:
        return list(self._history)

    def _force(self, state: State) -> None:
        """Test-only: jump straight to a state. Do not use in production code."""
        self._state = state
