#!/usr/bin/env python3
"""Short, operator-held, traction-motor-only test for SunFounder / G910.

DEFAULT: preview only.  --arm enables the Raspberry Pi GPIO and PCA9685.
Move the LEFT stick forward/back and press/hold A for a short wheel pulse.
Each A press produces at most ONE 0.7 s pulse; release A before another.
B and Ctrl+C immediately request a stop. No steering/camera outputs enabled.

WARNING: A Pi brownout/OS crash cannot guarantee a software motor stop.
Test with wheels LIFTED, accessible motor-power disconnect, separate motor
power, and no load. Verify wiring before --arm.
"""

from __future__ import annotations

import argparse
import select
import subprocess
import sys
import time
from pathlib import Path

from evdev import InputDevice, ecodes

# Allow invocation using absolute script path (no package installation).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ismartcar.hardware.pca9685 import PCA9685

from gamepad_dry_run import axis_percent, find_device


# Verified against original SunFounder server/motor.py, GPIO.BOARD numbering.
MOTOR0_A, MOTOR0_B = 11, 12
MOTOR1_A, MOTOR1_B = 13, 15
DIRECTION_PINS = (MOTOR0_A, MOTOR0_B, MOTOR1_A, MOTOR1_B)
PWM_CHANNELS = (4, 5)
PWM_HZ = 60
PULSE_SECONDS = 0.7
MAX_DUTY_PERCENT = 30
MIN_THROTTLE_PERCENT = 25
MAX_SESSION_SECONDS = 90


def get_power_flags() -> int | None:
    try:
        result = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True,
            text=True, check=True, timeout=3,
        )
        value = result.stdout.strip()
        if not value.startswith("throttled=0x"):
            return None
        return int(value.split("=", 1)[1], 16)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def set_motor_direction(gpio, forward: bool) -> None:
    """Original SunFounder config: forward0 = True, forward1 = True."""
    if forward:
        # Original 'True': A LOW, B HIGH for both motor channels.
        gpio.output(DIRECTION_PINS, (gpio.LOW, gpio.HIGH, gpio.LOW, gpio.HIGH))
    else:
        gpio.output(DIRECTION_PINS, (gpio.HIGH, gpio.LOW, gpio.HIGH, gpio.LOW))


def motor_stop(gpio, pwm: PCA9685) -> None:
    """Direction inputs inactive first, then all PCA9685 outputs full-off."""
    gpio.output(DIRECTION_PINS, gpio.LOW)
    pwm.all_outputs_off()


def get_throttle(device: InputDevice) -> int:
    info = device.absinfo(ecodes.ABS_Y)
    if info is None:
        raise RuntimeError("ABS_Y not available on selected gamepad")
    return axis_percent(info.value, info.min, info.max, 0.10, True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", action="store_true",
                        help="Enable one-pulse traction test; wheels must be lifted")
    parser.add_argument("--device", help="Override automatic Gamepad device discovery")
    args = parser.parse_args()

    print("SunFounder G910 motor test: BOARD GPIO 11/12/13/15, "
          "PCA9685 CH4/CH5 @ 60 Hz.", flush=True)
    print(f"Max {MAX_DUTY_PERCENT}% PWM, pulse <= {PULSE_SECONDS:.1f}s; "
          "A = hold to trigger, B = emergency stop.", flush=True)

    if not args.arm:
        print("PREVIEW ONLY: no GPIO or I2C writes. "
              "Use --arm only after checking motor power and lifting wheels.")
        return

    flags = get_power_flags()
    if flags is None:
        raise RuntimeError("Cannot verify Raspberry Pi power status")
    print(f"Power status: 0x{flags:X}", flush=True)
    if flags & 0x5:
        raise RuntimeError(
            "Current Pi undervoltage/throttling detected; refusing motion. "
            "Run with the working regulated 5V supply, not a brownout."
        )
    if flags & 0x50000:
        print("Note: power problems occurred earlier this boot.", flush=True)

    # Do not import/control GPIO at all unless explicitly armed.
    import RPi.GPIO as GPIO

    device_path = args.device or find_device("Gamepad")
    device = InputDevice(device_path)
    gpio_initialized = False
    pwm: PCA9685 | None = None
    try:
        print(f"Controller: {device.name} ({device_path})", flush=True)
        initial_throttle = get_throttle(device)
        if abs(initial_throttle) > 10:
            raise RuntimeError(
                "Left stick not centered at start. Release it and restart."
            )
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BOARD)
        # Force LOW *as outputs are configured*, before enabling motor power.
        for pin in DIRECTION_PINS:
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)
        gpio_initialized = True

        pwm = PCA9685()
        status = pwm.status()
        if not status["sleeping"]:
            raise RuntimeError("PWM chip is awake, refusing to interrupt another process")
        # Refuse if any of the 16 outputs may already be active.
        for channel in range(16):
            reg = pwm.LED0_ON_L + 4 * channel + 3
            if not (pwm.read_register(reg) & 0x10):
                raise RuntimeError(f"PCA9685 CH{channel} was not FULL_OFF")
        pwm.all_outputs_off()
        pwm.set_frequency(PWM_HZ)
        motor_stop(GPIO, pwm)

        print("READY (wheels MUST be in the air): move LEFT stick up/down, "
              "hold A to make one short pulse; release A to reset. "
              "B = STOP and EXIT; Ctrl+C = stop.", flush=True)
        throttle = 0
        a_pressed = False
        fired_this_press = False
        running_until = 0.0
        session_end = time.monotonic() + MAX_SESSION_SECONDS
        while time.monotonic() < session_end:
            now = time.monotonic()
            if running_until and now >= running_until:
                motor_stop(GPIO, pwm)
                running_until = 0.0
                print("STOP: pulse complete", flush=True)

            readable, _, _ = select.select([device], [], [], 0.02)
            if readable:
                for event in device.read():
                    if event.type == ecodes.EV_ABS and event.code == ecodes.ABS_Y:
                        info = device.absinfo(ecodes.ABS_Y)
                        throttle = axis_percent(
                            event.value, info.min, info.max, 0.10, True
                        )
                        if running_until and (
                            abs(throttle) < MIN_THROTTLE_PERCENT
                            or (throttle > 0) != (active_direction > 0)
                        ):
                            motor_stop(GPIO, pwm)
                            running_until = 0.0
                            print("STOP: stick centered/direction changed", flush=True)
                    elif event.type == ecodes.EV_KEY and event.code == ecodes.BTN_SOUTH:
                        a_pressed = event.value != 0
                        if not a_pressed:
                            fired_this_press = False
                            if running_until:
                                motor_stop(GPIO, pwm)
                                running_until = 0.0
                                print("STOP: A released", flush=True)
                    elif event.type == ecodes.EV_KEY and event.code == ecodes.BTN_EAST:
                        if event.value == 1:
                            print("STOP: B pressed; ending test", flush=True)
                            return

            # Arming requires a *fresh* A button press. One pulse per press.
            if a_pressed and not fired_this_press and abs(throttle) >= MIN_THROTTLE_PERCENT:
                fired_this_press = True
                fresh_flags = get_power_flags()
                if fresh_flags is None or fresh_flags & 0x5:
                    print(f"BLOCKED: power status {fresh_flags}, no motor command.",
                          flush=True)
                    continue
                active_direction = throttle
                duty = round(4095 * MAX_DUTY_PERCENT / 100)
                # PWM prepared while all direction inputs are still LOW.
                for channel in PWM_CHANNELS:
                    pwm.write_pwm(channel, 0, duty)
                set_motor_direction(GPIO, forward=throttle > 0)
                running_until = time.monotonic() + PULSE_SECONDS
                label = "forward" if throttle > 0 else "reverse"
                print(f"PULSE {label}: {MAX_DUTY_PERCENT}% PWM for max "
                      f"{PULSE_SECONDS:.1f}s", flush=True)
        print("STOP: test session time limit reached", flush=True)
    except KeyboardInterrupt:
        print("\nSTOP: Ctrl+C", flush=True)
    finally:
        # Shutdown is attempted on every normal/error exit. Still not a
        # substitute for a physical disconnect on Pi power failure.
        if gpio_initialized:
            try:
                GPIO.output(DIRECTION_PINS, GPIO.LOW)
            except Exception as error:
                print(f"GPIO stop failed: {error}", file=sys.stderr)
        if pwm is not None:
            try:
                pwm.all_outputs_off()
                pwm.sleep()
            except Exception as error:
                print(f"PWM stop failed: {error}", file=sys.stderr)
            finally:
                pwm.close()
        if gpio_initialized:
            GPIO.cleanup(DIRECTION_PINS)
        device.close()
        print("Motor test finished; STOP commanded, PWM sleeping.", flush=True)


if __name__ == "__main__":
    main()
