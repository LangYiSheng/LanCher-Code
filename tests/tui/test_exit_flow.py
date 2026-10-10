import pytest

from lancher_code.tui.exit_flow import ExitFlow


class Clock:
    def __init__(self) -> None:
        self.now = 10.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_idle_interrupt_requires_second_press_in_three_seconds():
    clock = Clock()
    flow = ExitFlow(clock=clock)

    assert flow.request_interrupt(busy=False) == "arm_exit"
    assert flow.state == "armed"
    assert flow.confirmation_deadline == 13.0
    clock.advance(2.9)
    assert flow.request_interrupt(busy=False) == "exit"
    assert flow.state == "closing"
    assert not flow.is_armed
    assert flow.request_interrupt(busy=False) == "ignore"
    assert flow.request_exit() == "ignore"


@pytest.mark.parametrize("elapsed", [3.0, 3.1, 60.0])
def test_expired_second_press_starts_a_new_window(elapsed):
    clock = Clock()
    flow = ExitFlow(clock=clock)
    flow.request_interrupt(busy=False)
    clock.advance(elapsed)

    assert flow.request_interrupt(busy=False) == "arm_exit"
    assert flow.confirmation_remaining == pytest.approx(3.0)
    assert not flow.closing


def test_confirmation_expires_without_another_key_event():
    clock = Clock()
    flow = ExitFlow(clock=clock)
    flow.request_interrupt(busy=False)
    clock.advance(1.5)
    assert flow.confirmation_remaining == 1.5
    clock.advance(1.5)

    assert flow.confirmation_remaining == 0.0
    assert flow.confirmation_deadline is None
    assert flow.state == "idle"


def test_continuing_to_interact_disarms_confirmation():
    flow = ExitFlow(clock=Clock())
    flow.request_interrupt(busy=False)
    flow.interact()

    assert flow.state == "idle"
    assert flow.request_interrupt(busy=False) == "arm_exit"


def test_working_interrupt_cancels_once_and_does_not_arm_exit():
    flow = ExitFlow(clock=Clock())
    flow.work_started()

    assert flow.request_interrupt(busy=True) == "cancel_work"
    assert flow.state == "stopping"
    assert not flow.is_armed
    assert flow.request_interrupt(busy=True) == "wait_for_stop"
    flow.interact()
    assert flow.request_interrupt(busy=True) == "wait_for_stop"
    assert not flow.closing


def test_finished_work_requires_two_fresh_idle_presses():
    flow = ExitFlow(clock=Clock())
    assert flow.request_interrupt(busy=True) == "cancel_work"
    flow.work_finished()

    assert flow.state == "idle"
    assert flow.request_interrupt(busy=False) == "arm_exit"
    assert flow.request_interrupt(busy=False) == "exit"


def test_idle_observation_clears_stop_latch_when_finish_event_is_late():
    flow = ExitFlow(clock=Clock())
    assert flow.request_interrupt(busy=True) == "cancel_work"

    assert flow.request_interrupt(busy=False) == "arm_exit"
    assert flow.state == "armed"


def test_external_cleanup_is_not_cancelled_or_treated_as_idle():
    flow = ExitFlow(clock=Clock())

    assert flow.request_interrupt(busy=False, stopping=True) == "wait_for_stop"
    assert flow.request_interrupt(busy=True, stopping=True) == "wait_for_stop"
    assert not flow.is_armed
    assert flow.request_interrupt(busy=False, stopping=False) == "arm_exit"


def test_work_start_discards_an_existing_exit_confirmation():
    flow = ExitFlow(clock=Clock())
    flow.request_interrupt(busy=False)
    flow.work_started()

    assert not flow.is_armed
    assert flow.request_interrupt(busy=True) == "cancel_work"
    flow.work_finished()
    assert flow.request_interrupt(busy=False) == "arm_exit"


def test_new_work_can_be_cancelled_after_a_previous_stop():
    flow = ExitFlow(clock=Clock())
    assert flow.request_interrupt(busy=True) == "cancel_work"
    flow.work_finished()
    flow.work_started()

    assert flow.request_interrupt(busy=True) == "cancel_work"


def test_busy_observation_also_clears_an_exit_confirmation():
    flow = ExitFlow(clock=Clock())
    flow.request_interrupt(busy=False)

    assert flow.request_interrupt(busy=True) == "cancel_work"
    assert not flow.is_armed


def test_explicit_exit_is_committed_once_during_work():
    flow = ExitFlow(clock=Clock())
    flow.request_interrupt(busy=True)

    assert flow.request_exit() == "exit"
    flow.interact()
    flow.work_finished()
    flow.work_started()
    assert flow.state == "closing"
    assert flow.request_interrupt(busy=True, stopping=True) == "ignore"


def test_confirmation_uses_injected_clock_and_interval():
    clock = Clock()
    flow = ExitFlow(clock=clock, confirmation_seconds=1.0)
    assert flow.request_interrupt(busy=False) == "arm_exit"
    clock.advance(0.9)

    assert flow.request_interrupt(busy=False) == "exit"


@pytest.mark.parametrize("seconds", [0.0, -1.0, float("inf"), float("nan")])
def test_invalid_confirmation_interval_is_rejected(seconds):
    with pytest.raises(ValueError, match="有限的正数"):
        ExitFlow(confirmation_seconds=seconds)
