"""Offline tests. Run from repo root: python3 -m unittest discover -s tests -v"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ismartcar.hardware.actuator_mapping import make_plan


class ActuatorMappingTests(unittest.TestCase):
    def test_neutral(self):
        result = make_plan()
        self.assertEqual(result.steering_count, 450)
        self.assertEqual(result.camera_pan_count, 450)
        self.assertEqual(result.camera_tilt_count, 280)
        self.assertEqual(result.motor_pwm_count, 0)
        self.assertEqual(result.motor_direction, "stop")

    def test_full_steering(self):
        self.assertEqual(make_plan(steering=-100).steering_count, 400)
        self.assertEqual(make_plan(steering=100).steering_count, 500)

    def test_camera_travel_limited(self):
        self.assertEqual(make_plan(camera_pan=-100).camera_pan_count, 420)
        self.assertEqual(make_plan(camera_pan=100).camera_pan_count, 480)
        self.assertEqual(make_plan(camera_tilt=-100).camera_tilt_count, 250)
        self.assertEqual(make_plan(camera_tilt=100).camera_tilt_count, 310)

    def test_throttle_direction_and_limit(self):
        forward = make_plan(throttle=100, speed_limit=30)
        reverse = make_plan(throttle=-100, speed_limit=30)
        self.assertEqual(forward.motor_pwm_count, 1200)
        self.assertEqual(reverse.motor_pwm_count, 1200)
        self.assertEqual(forward.motor_direction, "forward")
        self.assertEqual(reverse.motor_direction, "reverse")

    def test_emergency_stop_removes_drive(self):
        stopped = make_plan(throttle=100, emergency_stop=True)
        self.assertEqual(stopped.motor_pwm_count, 0)
        self.assertEqual(stopped.motor_direction, "stop")

    def test_input_clamped(self):
        self.assertEqual(make_plan(steering=999).steering_count, 500)
        self.assertEqual(make_plan(throttle=-999, speed_limit=999).motor_pwm_count, 4000)


if __name__ == "__main__":
    unittest.main()
