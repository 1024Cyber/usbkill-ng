# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Innocent I.
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from usbkill_ng import actions
from usbkill_ng.config import (DEFAULT_INI, ConfigError, Settings, load_settings,
                               parse_device_ids)

LIST = "      2 1000 null -     1319 manager -\n      4 1000 null seat0 1865 user -\n"
SHOW = {
    "2": "Name=null\nUser=1000\nType=unspecified\nActive=yes\nClass=manager\nDisplay=\n",
    "4": "Name=null\nUser=1000\nType=x11\nActive=yes\nClass=user\nDisplay=:0\n",
}


def cp(out="", rc=0, err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class FakeRun:
    def __init__(self, rc_default=0):
        self.calls = []
        self.rc_default = rc_default

    def __call__(self, argv, timeout=5):
        self.calls.append(list(argv))
        if argv[:2] == ["loginctl", "list-sessions"]:
            return cp(LIST)
        if argv[:2] == ["loginctl", "show-session"]:
            return cp(SHOW[argv[2]])
        return cp("", self.rc_default)


def write_cfg(text):
    d = tempfile.mkdtemp()
    p = Path(d) / "usbkill.ini"
    p.write_text(text)
    return p


class ConfigTests(unittest.TestCase):
    def test_default_ini_is_valid(self):
        s = load_settings(write_cfg(DEFAULT_INI))
        self.assertEqual(s.action, "lock")
        self.assertEqual(s.lock_methods, ("loginctl", "xflock4"))
        self.assertEqual(s.cooldown, 5.0)

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_settings(Path("/nonexistent/x.ini"))
        self.assertEqual(load_settings(Path("/nonexistent/x.ini"), allow_missing=True), Settings())

    def test_bad_values(self):
        for body in ("action = explode", "cooldown = 0", "poll_interval = 99",
                     "lock_methods = nope", "whitelist = 12345", "ignore = zz:zz"):
            with self.assertRaises(ConfigError, msg=body):
                load_settings(write_cfg("[config]\n" + body + "\n"))

    def test_device_ids(self):
        self.assertEqual(parse_device_ids("1D6B:0002, 8087:07da", "w"),
                         frozenset({"1d6b:0002", "8087:07da"}))


class ActionTests(unittest.TestCase):
    def test_find_session_picks_graphical(self):
        s = actions.find_graphical_session(FakeRun())
        self.assertEqual((s.session_id, s.uid, s.display), ("4", 1000, ":0"))

    def test_no_loginctl(self):
        self.assertIsNone(actions.find_graphical_session(lambda a, timeout=5: cp("", 127)))

    def test_plan_as_root_uses_runuser(self):
        sess = actions.GraphicalSession("4", "null", 1000, ":0", Path("/home/null"))
        steps = actions.plan_lock(("loginctl", "xflock4"), sess, 0, which=lambda n: "/bin/" + n)
        self.assertEqual(steps[0].argv, ("loginctl", "lock-sessions"))
        self.assertEqual(steps[1].argv[:4], ("runuser", "-u", "null", "--"))
        self.assertIn("DISPLAY=:0", steps[1].argv)
        self.assertIn("XAUTHORITY=/home/null/.Xauthority", steps[1].argv)

    def test_plan_skips(self):
        steps = actions.plan_lock(("loginctl", "xflock4"), None, 0, which=lambda n: None)
        self.assertTrue(all(s.skip_reason for s in steps))

    def test_dry_run_executes_nothing(self):
        fake = FakeRun()
        ok = actions.lock(Settings(), dry_run=True, run=fake, euid=0)
        self.assertTrue(ok)
        executed = [c for c in fake.calls if c[:2] not in (["loginctl", "list-sessions"], ["loginctl", "show-session"])]
        self.assertEqual(executed, [])

    def test_live_runs_commands(self):
        fake = FakeRun()
        with self._patched_which():
            self.assertTrue(actions.lock(Settings(), dry_run=False, run=fake, euid=0))
        self.assertIn(["loginctl", "lock-sessions"], fake.calls)
        self.assertTrue(any(c[:1] == ["runuser"] for c in fake.calls))

    def test_live_failure_reported(self):
        with self._patched_which():
            self.assertFalse(actions.lock(Settings(), dry_run=False, run=FakeRun(rc_default=1), euid=0))

    def _patched_which(self):
        import shutil
        from unittest import mock
        return mock.patch.object(shutil, "which", lambda n: "/bin/" + n)


if __name__ == "__main__":
    unittest.main()
