"""Minimal PCA9685 driver for iSmartCar (Python 3.11+).

Importing this module and constructing PCA9685 do not change PWM outputs.
The only executable self-test is read-only.
"""

from __future__ import annotations

import time

try:
    import smbus2 as smbus
except ImportError:
    import smbus


class PCA9685:
    MODE1 = 0x00
    MODE2 = 0x01
    LED0_ON_L = 0x06
    ALL_LED_OFF_L = 0xFC
    ALL_LED_OFF_H = 0xFD
    PRESCALE = 0xFE

    SLEEP = 0x10
    RESTART = 0x80

    def __init__(self, bus_number: int = 1, address: int = 0x40):
        """Open I2C bus without resetting or waking the controller."""
        self.address = address
        self._bus = smbus.SMBus(bus_number)

    def read_register(self, register: int) -> int:
        return self._bus.read_byte_data(self.address, register)

    def status(self) -> dict[str, int | bool]:
        mode1 = self.read_register(self.MODE1)
        mode2 = self.read_register(self.MODE2)
        prescale = self.read_register(self.PRESCALE)
        return {
            "mode1": mode1,
            "mode2": mode2,
            "prescale": prescale,
            "sleeping": bool(mode1 & self.SLEEP),
        }

    def set_frequency(self, hz: float) -> None:
        """Configure PWM frequency. Explicit write: may energize connected devices."""
        if not 24 <= hz <= 1500:
            raise ValueError("PWM frequency must be between 24 and 1500 Hz")
        prescale = round(25_000_000 / (4096 * hz) - 1)
        if not 3 <= prescale <= 255:
            raise ValueError("Calculated PWM prescaler is out of range")
        old_mode = self.read_register(self.MODE1)
        # Datasheet requires SLEEP while modifying PRESCALE.
        self._bus.write_byte_data(self.address, self.MODE1, (old_mode & 0x7F) | self.SLEEP)
        self._bus.write_byte_data(self.address, self.PRESCALE, prescale)
        # Explicitly wake the oscillator; do not enable any servo channel here.
        self._bus.write_byte_data(self.address, self.MODE1, old_mode & ~self.SLEEP)
        time.sleep(0.005)

    def write_pwm(self, channel: int, on: int, off: int) -> None:
        """Set PWM counts for a channel. Explicit write: moves attached hardware."""
        if not 0 <= channel <= 15:
            raise ValueError("channel must be 0..15")
        if not (0 <= on <= 4095 and 0 <= off <= 4095):
            raise ValueError("on and off must be 0..4095")
        register = self.LED0_ON_L + channel * 4
        # MODE1.AI is not enabled by default (currently MODE1=0x11).
        # Use explicit register addresses rather than a multi-byte block write.
        # Write OFF_H last: its full-off bit stays set while counts are loaded.
        self._bus.write_byte_data(self.address, register, on & 0xFF)
        self._bus.write_byte_data(self.address, register + 1, (on >> 8) & 0x0F)
        self._bus.write_byte_data(self.address, register + 2, off & 0xFF)
        self._bus.write_byte_data(self.address, register + 3, (off >> 8) & 0x0F)

    def all_outputs_off(self) -> None:
        """Set full-off for all outputs; only call when ready to change hardware."""
        self._bus.write_byte_data(self.address, self.ALL_LED_OFF_L, 0x00)
        self._bus.write_byte_data(self.address, self.ALL_LED_OFF_H, 0x10)

    def close(self) -> None:
        self._bus.close()

    def __enter__(self) -> "PCA9685":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()


if __name__ == "__main__":
    with PCA9685() as controller:
        state = controller.status()
    print(f"MODE1: 0x{state['mode1']:02X}")
    print(f"MODE2: 0x{state['mode2']:02X}")
    print(f"PRE_SCALE: 0x{state['prescale']:02X}")
    print(f"SLEEP: {state['sleeping']}")
    print("PCA9685: read-only check OK")
