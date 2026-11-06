# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Innocent I.
import io
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from usbkill_ng.cli import confirm_live, main
from usbkill_ng.config import DEFAULT_INI, ConfigError, Settings, load_settings
from usbkill_ng.watcher import Watcher
from tests.test_core import write_cfg
from tests.test_power import FakeStdin, cfg
from tests.test_watcher import STICK, WEBCAM, FakeSource, plug, unplug

NO_PAUSE = Path("/nonexistent/pause")


def run_with_clock(initial, steps, clock, **kw):
    devices, stop, calls = list(initial), threading.Event(), []
    settings = Settings(cooldown=0.01, debounce=0.0, pause_file=NO_PAUSE, **kw)
    Watcher(settings, lambda: list(devices), FakeSource(devices, steps, stop),
            lambda ch: calls.append(ch), stop, clock=lambda: clock[0]).run()
    return calls


class ArmDelayTests(unittest.TestCase):
    def test_changes_during_grace_period_never_trigger(self):
        calls = run_with_clock([WEBCAM], [plug(STICK), unplug(STICK)], [0.0], arm_delay=5)
        self.assertEqual(calls, [])

    def test_triggers_after_arming_and_grace_changes_are_the_new_baseline(self):
        clock = [0.0]
        later = lambda d: clock.__setitem__(0, 10.0)
        other = type(STICK)("1-5", "0951", "1666", "ZZZ", "Kingston", "DataTraveler")
        calls = run_with_clock([WEBCAM], [plug(STICK), later, plug(other)], clock, arm_delay=5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].added, (other,))       # STICK was absorbed during the grace period

    def test_zero_arm_delay_is_armed_immediately(self):
        calls = run_with_clock([WEBCAM], [plug(STICK)], [0.0], arm_delay=0)
        self.assertEqual(len(calls), 1)

    def test_config_and_cli_override(self):
        self.assertEqual(load_settings(write_cfg(DEFAULT_INI)).arm_delay, 0.0)
        self.assertEqual(load_settings(write_cfg("[config]\narm_delay = 20\n")).arm_delay, 20.0)
        for bad in ("-1", "301", "abc"):
            with self.assertRaises(ConfigError, msg=bad):
                load_settings(write_cfg(f"[config]\narm_delay = {bad}\n"))
        path = write_cfg("[config]\narm_delay = 20\n")
        out = io.StringIO()
        with redirect_stdout(out):
            main(["--show-config", "--config", str(path), "--arm-delay", "7"])
        self.assertIn("arm_delay = 7.0", out.getvalue())
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(main(["--show-config", "--config", str(path), "--arm-delay", "999"]), 2)


class ConfirmTests(unittest.TestCase):
    def test_lowercase_yes_is_accepted(self):
        for answer in ("YES", "yes", "  Yes  "):
            self.assertTrue(confirm_live(cfg(), False, stdin=FakeStdin(True), ask=lambda _: answer))

    def test_refusals_are_logged_with_a_reason(self):
        with self.assertLogs("usbkill", "ERROR") as cm:
            self.assertFalse(confirm_live(cfg(), False, stdin=FakeStdin(True), ask=lambda _: ""))
        self.assertIn("nothing was started", cm.output[0])
        with self.assertLogs("usbkill", "ERROR") as cm:
            self.assertFalse(confirm_live(cfg(), False, stdin=FakeStdin(False)))
        self.assertIn("no terminal", cm.output[0])

        def eof(_):
            raise EOFError
        with self.assertLogs("usbkill", "ERROR"):
            self.assertFalse(confirm_live(cfg(), False, stdin=FakeStdin(True), ask=eof))


if __name__ == "__main__":
    unittest.main()
