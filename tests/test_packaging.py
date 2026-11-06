# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Innocent I.
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNIT = ROOT / "packaging" / "usbkill-ng.service"


def sh(script, dest, *args):
    env = {**os.environ, "DESTDIR": str(dest)}
    return subprocess.run(["bash", str(ROOT / script), *args], env=env, capture_output=True, text=True)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.dest = Path(tempfile.mkdtemp())

    def test_install_stages_everything_and_enables_nothing(self):
        r = sh("install.sh", self.dest)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertTrue((self.dest / "opt/usbkill-ng/usbkill_ng/cli.py").exists())
        self.assertTrue((self.dest / "opt/usbkill-ng/README.md").exists())
        self.assertFalse(list((self.dest / "opt/usbkill-ng").rglob("__pycache__")))
        wrapper = self.dest / "usr/local/bin/usbkill-ng"
        self.assertEqual(wrapper.read_text().splitlines(),
                         ["#!/bin/sh",
                          'exec env PYTHONPATH=/opt/usbkill-ng /usr/bin/python3 -m usbkill_ng "$@"'])
        self.assertEqual(stat.S_IMODE(wrapper.stat().st_mode), 0o755)
        unit = self.dest / "etc/systemd/system/usbkill-ng.service"
        self.assertEqual(stat.S_IMODE(unit.stat().st_mode), 0o644)
        conf = (self.dest / "etc/usbkill/usbkill.ini").read_text()
        for key in ("action = lock", "delay = 0", "arm_delay = 0", "shutdown_mode = graceful"):
            self.assertIn(key, conf)
        self.assertFalse((self.dest / "etc/systemd/system/multi-user.target.wants").exists())
        for p in (self.dest / "opt/usbkill-ng").rglob("*"):
            self.assertFalse(p.stat().st_mode & stat.S_IWOTH, p)   # not world-writable

    def test_installed_copy_runs(self):
        sh("install.sh", self.dest)
        r = subprocess.run([sys.executable, "-m", "usbkill_ng", "--version"], cwd=tempfile.mkdtemp(),
                           env={**os.environ, "PYTHONPATH": str(self.dest / "opt/usbkill-ng")},
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("usbkill-ng", r.stdout)

    def test_existing_config_is_kept_and_template_written_beside_it(self):
        conf = self.dest / "etc/usbkill/usbkill.ini"
        conf.parent.mkdir(parents=True)
        conf.write_text("[config]\naction = restart\n")
        self.assertEqual(sh("install.sh", self.dest).returncode, 0)
        self.assertEqual(conf.read_text(), "[config]\naction = restart\n")
        self.assertIn("arm_delay", (conf.parent / "usbkill.ini.dist").read_text())

    def test_reinstall_is_idempotent(self):
        self.assertEqual(sh("install.sh", self.dest).returncode, 0)
        self.assertEqual(sh("install.sh", self.dest).returncode, 0)

    def test_dry_run_changes_nothing(self):
        r = sh("install.sh", self.dest, "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[dry-run]", r.stdout)
        self.assertEqual(list(self.dest.rglob("*")), [])

    def test_unknown_option_rejected(self):
        self.assertEqual(sh("install.sh", self.dest, "--bogus").returncode, 2)


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.dest = Path(tempfile.mkdtemp())
        sh("install.sh", self.dest)
        (self.dest / "var/log/usbkill").mkdir(parents=True)
        (self.dest / "var/log/usbkill/usbkill.log").write_text("x")

    def test_uninstall_removes_program_but_keeps_config_and_logs(self):
        self.assertEqual(sh("uninstall.sh", self.dest).returncode, 0)
        self.assertFalse((self.dest / "opt/usbkill-ng").exists())
        self.assertFalse((self.dest / "usr/local/bin/usbkill-ng").exists())
        self.assertFalse((self.dest / "etc/systemd/system/usbkill-ng.service").exists())
        self.assertTrue((self.dest / "etc/usbkill/usbkill.ini").exists())
        self.assertTrue((self.dest / "var/log/usbkill/usbkill.log").exists())

    def test_purge_removes_config_and_logs(self):
        self.assertEqual(sh("uninstall.sh", self.dest, "--purge").returncode, 0)
        self.assertFalse((self.dest / "etc/usbkill").exists())
        self.assertFalse((self.dest / "var/log/usbkill").exists())

    def test_dry_run_removes_nothing(self):
        before = sorted(self.dest.rglob("*"))
        self.assertEqual(sh("uninstall.sh", self.dest, "--dry-run", "--purge").returncode, 0)
        self.assertEqual(sorted(self.dest.rglob("*")), before)

    def test_uninstall_without_install_is_harmless(self):
        self.assertEqual(sh("uninstall.sh", Path(tempfile.mkdtemp())).returncode, 0)


class UnitTests(unittest.TestCase):
    def test_unit_contents(self):
        text = UNIT.read_text()
        for line in ("ConditionPathExists=!/etc/usbkill/disabled",
                     "ConditionPathExists=/etc/usbkill/usbkill.ini",
                     "WantedBy=multi-user.target", "Restart=on-failure",
                     "StartLimitBurst=5"):
            self.assertIn(line, text)
        exec_line = [l for l in text.splitlines() if l.startswith("ExecStart=")][0]
        for part in ("--live", "--watch", "--yes", "--arm-delay"):
            self.assertIn(part, exec_line)
        self.assertNotIn("Restart=always", text)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze not available")
    def test_systemd_analyze_verify(self):
        d = Path(tempfile.mkdtemp())
        unit = d / "usbkill-ng.service"
        unit.write_text(UNIT.read_text().replace("/usr/local/bin/usbkill-ng", sys.executable))
        r = subprocess.run(["systemd-analyze", "verify", "--man=no", str(unit)],
                           capture_output=True, text=True)
        problems = [l for l in (r.stdout + r.stderr).splitlines() if str(unit) in l or "usbkill" in l]
        self.assertEqual(problems, [], r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
