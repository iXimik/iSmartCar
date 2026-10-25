"""Offline tests for the direct-drive bench sequence. No GPIO/I2C writes."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "ismartcar" / "hardware"))

from bench_actuators_test import (
    SERVO_TESTS, servo_sequence, motor_sequence, set_motor_direction,
)


class FakePWM:
    def __init__(self):
        self.events = []

    def write_pwm(self, channel, on, off):
        self.events.append(("pwm", channel, on, off))

    def all_outputs_off(self):
        self.events.append(("off",))


class FakeGPIO:
    LOW = 0
    HIGH = 1

    def __init__(self):
        self.events = []

    def output(self, pins, level):
        self.events.append((tuple(pins), level))


class BenchTest(unittest.TestCase):
    def test_servo_channels_and_counts_are_bounded(self):
        pwm = FakePWM()
        with patch("bench_actuators_test.time.sleep"):
            servo_sequence(pwm)
        writes = [e for e in pwm.events if e[0] == "pwm"]
        self.assertEqual(len(writes), 12)
        self.assertEqual({e[1] for e in writes}, {0, 14, 15})
        for _, ch, center, delta in SERVO_TESTS:
            target = [e[3] for e in writes if e[1] == ch]
            self.assertEqual(target, [center, center - delta, center + delta, center])
        self.assertEqual(sum(e[0] == "off" for e in pwm.events), 3)

    def test_forward_backward_motor_gpio_patterns(self):
        gpio = FakeGPIO()
        set_motor_direction(gpio, "forward")
        set_motor_direction(gpio, "backward")
        self.assertEqual(gpio.events[0][1], (0, 1, 0, 1))
        self.assertEqual(gpio.events[1][1], (1, 0, 1, 0))

    def test_both_motor_pulses_stop_and_disable_pwm(self):
        pwm = FakePWM()
        gpio = FakeGPIO()
        with (
            patch("bench_actuators_test.time.sleep"),
            patch("builtins.input", side_effect=["ВПЕРЁД", "НАЗАД"]),
        ):
            motor_sequence(pwm, gpio, 0.2)
        self.assertEqual(sum(e[0] == "off" for e in pwm.events), 2)
        self.assertEqual(
            [e[1] for e in pwm.events if e[0] == "pwm"], [4, 5, 4, 5]
        )
        self.assertEqual(gpio.events[-1][1], 0)

    def test_motor_sequence_can_skip_all_pulses(self):
        pwm = FakePWM()
        gpio = FakeGPIO()
        with (
            patch("bench_actuators_test.time.sleep"),
            patch("builtins.input", side_effect=["", ""]),
        ):
            motor_sequence(pwm, gpio, 0.2)
        self.assertEqual(pwm.events, [])
        self.assertEqual(gpio.events, [])


if __name__ == "__main__":
    unittest.main()
