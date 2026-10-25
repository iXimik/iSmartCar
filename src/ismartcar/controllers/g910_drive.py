#!/usr/bin/env python3
"""G910 -> SunFounder traction motors: short, bounded bench pulses.

NO STEERING OR CAMERA.  DEFAULT = input-only preview: no GPIO/I2C writes.
Real hardware mode: --arm (and --accept-undervoltage if Raspberry Pi reports
live under-voltage). The physical wheels MUST be lifted and secured.

Hold A and move left stick up/down, or move stick first and press A. Either
order works. ONE pulse per NEW A press; release and press A for another.
B emergency-stops/ends test. A release or stick neutral stops an active pulse.
Motor direction pins are Raspberry Pi GPIO.BOARD 11/12/13/15; PCA9685 PWM
channels are CH4/CH5 at 60 Hz, taken from the verified bench motor test.

Software STOP cannot recover from a Pi crash or cut off the motor battery.
"""

from __future__ import annotations

import argparse
import select
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from evdev import InputDevice, ecodes

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ismartcar.hardware.bench_actuators_test import (
    MOTOR_DUTY, MOTOR_GPIO_BOARD, MOTOR_PWM, PWM_HZ, check_pwm_safe,
    power_flags, set_motor_direction, stop_motors,
)
from ismartcar.hardware.pca9685 import PCA9685
from gamepad_dry_run import axis_percent, find_device

ARM_WINDOW_SECONDS = 3.0
MIN_THROTTLE_PERCENT = 25
SESSION_SECONDS = 45.0
PULSE_SECONDS = 0.35
LOW_VOLTAGE_PULSE_SECONDS = 0.20
POLL_SECONDS = 0.01


@dataclass
class Gesture:
    """Pure, stateful gesture decoder; no writes to motors or GPIO."""
    throttle: int = 0
    a_down: bool = False
    consumed: bool = False
    arm_until: float = 0.0
    emergency: bool = False
    abort_pulse: bool = False

    def observe(self, event_type: int, code: int, value: int, now: float,
                axis_min: int, axis_max: int) -> None:
        if event_type == ecodes.EV_KEY and code == ecodes.BTN_EAST:
            if value == 1:
                self.emergency = True
                self.abort_pulse = True
            return
        if event_type == ecodes.EV_KEY and code == ecodes.BTN_SOUTH:
            if value == 1 and not self.a_down:
                # The operator must produce a fresh A press.
                self.a_down = True
                self.consumed = False
                self.arm_until = now + ARM_WINDOW_SECONDS
            elif value == 0:
                self.a_down = False
                self.arm_until = 0.0
                self.abort_pulse = True
            # Linux EV_KEY value=2 means held-key auto-repeat: NEVER re-arm.
            return
        if event_type == ecodes.EV_ABS and code == ecodes.ABS_Y:
            self.throttle = axis_percent(value, axis_min, axis_max, 0.10, True)
            if abs(self.throttle) < MIN_THROTTLE_PERCENT:
                self.abort_pulse = True

    def request(self, now: float) -> str | None:
        if (self.emergency or not self.a_down or self.consumed or
                now >= self.arm_until or
                abs(self.throttle) < MIN_THROTTLE_PERCENT):
            return None
        self.consumed = True
        return "forward" if self.throttle > 0 else "backward"

    def cancel_on_disconnect(self) -> None:
        self.a_down = False
        self.arm_until = 0.0
        self.consumed = True
        self.abort_pulse = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", action="store_true",
                        help="Enable one bounded traction pulse per press of A")
    parser.add_argument("--accept-undervoltage", action="store_true",
                        help="Permit supervised lifted-wheel test with Pi undervoltage")
    parser.add_argument("--device", help="Optional /dev/input/eventX override")
    args = parser.parse_args()
    if args.accept_undervoltage and not args.arm:
        parser.error("--accept-undervoltage requires --arm")

    print("G910 MOTOR CONTROL | A+left stick = ONE pulse; B = STOP.", flush=True)
    duration = (LOW_VOLTAGE_PULSE_SECONDS
                if args.accept_undervoltage else PULSE_SECONDS)
    print(f"PWM CH4/5 = {MOTOR_DUTY}/4095 (~39%); "
          f"max pulse {duration:.2f}s; max session {SESSION_SECONDS:.0f}s.",
          flush=True)

    if args.arm:
        flags = power_flags()
        print(f"Raspberry Pi power status: 0x{flags:X}", flush=True)
        if flags & 0x5 and not args.accept_undervoltage:
            raise RuntimeError("Current Pi undervoltage: motor test blocked. "
                               "For a short attended bench test, use "
                               "--arm --accept-undervoltage.")
        if flags & 0x5:
            print("WARNING: Pi supply measured 4.60V. A Pi reset can leave "
                  "motor power active! Wheels MUST be lifted; physical "
                  "motor-battery cut-off must be accessible.", flush=True)
        print("Confirm: secured wheels off ground, good motor batteries, "
              "accessible physical motor-power switch.", flush=True)
        if input("Enter СТАРТ to enable motor hardware: ").strip().upper() != "СТАРТ":
            print("Aborted; no motor hardware changed.", flush=True)
            return 0
    else:
        print("INPUT-ONLY PREVIEW: no I2C, GPIO or motor commands.", flush=True)

    device_path = args.device or find_device("Gamepad")
    device = InputDevice(device_path)
    pwm: PCA9685 | None = None
    gpio = None
    gpio_ready = False
    outputs_touched = False
    try:
        axis = device.absinfo(ecodes.ABS_Y)
        if axis is None:
            raise RuntimeError("Gamepad ABS_Y axis unavailable")
        initial_throttle = axis_percent(axis.value, axis.min, axis.max, 0.10, True)
        if abs(initial_throttle) > 10:
            raise RuntimeError("Release LEFT stick to center and restart.")
        if ecodes.BTN_SOUTH in device.active_keys():
            raise RuntimeError("Release A before starting: fresh press required.")

        if args.arm:
            pwm = PCA9685()
            check_pwm_safe(pwm)
            pwm.all_outputs_off()
            outputs_touched = True
            import RPi.GPIO as GPIO
            gpio = GPIO
            gpio.setwarnings(False)
            gpio.setmode(gpio.BOARD)
            for pin in MOTOR_GPIO_BOARD:
                gpio.setup(pin, gpio.OUT, initial=gpio.LOW)
            gpio_ready = True
            pwm.set_frequency(PWM_HZ)

        gate = Gesture()
        gate.throttle = initial_throttle
        running_until = 0.0
        active_direction = None
        session_end = time.monotonic() + SESSION_SECONDS
        print(f"READY: {device.name} ({device_path}). "
              "Hold A THEN move left stick, OR stick THEN A. "
              "Release A before next pulse. B ends.", flush=True)

        while time.monotonic() < session_end:
            now = time.monotonic()
            if running_until and now >= running_until:
                if args.arm:
                    stop_motors(gpio, pwm)
                running_until = 0.0
                active_direction = None
                print("STOP: pulse timeout", flush=True)

            readable, _, _ = select.select([device], [], [], POLL_SECONDS)
            if readable:
                try:
                    batch = device.read()
                except OSError as exc:
                    print(f"INPUT LOST: {exc}; stopping", flush=True)
                    gate.cancel_on_disconnect()
                    break
                for event in batch:
                    if (event.type == ecodes.EV_SYN and
                            event.code == ecodes.SYN_DROPPED):
                        print("INPUT DESYNC: stopping; restart test", flush=True)
                        gate.cancel_on_disconnect()
                        break
                    gate.observe(event.type, event.code, event.value,
                                 time.monotonic(), axis.min, axis.max)
                if gate.emergency:
                    print("STOP: B pressed; test ended", flush=True)
                    break
                if gate.abort_pulse and running_until:
                    if args.arm:
                        stop_motors(gpio, pwm)
                    running_until = 0.0
                    active_direction = None
                    print("STOP: A released or stick centered", flush=True)
                gate.abort_pulse = False

                if not running_until:
                    direction = gate.request(time.monotonic())
                    if direction is not None:
                        if not args.arm:
                            print(f"WOULD PULSE {direction}: "
                                  f"throttle={gate.throttle:+d}% (NO OUTPUT)",
                                  flush=True)
                            continue
                        # Direction LOW while programming PWM, then direction HIGH.
                        gpio.output(MOTOR_GPIO_BOARD, gpio.LOW)
                        for ch in MOTOR_PWM:
                            pwm.write_pwm(ch, 0, MOTOR_DUTY)
                        set_motor_direction(gpio, direction)
                        running_until = time.monotonic() + duration
                        active_direction = direction
                        print(f"PULSE {direction}: {duration:.2f}s "
                              f"at PWM {MOTOR_DUTY}/4095", flush=True)
            if running_until and active_direction is not None:
                if (not gate.a_down or gate.emergency or
                        abs(gate.throttle) < MIN_THROTTLE_PERCENT or
                        (gate.throttle > 0) != (active_direction == "forward")):
                    if args.arm:
                        stop_motors(gpio, pwm)
                    running_until = 0.0
                    active_direction = None
                    print("STOP: deadman/throttle/direction changed", flush=True)

        print("Test session finished.", flush=True)
        return 0
    except KeyboardInterrupt:
        print("\nSTOP: Ctrl+C", flush=True)
        return 130
    finally:
        # At shutdown always attempt PWM OFF even if a GPIO write failed.
        if gpio_ready and pwm is not None:
            try:
                stop_motors(gpio, pwm)
            except Exception as exc:
                print(f"WARNING: motor STOP not confirmed: {exc}; "
                      "disconnect motor battery!", file=sys.stderr, flush=True)
        if pwm is not None:
            if outputs_touched:
                try:
                    pwm.all_outputs_off()
                    pwm.sleep()
                    print("PCA9685: all outputs OFF and sleeping.", flush=True)
                except Exception as exc:
                    print(f"WARNING: PCA9685 OFF not confirmed: {exc}; "
                          "disconnect motor battery!", file=sys.stderr, flush=True)
            pwm.close()
        if gpio_ready:
            try:
                gpio.cleanup(MOTOR_GPIO_BOARD)
            except Exception as exc:
                print(f"WARNING: GPIO cleanup failed: {exc}",
                      file=sys.stderr, flush=True)
        device.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
