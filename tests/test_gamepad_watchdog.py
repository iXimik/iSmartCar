"""Hardware-free safety tests for the five-minute G910 Bluetooth watchdog."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "ismartcar" / "controllers"))
import gamepad_watchdog as watcher

ASUS_PATH = "/org/bluez/hci1"
G910_PATH = "/org/bluez/hci1/dev_05_67_39_43_89_01"


class BluetoothWatchdogTests(unittest.TestCase):
    def test_consistent_connected_link_is_never_disconnected(self):
        with (
            patch.object(watcher, "bluez_status", return_value=(ASUS_PATH, True, True)),
            patch.object(watcher, "has_acl", return_value=True),
            patch.object(watcher, "command") as cmd,
            patch.object(watcher.time, "sleep") as sleep,
            patch.object(watcher, "find_objects") as find,
        ):
            watcher.clear_stale_acl(object(), dry_run=False)
            cmd.assert_not_called()
            sleep.assert_not_called()
            find.assert_not_called()

    def test_fully_disconnected_device_is_not_modified(self):
        with (
            patch.object(watcher, "bluez_status", return_value=(ASUS_PATH, False, False)),
            patch.object(watcher, "has_acl", return_value=False),
            patch.object(watcher, "command") as cmd,
            patch.object(watcher.time, "sleep") as sleep,
        ):
            watcher.clear_stale_acl(object(), dry_run=False)
            cmd.assert_not_called()
            sleep.assert_not_called()

    def test_phantom_bluez_link_requires_confirmation(self):
        with (
            patch.object(watcher, "bluez_status", side_effect=[
                (ASUS_PATH, True, True), (ASUS_PATH, True, True)
            ]),
            patch.object(watcher, "has_acl", return_value=False),
            patch.object(watcher, "find_objects", return_value=(
                ASUS_PATH, G910_PATH, {"Paired": True, "Connected": True}
            )),
            patch.object(watcher.time, "sleep") as sleep,
            patch.object(watcher, "command") as cmd,
            patch.object(watcher, "log") as logger,
        ):
            watcher.clear_stale_acl(object(), dry_run=True)
            sleep.assert_called_once_with(4)
            cmd.assert_not_called()
            self.assertTrue(any(
                "DRY-RUN: BlueZ Device1.Disconnect()" in str(c.args[0])
                for c in logger.call_args_list
            ))

    def test_acl_returning_during_confirmation_does_not_disconnect(self):
        with (
            patch.object(watcher, "bluez_status", side_effect=[
                (ASUS_PATH, True, True), (ASUS_PATH, True, True)
            ]),
            patch.object(watcher, "has_acl", side_effect=[False, True]),
            patch.object(watcher, "find_objects") as find,
            patch.object(watcher, "command") as cmd,
            patch.object(watcher.time, "sleep") as sleep,
        ):
            watcher.clear_stale_acl(object(), dry_run=False)
            sleep.assert_called_once_with(4)
            find.assert_not_called()
            cmd.assert_not_called()

    def test_stale_acl_uses_targeted_disconnect_in_dry_run(self):
        with (
            patch.object(watcher, "bluez_status", side_effect=[
                (ASUS_PATH, False, False), (ASUS_PATH, False, False)
            ]),
            patch.object(watcher, "has_acl", return_value=True),
            patch.object(watcher.time, "sleep"),
            patch.object(watcher, "command") as cmd,
            patch.object(watcher, "log") as logger,
        ):
            watcher.clear_stale_acl(object(), dry_run=True)
            cmd.assert_not_called()
            self.assertTrue(any(
                "DRY-RUN: btmgmt" in str(c.args[0])
                for c in logger.call_args_list
            ))


if __name__ == "__main__":
    unittest.main()
