#!/usr/bin/env python3
"""Main iSmartCar G910 controller: steering, camera pan/tilt and traction.

Gamepad mapping (from config/g910.json):
  LEFT X: steering CH0          RIGHT X: camera pan CH14
  LEFT Y: traction forward/back RIGHT Y: camera tilt CH15
  A: hold-to-run traction       B: emergency stop + exit
  X: steering centre            Y: camera centre

Default: DRY RUN (NO I2C/GPIO WRITES). Three armed hardware modes:
  --arm --servos-only : steering and camera, motors NEVER energized
  --arm --motor-pulses: steering/camera + ONE 0.20/0.35s motor pulse
                        per fresh A press (bench mode)
  --arm --drive       : proportional motor throttle while A is held,
                        ONLY with stable Pi power + verified HARDWARE cutoff.

Use --accept-undervoltage only for attended, lifted-wheel bench operation
with --servos-only or --motor-pulses; it is NEVER permitted for --drive.
4.60V was measured at Pi GPIO on this car: software cannot guarantee STOP
on Pi brownout. Never use previously deeply discharged Li-ion cells.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import select
import sys
import time
from pathlib import Path

from evdev import InputDevice, ecodes

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ismartcar.hardware.actuator_mapping import (
    CAMERA_PAN_CHANNEL, CAMERA_TILT_CHANNEL, STEERING_CHANNEL,
)
from ismartcar.hardware.bench_actuators_test import (
    MOTOR_DUTY, MOTOR_GPIO_BOARD, MOTOR_PWM, PWM_HZ,
    check_pwm_safe, power_flags, set_motor_direction,
)
from ismartcar.hardware.pca9685 import PCA9685
from gamepad_dry_run import axis_percent, find_device
from g910_drive import Gesture

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG = REPO_ROOT / "config" / "g910.json"
# Tested servo centres, with deliberately smaller-than-full mechanical travel.
SERVOS = {
    "steering": (STEERING_CHANNEL, 450, 15),
    "camera_pan": (CAMERA_PAN_CHANNEL, 450, 15),
    "camera_tilt": (CAMERA_TILT_CHANNEL, 280, 12),
}
MIN_DRIVE_THROTTLE = 25
DRIVE_MAX_DUTY = MOTOR_DUTY  # tested ~39%; throttle scales only downward.
PULSE_NORMAL = 0.35
PULSE_LOW_VOLTAGE = 0.20
SESSION_SECONDS = 75.0
INPUT_TIMEOUT = 0.65  # Stop continuous drive without fresh gamepad events.
SERVO_INTERVAL = 0.04
SERVO_PULSE_SECONDS = 0.18  # Short one-at-a-time positioning pulses, no continuous holding
POLL_INTERVAL = 0.015
POWER_CHECK_INTERVAL = 1.0
LOCKFILE = "/run/lock/ismartcar-actuators.lock"


def servo_selected(name: str, servo_filter: str | None) -> bool:
    """Choose one actuator for cautious, isolated servo bench checks."""
    return servo_filter is None or name == servo_filter


def servo_counts(name: str, percent: int) -> int:
    """Conservative channel positions; pure function."""
    _, centre, travel = SERVOS[name]
    percent = max(-100, min(100, int(percent)))
    return centre + round(travel * percent / 100)


def traction_command(throttle_percent: int) -> tuple[str, int] | None:
    """Proportional PWM from tested 1600-count maximum; pure function."""
    throttle_percent = max(-100, min(100, int(throttle_percent)))
    if abs(throttle_percent) < MIN_DRIVE_THROTTLE:
        return None
    pwm_count = round(DRIVE_MAX_DUTY * abs(throttle_percent) / 100)
    return ("forward" if throttle_percent > 0 else "backward", pwm_count)


def stop_traction(gpio, pwm: PCA9685) -> None:
    """Disable BOTH motor directions and motor channels ONLY; preserve servos."""
    try:
        gpio.output(MOTOR_GPIO_BOARD, gpio.LOW)
    finally:
        # Distinct from all_outputs_off(): do not interrupt camera/steering.
        for channel in MOTOR_PWM:
            pwm.write_pwm(channel, 0, 0)


def write_traction(gpio, pwm: PCA9685, direction: str, duty: int) -> None:
    """Avoid live reversal: drop direction & PWM before energizing new way."""
    stop_traction(gpio, pwm)
    for channel in MOTOR_PWM:
        pwm.write_pwm(channel, 0, duty)
    set_motor_direction(gpio, direction)


def read_axis(device: InputDevice, event_name: str, invert: bool, deadzone: float) -> int:
    code = getattr(ecodes, event_name)
    info = device.absinfo(code)
    if info is None:
        raise RuntimeError(f"Gamepad does not have axis {event_name}")
    return axis_percent(info.value, info.min, info.max, deadzone, invert)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", action="store_true", help="Enable hardware outputs")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--servos-only", action="store_true",
                      help="Allow only steering and camera motion")
    mode.add_argument("--motor-pulses", action="store_true",
                      help="Allow one short motor pulse per fresh press of A")
    mode.add_argument("--drive", action="store_true",
                      help="Proportional traction while A is held; stable power only")
    parser.add_argument("--hardware-cutoff-confirmed", action="store_true",
                        help="Confirm independent PHYSICAL failsafe/cutoff installed and tested")
    parser.add_argument("--accept-undervoltage", action="store_true",
                        help="Risky lifted-wheel bench override; never works with --drive")
    parser.add_argument(
        "--servo-filter", choices=tuple(SERVOS),
        help="Enable ONLY one servo output for isolated --servos-only checks",
    )
    parser.add_argument("--device", help="Optional /dev/input/eventX override")
    args = parser.parse_args()

    if args.arm and not (args.servos_only or args.motor_pulses or args.drive):
        parser.error("--arm requires one of --servos-only, --motor-pulses, --drive")
    if (args.accept_undervoltage or args.hardware_cutoff_confirmed) and not args.arm:
        parser.error("Safety confirmation flags require --arm")
    if args.drive and args.accept_undervoltage:
        parser.error("--drive NEVER permits --accept-undervoltage")
    if args.drive and not args.hardware_cutoff_confirmed:
        parser.error("--drive requires --hardware-cutoff-confirmed")
    if args.hardware_cutoff_confirmed and not args.drive:
        parser.error("--hardware-cutoff-confirmed is only valid with --drive")
    if args.servo_filter and not args.servos_only:
        parser.error("--servo-filter requires --servos-only")

    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    deadzone = float(config["deadzone"])
    if not 0 <= deadzone < 1:
        raise RuntimeError("Invalid deadzone in g910.json")
    axis_specs = {
        getattr(ecodes, spec["event"]): (name, bool(spec["invert"]))
        for name, spec in config["axes"].items()
    }
    axes = {name: 0 for name in SERVOS}
    axes["throttle"] = 0

    print("iSmartCar MAIN | G910", flush=True)
    print("LEFT: steer + throttle | RIGHT: camera pan + tilt", flush=True)
    print("A: motor deadman/short pulse | B: emergency STOP | "
          "X: centre steering | Y: centre camera", flush=True)
    if args.servo_filter:
        print(f"ISOLATED SERVO: only {args.servo_filter} "
              f"(CH{SERVOS[args.servo_filter][0]}); other channels disabled.",
              flush=True)
    if not args.arm:
        print("DRY RUN ONLY: absolutely no I2C/GPIO output.", flush=True)
    else:
        flags = power_flags()
        print(f"Pi power flags: 0x{flags:X}", flush=True)
        if flags & 0x5:
            if args.drive or not args.accept_undervoltage:
                raise RuntimeError(
                    "Pi undervoltage/throttling detected: hardware mode blocked. "
                    "Only supervised --servos-only or --motor-pulses with "
                    "--accept-undervoltage can override this for bench tests."
                )
            print("WARNING: ~4.60V Pi supply. Brownout may defeat software STOP. "
                  "No free-driving. Use physical motor power disconnect.", flush=True)
        if args.drive:
            print("DRIVE MODE: continuous traction requires independently verified "
                  "hardware power cutoff, stable Pi 5V and wheels raised for "
                  "the first bench trial.", flush=True)
        else:
            print("BENCH MODE: secure chassis, lift wheels, make sure servos have "
                  "clearance and motor battery disconnect is accessible.", flush=True)
        if input("Type СТАРТ to allow hardware outputs: ").strip().upper() != "СТАРТ":
            print("Aborted without touching hardware.", flush=True)
            return 0

    # Preview still uses actual device and same mapping as hardware.
    device = InputDevice(args.device or find_device(config["device_name"]))
    lock = None
    pwm = None
    gpio = None
    gpio_ready = False
    touched_pwm = False
    try:
        # Initial inputs should be neutral, but not necessarily exactly 128.
        for code, (name, invert) in axis_specs.items():
            info = device.absinfo(code)
            if info is None:
                raise RuntimeError(f"Missing {ecodes.bytype[ecodes.EV_ABS].get(code, code)}")
            axes[name] = axis_percent(info.value, info.min, info.max,
                                      deadzone, invert)
        if any(abs(value) > 12 for value in axes.values()):
            raise RuntimeError("Center BOTH sticks before starting")
        if ecodes.BTN_SOUTH in device.active_keys():
            raise RuntimeError("Release A before starting: fresh press is required")

        if args.arm:
            # Only one armed controller. Other legacy hardware scripts must be stopped.
            lock = open(LOCKFILE, "w")
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another main actuator controller is already active") from exc

            pwm = PCA9685()
            check_pwm_safe(pwm)
            pwm.all_outputs_off()
            touched_pwm = True
            if not args.servos_only:
                import RPi.GPIO as GPIO
                gpio = GPIO
                gpio.setwarnings(False)
                gpio.setmode(gpio.BOARD)
                for pin in MOTOR_GPIO_BOARD:
                    gpio.setup(pin, gpio.OUT, initial=gpio.LOW)
                gpio_ready = True
            pwm.set_frequency(PWM_HZ)
            # CRITICAL: NEVER move servos on initialisation. Previous version
            # held all 3 channels continuously, causing extreme travel and
            # apparent Pi/Bluetooth power faults on actual hardware.
            print("SAFE START: servo PWM outputs remain OFF until explicit "
                  "gamepad stick/X/Y events.", flush=True)

        gate = Gesture()
        last_pwm = {name: servo_counts(name, 0) for name in SERVOS}
        axis_dirty = False
        last_servo_update = 0.0
        pending_servos: dict[str, int] = {}
        active_servo_channel: int | None = None
        active_servo_until = 0.0
        active_drive = None
        running_until = 0.0
        last_event = time.monotonic()
        next_power_check = last_event + POWER_CHECK_INTERVAL
        session_deadline = last_event + SESSION_SECONDS
        print(f"READY: {device.name} ({device.path}). "
              f"{'physical hardware enabled' if args.arm else 'preview only'}. "
              "B = STOP.", flush=True)

        while time.monotonic() < session_deadline:
            now = time.monotonic()
            # Timeout precedes event handling; loops are not dependent on new input.
            if running_until and now >= running_until:
                if args.arm and gpio_ready:
                    stop_traction(gpio, pwm)
                running_until = 0.0
                active_drive = None
                print("STOP: motor pulse ended", flush=True)

            if active_servo_channel is not None and now >= active_servo_until:
                # Disable the servo channel, not just the driving motor.
                # A stuck servo can otherwise draw current indefinitely.
                pwm.write_pwm(active_servo_channel, 0, 0)
                print(f"SERVO CH{active_servo_channel}: PWM OFF after "
                      f"{SERVO_PULSE_SECONDS:.2f}s", flush=True)
                active_servo_channel = None

            if (args.drive and active_drive and now - last_event > INPUT_TIMEOUT):
                stop_traction(gpio, pwm)
                print("STOP: no fresh gamepad events (watchdog timeout); "
                      "release A before restarting", flush=True)
                active_drive = None
                gate.cancel_on_disconnect()

            if args.arm and now >= next_power_check:
                next_power_check = now + POWER_CHECK_INTERVAL
                if args.drive and power_flags() & 0x5:
                    if gpio_ready:
                        stop_traction(gpio, pwm)
                    print("STOP: Pi undervoltage detected while driving", flush=True)
                    break

            readable, _, _ = select.select([device], [], [], POLL_INTERVAL)
            if readable:
                input_desync = False
                try:
                    batch = device.read()
                except OSError as exc:
                    print(f"STOP: gamepad input lost: {exc}", flush=True)
                    break
                for event in batch:
                    if event.type == ecodes.EV_SYN and event.code == ecodes.SYN_DROPPED:
                        print("STOP: gamepad event desynchronization", flush=True)
                        gate.cancel_on_disconnect()
                        input_desync = True
                        break
                    # Kernel-generated EV_KEY=2 auto-repeat may continue even
                    # when the physical Bluetooth link has stopped. It MUST
                    # NOT refresh the drive input watchdog.
                    if (event.type == ecodes.EV_ABS or
                            (event.type == ecodes.EV_KEY and event.value in (0, 1))):
                        last_event = time.monotonic()
                    if event.type == ecodes.EV_ABS and event.code in axis_specs:
                        name, invert = axis_specs[event.code]
                        info = device.absinfo(event.code)
                        value = axis_percent(event.value, info.min, info.max,
                                             deadzone, invert)
                        if axes[name] != value:
                            axes[name] = value
                            if name in SERVOS:
                                axis_dirty = True
                        if event.code == ecodes.ABS_Y:
                            gate.observe(event.type, event.code, event.value,
                                         time.monotonic(), info.min, info.max)
                    elif event.type == ecodes.EV_KEY:
                        # A and B are shared with the tested g910_drive state machine.
                        gate.observe(event.type, event.code, event.value,
                                     time.monotonic(), 0, 255)
                        if event.value == 1 and event.code == ecodes.BTN_WEST:
                            axes["steering"] = 0
                            axis_dirty = True
                            print("CENTER: steering", flush=True)
                        elif event.value == 1 and event.code == ecodes.BTN_NORTH:
                            axes["camera_pan"] = axes["camera_tilt"] = 0
                            axis_dirty = True
                            print("CENTER: camera", flush=True)
                if input_desync:
                    break
                if gate.emergency:
                    print("STOP: B pressed (emergency)", flush=True)
                    break
                if gate.abort_pulse and active_drive is not None:
                    if args.arm and gpio_ready:
                        stop_traction(gpio, pwm)
                    active_drive = None
                    running_until = 0.0
                    print("STOP: A released or throttle neutral", flush=True)
                gate.abort_pulse = False

            # Servo targets are QUEUED, not held. Never energize multiple
            # servos at once and never issue any startup centering commands.
            if axis_dirty and now - last_servo_update >= SERVO_INTERVAL:
                for name, (ch, _, _) in SERVOS.items():
                    if not servo_selected(name, args.servo_filter):
                        continue
                    target = servo_counts(name, axes[name])
                    if last_pwm[name] != target:
                        if args.arm:
                            pending_servos[name] = target
                        else:
                            print(f"SERVO CH{ch} {name}: {target} (PREVIEW)",
                                  flush=True)
                        last_pwm[name] = target
                last_servo_update = now
                axis_dirty = False

            if args.motor_pulses or not args.arm:
                # Preview mode uses pulse-style A gating and never energizes motors.
                if not active_drive and active_servo_channel is None:
                    direction = gate.request(now)
                    if direction is not None:
                        if args.arm and gpio_ready:
                            # Same known-good PWM counts and direction GPIO pattern
                            # as bench_actuators_test.py and g910_drive.py.
                            write_traction(gpio, pwm, direction, MOTOR_DUTY)
                            running_until = time.monotonic() + (
                                PULSE_LOW_VOLTAGE if args.accept_undervoltage
                                else PULSE_NORMAL
                            )
                            active_drive = direction
                            print(f"PULSE {direction}: PWM {MOTOR_DUTY}/4095", flush=True)
                        else:
                            print(f"WOULD PULSE {direction}: "
                                  f"throttle={gate.throttle:+d}% (PREVIEW)",
                                  flush=True)
                if active_drive is not None and (
                    not gate.a_down or abs(gate.throttle) < MIN_DRIVE_THROTTLE or
                    (gate.throttle > 0) != (active_drive == "forward")
                ):
                    if args.arm and gpio_ready:
                        stop_traction(gpio, pwm)
                    active_drive = None
                    running_until = 0.0
                    print("STOP: pulse deadman/direction changed", flush=True)
            elif args.drive:
                # Fresh A press must precede continuous drive; drive scales with stick.
                if gate.emergency or not gate.a_down or gate.consumed and active_drive is None:
                    desired = None
                else:
                    desired = traction_command(gate.throttle)
                # Note: once A is down, request() consumes the arm latch once.
                if active_drive is None and desired is not None:
                    allowed = gate.request(now)
                    if allowed is None:
                        desired = None
                if desired is None:
                    if active_drive:
                        stop_traction(gpio, pwm)
                        active_drive = None
                        print("STOP: motor deadman", flush=True)
                else:
                    direction, duty = desired
                    if active_drive and direction != active_drive[0]:
                        # Never reverse direction under a held A: stop and
                        # require a NEW A press after a deliberate release.
                        stop_traction(gpio, pwm)
                        active_drive = None
                        gate.cancel_on_disconnect()
                        print("STOP: direction reversal; release A to re-arm", flush=True)
                    elif active_drive != desired:
                        if active_drive is None:
                            write_traction(gpio, pwm, direction, duty)
                        else:
                            # Same direction: update enable duty without
                            # bouncing motor direction GPIO on each axis event.
                            for ch in MOTOR_PWM:
                                pwm.write_pwm(ch, 0, duty)
                        active_drive = desired
                        print(f"DRIVE: {direction} PWM={duty}", flush=True)

            # One servo at a time, never during active motor traction. We
            # allow at most 0.18s of PWM for each requested position; no
            # servo is continuously powered while awaiting joystick input.
            if (args.arm and active_servo_channel is None and
                    active_drive is None and pending_servos):
                name = next(iter(pending_servos))
                target = pending_servos.pop(name)
                ch = SERVOS[name][0]
                pwm.write_pwm(ch, 0, target)
                active_servo_channel = ch
                active_servo_until = time.monotonic() + SERVO_PULSE_SECONDS
                print(f"SERVO CH{ch} {name}: PWM={target}, brief pulse",
                      flush=True)

        print("Main controller session ended.", flush=True)
        return 0
    except KeyboardInterrupt:
        print("\nSTOP: Ctrl+C", flush=True)
        return 130
    finally:
        # STOP always attempts to cut motor directions and PWM, then servos.
        if gpio_ready:
            try:
                stop_traction(gpio, pwm)
            except Exception as exc:
                print(f"WARNING: motor STOP failed: {exc}; cut motor power!",
                      file=sys.stderr, flush=True)
        if pwm is not None:
            if touched_pwm:
                try:
                    pwm.all_outputs_off()
                    pwm.sleep()
                    print("PCA9685 FULL_OFF and SLEEP.", flush=True)
                except Exception as exc:
                    print(f"WARNING: PCA shutdown not confirmed: {exc}",
                          file=sys.stderr, flush=True)
            pwm.close()
        if gpio_ready:
            try:
                gpio.cleanup(MOTOR_GPIO_BOARD)
            except Exception as exc:
                print(f"WARNING: GPIO cleanup failed: {exc}",
                      file=sys.stderr, flush=True)
        if lock is not None:
            lock.close()
        device.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
