"""Input gesture tests for the G910 motor-only pilot. No motor hardware used."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "src" / "ismartcar" / "controllers")
)
from evdev import ecodes

from g910_drive import Gesture


class G910MotorPilotTests(unittest.TestCase):
    def test_stick_before_a_starts_one_forward_pulse(self):
        gate = Gesture()
        gate.observe(ecodes.EV_ABS, ecodes.ABS_Y, 0, 10.0, 0, 255)
        self.assertIsNone(gate.request(10.0))
        gate.observe(ecodes.EV_KEY, ecodes.BTN_SOUTH, 1, 10.1, 0, 255)
        self.assertEqual(gate.request(10.1), "forward")
        self.assertIsNone(gate.request(10.2))

    def test_a_before_stick_also_works(self):
        gate = Gesture()
        gate.observe(ecodes.EV_KEY, ecodes.BTN_SOUTH, 1, 10.0, 0, 255)
        self.assertIsNone(gate.request(10.1))
        gate.observe(ecodes.EV_ABS, ecodes.ABS_Y, 255, 10.2, 0, 255)
        self.assertEqual(gate.request(10.2), "backward")

    def test_button_repeat_never_rearms_after_one_pulse(self):
        gate = Gesture()
        gate.observe(ecodes.EV_ABS, ecodes.ABS_Y, 0, 10.0, 0, 255)
        gate.observe(ecodes.EV_KEY, ecodes.BTN_SOUTH, 1, 10.1, 0, 255)
        self.assertEqual(gate.request(10.1), "forward")
        gate.observe(ecodes.EV_KEY, ecodes.BTN_SOUTH, 2, 10.2, 0, 255)
        self.assertIsNone(gate.request(10.2))

    def test_fresh_press_after_release_allows_new_pulse(self):
        gate = Gesture()
        gate.observe(ecodes.EV_ABS, ecodes.ABS_Y, 0, 10.0, 0, 255)
        gate.observe(ecodes.EV_KEY, ecodes.BTN_SOUTH, 1, 10.0, 0, 255)
        self.assertEqual(gate.request(10.1), "forward")
        gate.observe(ecodes.EV_KEY, ecodes.BTN_SOUTH, 0, 10.2, 0, 255)
        self.assertTrue(gate.abort_pulse)
        gate.observe(ecodes.EV_KEY, ecodes.BTN_SOUTH, 1, 10.3, 0, 255)
        self.assertEqual(gate.request(10.3), "forward")

    def test_neutral_and_arm_timeout_block_motion(self):
        gate = Gesture()
        gate.observe(ecodes.EV_KEY, ecodes.BTN_SOUTH, 1, 10.0, 0, 255)
        self.assertIsNone(gate.request(10.1))
        gate.observe(ecodes.EV_ABS, ecodes.ABS_Y, 0, 14.0, 0, 255)
        self.assertIsNone(gate.request(14.0))

    def test_b_latches_emergency_stop(self):
        gate = Gesture()
        gate.observe(ecodes.EV_ABS, ecodes.ABS_Y, 0, 10.0, 0, 255)
        gate.observe(ecodes.EV_KEY, ecodes.BTN_SOUTH, 1, 10.0, 0, 255)
        gate.observe(ecodes.EV_KEY, ecodes.BTN_EAST, 1, 10.1, 0, 255)
        self.assertIsNone(gate.request(10.2))
        self.assertTrue(gate.emergency)
        self.assertTrue(gate.abort_pulse)

    def test_disconnect_disarms_and_aborts(self):
        gate = Gesture()
        gate.observe(ecodes.EV_ABS, ecodes.ABS_Y, 0, 10.0, 0, 255)
        gate.observe(ecodes.EV_KEY, ecodes.BTN_SOUTH, 1, 10.0, 0, 255)
        gate.cancel_on_disconnect()
        self.assertIsNone(gate.request(10.1))
        self.assertTrue(gate.abort_pulse)
        self.assertFalse(gate.a_down)


if __name__ == "__main__":
    unittest.main()
