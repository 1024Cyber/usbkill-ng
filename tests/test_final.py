# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Innocent I.
import io
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from usbkill_ng import __version__, cli
from usbkill_ng.config import Settings, parse_summary, settings_summary
from tests.test_packaging import ROOT, UNIT, sh
from tests.test_watcher import WEBCAM, FakeSource
from tests.test_core import FakeRun


def run_main(*argv, run=None):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv), run=run) if run else cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def make_config(tmp: Path, **extra) -> Path:
    keys = {"action": "lock", "delay": "0", "log_file": str(tmp / "usbkill.log"),
            "pause_file": str(tmp / "pause")}
    keys.update(extra)
    path = tmp / "usbkill.ini"
    path.write_text("[config]\n" + "".join(f"{k} = {v}\n" for k, v in keys.items()))
    return path


def systemctl(active="active", enabled="enabled"):
    def fake(argv, timeout=5):
        out = {"is-active": active, "is-enabled": enabled}.get(argv[1], "")
        return subprocess.CompletedProcess(argv, 0, out + "\n", "")
    return fake


class SummaryTests(unittest.TestCase):
    def test_roundtrip(self):
        s = Settings(action="restart", delay=10, lock_methods=("loginctl", "xflock4"),
                     whitelist=frozenset({"0781:5567"}))
        d = parse_summary(settings_summary(s, "9.9"))
        self.assertEqual((d["version"], d["action"], d["delay"], d["lock_first"]), ("9.9", "restart", "10", "true"))
        self.assertEqual((d["lock_methods"], d["whitelist"], d["ignore"]), ("loginctl,xflock4", "0781:5567", "-"))

    def test_watch_logs_settings_line_at_startup(self):
        stop_holder = {}

        def fake_source(settings, stop):
            stop_holder["stop"] = stop
            return FakeSource([WEBCAM], [], stop)

        with mock.patch.object(cli, "read_devices", lambda: [WEBCAM]), \
             mock.patch.object(cli, "make_source", fake_source), \
             mock.patch.object(cli, "install_stop_handlers", lambda stop: None), \
             self.assertLogs("usbkill", "INFO") as cm:
            code = cli.watch(Settings(pause_file=Path("/nonexistent/p")), live=False, assume_yes=False, run=FakeRun())
        self.assertEqual(code, 0)
        line = [l for l in cm.output if "settings:" in l][0]
        self.assertIn("action=lock", line)
        self.assertIn(f"version={__version__}", line)


class PauseResumeTests(unittest.TestCase):
    def test_pause_and_resume(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = str(make_config(tmp))
        self.assertEqual(run_main("--resume", "--config", cfg)[0], 0)          # not paused: harmless
        code, out, _ = run_main("--pause", "--config", cfg)
        self.assertEqual(code, 0)
        self.assertTrue((tmp / "pause").exists())
        self.assertIn("paused", out)
        self.assertEqual(run_main("--resume", "--config", cfg)[0], 0)
        self.assertFalse((tmp / "pause").exists())

    def test_pause_error_is_reported(self):
        cfg = str(make_config(Path(tempfile.mkdtemp()), pause_file="/proc/nope/pause"))
        code, _, err = run_main("--pause", "--config", cfg)
        self.assertEqual(code, 1)
        self.assertIn("cannot create", err)

    def test_pause_file_really_suppresses_actions(self):
        # integration with the watcher: a paused trigger must not call the action
        from tests.test_watcher import STICK, plug
        import threading
        from usbkill_ng.watcher import Watcher
        tmp = Path(tempfile.mkdtemp())
        cfg = str(make_config(tmp))
        run_main("--pause", "--config", cfg)
        devices, stop, calls = [WEBCAM], threading.Event(), []
        s = Settings(cooldown=0.01, debounce=0.0, pause_file=tmp / "pause")
        Watcher(s, lambda: list(devices), FakeSource(devices, [plug(STICK)], stop),
                lambda ch: calls.append(ch), stop).run()
        self.assertEqual(calls, [])


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def write_log(self, **running):
        line = "2026-10-17 10:00:00,000 INFO    settings: " + settings_summary(
            Settings(**running), "2.0.0")
        (self.tmp / "usbkill.log").write_text("noise\n" + line + "\nmore noise\n")

    def status(self, cfg, run):
        code, out, _ = run_main("--status", "--config", str(cfg), run=run)
        self.assertEqual(code, 0)
        return out

    def test_stale_service_is_detected(self):
        self.write_log(action="restart", delay=10.0)
        out = self.status(make_config(self.tmp, action="lock", delay="0"), systemctl())
        self.assertIn("WARNING", out)
        self.assertIn("action (restart -> lock)", out)
        self.assertIn("delay (10 -> 0)", out)
        self.assertIn("sudo systemctl restart usbkill-ng", out)

    def test_matching_service_is_ok_and_arm_delay_is_ignored(self):
        self.write_log(action="lock", delay=0.0, arm_delay=20.0)       # service started with --arm-delay 20
        out = self.status(make_config(self.tmp, action="lock", delay="0", arm_delay="0"), systemctl())
        self.assertIn("OK: the running service matches", out)
        self.assertNotIn("WARNING", out)

    def test_inactive_service_is_not_compared(self):
        self.write_log(action="restart")
        out = self.status(make_config(self.tmp), systemctl(active="inactive", enabled="disabled"))
        self.assertIn("inactive (at boot: disabled)", out)
        self.assertIn("nothing to compare", out)
        self.assertNotIn("WARNING", out)

    def test_old_service_without_settings_line(self):
        (self.tmp / "usbkill.log").write_text("2026-10-16 INFO    mode: LIVE\n")
        out = self.status(make_config(self.tmp), systemctl())
        self.assertIn("no 'settings:' line", out)

    def test_missing_log_and_pause_flags_shown(self):
        cfgp = make_config(self.tmp)
        (self.tmp / "pause").write_text("")
        out = self.status(cfgp, systemctl())
        self.assertIn("YES - actions suppressed", out)
        self.assertIn("cannot read the log", out)

    def test_newest_settings_line_wins(self):
        log = self.tmp / "usbkill.log"
        old = "x INFO    settings: " + settings_summary(Settings(action="shutdown"), "1")
        new = "x INFO    settings: " + settings_summary(Settings(action="lock"), "2")
        log.write_text(old + "\n" + new + "\n")
        self.assertEqual(cli.last_started_settings(log)["action"], "lock")


class PackagingFinalTests(unittest.TestCase):
    def test_unit_has_no_ordering_loop(self):
        directives = [l.strip() for l in UNIT.read_text().splitlines()
                      if l.strip() and not l.strip().startswith("#")]
        after = [l for l in directives if l.startswith("After=")]
        self.assertEqual(after, ["After=systemd-logind.service systemd-udevd.service"])
        self.assertNotIn("multi-user.target", " ".join(l for l in directives if not l.startswith("WantedBy=")))

    def test_logrotate_rule(self):
        text = (ROOT / "packaging" / "usbkill-ng.logrotate").read_text()
        for word in ("/var/log/usbkill/usbkill.log", "rotate 8", "compress", "copytruncate"):
            self.assertIn(word, text)

    @unittest.skipUnless(shutil.which("logrotate"), "logrotate not available")
    def test_logrotate_syntax(self):
        r = subprocess.run(["logrotate", "-d", str(ROOT / "packaging" / "usbkill-ng.logrotate")],
                           capture_output=True, text=True)
        self.assertNotIn("error", (r.stdout + r.stderr).lower().replace("0 errors", ""), r.stdout + r.stderr)

    def test_installer_adds_and_uninstaller_removes_logrotate_and_docs(self):
        dest = Path(tempfile.mkdtemp())
        self.assertEqual(sh("install.sh", dest).returncode, 0)
        self.assertTrue((dest / "etc/logrotate.d/usbkill-ng").exists())
        for doc in ("README.md", "LICENSE", "NOTICE"):
            self.assertTrue((dest / "opt/usbkill-ng" / doc).exists(), doc)
        self.assertEqual(sh("uninstall.sh", dest).returncode, 0)
        self.assertFalse((dest / "etc/logrotate.d/usbkill-ng").exists())

    def test_license_notice_and_spdx_headers(self):
        lic = (ROOT / "LICENSE").read_text()
        self.assertIn("GNU GENERAL PUBLIC LICENSE", lic)
        self.assertIn("Version 3, 29 June 2007", lic)
        notice = (ROOT / "NOTICE").read_text()
        self.assertIn("hephaest0s/usbkill", notice)
        self.assertIn("GPL-3.0-or-later", notice)
        missing = [str(p) for d in ("usbkill_ng", "tests") for p in (ROOT / d).glob("*.py")
                   if "SPDX-License-Identifier: GPL-3.0-or-later" not in p.read_text()]
        self.assertEqual(missing, [])

    def test_version_is_final(self):
        self.assertEqual(__version__, "2.0.0")


if __name__ == "__main__":
    unittest.main()


class AboutTests(unittest.TestCase):
    def test_about_works_without_any_config(self):
        code, out, _ = run_main("--about", "--config", "/nonexistent/usbkill.ini")
        self.assertEqual(code, 0)
        for text in (f"usbkill-ng {__version__}", "Author:", "GitHub:", "Twitter:", "Project:",
                     "GPL-3.0-or-later", "hephaest0s/usbkill"):
            self.assertIn(text, out)

    def test_metadata_matches_the_about_output(self):
        from usbkill_ng import __author__, __github__, __twitter__, __url__
        _, out, _ = run_main("--about")
        for value in (__author__, __github__, __twitter__, __url__):
            self.assertIn(value, out)

    def test_original_author_credit_is_kept(self):
        self.assertIn("Hephaestos", (ROOT / "NOTICE").read_text())
        self.assertIn("hephaest0s/usbkill", (ROOT / "README.md").read_text())
