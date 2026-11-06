# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Innocent I.
import io
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from usbkill_ng import power
from usbkill_ng.cli import confirm_live, main, show_config
from usbkill_ng.config import DEFAULT_INI, ConfigError, Settings, load_settings
from tests.test_core import FakeRun, write_cfg

NO_PAUSE = Path("/nonexistent/pause")
POWER_PREFIXES = ("systemctl", "poweroff", "reboot", "sync", "shutdown")


def cfg(**kw):
    base = dict(action="shutdown", delay=0.0, cooldown=0.01, debounce=0.0,
                shutdown_mode="graceful", lock_first=False, pause_file=NO_PAUSE)
    base.update(kw)
    return Settings(**base)


class ScriptedRun(FakeRun):
    """FakeRun where chosen commands (by first two argv words) return a failure code."""

    def __init__(self, failing=()):
        super().__init__()
        self.failing = set(failing)
        self.times = []

    def __call__(self, argv, timeout=5):
        self.times.append((time.monotonic(), list(argv)))
        if " ".join(argv) in self.failing:
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 1, "", "refused")
        return super().__call__(argv, timeout)

    def power_calls(self):
        return [c for c in self.calls if c and c[0] in POWER_PREFIXES]


def with_which():
    return mock.patch.object(shutil, "which", lambda n: "/bin/" + n)


class ConfigTests(unittest.TestCase):
    def test_new_keys(self):
        s = load_settings(write_cfg(
            "[config]\naction = restart\ndelay = 10\nshutdown_mode = fast\nlock_first = no\n"))
        self.assertEqual((s.action, s.delay, s.shutdown_mode, s.lock_first), ("restart", 10.0, "fast", False))

    def test_defaults_in_template(self):
        s = load_settings(write_cfg(DEFAULT_INI))
        self.assertEqual((s.action, s.delay, s.shutdown_mode, s.lock_first), ("lock", 0.0, "graceful", True))

    def test_bad_values(self):
        for body in ("delay = -1", "delay = 301", "delay = abc", "shutdown_mode = nuke",
                     "lock_first = maybe", "action = explode"):
            with self.assertRaises(ConfigError, msg=body):
                load_settings(write_cfg("[config]\n" + body + "\n"))


class PlanTests(unittest.TestCase):
    def names(self, action, mode):
        pre, chain = power.plan_power(action, mode, which=lambda n: "/bin/" + n)
        return [s.name for s in pre], [s.name for s in chain]

    def test_chains(self):
        self.assertEqual(self.names("shutdown", "graceful"),
                         ([], ["systemctl poweroff", "systemctl poweroff -i", "poweroff -f"]))
        self.assertEqual(self.names("restart", "graceful")[1][0], "systemctl reboot")
        self.assertEqual(self.names("shutdown", "fast"), ([], ["systemctl poweroff -f", "poweroff -f"]))
        self.assertEqual(self.names("restart", "immediate"),
                         (["sync"], ["systemctl reboot -f -f", "reboot -f -f"]))

    def test_missing_commands_are_skipped(self):
        _, chain = power.plan_power("shutdown", "graceful", which=lambda n: None)
        self.assertTrue(all(s.skip_reason for s in chain))


class ActionTests(unittest.TestCase):
    def test_dry_run_is_inert_and_does_not_wait(self):
        fake = ScriptedRun()
        t0 = time.time()
        with with_which():
            out = power.run_action(cfg(delay=5, lock_first=True), dry_run=True, run=fake, euid=0)
        self.assertTrue(out.ok)
        self.assertFalse(out.powered)
        self.assertLess(time.time() - t0, 1)
        self.assertEqual(fake.power_calls(), [])
        self.assertNotIn(["loginctl", "lock-sessions"], fake.calls)

    def test_live_shutdown_runs_first_command_only(self):
        fake = ScriptedRun()
        with with_which():
            out = power.run_action(cfg(), dry_run=False, run=fake, euid=0)
        self.assertTrue(out.powered)
        self.assertEqual(fake.power_calls(), [["systemctl", "poweroff"]])

    def test_live_restart_fast_and_immediate(self):
        with with_which():
            fake = ScriptedRun()
            power.run_action(cfg(action="restart", shutdown_mode="fast"), False, fake, euid=0)
            self.assertEqual(fake.power_calls(), [["systemctl", "reboot", "-f"]])
            fake = ScriptedRun()
            power.run_action(cfg(shutdown_mode="immediate"), False, fake, euid=0)
            self.assertEqual(fake.power_calls(), [["sync"], ["systemctl", "poweroff", "-f", "-f"]])

    def test_fallback_chain(self):
        fake = ScriptedRun(failing={"systemctl poweroff", "systemctl poweroff -i"})
        with with_which():
            out = power.run_action(cfg(), dry_run=False, run=fake, euid=0)
        self.assertTrue(out.powered)
        self.assertEqual(fake.power_calls(),
                         [["systemctl", "poweroff"], ["systemctl", "poweroff", "-i"], ["poweroff", "-f"]])

    def test_total_failure_falls_back_to_lock(self):
        fake = ScriptedRun(failing={"systemctl poweroff", "systemctl poweroff -i", "poweroff -f"})
        with with_which():
            out = power.run_action(cfg(lock_first=False), dry_run=False, run=fake, euid=0)
        self.assertFalse(out.ok)
        self.assertFalse(out.powered)
        self.assertIn(["loginctl", "lock-sessions"], fake.calls)

    def test_total_failure_does_not_lock_twice_when_lock_first(self):
        fake = ScriptedRun(failing={"systemctl poweroff", "systemctl poweroff -i", "poweroff -f"})
        with with_which():
            power.run_action(cfg(lock_first=True), dry_run=False, run=fake, euid=0)
        self.assertEqual(fake.calls.count(["loginctl", "lock-sessions"]), 1)

    def test_lock_first_locks_before_the_delayed_action(self):
        fake = ScriptedRun()
        with with_which():
            power.run_action(cfg(lock_first=True, delay=0.4), dry_run=False, run=fake, euid=0)
        order = [argv for _, argv in fake.times]
        self.assertLess(order.index(["loginctl", "lock-sessions"]), order.index(["systemctl", "poweroff"]))
        lock_t = dict((tuple(a), t) for t, a in fake.times)
        self.assertGreaterEqual(lock_t[("systemctl", "poweroff")] - lock_t[("loginctl", "lock-sessions")], 0.3)

    def test_countdown_completes_then_acts(self):
        fake = ScriptedRun()
        t0 = time.monotonic()
        with with_which():
            out = power.run_action(cfg(delay=0.4), dry_run=False, run=fake, euid=0)
        self.assertTrue(out.powered)
        self.assertGreaterEqual(time.monotonic() - t0, 0.35)

    def test_pause_file_cancels_countdown(self):
        pause = Path(tempfile.mkdtemp()) / "pause"
        pause.write_text("")
        fake = ScriptedRun()
        with with_which():
            out = power.run_action(cfg(delay=5, pause_file=pause), dry_run=False, run=fake, euid=0)
        self.assertTrue(out.cancelled)
        self.assertEqual(fake.power_calls(), [])

    def test_stop_event_cancels_countdown(self):
        stop = threading.Event()
        threading.Timer(0.2, stop.set).start()
        fake = ScriptedRun()
        t0 = time.monotonic()
        with with_which():
            out = power.run_action(cfg(delay=5), dry_run=False, run=fake, stop=stop, euid=0)
        self.assertTrue(out.cancelled)
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(fake.power_calls(), [])

    def test_lock_action_honours_delay(self):
        fake = ScriptedRun()
        t0 = time.monotonic()
        with with_which():
            out = power.run_action(cfg(action="lock", delay=0.3), dry_run=False, run=fake, euid=0)
        self.assertTrue(out.ok)
        self.assertFalse(out.powered)
        self.assertGreaterEqual(time.monotonic() - t0, 0.25)
        self.assertEqual(fake.power_calls(), [])

    def test_preflight(self):
        self.assertEqual(power.power_preflight(cfg(), FakeRun(), 0, which=lambda n: "/bin/" + n), [])
        self.assertTrue(power.power_preflight(cfg(), FakeRun(), 0, which=lambda n: None))


class FakeStdin:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty


class CliTests(unittest.TestCase):
    def test_confirm_live(self):
        self.assertTrue(confirm_live(cfg(action="lock"), False))
        self.assertTrue(confirm_live(cfg(), True))
        self.assertFalse(confirm_live(cfg(), False, stdin=FakeStdin(False)))
        self.assertTrue(confirm_live(cfg(), False, stdin=FakeStdin(True), ask=lambda _: "YES"))
        self.assertFalse(confirm_live(cfg(), False, stdin=FakeStdin(True), ask=lambda _: "yes please"))

    def test_show_config(self):
        path = write_cfg("[config]\naction = restart\ndelay = 10\nwhitelist = 0781:5567\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["--show-config", "--config", str(path)]), 0)
        out = buf.getvalue()
        for expected in ("action = restart", "delay = 10.0", "shutdown_mode = graceful",
                         "lock_first = True", "whitelist = 0781:5567"):
            self.assertIn(expected, out)

    def test_dry_run_test_action_for_each_action(self):
        for action in ("lock", "shutdown", "restart"):
            path = write_cfg(f"[config]\naction = {action}\ndelay = 30\nlog_file = /tmp/ukng-test.log\n")
            fake = ScriptedRun()
            with with_which():
                code = main(["--test-action", "--config", str(path)], run=fake)
            self.assertEqual(code, 0, action)
            self.assertEqual(fake.power_calls(), [], action)


if __name__ == "__main__":
    unittest.main()
