"""Hardware-free checks of main G910 steering, camera and traction mapping."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] /
                       "src" / "ismartcar" / "controllers"))

from g910_main import (
    SERVOS, servo_counts, servo_selected, traction_command, stop_traction, write_traction,
)


class StubPWM:
    def __init__(self):
        self.calls = []

    def write_pwm(self, ch, on, off):
        self.calls.append((ch, on, off))


class StubGPIO:
    LOW, HIGH = 0, 1

    def __init__(self):
        self.calls = []

    def output(self, pins, value):
        self.calls.append((tuple(pins), value))


class MainControllerMappingTests(unittest.TestCase):
    def test_all_three_real_servo_channels(self):
        self.assertEqual({v[0] for v in SERVOS.values()}, {0, 14, 15})
        self.assertEqual(servo_counts("steering", 0), 450)
        self.assertEqual(servo_counts("camera_pan", 0), 450)
        self.assertEqual(servo_counts("camera_tilt", 0), 280)

    def test_servo_travel_stays_within_successful_bench_values(self):
        for name, (_, center, delta) in SERVOS.items():
            self.assertEqual(servo_counts(name, -100), center - delta)
            self.assertEqual(servo_counts(name, 100), center + delta)
            self.assertEqual(servo_counts(name, -999), center - delta)
            self.assertEqual(servo_counts(name, 999), center + delta)

    def test_direction_and_proportional_motor_duty(self):
        self.assertEqual(traction_command(100), ("forward", 1600))
        self.assertEqual(traction_command(-100), ("backward", 1600))
        self.assertEqual(traction_command(50), ("forward", 800))
        self.assertEqual(traction_command(-50), ("backward", 800))

    def test_neutral_never_enables_motor(self):
        for pct in (0, 10, -10, 24, -24):
            self.assertIsNone(traction_command(pct))
        self.assertEqual(traction_command(999), ("forward", 1600))

    def test_stop_traction_keeps_servo_channels_unchanged(self):
        gpio, pwm = StubGPIO(), StubPWM()
        stop_traction(gpio, pwm)
        self.assertEqual(pwm.calls, [(4, 0, 0), (5, 0, 0)])
        self.assertEqual(gpio.calls, [((11, 12, 13, 15), 0)])

    def test_motor_start_stops_previous_traction_first(self):
        gpio, pwm = StubGPIO(), StubPWM()
        write_traction(gpio, pwm, "forward", 800)
        self.assertEqual(
            pwm.calls,
            [(4, 0, 0), (5, 0, 0), (4, 0, 800), (5, 0, 800)],
        )
        self.assertEqual(gpio.calls[0], ((11, 12, 13, 15), 0))
        self.assertEqual(gpio.calls[-1], ((11, 12, 13, 15), (0, 1, 0, 1)))


    def test_no_servo_commands_during_main_initialization(self):
        # Regression for the first real-hardware failure: the old version
        # energized CH0, CH14, CH15 simultaneously before READY.
        import inspect
        import g910_main

        startup = inspect.getsource(g910_main.main).split(
            "while time.monotonic() < session_deadline:", 1
        )[0]
        self.assertNotIn("pwm.write_pwm(", startup)

    def test_isolated_servo_filter_only_allows_selected_channel(self):
        for selected in SERVOS:
            enabled = [name for name in SERVOS if servo_selected(name, selected)]
            self.assertEqual(enabled, [selected])
        self.assertEqual(
            [name for name in SERVOS if servo_selected(name, None)],
            list(SERVOS),
        )

    def test_servo_is_bounded_single_channel_pulse(self):
        import g910_main

        self.assertLessEqual(g910_main.SERVO_PULSE_SECONDS, 0.2)
        self.assertGreater(g910_main.SERVO_PULSE_SECONDS, 0)


if __name__ == "__main__":
    unittest.main()
