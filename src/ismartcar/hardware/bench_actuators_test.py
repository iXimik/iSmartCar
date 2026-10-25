#!/usr/bin/env python3
"""iSmartCar standalone bench test: three servos and two traction motors.

NO GAMEPAD REQUIRED. Raspberry Pi 3B+ / SunFounder PCA9685 (0x40).
Default invocation prints the steps WITHOUT moving any hardware.

Run only on a secured chassis with wheels in the air, known-good batteries,
a physical motor-power disconnect and free servo travel. Servo center values
are provisional. Stop immediately if any linkage binds or heats up.

The --accept-undervoltage option is a deliberate, risky operator override.
A Pi crash may leave motors powered: software STOP is NOT a hardware failsafe.
"""

from __future__ import annotations

import argparse
import re
import signal
import subprocess
import sys
import time

from pca9685 import PCA9685

PCA_ADDRESS = 0x40
PWM_HZ = 60

# Confirmed channel assignments from the existing iSmartCar project.
# Camera center values are provisional; start with small excursions only.
SERVO_TESTS = (
    ("Руль", 0, 450, 15),
    ("Камера: поворот влево/вправо", 14, 450, 15),
    ("Камера: наклон вверх/вниз", 15, 280, 12),
)

MOTOR_GPIO_BOARD = (11, 12, 13, 15)
MOTOR_PWM = (4, 5)
MOTOR_DUTY = 1600                  # ~39% of 4095
MOTOR_SECONDS = 0.35
LOW_POWER_MOTOR_SECONDS = 0.20
SERVO_HOLD_SECONDS = 0.30


def power_flags() -> int:
    result = subprocess.run(
        ["vcgencmd", "get_throttled"], capture_output=True,
        text=True, check=True, timeout=4,
    )
    value = result.stdout.strip()
    if not re.fullmatch(r"throttled=0x[0-9a-fA-F]+", value):
        raise RuntimeError(f"Unknown power status: {value!r}")
    return int(value.partition("=")[2], 16)


def check_pwm_safe(pwm: PCA9685) -> None:
    status = pwm.status()
    print(f"PCA9685: MODE1=0x{status['mode1']:02X}, "
          f"MODE2=0x{status['mode2']:02X}, "
          f"SLEEP={status['sleeping']}", flush=True)
    if not status["sleeping"]:
        raise RuntimeError(
            "PCA9685 already awake: another program may be using the outputs."
        )
    for ch in range(16):
        off_h = pwm.read_register(pwm.LED0_ON_L + ch * 4 + 3)
        if not off_h & 0x10:
            raise RuntimeError(
                f"CH{ch} is not FULL_OFF. No tests started; check other programs."
            )


def servo_sequence(pwm: PCA9685) -> None:
    print("\n=== СЕРВОПРИВОДЫ ===", flush=True)
    for name, ch, center, delta in SERVO_TESTS:
        print(f"CH{ch}: {name}; позиции {center}, {center-delta}, "
              f"{center+delta}, {center}", flush=True)
        try:
            for pulse in (center, center - delta, center + delta, center):
                print(f"  CH{ch} PWM={pulse}", flush=True)
                pwm.write_pwm(ch, 0, pulse)
                time.sleep(SERVO_HOLD_SECONDS)
        finally:
            # Broadcast FULL_OFF to all 16 channels before moving on.
            pwm.all_outputs_off()
        time.sleep(0.2)
    print("Сервоприводы: тест завершён, PWM выключен.", flush=True)


def set_motor_direction(gpio, direction: str) -> None:
    if direction == "forward":
        levels = (gpio.LOW, gpio.HIGH, gpio.LOW, gpio.HIGH)
    elif direction == "backward":
        levels = (gpio.HIGH, gpio.LOW, gpio.HIGH, gpio.LOW)
    else:
        raise ValueError(f"Unknown motor direction: {direction}")
    gpio.output(MOTOR_GPIO_BOARD, levels)


def stop_motors(gpio, pwm: PCA9685) -> None:
    # Disable directional inputs before disabling motor PWM outputs.
    gpio.output(MOTOR_GPIO_BOARD, gpio.LOW)
    pwm.all_outputs_off()


def motor_sequence(pwm: PCA9685, gpio, duration: float) -> None:
    print("\n=== ТЯГОВЫЕ ДВИГАТЕЛИ ===", flush=True)
    print(f"PWM={MOTOR_DUTY}/4095 (~39%), импульс {duration:.2f} с.", flush=True)
    for direction, name in (
        ("forward", "ВПЕРЁД"),
        ("backward", "НАЗАД"),
    ):
        answer = input(
            f"Колёса вывешены? Отключение питания доступно? "
            f"Введите {name} для импульса {name}, либо Enter для пропуска: "
        ).strip().upper()
        if answer != name:
            print(f"ПРОПУСК: {name}", flush=True)
            continue
        # Both direction inputs are LOW until both enable-PWM channels
        # have been configured; only then enable motor direction.
        try:
            gpio.output(MOTOR_GPIO_BOARD, gpio.LOW)
            for channel in MOTOR_PWM:
                pwm.write_pwm(channel, 0, MOTOR_DUTY)
            print(f"МОТОРЫ: {name} (CH4/5 PWM, GPIO direction)", flush=True)
            set_motor_direction(gpio, direction)
            time.sleep(duration)
        finally:
            stop_motors(gpio, pwm)
            print("МОТОРЫ: STOP, обе линии направления LOW, PWM FULL_OFF.",
                  flush=True)
        time.sleep(0.5)


def preview(run_servos: bool, run_motors: bool, low_power: bool) -> None:
    print("iSmartCar BENCH TEST — без геймпада", flush=True)
    print("По умолчанию: ПРЕДПРОСМОТР. Никаких команд GPIO/I2C.", flush=True)
    if run_servos:
        print("Серво: CH0 руль центр 450 +/-15; "
              "CH14 камера PAN центр 450 +/-15; "
              "CH15 камера TILT центр 280 +/-12.", flush=True)
    if run_motors:
        seconds = LOW_POWER_MOTOR_SECONDS if low_power else MOTOR_SECONDS
        print("Моторы: PCA CH4/5, GPIO BOARD 11/12/13/15; "
              f"вперёд и назад ~39% на {seconds:.2f} с каждый.", flush=True)
    print("Реальный запуск: --arm; при подтверждённом пониженном "
          "напряжении потребуется --accept-undervoltage.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", action="store_true",
                        help="Enable physical test after explicit terminal confirmation")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--servos-only", action="store_true",
                       help="Only steering and camera servos")
    group.add_argument("--motors-only", action="store_true",
                       help="Only forward/reverse traction pulses")
    parser.add_argument("--accept-undervoltage", action="store_true",
                        help="Explicit supervised bench override for current Pi undervoltage")
    args = parser.parse_args()

    if args.accept_undervoltage and not args.arm:
        parser.error("--accept-undervoltage requires --arm")
    run_servos = not args.motors_only
    run_motors = not args.servos_only
    preview(run_servos, run_motors, args.accept_undervoltage)
    if not args.arm:
        return 0

    try:
        flags = power_flags()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Cannot verify Raspberry Pi supply: {exc}") from exc
    print(f"Raspberry Pi: throttled=0x{flags:X}", flush=True)
    if flags & 0x5:
        if not args.accept_undervoltage:
            raise RuntimeError(
                "Active undervoltage/throttling. Refusing hardware motion. "
                "Correct Pi 5V supply before --arm, or use an explicit "
                "--accept-undervoltage supervised bench override."
            )
        print("ВНИМАНИЕ: питание Raspberry Pi занижено! "
              "Возможна перезагрузка и потеря программного STOP.", flush=True)

    print("Убедись: колёса НЕ касаются поверхности, механизм руля/камеры "
          "свободен, установлены исправные аккумуляторы и есть физическое "
          "отключение питания моторов.", flush=True)
    if input("Для начала введи СТАРТ (или START): ").strip().upper() not in (
        "СТАРТ", "START"
    ):
        print("Отменено без изменения оборудования.", flush=True)
        return 0

    # The controller must be idle at startup; only our program can own it.
    pwm = PCA9685(address=PCA_ADDRESS)
    gpio = None
    gpio_configured = False
    outputs_touched = False
    try:
        check_pwm_safe(pwm)
        # First make every PCA9685 channel FULL_OFF, then configure motors to
        # direction LOW. The 'ALL_LED_OFF_H' broadcast sets each channel's
        # FULL_OFF bit; each later channel write enables only that channel.
        pwm.all_outputs_off()
        outputs_touched = True

        if run_motors:
            import RPi.GPIO as GPIO
            gpio = GPIO
            gpio.setwarnings(False)
            gpio.setmode(gpio.BOARD)
            # Each pin initialized LOW; never change directions before PWM idle.
            for pin in MOTOR_GPIO_BOARD:
                gpio.setup(pin, gpio.OUT, initial=gpio.LOW)
            gpio_configured = True

        pwm.set_frequency(PWM_HZ)
        print("PCA9685: 60 Гц, выходы готовы.", flush=True)
        if run_servos:
            servo_sequence(pwm)
        if run_motors:
            motor_duration = (
                LOW_POWER_MOTOR_SECONDS if args.accept_undervoltage
                else MOTOR_SECONDS
            )
            motor_sequence(pwm, gpio, motor_duration)
        print("\nИСПЫТАНИЕ ЗАВЕРШЕНО.", flush=True)
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\nАВАРИЙНАЯ ОСТАНОВКА: прервано пользователем.", flush=True)
        return 130
    finally:
        # Never claim that a hardware stop worked unless writes succeed.
        if gpio is not None and gpio_configured:
            try:
                gpio.output(MOTOR_GPIO_BOARD, gpio.LOW)
            except Exception as exc:
                print(f"WARNING: Cannot stop GPIO directions: {exc}",
                      file=sys.stderr, flush=True)
        if outputs_touched:
            try:
                pwm.all_outputs_off()
                print("PCA9685: all outputs FULL_OFF.", flush=True)
            except Exception as exc:
                print(f"WARNING: PWM OFF not confirmed: {exc}",
                      file=sys.stderr, flush=True)
            try:
                pwm.sleep()
                print("PCA9685: SLEEP.", flush=True)
            except Exception as exc:
                print(f"WARNING: PWM SLEEP not confirmed: {exc}",
                      file=sys.stderr, flush=True)
        if gpio is not None and gpio_configured:
            try:
                gpio.cleanup(MOTOR_GPIO_BOARD)
            except Exception as exc:
                print(f"WARNING: GPIO cleanup failed: {exc}",
                      file=sys.stderr, flush=True)
        pwm.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
