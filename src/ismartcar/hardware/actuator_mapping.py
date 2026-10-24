"""Pure translation from normalized G910 commands to SunFounder actuator targets.

IMPORTANT: planning only. This file never opens GPIO/I2C or energizes hardware.
The camera limits below are provisional and deliberately narrower than the
original SunFounder ranges. Calibrate on the actual vehicle before actuation.
"""

from __future__ import annotations

from dataclasses import dataclass

# SunFounder Smart Video Car Kit original channel and pin mapping.
STEERING_CHANNEL = 0
MOTOR0_ENABLE_CHANNEL = 4
MOTOR1_ENABLE_CHANNEL = 5
CAMERA_PAN_CHANNEL = 14
CAMERA_TILT_CHANNEL = 15

MOTOR_DIRECTION_GPIO_BOARD_PINS = (11, 12, 13, 15)
LEGACY_PWM_HZ = 60
MAX_MOTOR_PWM_COUNT = 4000


@dataclass(frozen=True)
class ActuatorPlan:
    """A computed command, NOT something applied to real hardware."""

    steering_channel: int
    steering_count: int
    motor_enable_channels: tuple[int, int]
    motor_pwm_count: int
    motor_direction: str
    camera_pan_channel: int
    camera_pan_count: int
    camera_tilt_channel: int
    camera_tilt_count: int
    pwm_hz: int


def _percent(value: int | float) -> float:
    """Clamp incoming signed percentage to [-100, 100]."""
    return max(-100.0, min(100.0, float(value)))


def make_plan(
    *,
    steering: int | float = 0,
    throttle: int | float = 0,
    camera_pan: int | float = 0,
    camera_tilt: int | float = 0,
    speed_limit: int | float = 30,
    emergency_stop: bool = False,
) -> ActuatorPlan:
    """Compute PWM counts for the original SunFounder layout.

    Axes are the normalized signed percentages already emitted by
    gamepad_dry_run.py. +throttle is forward by convention; the actual
    motor direction GPIO polarities must be calibrated before use.

    The camera's initial center counts come from the original project:
    pan=450, tilt=280 at 60 Hz. Reduced +/-30-count travel is provisional.
    """
    steer = _percent(steering)
    power = _percent(throttle)
    pan = _percent(camera_pan)
    tilt = _percent(camera_tilt)
    speed_cap = max(0.0, min(100.0, float(speed_limit)))

    motor_pwm = round(MAX_MOTOR_PWM_COUNT * abs(power) / 100.0 * speed_cap / 100.0)
    if emergency_stop or power == 0 or speed_cap == 0:
        motor_pwm = 0
        direction = "stop"
    elif power > 0:
        direction = "forward"
    else:
        direction = "reverse"

    return ActuatorPlan(
        steering_channel=STEERING_CHANNEL,
        steering_count=round(450 + 50 * steer / 100.0),
        motor_enable_channels=(MOTOR0_ENABLE_CHANNEL, MOTOR1_ENABLE_CHANNEL),
        motor_pwm_count=motor_pwm,
        motor_direction=direction,
        camera_pan_channel=CAMERA_PAN_CHANNEL,
        camera_pan_count=round(450 + 30 * pan / 100.0),
        camera_tilt_channel=CAMERA_TILT_CHANNEL,
        camera_tilt_count=round(280 + 30 * tilt / 100.0),
        pwm_hz=LEGACY_PWM_HZ,
    )


if __name__ == "__main__":
    print(make_plan())
    print("Preview only: no GPIO/I2C or motors were activated.")
