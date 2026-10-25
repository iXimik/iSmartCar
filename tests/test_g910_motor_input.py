"""Pure decision tests for the G910 motor start gesture. No hardware writes."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "ismartcar" / "controllers"))
from g910_motor_test import pulse_direction


class MotorGestureTests(unittest.TestCase):
    def test_fresh_a_with_forward_stick(self):
        self.assertEqual(pulse_direction(100, 1), "forward")
        self.assertEqual(pulse_direction(25, 1), "forward")

    def test_fresh_a_with_reverse_stick(self):
        self.assertEqual(pulse_direction(-100, 1), "reverse")
        self.assertEqual(pulse_direction(-25, 1), "reverse")

    def test_a_pressed_at_neutral_does_not_start(self):
        self.assertIsNone(pulse_direction(0, 1))
        self.assertIsNone(pulse_direction(24, 1))
        self.assertIsNone(pulse_direction(-24, 1))

    def test_auto_repeat_does_not_start(self):
        self.assertIsNone(pulse_direction(100, 2))
        self.assertIsNone(pulse_direction(-100, 2))

    def test_release_does_not_start(self):
        self.assertIsNone(pulse_direction(100, 0))
        self.assertIsNone(pulse_direction(-100, 0))


if __name__ == "__main__":
    unittest.main()
