"""Offline safety and register-write tests for PCA9685 driver."""

import sys
import unittest
from pathlib import Path
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ismartcar.hardware.pca9685 import PCA9685


class PCA9685DriverTests(unittest.TestCase):
    def setUp(self):
        self.patch_bus = patch("ismartcar.hardware.pca9685.smbus.SMBus")
        self.mock_factory = self.patch_bus.start()
        self.addCleanup(self.patch_bus.stop)
        self.driver = PCA9685()
        self.bus = self.mock_factory.return_value

    def test_init_does_not_write_outputs(self):
        self.bus.write_byte_data.assert_not_called()

    def test_pwm_writes_explicit_addresses_without_auto_increment(self):
        self.driver.write_pwm(0, 0, 450)
        self.assertEqual(
            self.bus.write_byte_data.call_args_list,
            [
                call(0x40, 0x06, 0x00),
                call(0x40, 0x07, 0x00),
                call(0x40, 0x08, 0xC2),
                call(0x40, 0x09, 0x01),
            ],
        )
        self.bus.write_i2c_block_data.assert_not_called()

    def test_all_outputs_off(self):
        self.driver.all_outputs_off()
        self.assertEqual(
            self.bus.write_byte_data.call_args_list,
            [call(0x40, 0xFC, 0), call(0x40, 0xFD, 0x10)],
        )

    def test_sleep_uses_existing_mode1(self):
        self.bus.read_byte_data.return_value = 0x01
        self.driver.sleep()
        self.bus.write_byte_data.assert_called_once_with(0x40, 0x00, 0x11)

    def test_invalid_channel_is_rejected_without_writes(self):
        with self.assertRaises(ValueError):
            self.driver.write_pwm(16, 0, 450)
        self.bus.write_byte_data.assert_not_called()


if __name__ == "__main__":
    unittest.main()
