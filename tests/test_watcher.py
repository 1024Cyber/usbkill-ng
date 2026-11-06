# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Innocent I.
import tempfile
import threading
import time
import unittest
from pathlib import Path

from usbkill_ng import actions
from usbkill_ng.config import Settings, load_settings, parse_device_ids, ConfigError
from usbkill_ng.devices import (Device, DeviceReadError, diff_devices, matches, read_devices)
from usbkill_ng.watcher import Watcher
from tests.test_core import FakeRun, write_cfg

WEBCAM = Device("1-3", "04f2", "b330", "", "Chicony", "720p webcam")
BT = Device("1-4", "8087", "07da", "", "Intel", "Bluetooth")
STICK = Device("1-2", "0781", "5567", "AAA111", "SanDisk", "Cruzer")
STICK2 = Device("1-2", "0781", "5567", "BBB222", "SanDisk", "Cruzer")


def make_sysfs(spec):
    root = Path(tempfile.mkdtemp())
    for name, attrs in spec.items():
        d = root / name
        d.mkdir()
        for k, v in attrs.items():
            (d / k).write_text(v + "\n")
    return root


class SysfsTests(unittest.TestCase):
    def test_reads_devices_and_skips_interfaces(self):
        root = make_sysfs({
            "usb1": {"idVendor": "1D6B", "idProduct": "0002", "serial": "0000:00:14.0"},
            "1-4": {"idVendor": "8087", "idProduct": "07da", "product": "Bluetooth"},
            "1-4:1.0": {"bInterfaceClass": "e0"},
            "1-9": {"idVendor": "", "idProduct": ""},          # vanishing device
        })
        devs = read_devices(root)
        self.assertEqual([d.path for d in devs], ["1-4", "usb1"])
        self.assertEqual(devs[1].vid, "1d6b")                 # lower-cased
        self.assertEqual(devs[0].product, "Bluetooth")

    def test_unreadable_sysfs_raises_not_empty(self):
        with self.assertRaises(DeviceReadError):
            read_devices(Path("/nonexistent/sysfs"))


class MatchTests(unittest.TestCase):
    def test_vidpid_and_serial(self):
        self.assertTrue(matches(STICK, "0781:5567"))
        self.assertTrue(matches(STICK, "0781:5567:aaa111"))
        self.assertFalse(matches(STICK2, "0781:5567:aaa111"))
        self.assertFalse(matches(STICK, "0781:0000"))

    def test_config_accepts_serial_form(self):
        self.assertEqual(parse_device_ids("0781:5567:AAA111", "w"), frozenset({"0781:5567:aaa111"}))
        with self.assertRaises(ConfigError):
            parse_device_ids("0781:5567:", "w")


class DiffTests(unittest.TestCase):
    def test_add_remove_and_exempt(self):
        base = [WEBCAM, BT]
        self.assertFalse(diff_devices(base, base))
        self.assertEqual(diff_devices(base, base + [STICK]).added, (STICK,))
        self.assertEqual(diff_devices(base, [WEBCAM]).removed, (BT,))
        self.assertFalse(diff_devices(base, [WEBCAM], exempt={"8087:07da"}))
        self.assertFalse(diff_devices(base, base + [STICK], exempt={"0781:5567"}))
        # serial-pinned whitelist does not cover a different stick
        self.assertTrue(diff_devices(base, base + [STICK2], exempt={"0781:5567:aaa111"}))


class FakeSource:
    """Each wait() runs the next scripted step (which mutates the fake bus), then reports an event."""

    def __init__(self, devices, steps, stop):
        self.devices, self.steps, self.stop = devices, list(steps), stop

    def wait(self, timeout):
        if not self.steps:
            self.stop.set()
            return False
        self.steps.pop(0)(self.devices)
        return True


def run_watcher(initial, steps, **cfg):
    devices = list(initial)
    stop = threading.Event()
    calls = []
    settings = Settings(cooldown=0.01, debounce=0.0, pause_file=Path("/nonexistent/pause"), **cfg)
    w = Watcher(settings, lambda: list(devices), FakeSource(devices, steps, stop),
                lambda changes: calls.append(changes), stop)
    w.run()
    return calls, w


def plug(dev):
    return lambda devs: devs.append(dev)


def unplug(dev):
    return lambda devs: devs.remove(dev)


class WatcherTests(unittest.TestCase):
    def test_no_change_no_trigger(self):
        calls, _ = run_watcher([WEBCAM], [lambda d: None, lambda d: None])
        self.assertEqual(calls, [])

    def test_plug_then_unplug_each_trigger_once(self):
        calls, _ = run_watcher([WEBCAM], [plug(STICK), lambda d: None, unplug(STICK)])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].added, (STICK,))
        self.assertEqual(calls[1].removed, (STICK,))

    def test_persisting_device_does_not_retrigger(self):
        calls, _ = run_watcher([WEBCAM], [plug(STICK)] + [lambda d: None] * 5)
        self.assertEqual(len(calls), 1)

    def test_whitelisted_device_is_silent_both_ways(self):
        calls, _ = run_watcher([WEBCAM], [plug(STICK), unplug(STICK)],
                               whitelist=frozenset({"0781:5567"}))
        self.assertEqual(calls, [])

    def test_ignored_internal_device_may_vanish(self):
        calls, _ = run_watcher([WEBCAM, BT], [unplug(BT)], ignore=frozenset({"8087:07da"}))
        self.assertEqual(calls, [])
        calls, _ = run_watcher([WEBCAM, BT], [unplug(WEBCAM)], ignore=frozenset({"8087:07da"}))
        self.assertEqual(len(calls), 1)

    def test_read_error_is_not_a_trigger(self):
        devices, stop, calls = [WEBCAM], threading.Event(), []
        state = {"n": 0}

        def flaky():
            state["n"] += 1
            if state["n"] == 2:
                raise DeviceReadError("transient")
            return list(devices)

        settings = Settings(cooldown=0.01, debounce=0.0)
        src = FakeSource(devices, [lambda d: None, lambda d: None], stop)
        Watcher(settings, flaky, src, lambda c: calls.append(c), stop).run()
        self.assertEqual(calls, [])

    def test_initial_read_failure_refuses_to_start(self):
        stop = threading.Event()

        def boom():
            raise DeviceReadError("no sysfs")

        with self.assertRaises(DeviceReadError):
            Watcher(Settings(), boom, FakeSource([], [], stop), lambda c: None, stop).run()

    def test_pause_file_suppresses_action(self):
        pause = Path(tempfile.mkdtemp()) / "pause"
        pause.write_text("")
        devices, stop, calls = [WEBCAM], threading.Event(), []
        settings = Settings(cooldown=0.01, debounce=0.0, pause_file=pause)
        Watcher(settings, lambda: list(devices), FakeSource(devices, [plug(STICK)], stop),
                lambda c: calls.append(c), stop).run()
        self.assertEqual(calls, [])


class ParallelAndPreflightTests(unittest.TestCase):
    def test_live_steps_run_in_parallel(self):
        def slow(argv, timeout=5):
            time.sleep(0.4)
            return actions.subprocess.CompletedProcess(argv, 0, "", "")
        steps = [actions.Step("a", ("a",)), actions.Step("b", ("b",))]
        t0 = time.time()
        self.assertTrue(actions.execute_steps(steps, dry_run=False, run=slow))
        self.assertLess(time.time() - t0, 0.7)

    def test_preflight(self):
        from unittest import mock
        import shutil
        with mock.patch.object(shutil, "which", lambda n: "/bin/" + n):
            self.assertEqual(actions.lock_preflight(Settings(), FakeRun(), euid=0), [])
        with mock.patch.object(shutil, "which", lambda n: None):
            self.assertTrue(actions.lock_preflight(Settings(), FakeRun(), euid=0))

    def test_new_config_keys(self):
        s = load_settings(write_cfg("[config]\ndebounce = 0.5\npause_file = /tmp/p\n"))
        self.assertEqual((s.debounce, str(s.pause_file)), (0.5, "/tmp/p"))
        with self.assertRaises(ConfigError):
            load_settings(write_cfg("[config]\ndebounce = 9\n"))


if __name__ == "__main__":
    unittest.main()
