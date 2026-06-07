import pytest

from ndi_rmtp.state import State, StateMachine


def test_initial_state_is_idle():
    sm = StateMachine()
    assert sm.state == State.IDLE


@pytest.mark.parametrize(
    "frm,to",
    [
        (State.IDLE, State.STARTING),
        (State.STARTING, State.STREAMING),
        (State.STARTING, State.ERROR),
        (State.STARTING, State.STOPPING),
        (State.STREAMING, State.STOPPING),
        (State.STREAMING, State.ERROR),
        (State.STOPPING, State.IDLE),
        (State.STOPPING, State.ERROR),
        (State.ERROR, State.IDLE),
        (State.ERROR, State.STARTING),
    ],
)
def test_valid_transitions_succeed(frm, to):
    sm = StateMachine()
    sm._force(frm)  # test-only helper
    sm.transition(to, reason="test")
    assert sm.state == to


@pytest.mark.parametrize(
    "frm,to",
    [
        (State.IDLE, State.STREAMING),       # must go through STARTING
        (State.IDLE, State.STOPPING),
        (State.IDLE, State.ERROR),
        (State.STREAMING, State.STARTING),   # cannot bounce backward
        (State.STREAMING, State.IDLE),       # must go through STOPPING
        (State.STOPPING, State.STREAMING),
        (State.STOPPING, State.STARTING),
    ],
)
def test_invalid_transitions_raise(frm, to):
    sm = StateMachine()
    sm._force(frm)
    with pytest.raises(ValueError):
        sm.transition(to, reason="test")
    assert sm.state == frm  # state unchanged on failed transition


def test_transition_history_recorded():
    sm = StateMachine()
    sm.transition(State.STARTING, reason="user start")
    sm.transition(State.STREAMING, reason="first frame")
    history = sm.history()
    assert [h.to for h in history] == [State.STARTING, State.STREAMING]
    assert history[0].reason == "user start"
